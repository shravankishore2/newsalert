"""SQLite persistence: alerts, live quotes, historical bars."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from .signals import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,              -- live | replay
    run_id TEXT NOT NULL,
    variant TEXT NOT NULL,           -- filtered | unfiltered
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,             -- price timestamp (epoch s)
    direction INTEGER NOT NULL,
    move REAL NOT NULL,
    ref_ts INTEGER NOT NULL,
    ref_price REAL NOT NULL,
    price REAL NOT NULL,
    index_move REAL,
    corr REAL,
    beta REAL,
    fast_sma REAL,
    slow_sma REAL,
    fast_move REAL,
    residual REAL,
    received_ns INTEGER,             -- monotonic ns when the price update was received
    sent_ns INTEGER,                 -- monotonic ns when the alert was sent
    latency_ms REAL,
    delivered INTEGER NOT NULL DEFAULT 0,
    news TEXT
);
CREATE INDEX IF NOT EXISTS alerts_run ON alerts(run_id, variant);
CREATE INDEX IF NOT EXISTS alerts_symbol_ts ON alerts(symbol, ts);
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS bars_ts ON bars(ts);
CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,             -- JSON
    updated_at REAL NOT NULL         -- epoch seconds
);
"""

# Columns added after the first release; ALTERed into older databases on open.
_MIGRATIONS = {"alerts": ["fast_sma REAL", "slow_sma REAL", "fast_move REAL", "residual REAL"]}


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # dashboard reads while live/demo writes
        self._migrate()
        self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        for table, cols in _MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue  # fresh database: SCHEMA creates the full table
            for col in cols:
                if col.split()[0] not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

    def close(self) -> None:
        self.conn.close()

    # --- alerts -----------------------------------------------------------
    def insert_alert(self, a: Alert, *, mode: str, run_id: str, variant: str,
                     received_ns: int | None, sent_ns: int | None, delivered: bool) -> int:
        latency = (sent_ns - received_ns) / 1e6 if (sent_ns and received_ns) else None
        cur = self.conn.execute(
            """INSERT INTO alerts (mode, run_id, variant, symbol, ts, direction, move, ref_ts,
               ref_price, price, index_move, corr, beta, fast_sma, slow_sma, fast_move, residual,
               received_ns, sent_ns, latency_ms, delivered)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mode, run_id, variant, a.symbol, a.ts, a.direction, a.move, a.ref_ts, a.ref_price,
             a.price, a.index_move, a.corr, a.beta, a.fast_sma, a.slow_sma, a.fast_move, a.residual,
             received_ns, sent_ns, latency, int(delivered)),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_alert_news(self, alert_id: int, news: list[dict]) -> None:
        self.conn.execute("UPDATE alerts SET news=? WHERE id=?", (json.dumps(news), alert_id))
        self.conn.commit()

    def alerts(self, run_id: str, variant: str) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        try:
            return self.conn.execute(
                "SELECT * FROM alerts WHERE run_id=? AND variant=? ORDER BY ts, symbol",
                (run_id, variant)).fetchall()
        finally:
            self.conn.row_factory = None

    def live_latencies(self) -> list[float]:
        return [r[0] for r in self.conn.execute(
            "SELECT latency_ms FROM alerts WHERE mode='live' AND delivered=1 AND latency_ms IS NOT NULL")]

    # --- status (written by live/demo, read by the dashboard) ------------------
    def set_status(self, key: str, value, now: float | None = None) -> None:
        import time as _t
        self.conn.execute("INSERT OR REPLACE INTO status VALUES (?,?,?)",
                          (key, json.dumps(value), _t.time() if now is None else now))
        self.conn.commit()

    def get_status(self) -> dict[str, dict]:
        return {k: {"value": json.loads(v), "updated_at": u}
                for k, v, u in self.conn.execute("SELECT key, value, updated_at FROM status")}

    # --- live quotes --------------------------------------------------------
    def insert_quote(self, symbol: str, ts: int, price: float) -> None:
        self.conn.execute("INSERT OR IGNORE INTO quotes VALUES (?,?,?)", (symbol, ts, price))
        self.conn.commit()

    # --- historical bars ------------------------------------------------------
    def insert_bars(self, rows: Iterable[tuple[str, int, float]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?)", rows)
        self.conn.commit()

    def iter_bars(self, table: str, index_symbol: str) -> Iterator[tuple[str, int, float]]:
        """All rows ordered by time, index symbol first within each timestamp."""
        col = "close" if table == "bars" else "price"
        yield from self.conn.execute(
            f"SELECT symbol, ts, {col} FROM {table} "
            "ORDER BY ts, CASE WHEN symbol=? THEN 0 ELSE 1 END, symbol", (index_symbol,))

    def prices_between(self, table: str, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        col = "close" if table == "bars" else "price"
        return self.conn.execute(
            f"SELECT ts, {col} FROM {table} WHERE symbol=? AND ts>? AND ts<=? ORDER BY ts",
            (symbol, start, end)).fetchall()

    def trading_days(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(DISTINCT date(ts, 'unixepoch')) FROM {table}").fetchone()[0]

    def bar_summary(self, table: str) -> tuple[int, int, int | None, int | None]:
        return self.conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts), MAX(ts) FROM {table}").fetchone()
