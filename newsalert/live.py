"""Live mode: batched Dhan LTP each cycle, run the engine, publish alerts to the dashboard.

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

Alerts are written to SQLite; the dashboard (web/app.py) pushes new rows to browsers.
News comes from the separate news service (news/ingest.py); a price alert on a stock within
`link_window_min` after a news alert on that stock is linked to it.
Latency is measured from the LTP response being parsed (received) to the alert row being
committed (sent). The dashboard's push loop adds at most its poll interval on top.
Status for the dashboard (cycle times, token, feed health) is written to the status table.
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
from .clients import DhanClient, FetchError, Instrument, TokenRejected
from .config import Ticker
from .market import IST, MarketCalendar
from .signals import Alert, Engine
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class CycleStats:
    ok: int = 0
    failed: int = 0
    alerts: int = 0
    failures: list[str] = field(default_factory=list)


class LiveMonitor:
    def __init__(self, *, tickers: list[Ticker], index: Instrument, engine: Engine, dhan: DhanClient,
                 auth: DhanAuth, store: Store, calendar: MarketCalendar,
                 cycle_s: float = 60, token_refresh_lead_min: float = 30, link_window_min: float = 60,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.tickers = tickers
        self.names = {t.symbol: t.name for t in tickers}
        self.index = index
        self.instruments = [index] + [Instrument(t.symbol, t.security_id, "NSE_EQ", "EQUITY") for t in tickers]
        self.engine, self.dhan, self.auth = engine, dhan, auth
        self.store, self.calendar = store, calendar
        self.link_window_s = link_window_min * 60
        self.cycle_s, self.refresh_lead = cycle_s, timedelta(minutes=token_refresh_lead_min)
        self.clock, self.sleep = clock, sleep
        self.run_id = "live-" + uuid.uuid4().hex[:8]
        self._bg: set[asyncio.Task] = set()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    # -- status for the dashboard ------------------------------------------------
    def publish_token_status(self, error: str | None = None) -> None:
        tok = self.auth.token
        self.store.set_status("token", {
            "state": "error" if error else ("valid" if tok and tok.expiry > self.now() else "missing"),
            "expires_at": tok.expiry.isoformat() if tok else None,
            "auto_refresh": self.auth.can_generate,
            "error": error,
        }, now=self.clock())

    # -- cycle ------------------------------------------------------------------------
    async def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        started = self.clock()
        try:
            prices = await self.dhan.ltp(self.instruments)
        except (FetchError, AuthError) as e:
            if isinstance(e, (TokenRejected, AuthError)):
                self.publish_token_status(str(e))
            log.warning("cycle skipped: %s", e)
            stats.failed = len(self.instruments)
            self._publish_cycle(started, stats, str(e))
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
                self._dispatch(alert, received_ns)
        self._publish_cycle(started, stats, None)
        return stats

    def _publish_cycle(self, started: float, stats: CycleStats, error: str | None) -> None:
        self.store.set_status("cycle", {"at": started, "ok": stats.ok, "failed": stats.failed,
                                        "alerts": stats.alerts, "error": error,
                                        "missing": stats.failures[:20]}, now=self.clock())

    def _dispatch(self, alert: Alert, received_ns: int) -> None:
        alert_id = self.store.insert_alert(alert, mode="live", run_id=self.run_id, variant="filtered",
                                           received_ns=received_ns, sent_ns=None, delivered=False)
        sent_ns = time.perf_counter_ns()
        self.store.conn.execute("UPDATE alerts SET sent_ns=?, latency_ms=?, delivered=1 WHERE id=?",
                                (sent_ns, (sent_ns - received_ns) / 1e6, alert_id))
        self.store.conn.commit()
        linked = self.store.link_price_alert(alert_id, alert.symbol, alert.ts, int(self.link_window_s))
        log.info("ALERT %s %+.2f%% latency=%.1fms%s", alert.symbol, alert.move * 100, (sent_ns - received_ns) / 1e6,
                 f" linked to news {linked}" if linked else "")

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

    # -- scheduler ------------------------------------------------------------------
    async def pre_open(self, session_close: datetime) -> None:
        """Make sure the token outlives the coming session (refresh margin included)."""
        tok = self.auth.token
        if tok is None or tok.expiry < session_close + self.auth.refresh_margin:
            try:
                await self.auth.generate()
                self.publish_token_status()
            except AuthError as e:
                log.error("pre-open token refresh failed: %s (will retry on first request)", e)
                self.publish_token_status(str(e))

    async def _sleep_until(self, when: datetime) -> None:
        while True:
            left = (when - self.now()).total_seconds()
            if left <= 0:
                return
            await self.sleep(min(left, 3600))

    async def run(self, max_cycles: int | None = None, stop_after: datetime | None = None) -> None:
        n = 0
        refreshed_for: datetime | None = None
        self.store.set_status("mode", {"mode": "live", "run_id": self.run_id}, now=self.clock())
        self.publish_token_status()
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
            await self.sleep(max(0.0, self.cycle_s - (self.clock() - start)))
        await self.drain()
