"""Build / version identification.

Computes a short fingerprint over the mtimes of all source .py files in the
package. The fingerprint changes whenever any source file is modified, so the
UI can tell at a glance whether the running backend has the latest code.

We snapshot the fingerprint at process startup; later requests can compare it
against the current on-disk fingerprint to detect "code changed but server
not restarted."
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

_PKG_DIR = Path(__file__).resolve().parent


def _source_files() -> list[Path]:
    return sorted(_PKG_DIR.glob("*.py"))


def compute_fingerprint() -> dict:
    files = _source_files()
    if not files:
        return {"fingerprint": "0", "latest_file": "", "latest_mtime": 0.0}
    parts: list[str] = []
    latest_mtime = 0.0
    latest_file = ""
    for f in files:
        m = f.stat().st_mtime
        parts.append(f"{f.name}:{m:.6f}")
        if m > latest_mtime:
            latest_mtime = m
            latest_file = f.name
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]
    return {
        "fingerprint": digest,
        "latest_file": latest_file,
        "latest_mtime": latest_mtime,
    }


# Captured once at import time — represents the code that this process loaded.
STARTED_AT = datetime.now(tz=timezone.utc)
STARTUP = compute_fingerprint()


def status() -> dict:
    """Combine the captured startup info with a fresh on-disk fingerprint so
    callers can detect drift."""
    current = compute_fingerprint()
    return {
        "version": __version__,
        "started_at_utc": STARTED_AT.isoformat(),
        "started_at_unix": STARTED_AT.timestamp(),
        "uptime_seconds": (
            datetime.now(tz=timezone.utc) - STARTED_AT
        ).total_seconds(),
        "build": {
            "running": STARTUP["fingerprint"],
            "disk": current["fingerprint"],
            "stale": STARTUP["fingerprint"] != current["fingerprint"],
            "latest_source_file": current["latest_file"],
            "latest_source_mtime_unix": current["latest_mtime"],
        },
    }
