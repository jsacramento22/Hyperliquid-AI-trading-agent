from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    model TEXT NOT NULL,
    network TEXT NOT NULL,
    reasoning TEXT,
    raw_tool_calls TEXT NOT NULL,        -- JSON array
    executed_actions TEXT NOT NULL,       -- JSON array
    rejected_actions TEXT NOT NULL        -- JSON array
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_usd REAL,
    raw_response TEXT NOT NULL            -- JSON
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    equity_usd REAL NOT NULL,
    free_margin_usd REAL NOT NULL,
    total_notional_usd REAL NOT NULL,
    margin_used_usd REAL NOT NULL,
    positions_json TEXT NOT NULL          -- JSON
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,                  -- JSON
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(ts_utc);

CREATE TABLE IF NOT EXISTS cycle_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT,
    component TEXT NOT NULL,              -- "agent_cycle" | "tp_monitor"
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cycle_errors_ts ON cycle_errors(ts_utc);
"""


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json(obj: Any) -> str:
    def default(o: Any) -> Any:
        if is_dataclass(o):
            return asdict(o)
        return str(o)

    return json.dumps(obj, default=default, ensure_ascii=False)


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def log_decision(
        self,
        *,
        cycle_id: str,
        model: str,
        network: str,
        reasoning: str,
        raw_tool_calls: list[dict],
        executed_actions: list[dict],
        rejected_actions: list[dict],
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO decisions "
                "(ts_utc, cycle_id, model, network, reasoning, raw_tool_calls, executed_actions, rejected_actions) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    _utc_now(),
                    cycle_id,
                    model,
                    network,
                    reasoning,
                    _json(raw_tool_calls),
                    _json(executed_actions),
                    _json(rejected_actions),
                ),
            )

    def log_fill(
        self,
        *,
        cycle_id: str,
        asset: str,
        side: str,
        requested_usd: float | None,
        raw_response: Any,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO fills (ts_utc, cycle_id, asset, side, requested_usd, raw_response) "
                "VALUES (?,?,?,?,?,?)",
                (_utc_now(), cycle_id, asset, side, requested_usd, _json(raw_response)),
            )

    def log_equity(self, *, cycle_id: str, account) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO equity_snapshots "
                "(ts_utc, cycle_id, equity_usd, free_margin_usd, total_notional_usd, margin_used_usd, positions_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    _utc_now(),
                    cycle_id,
                    account.equity_usd,
                    account.free_margin_usd,
                    account.total_notional_usd,
                    account.margin_used_usd,
                    _json(account.positions),
                ),
            )

    def log_cycle_error(
        self,
        *,
        cycle_id: str | None,
        component: str,
        error_type: str,
        error_message: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO cycle_errors "
                "(ts_utc, cycle_id, component, error_type, error_message) "
                "VALUES (?,?,?,?,?)",
                (
                    _utc_now(),
                    cycle_id,
                    component,
                    error_type,
                    (error_message or "")[:1000],
                ),
            )

    def last_cycle_error(self, *, within_seconds: int = 3600) -> dict | None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=within_seconds)
        cutoff_iso = cutoff.isoformat()
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT * FROM cycle_errors WHERE ts_utc >= ? ORDER BY id DESC LIMIT 1",
                (cutoff_iso,),
            ).fetchone()
        return dict(row) if row else None

    def last_cycle_ts_utc(self) -> str | None:
        """ISO timestamp of the most recently logged decision. None if the
        table is empty (fresh process or freshly cleared DB)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT ts_utc FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def starting_equity_today(self) -> float | None:
        """Equity at the first snapshot recorded today (UTC). None if no snapshot yet."""
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as c:
            row = c.execute(
                "SELECT equity_usd FROM equity_snapshots WHERE ts_utc LIKE ? "
                "ORDER BY id ASC LIMIT 1",
                (f"{today}%",),
            ).fetchone()
        return float(row[0]) if row else None

    def recent_decisions(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_fills(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def log_token_usage(
        self,
        *,
        cycle_id: str,
        model: str,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_5m_tokens: int,
        cache_write_1h_tokens: int,
        output_tokens: int,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO token_usage "
                "(ts_utc, cycle_id, model, input_tokens, cache_read_tokens, "
                "cache_write_5m_tokens, cache_write_1h_tokens, output_tokens) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    _utc_now(),
                    cycle_id,
                    model,
                    int(input_tokens),
                    int(cache_read_tokens),
                    int(cache_write_5m_tokens),
                    int(cache_write_1h_tokens),
                    int(output_tokens),
                ),
            )

    def token_usage_since(self, hours: int) -> list[dict]:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM token_usage WHERE ts_utc >= ? ORDER BY id ASC",
                (cutoff_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def equity_snapshots_since(self, hours: int) -> list[dict]:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT ts_utc, cycle_id, equity_usd, free_margin_usd, "
                "total_notional_usd, margin_used_usd "
                "FROM equity_snapshots WHERE ts_utc >= ? ORDER BY id ASC",
                (cutoff_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_runtime_value(self, key: str) -> Any | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def set_runtime_value(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, _json(value), _utc_now()),
            )
