"""Demo mode: replay stored 1-minute bars through the real alert engine at a chosen speed,
writing alerts and status to a demo database exactly as live mode would. The dashboard
reads it like live data but labels everything as replay.

With a news archive (the live alerts.db), archived news alerts are replayed together with
prices, only on days where both exist (plus one warm-up day before). Each news alert
appears when the replay clock reaches its original alert time (news from outside market
hours appears at the next replayed open), and price alerts link to it as in live mode.

No credentials or market hours needed. Replayed data has no archived news, so demo
alerts have no news items (the UI says so rather than showing made-up headlines).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .signals import Engine, Params
from .store import Store

log = logging.getLogger(__name__)

MAX_GAP_SLEEP_S = 3.0   # overnight/weekend gaps are compressed to at most this much real time


class DemoDriver:
    def __init__(self, *, history_db: str, demo_db: str, dataset: dict, alerts_cfg: dict,
                 speed: float = 60, warm_days: int = 1, session: tuple[int, int, int] | None = None,
                 news_db: str | None = None, link_window_min: float = 60,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time):
        self.history = Store(history_db)
        for suffix in ("", "-wal", "-shm"):   # the demo database is scratch, rebuilt each start
            Path(demo_db + suffix).unlink(missing_ok=True)
        self.out = Store(demo_db)
        self.dataset, self.speed, self.warm_days = dataset, speed, warm_days
        self.tz = ZoneInfo(dataset["timezone"])
        self.index = dataset["index_symbol"]
        self.engine = Engine(Params.from_config(alerts_cfg), self.index)
        self.sleep, self.clock = sleep, clock
        self.session = session  # same out-of-hours filter as `replay`, so demo shows what was measured
        self.run_id = "demo-" + uuid.uuid4().hex[:8]
        self.sim_ts: int | None = None
        self.market_open = False
        self.minutes_done = 0
        # Minutes and days come from the index's own bars (primary-key lookup), never a scan of all
        # ~12M rows: on 2026-09-28 such scans blocked the demo's web server for minutes (HTTP 502).
        self._index_ts = [t for (t,) in self.history.conn.execute(
            "SELECT ts FROM bars WHERE symbol = ? ORDER BY ts", (self.index,))]
        self.bar_days = sorted({datetime.fromtimestamp(t, self.tz).date() for t in self._index_ts})
        self.total_minutes = self._count_minutes(None)
        self.alerts = 0
        self.finished = False
        self.link_window_s = link_window_min * 60
        self.action_items: list[dict] = []
        self.news: list[dict] = self._load_news(news_db) if news_db else []
        self.news_emitted = 0
        self.action_emitted = 0
        self.days: set | None = None          # replay only these dates (None = all)
        if self.news:
            bar_days = self.bar_days
            news_days = {datetime.fromtimestamp(n["created_at"], self.tz).date() for n in self.news}
            both = sorted(d for d in bar_days if d in news_days)
            if both:
                warm = [d for d in bar_days if d < both[0]][-1:]
                self.days = set(both) | set(warm)
                self.warm_days = len(warm)
                self.total_minutes = self._count_minutes(self.days)
            self.news = [n for n in self.news if self.days is None or
                         datetime.fromtimestamp(n["created_at"], self.tz).date() >= min(self.days)]
            if self.days is not None:
                self.action_items = [a for a in self.action_items
                                     if datetime.fromtimestamp(a["fetched_at"], self.tz).date() >= min(self.days)]

    def _publish(self, n_prices: int = 0, n_alerts: int = 0) -> None:
        now = self.clock()
        self.out.set_status("replay", {
            "dataset": self.dataset["label"], "sim_ts": self.sim_ts, "market_open": self.market_open,
            "speed": self.speed, "minutes_done": self.minutes_done, "total_minutes": self.total_minutes,
            "alerts": self.alerts, "finished": self.finished,
            "news_archive": {"alerts": len(self.news), "emitted": self.news_emitted,
                             "days": sorted(str(d) for d in self.days) if self.days else []}}, now=now)
        if n_prices:
            self.out.set_status("cycle", {"at": now, "sim_ts": self.sim_ts, "ok": n_prices, "failed": 0,
                                          "alerts": n_alerts, "error": None, "missing": []}, now=now)

    def _process(self, ts: int, group: list[tuple[str, float]]) -> int:
        n_alerts = 0
        for symbol, price in group:
            received = time.perf_counter_ns()
            a = self.engine.on_price(symbol, ts, price)
            if a is None:
                continue
            alert_id = self.out.insert_alert(a, mode="demo", run_id=self.run_id, variant="filtered",
                                             received_ns=received, sent_ns=time.perf_counter_ns(), delivered=True)
            self.out.link_price_alert(alert_id, symbol, ts, int(self.link_window_s))
            n_alerts += 1
        self.alerts += n_alerts
        return n_alerts

    def _load_news(self, path: str) -> list[dict]:
        if not Path(path).exists():
            return []
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='news_alerts'").fetchone():
                return []
            item_cols = {r[1] for r in conn.execute("PRAGMA table_info(news_items)")}
            cols = [c.strip() for c in "source, feed, key, url, symbol_hint, headline, summary, label, action_kind, action_date, content_hash, published_at, fetched_at, status, classifier, classification, classified_at, error".split(",") if c.strip() in item_cols]
            def item(item_id):
                row = conn.execute(f"SELECT {', '.join(cols)} FROM news_items WHERE id=?", (item_id,)).fetchone()
                return dict(row) if row else None
            out = []
            for n in conn.execute("SELECT * FROM news_alerts WHERE mode='live' ORDER BY created_at"):
                d = dict(n)
                d["stocks"] = [dict(s) for s in conn.execute(
                    "SELECT ticker, relation, direction, strength, reason FROM news_alert_stocks WHERE news_alert_id=?",
                    (n["id"],))]
                d["item"] = item(n["item_id"])
                out.append(d)
            # corporate-action filings that never raised an alert (e.g. record dates) for the actions board
            self.action_items = []
            if "action_kind" in item_cols:
                alerted = {d["item_id"] for d in out}
                for r in conn.execute(f"SELECT id, {', '.join(cols)} FROM news_items WHERE action_kind IS NOT NULL "
                                      "ORDER BY fetched_at"):
                    if r["id"] not in alerted:
                        self.action_items.append({k: r[k] for k in cols})
            return out
        finally:
            conn.close()

    def _copy_item(self, it: dict | None) -> int | None:
        """Copy an archived news item (derived fields only for NSE, as archived) into the demo db."""
        if not it:
            return None
        cols = list(it)
        cur = self.out.conn.execute(f"INSERT OR IGNORE INTO news_items ({', '.join(cols)}) VALUES "
                                    f"({', '.join('?' * len(cols))})", [it[c] for c in cols])
        if cur.rowcount:
            return cur.lastrowid
        row = self.out.conn.execute("SELECT id FROM news_items WHERE key=?", (it["key"],)).fetchone()
        return row[0] if row else None

    def _emit_news(self, upto: float) -> None:
        while self.action_emitted < len(self.action_items) and self.action_items[self.action_emitted]["fetched_at"] <= upto:
            self._copy_item(self.action_items[self.action_emitted])
            self.action_emitted += 1
        while self.news_emitted < len(self.news) and self.news[self.news_emitted]["created_at"] <= upto:
            n = self.news[self.news_emitted]
            item_id = self._copy_item(n.get("item")) or n["item_id"]
            cur = self.out.conn.execute(
                """INSERT INTO news_alerts (item_id, mode, created_at, published_at, source, event_type, confidence,
                   headline, url, classifier) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (item_id, "demo", n["created_at"], n["published_at"], n["source"], n["event_type"],
                 n["confidence"], n["headline"], n["url"], n["classifier"]))
            for st in n["stocks"]:
                self.out.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, "
                                      "reason) VALUES (?,?,?,?,?,?)", (cur.lastrowid, st["ticker"], st["relation"],
                                                                     st["direction"], st["strength"], st["reason"]))
            self.news_emitted += 1
        self.out.conn.commit()

    def _in_session(self, ts: int) -> bool:
        if self.session is None:
            return True
        off, o, c = self.session
        return o <= (ts + off) % 86400 < c

    def _count_minutes(self, days) -> int:
        return sum(1 for t in self._index_ts if self._in_session(t) and
                   (days is None or datetime.fromtimestamp(t, self.tz).date() in days))

    def _minutes(self):
        """Replay minutes day by day (small indexed queries) instead of one sort over every bar."""
        for day in self.bar_days:
            if self.days is not None and day not in self.days:
                continue
            start = int(datetime.combine(day, datetime.min.time(), self.tz).timestamp())
            cur_ts, group = None, []
            for symbol, ts, price in self.history.iter_bars("bars", self.index, self.session, start, start + 86400):
                if ts != cur_ts and group:
                    yield cur_ts, group
                    group = []
                cur_ts = ts
                group.append((symbol, price))
            if group:
                yield cur_ts, group

    async def run(self) -> None:
        self.out.set_status("mode", {"mode": "demo", "run_id": self.run_id, "dataset": self.dataset["label"]})
        self.out.set_status("token", {"state": "not used", "note": "demo mode replays stored data; no Dhan login"})
        self.out.set_status("news", {"replay": True, "archived_alerts": len(self.news),
                                     "note": ("replaying archived news alerts with prices" if self.news else
                                              "no archived news for these days yet; prices only")})
        warm_dates: set = set()
        prev = None
        for ts, group in self._minutes():
            day = datetime.fromtimestamp(ts, self.tz).date()
            warming = len(warm_dates | {day}) <= self.warm_days
            if warming:
                warm_dates.add(day)
            elif prev is not None:
                gap = ts - prev
                if gap > 300:            # session break: show the market as closed briefly
                    self.market_open = False
                    self._publish()
                    await self.sleep(min(gap / self.speed, MAX_GAP_SLEEP_S))
                else:
                    await self.sleep(gap / self.speed)
            self.sim_ts, self.market_open = ts, True
            if self.news or self.action_items:
                self._emit_news(ts)
            n = self._process(ts, group)
            self.minutes_done += 1
            if not warming or self.minutes_done % 60 == 0:
                self._publish(len(group), n)
            if warming and self.minutes_done % 30 == 0:
                await asyncio.sleep(0)   # keep the web server responsive while warming up
            prev = ts
        self.finished, self.market_open = True, False
        self._publish()
        log.info("demo replay finished: %d alerts over %d minutes", self.alerts, self.minutes_done)

    def market_state(self) -> dict:
        return {"open": self.market_open, "now_ts": self.sim_ts, "simulated": True,
                "timezone": self.dataset["timezone"]}

    def prices(self, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
        if self.sim_ts is not None:
            end = min(end, self.sim_ts)   # never show "future" prices during the replay
        return self.history.prices_between("bars", symbol, start - 1, end, self.session)
