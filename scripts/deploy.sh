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
    . "${DROPLET_USER}@${DROPLET_HOST}:${DROPLET_PATH}"

if [[ "$RSYNC_FLAGS" == *n* ]]; then
    echo
    echo "=== Dry-run complete (no files transferred, no restart) ==="
    exit 0
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
