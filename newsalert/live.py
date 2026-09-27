"""Live mode: poll Finnhub quotes within the rate limit, run the engine, send alerts.

Scheduling: one shared Finnhub limiter (per-second and per-minute windows). A producer
walks the ticker list round-robin and interleaves an index quote every `index_every`
tickers; a few workers pull jobs and fetch. A failed fetch drops that ticker for the
cycle (nothing is fed to the engine), so no alert is ever computed from stale data.

Latency is measured from the moment a quote response is parsed (received) to the
moment Telegram acknowledges the alert message (sent). News is fetched after the alert
is sent and posted as a reply, so news lookups never delay the alert itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .clients import FetchError, FinnhubClient, NewsApiClient, TelegramClient
from .config import Ticker
from .signals import Alert, Engine
from .store import Store

log = logging.getLogger(__name__)


def format_alert(a: Alert, name: str = "") -> str:
    arrow = "▲" if a.direction > 0 else "▼"
    when = datetime.fromtimestamp(a.ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"{arrow} {a.symbol} {a.move:+.2%} in {(a.ts - a.ref_ts) / 60:.0f} min"
             + (f" — {name}" if name else ""),
             f"{a.ref_price:.2f} → {a.price:.2f} at {when}"]
    if a.index_move is not None:
        extra = f"index {a.index_move:+.2%}"
        if a.corr is not None:
            extra += f", corr {a.corr:.2f}, beta {a.beta:.2f}"
        lines.append(extra)
    return "\n".join(lines)


def format_news(items: list[dict]) -> str:
    if not items:
        return "No recent headlines found."
    return "Headlines:\n" + "\n".join(f"• {n['headline']} ({n['source']})\n  {n['url']}" for n in items)


@dataclass
class CycleStats:
    ok: int = 0
    failed: int = 0
    stale: int = 0
    alerts: int = 0
    failures: list[str] = field(default_factory=list)


class LiveMonitor:
    def __init__(self, *, tickers: list[Ticker], index_symbol: str, engine: Engine,
                 finnhub: FinnhubClient, telegram: TelegramClient | None,
                 newsapi: NewsApiClient | None, store: Store, workers: int = 4,
                 index_every: int = 10, news_lookback_days: int = 2, newsapi_lookback_days: int = 3):
        self.tickers = tickers
        self.names = {t.symbol: t.name for t in tickers}
        self.index_symbol = index_symbol
        self.engine, self.finnhub, self.telegram, self.newsapi = engine, finnhub, telegram, newsapi
        self.store = store
        self.workers, self.index_every = workers, max(1, index_every)
        self.news_lookback_days, self.newsapi_lookback_days = news_lookback_days, newsapi_lookback_days
        self.run_id = "live-" + uuid.uuid4().hex[:8]
        self._bg: set[asyncio.Task] = set()

    def _jobs(self) -> list[str]:
        jobs = []
        for i, t in enumerate(self.tickers):
            if i % self.index_every == 0:
                jobs.append(self.index_symbol)
            jobs.append(t.symbol)
        return jobs

    async def _fetch_one(self, symbol: str, stats: CycleStats) -> None:
        try:
            q = await self.finnhub.quote(symbol)
        except FetchError as e:  # includes RateLimited; the limiter is already paused
            stats.failed += 1
            stats.failures.append(f"{symbol}: {e}")
            log.warning("skip %s this cycle: %s", symbol, e)
            return
        received_ns = time.perf_counter_ns()
        stats.ok += 1
        series = self.engine.series.get(q.symbol)
        if series is not None and series.ts and q.ts <= series.ts[-1]:
            stats.stale += 1  # no trade since last poll (e.g. market closed)
            return
        alert = self.engine.on_price(q.symbol, q.ts, q.price)
        if alert is not None:
            stats.alerts += 1
            await self._dispatch(alert, received_ns)
        self.store.insert_quote(q.symbol, q.ts, q.price)

    async def _dispatch(self, alert: Alert, received_ns: int) -> None:
        msg_id, sent_ns = None, None
        if self.telegram is not None:
            try:
                msg_id = await self.telegram.send(format_alert(alert, self.names.get(alert.symbol, "")))
                sent_ns = time.perf_counter_ns()
            except FetchError as e:
                log.error("telegram send failed for %s: %s", alert.symbol, e)
        alert_id = self.store.insert_alert(alert, mode="live", run_id=self.run_id, variant="filtered",
                                           received_ns=received_ns, sent_ns=sent_ns,
                                           delivered=sent_ns is not None)
        log.info("ALERT %s %+.2f%% latency=%s", alert.symbol, alert.move * 100,
                 f"{(sent_ns - received_ns) / 1e6:.1f}ms" if sent_ns else "n/a")
        task = asyncio.create_task(self._news(alert, alert_id, msg_id))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _news(self, alert: Alert, alert_id: int, reply_to: int | None) -> None:
        today = datetime.now(timezone.utc).date()
        items: list[dict] = []
        try:
            items += await self.finnhub.company_news(alert.symbol, today, self.news_lookback_days)
        except FetchError as e:
            log.warning("finnhub news failed for %s: %s", alert.symbol, e)
        if self.newsapi is not None:
            try:
                items += await self.newsapi.headlines(alert.symbol, self.names.get(alert.symbol, ""),
                                                      today, self.newsapi_lookback_days)
            except FetchError as e:
                log.warning("newsapi failed for %s: %s", alert.symbol, e)
        self.store.set_alert_news(alert_id, items)
        if self.telegram is not None and reply_to is not None:
            try:
                await self.telegram.send(format_news(items), reply_to=reply_to)
            except FetchError as e:
                log.warning("telegram news reply failed: %s", e)

    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        queue: asyncio.Queue[str] = asyncio.Queue()
        for s in self._jobs():
            queue.put_nowait(s)

        async def worker() -> None:
            while True:
                try:
                    sym = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await self._fetch_one(sym, stats)

        await asyncio.gather(*(worker() for _ in range(self.workers)))
        return stats

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    async def run(self, cycles: int | None = None) -> None:
        n = 0
        while cycles is None or n < cycles:
            start = time.monotonic()
            stats = await self.run_cycle()
            n += 1
            log.info("cycle %d: %d ok, %d failed, %d unchanged, %d alerts in %.1fs",
                     n, stats.ok, stats.failed, stats.stale, stats.alerts, time.monotonic() - start)
        await self.drain()
