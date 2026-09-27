"""Live mode: batched Dhan LTP each cycle, run the engine, send alerts.

Scheduling
- Dhan's Quote APIs allow 1 request/s and up to 1000 instruments per LTP request, so one
  request per cycle covers all 500 tickers plus NIFTY 50: every ticker is checked every
  cycle. The cycle defaults to 60 s so live samples match the 1-minute bars the filters
  were measured on in replay.
- Only polls during the NSE session (09:15-15:30 IST) on trading days. Outside it the
  monitor sleeps until `token_refresh_lead_min` before the next open, refreshes the Dhan
  token if it would expire before that session ends, then sleeps until the open.
- A failed LTP request skips the whole cycle; a ticker missing from the reply is skipped
  for that cycle. Either way the engine sees nothing, so no alert uses stale data.

Latency is measured from the LTP response being parsed (received) to Telegram
acknowledging the alert (sent). News is fetched afterwards and posted as a reply.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .auth import AuthError, DhanAuth
from .clients import DhanClient, FetchError, Instrument, RssFeed, TelegramClient, TokenRejected, match_news
from .config import Ticker
from .market import IST, MarketCalendar
from .signals import Alert, Engine
from .store import Store

log = logging.getLogger(__name__)


def format_alert(a: Alert, name: str = "", index_name: str = "NIFTY 50") -> str:
    arrow = "▲" if a.direction > 0 else "▼"
    when = datetime.fromtimestamp(a.ts, IST).strftime("%Y-%m-%d %H:%M:%S IST")
    lines = [f"{arrow} {a.symbol} {a.move:+.2%} in {(a.ts - a.ref_ts) / 60:.0f} min"
             + (f" — {name}" if name else ""),
             f"₹{a.ref_price:,.2f} → ₹{a.price:,.2f} at {when}"]
    if a.index_move is not None:
        extra = f"{index_name} {a.index_move:+.2%}"
        if a.corr is not None:
            extra += f", corr {a.corr:.2f}, beta {a.beta:.2f}"
        lines.append(extra)
    return "\n".join(lines)


def format_news(items: list) -> str:
    if not items:
        return "No matching announcements or headlines found."
    return "News:\n" + "\n".join(f"• {n.headline} ({n.source})\n  {n.url}" for n in items)


@dataclass
class CycleStats:
    ok: int = 0
    failed: int = 0
    alerts: int = 0
    failures: list[str] = field(default_factory=list)


class LiveMonitor:
    def __init__(self, *, tickers: list[Ticker], index: Instrument, engine: Engine, dhan: DhanClient,
                 auth: DhanAuth, telegram: TelegramClient | None, feeds: list[RssFeed], store: Store,
                 calendar: MarketCalendar, cycle_s: float = 60, token_refresh_lead_min: float = 30,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.tickers = tickers
        self.names = {t.symbol: t.name for t in tickers}
        self.index = index
        self.instruments = [index] + [Instrument(t.symbol, t.security_id, "NSE_EQ", "EQUITY") for t in tickers]
        self.engine, self.dhan, self.auth, self.telegram = engine, dhan, auth, telegram
        self.feeds, self.store, self.calendar = feeds, store, calendar
        self.cycle_s, self.refresh_lead = cycle_s, timedelta(minutes=token_refresh_lead_min)
        self.clock, self.sleep = clock, sleep
        self.run_id = "live-" + uuid.uuid4().hex[:8]
        self._bg: set[asyncio.Task] = set()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        try:
            prices = await self.dhan.ltp(self.instruments)
        except TokenRejected as e:
            log.warning("cycle skipped: %s; token will be regenerated", e)
            stats.failed = len(self.instruments)
            return stats
        except (FetchError, AuthError) as e:
            log.warning("cycle skipped: %s", e)
            stats.failed = len(self.instruments)
            return stats
        received_ns = time.perf_counter_ns()
        ts = int(self.clock())
        # index first, so the correlation filter sees this cycle's index price
        for ins in self.instruments:
            px = prices.get(ins.symbol)
            if px is None:
                stats.failed += 1
                stats.failures.append(ins.symbol)
                continue
            stats.ok += 1
            alert = self.engine.on_price(ins.symbol, ts, px)
            self.store.insert_quote(ins.symbol, ts, px)
            if alert is not None:
                stats.alerts += 1
                await self._dispatch(alert, received_ns)
        return stats

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
        found = []
        for feed in self.feeds:
            try:
                found += match_news(await feed.items(), alert.symbol, self.names.get(alert.symbol, ""))
            except FetchError as e:
                log.warning("news feed %s failed: %s", feed.name, e)
        self.store.set_alert_news(alert_id, [{"source": n.source, "headline": n.headline, "url": n.url,
                                              "published": n.published} for n in found])
        if self.telegram is not None and reply_to is not None:
            try:
                await self.telegram.send(format_news(found), reply_to=reply_to)
            except FetchError as e:
                log.warning("telegram news reply failed: %s", e)

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    async def pre_open(self, session_close: datetime) -> None:
        """Make sure the token outlives the coming session (refresh margin included)."""
        tok = self.auth.token
        if tok is None or tok.expiry < session_close + self.auth.refresh_margin:
            try:
                await self.auth.generate()
            except AuthError as e:
                log.error("pre-open token refresh failed: %s (will retry on first request)", e)

    async def _sleep_until(self, when: datetime) -> None:
        while True:
            left = (when - self.now()).total_seconds()
            if left <= 0:
                return
            await self.sleep(min(left, 3600))

    async def run(self, max_cycles: int | None = None, stop_after: datetime | None = None) -> None:
        n = 0
        refreshed_for: datetime | None = None
        while max_cycles is None or n < max_cycles:
            now = self.now()
            if stop_after and now >= stop_after:
                break
            nxt = self.calendar.next_open(now)
            if nxt > now:  # market closed: wait for pre-open, refresh token, wait for open
                log.info("market closed; next session opens %s", nxt.astimezone(IST).isoformat(timespec="minutes"))
                if stop_after and nxt - self.refresh_lead >= stop_after:
                    await self._sleep_until(stop_after)
                    break
                await self._sleep_until(nxt - self.refresh_lead)
                if refreshed_for != nxt:
                    _, close = self.calendar.session(nxt.astimezone(IST).date())
                    await self.pre_open(close)
                    refreshed_for = nxt
                await self._sleep_until(nxt)
                continue
            start = self.clock()
            stats = await self.run_cycle()
            n += 1
            log.info("cycle %d: %d ok, %d failed, %d alerts in %.2fs",
                     n, stats.ok, stats.failed, stats.alerts, self.clock() - start)
            # align to the next cycle boundary
            await self.sleep(max(0.0, self.cycle_s - (self.clock() - start)))
        await self.drain()
