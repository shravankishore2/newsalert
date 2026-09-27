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
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- nse | businessline
    feed TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,        -- guid or link: an item is ingested once
    url TEXT NOT NULL,
    symbol_hint TEXT,                -- NSE: symbol from the filing link
    headline TEXT,                   -- NULL for NSE (its text is never stored)
    summary TEXT,                    -- feed summary if provided; NULL for NSE
    label TEXT,                      -- NSE: derived label, e.g. "Order/contract win"
    content_hash TEXT,
    published_at REAL,
    fetched_at REAL NOT NULL,
    status TEXT NOT NULL,            -- pending | classified | unclassified | skipped
    classifier TEXT,                 -- rules | gemini
    classification TEXT,             -- JSON
    attempts INTEGER NOT NULL DEFAULT 0,
    classified_at REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS news_items_status ON news_items(status, id);
CREATE TABLE IF NOT EXISTS classification_cache (
    content_hash TEXT PRIMARY KEY,   -- same headline+summary is never classified twice
    classification TEXT NOT NULL,
    rejected TEXT,
    model TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS news_alerts (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL UNIQUE,
    mode TEXT NOT NULL DEFAULT 'live',
    created_at REAL NOT NULL,        -- when our alert fired
    published_at REAL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    confidence REAL,
    headline TEXT,                   -- BusinessLine headline, or NSE derived label
    url TEXT NOT NULL,
    classifier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_alert_stocks (
    id INTEGER PRIMARY KEY,
    news_alert_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    relation TEXT NOT NULL,
    direction TEXT,                  -- up | down | NULL (not inferred)
    strength TEXT,
    reason TEXT,
    t0 REAL, t0_rule TEXT,           -- event-study start and how it was chosen
    ret_15m REAL, idx_15m REAL, abn_15m REAL,
    ret_1h REAL, idx_1h REAL, abn_1h REAL,
    ret_close REAL, idx_close REAL, abn_close REAL,
    evaluated_at REAL, eval_note TEXT
);
CREATE INDEX IF NOT EXISTS nas_ticker ON news_alert_stocks(ticker);
CREATE TABLE IF NOT EXISTS price_news_links (
    price_alert_id INTEGER NOT NULL,
    news_alert_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    minutes_after REAL NOT NULL,
    PRIMARY KEY (price_alert_id, news_alert_id)
);
CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (provider, day)
);
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
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")  # dashboard reads while live/demo writes
        self.conn.execute("PRAGMA busy_timeout=30000")  # live monitor and news service share alerts.db
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

    # --- api usage (daily request counters) ---------------------------------------------
    def usage(self, provider: str, day: str) -> int:
        row = self.conn.execute("SELECT count FROM api_usage WHERE provider=? AND day=?", (provider, day)).fetchone()
        return row[0] if row else 0

    def add_usage(self, provider: str, day: str) -> None:
        self.conn.execute("""INSERT INTO api_usage VALUES (?,?,1)
                             ON CONFLICT(provider, day) DO UPDATE SET count=count+1""", (provider, day))
        self.conn.commit()

    # --- price <-> news links ---------------------------------------------------------------
    def link_price_alert(self, price_alert_id: int, symbol: str, ts: int, window_s: int = 3600) -> list[int]:
        """Link a price alert to news alerts on the same stock fired within window_s before it."""
        rows = self.conn.execute(
            """SELECT na.id, na.created_at FROM news_alerts na JOIN news_alert_stocks s ON s.news_alert_id = na.id
               WHERE s.ticker = ? AND na.created_at <= ? AND na.created_at >= ?""",
            (symbol, ts, ts - window_s)).fetchall()
        for nid, created in rows:
            self.conn.execute("INSERT OR IGNORE INTO price_news_links VALUES (?,?,?,?)",
                              (price_alert_id, nid, symbol, (ts - created) / 60))
        self.conn.commit()
        return [r[0] for r in rows]

    # --- live quotes --------------------------------------------------------
    def insert_quote(self, symbol: str, ts: int, price: float) -> None:
        self.conn.execute("INSERT OR IGNORE INTO quotes VALUES (?,?,?)", (symbol, ts, price))
        self.conn.commit()

    # --- historical bars ------------------------------------------------------
    def insert_bars(self, rows: Iterable[tuple[str, int, float]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?)", rows)
        self.conn.commit()

    @staticmethod
    def _session_sql(session: tuple[int, int, int] | None) -> tuple[str, tuple]:
        """session = (utc_offset_s, open_s, close_s): keep rows whose local time-of-day is in
        [open, close). Only for fixed-offset markets (IST has no DST)."""
        if session is None:
            return "", ()
        off, o, c = session
        return " AND ((ts + ?) % 86400) >= ? AND ((ts + ?) % 86400) < ?", (off, o, off, c)

    def iter_bars(self, table: str, index_symbol: str,
                  session: tuple[int, int, int] | None = None) -> Iterator[tuple[str, int, float]]:
        """All rows ordered by time, index symbol first within each timestamp."""
        col = "close" if table == "bars" else "price"
        where, args = self._session_sql(session)
        yield from self.conn.execute(
            f"SELECT symbol, ts, {col} FROM {table} WHERE 1=1{where} "
            "ORDER BY ts, CASE WHEN symbol=? THEN 0 ELSE 1 END, symbol", (*args, index_symbol))

    def prices_between(self, table: str, symbol: str, start: int, end: int,
                       session: tuple[int, int, int] | None = None) -> list[tuple[int, float]]:
        col = "close" if table == "bars" else "price"
        where, args = self._session_sql(session)
        return self.conn.execute(
            f"SELECT ts, {col} FROM {table} WHERE symbol=? AND ts>? AND ts<=?{where} ORDER BY ts",
            (symbol, start, end, *args)).fetchall()

    def trading_days(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(DISTINCT date(ts, 'unixepoch')) FROM {table}").fetchone()[0]

    def bar_summary(self, table: str) -> tuple[int, int, int | None, int | None]:
        return self.conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts), MAX(ts) FROM {table}").fetchone()
