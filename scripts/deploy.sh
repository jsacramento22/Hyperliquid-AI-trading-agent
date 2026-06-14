#!/usr/bin/env bash
# Deploy local changes to the production droplet.
#
# Rsyncs the project (excluding venv, node_modules, build caches, secrets-dir
# data, and git metadata) and restarts the hl-agent systemd service.
#
# Usage:
#   ./scripts/deploy.sh              # rsync + restart
#   ./scripts/deploy.sh --no-restart # just sync code, don't bounce the bot
#   ./scripts/deploy.sh --dry-run    # show what would be transferred, don't transfer
#
# Tip: alias this in your shell for one-key deploys:
#   alias hl-deploy='cd /Users/josealvarosacramento/work/hyperliquid && ./scripts/deploy.sh'

set -euo pipefail

DROPLET_USER="hlagent"
DROPLET_HOST="188.166.33.135"
DROPLET_PATH="~/hyperliquid/"

# Verify we're at the repo root
if [[ ! -f "pyproject.toml" || ! -d "src/hl_agent" ]]; then
    echo "✗ Not at the project root. Run from /Users/josealvarosacramento/work/hyperliquid/"
    exit 1
fi

# Parse flags
RESTART=true
RSYNC_FLAGS="-avz"
for arg in "$@"; do
    case "$arg" in
        --no-restart) RESTART=false ;;
        --dry-run)    RSYNC_FLAGS="-avzn" ;;
        -h|--help)
            sed -n '2,12p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg (use --help)"
            exit 1
            ;;
    esac
done

# Optional sanity hints (non-blocking)
if command -v git >/dev/null 2>&1; then
    if ! git diff --quiet 2>/dev/null; then
        echo "⚠  Uncommitted local changes — deploying anyway."
    fi
fi

echo "=== Rsync → ${DROPLET_USER}@${DROPLET_HOST}:${DROPLET_PATH} ==="
# Capture rsync output so we can detect when pyproject.toml changed and
# auto-install deps. The `| tee` preserves live streaming to the terminal.
# pipefail (set -o) propagates rsync's exit code through the pipe.
RSYNC_LOG=$(mktemp)
trap 'rm -f "$RSYNC_LOG"' EXIT
rsync $RSYNC_FLAGS \
    --exclude='.venv' \
    --exclude='web/node_modules' \
    --exclude='web/.next' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    --exclude='.git' \
    --exclude='data' \
    --exclude='*.db.bak' \
    . "${DROPLET_USER}@${DROPLET_HOST}:${DROPLET_PATH}" | tee "$RSYNC_LOG"

# Did this transfer touch pyproject.toml? If yes, deps may have changed and
# the venv on the droplet won't have them — rsync doesn't run pip.
PYPROJECT_CHANGED=false
if grep -q '^pyproject\.toml$' "$RSYNC_LOG"; then
    PYPROJECT_CHANGED=true
fi

if [[ "$RSYNC_FLAGS" == *n* ]]; then
    echo
    echo "=== Dry-run complete (no files transferred, no restart) ==="
    if [[ "$PYPROJECT_CHANGED" == "true" ]]; then
        echo "(Note: pyproject.toml would have changed — a real run would now"
        echo " install deps via: .venv/bin/pip install -e '.[dev]')"
    fi
    exit 0
fi

# Auto-install deps when pyproject.toml moved. Runs BEFORE the restart so
# the service comes back up against the new dep set rather than crashing
# on ModuleNotFoundError. Also runs when --no-restart is set, since the
# user will restart later and the deps need to be in place.
if [[ "$PYPROJECT_CHANGED" == "true" ]]; then
    echo
    echo "=== pyproject.toml changed — installing deps on droplet ==="
    ssh "${DROPLET_USER}@${DROPLET_HOST}" \
        "cd ~/hyperliquid && .venv/bin/pip install -e '.[dev]' --quiet"
    echo "✓ Deps installed."
fi

if [[ "$RESTART" == "false" ]]; then
    echo
    echo "=== Sync complete (skipping restart per --no-restart) ==="
    exit 0
fi

echo
echo "=== Restarting hl-agent service ==="
ssh "${DROPLET_USER}@${DROPLET_HOST}" 'sudo systemctl restart hl-agent && \
    sleep 2 && \
    sudo systemctl status hl-agent --no-pager | head -8 && \
    echo && \
    echo "=== First log lines after restart ===" && \
    sudo journalctl -u hl-agent --since "10 seconds ago" --no-pager | tail -15'

echo
echo "✓ Deploy complete."
