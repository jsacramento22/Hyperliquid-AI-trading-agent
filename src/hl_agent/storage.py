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

-- Mode A advisor signal from the LightGBM tree. Logged BEFORE the LLM
-- runs so we have a record even if the cycle errors out later. The mid
-- price is captured so Phase 3 outcome backfill can score the prediction
-- against actual realised direction at horizon_bars × 15min ahead.
CREATE TABLE IF NOT EXISTS tree_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    prob_up REAL NOT NULL,
    predicted_direction TEXT NOT NULL,    -- "up" | "down"
    confidence TEXT NOT NULL,             -- "low" | "medium" | "high"
    model_version TEXT NOT NULL,
    horizon_bars INTEGER NOT NULL,
    mid_price REAL NOT NULL,
    -- Phase 3 outcome columns: NULL until the prediction's horizon has
    -- elapsed and tree_outcomes.backfill scores it against the realised
    -- close price. `correct` is stored as 0/1 (SQLite INTEGER) so it
    -- aggregates cleanly via SUM in summary queries.
    realized_close REAL,
    realized_direction TEXT,              -- "up" | "down"
    correct INTEGER                        -- 0 | 1 | NULL (unscored)
);

CREATE INDEX IF NOT EXISTS idx_tree_predictions_ts ON tree_predictions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_tree_predictions_cycle ON tree_predictions(cycle_id);
"""


def _migrate_tree_predictions(conn: sqlite3.Connection) -> None:
    """Add Phase 3 outcome columns to tree_predictions on DBs that were
    initialised at the Phase 2 schema (no realized_* columns). SQLite
    can't add a column inside an `IF NOT EXISTS` so we inspect the
    existing column list and ALTER on demand. Idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tree_predictions)").fetchall()}
    if not cols:
        # Table doesn't exist yet — the CREATE in SCHEMA will run first
        # and includes all columns, no migration needed.
        return
    for col, ddl in (
        ("realized_close", "ALTER TABLE tree_predictions ADD COLUMN realized_close REAL"),
        ("realized_direction", "ALTER TABLE tree_predictions ADD COLUMN realized_direction TEXT"),
        ("correct", "ALTER TABLE tree_predictions ADD COLUMN correct INTEGER"),
    ):
        if col not in cols:
            conn.execute(ddl)

# Note: shadow_decisions / shadow_token_usage tables may exist in older
# DBs from the A/B experiment. They are no longer written to and can be
# dropped manually if disk matters — left in place for harmless backwards
# compatibility.


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
            _migrate_tree_predictions(c)

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

    def opens_in_cycle_by_side(self, cycle_id: str) -> dict[str, str]:
        """Map of asset → 'buy'|'sell' for opening fills logged in this
        cycle_id. Used by the correlation block in Executor to detect
        same-cycle stacking on correlated assets. If an asset has multiple
        opens in one cycle, the first-logged side is returned (averaging
        in stays same-direction; cancel-then-flip would be unusual)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT asset, side FROM fills WHERE cycle_id = ? "
                "AND side IN ('buy', 'sell') ORDER BY id ASC",
                (cycle_id,),
            ).fetchall()
        out: dict[str, str] = {}
        for asset, side in rows:
            out.setdefault(asset, side)
        return out

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

    def log_tree_prediction(
        self,
        *,
        cycle_id: str,
        asset: str,
        prob_up: float,
        predicted_direction: str,
        confidence: str,
        model_version: str,
        horizon_bars: int,
        mid_price: float,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO tree_predictions "
                "(ts_utc, cycle_id, asset, prob_up, predicted_direction, "
                "confidence, model_version, horizon_bars, mid_price) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _utc_now(),
                    cycle_id,
                    asset,
                    float(prob_up),
                    predicted_direction,
                    confidence,
                    model_version,
                    int(horizon_bars),
                    float(mid_price),
                ),
            )

    def recent_tree_predictions(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM tree_predictions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def tree_predictions_needing_backfill(
        self, *, max_age_hours: int = 24
    ) -> list[dict]:
        """Predictions older than their horizon but lacking realized_close.

        `max_age_hours` bounds the search — once a prediction is older than
        this and still unscored, we treat it as permanently unscoreable
        (its target close falls outside any snapshot we'd reasonably have)
        and stop trying. Default 24h: at 15-min cadence we should always
        score within a few minutes of the horizon, so missing scores past
        24h are stuck and clog the query for no benefit.
        """
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM tree_predictions "
                "WHERE realized_close IS NULL AND ts_utc >= ? "
                "ORDER BY ts_utc ASC",
                (cutoff_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_tree_outcome(
        self,
        *,
        prediction_id: int,
        realized_close: float,
        realized_direction: str,
        correct: bool,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tree_predictions SET realized_close = ?, "
                "realized_direction = ?, correct = ? WHERE id = ?",
                (
                    float(realized_close),
                    realized_direction,
                    1 if correct else 0,
                    int(prediction_id),
                ),
            )

    def tree_accuracy_summary(
        self, *, hours: int, asset: str | None = None
    ) -> dict:
        """Roll up scored predictions over the last `hours` window. Returns
        zeros (not NaN) when no scored rows exist so the dashboard always
        has numeric values to render."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()
        sql = (
            "SELECT COUNT(*) AS n, SUM(correct) AS hits FROM tree_predictions "
            "WHERE realized_close IS NOT NULL AND ts_utc >= ?"
        )
        params: list = [cutoff_iso]
        if asset is not None:
            sql += " AND asset = ?"
            params.append(asset)
        with self._conn() as c:
            row = c.execute(sql, tuple(params)).fetchone()
        n = int(row[0] or 0)
        hits = int(row[1] or 0)
        return {
            "scored_count": n,
            "correct_count": hits,
            "accuracy": (hits / n) if n > 0 else 0.0,
        }

    def latest_tree_prediction_per_asset(self) -> dict[str, dict]:
        """Most recent row per asset, scored or not — used by the dashboard
        to surface 'what's the model saying right now'."""
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            # SQLite GROUP BY semantics: when there's no aggregate, the
            # value of the non-grouped column is implementation-defined.
            # Use the MAX(id) → row-fetch pattern instead.
            rows = c.execute(
                "SELECT * FROM tree_predictions WHERE id IN "
                "(SELECT MAX(id) FROM tree_predictions GROUP BY asset)"
            ).fetchall()
        return {r["asset"]: dict(r) for r in rows}

    def tree_predictions_since(self, hours: int) -> list[dict]:
        cutoff = datetime.now(tz=timezone.utc).timestamp() - hours * 3600
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM tree_predictions WHERE ts_utc >= ? "
                "ORDER BY id ASC",
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
