"""SQLite persistence: alerts, live quotes, API usage counters, historical bars."""

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
    received_ns INTEGER,             -- monotonic ns when the price update was received
    sent_ns INTEGER,                 -- monotonic ns when the alert was sent
    latency_ms REAL,
    delivered INTEGER NOT NULL DEFAULT 0,
    news TEXT
);
CREATE INDEX IF NOT EXISTS alerts_run ON alerts(run_id, variant);
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS bars_ts ON bars(ts);
"""


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- alerts -----------------------------------------------------------
    def insert_alert(self, a: Alert, *, mode: str, run_id: str, variant: str,
                     received_ns: int | None, sent_ns: int | None, delivered: bool) -> int:
        latency = (sent_ns - received_ns) / 1e6 if (sent_ns and received_ns) else None
        cur = self.conn.execute(
            """INSERT INTO alerts (mode, run_id, variant, symbol, ts, direction, move, ref_ts,
               ref_price, price, index_move, corr, beta, received_ns, sent_ns, latency_ms, delivered)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mode, run_id, variant, a.symbol, a.ts, a.direction, a.move, a.ref_ts, a.ref_price,
             a.price, a.index_move, a.corr, a.beta, received_ns, sent_ns, latency, int(delivered)),
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

    # --- live quotes --------------------------------------------------------
    def insert_quote(self, symbol: str, ts: int, price: float) -> None:
        self.conn.execute("INSERT OR IGNORE INTO quotes VALUES (?,?,?)", (symbol, ts, price))
        self.conn.commit()

    # --- api usage ----------------------------------------------------------
    def usage(self, provider: str, day: str) -> int:
        row = self.conn.execute("SELECT count FROM api_usage WHERE provider=? AND day=?",
                                (provider, day)).fetchone()
        return row[0] if row else 0

    def add_usage(self, provider: str, day: str) -> None:
        self.conn.execute(
            """INSERT INTO api_usage VALUES (?,?,1)
               ON CONFLICT(provider, day) DO UPDATE SET count=count+1""", (provider, day))
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
