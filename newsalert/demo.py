"""Demo mode: replay stored 1-minute bars through the real alert engine at a chosen speed,
writing alerts and status to a demo database exactly as live mode would. The dashboard
reads it like live data but labels everything as replay.

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
                 speed: float = 60, warm_days: int = 1,
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
        self.run_id = "demo-" + uuid.uuid4().hex[:8]
        self.sim_ts: int | None = None
        self.market_open = False
        self.minutes_done = 0
        self.total_minutes = self.history.conn.execute("SELECT COUNT(DISTINCT ts) FROM bars").fetchone()[0]
        self.alerts = 0
        self.finished = False

    def _publish(self, n_prices: int = 0, n_alerts: int = 0) -> None:
        now = self.clock()
        self.out.set_status("replay", {
            "dataset": self.dataset["label"], "sim_ts": self.sim_ts, "market_open": self.market_open,
            "speed": self.speed, "minutes_done": self.minutes_done, "total_minutes": self.total_minutes,
            "alerts": self.alerts, "finished": self.finished}, now=now)
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
            self.out.set_alert_news(alert_id, [])
            n_alerts += 1
        self.alerts += n_alerts
        return n_alerts

    def _minutes(self):
        cur_ts, group = None, []
        for symbol, ts, price in self.history.iter_bars("bars", self.index):
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
        self.out.set_status("feeds", {"_note": {"ok": None, "error": None,
                                                "note": "demo mode: replayed data has no archived news"}})
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
        return self.history.prices_between("bars", symbol, start - 1, end)
