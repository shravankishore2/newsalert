"""Download 1-minute bars from Dhan's intraday historical endpoint for replay.

Dhan serves 1-minute bars for up to 5 years back, at most 90 days per request, under the
Data API limits (5 requests/s, 100,000/day). One request per instrument covers 90 days.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta

from .clients import DhanClient, FetchError, Instrument
from .market import IST
from .store import Store

log = logging.getLogger(__name__)

MAX_DAYS_PER_REQUEST = 90


def chunks(start: datetime, end: datetime, days: int = MAX_DAYS_PER_REQUEST) -> list[tuple[datetime, datetime]]:
    out, s = [], start
    while s < end:
        e = min(s + timedelta(days=days), end)
        out.append((s, e))
        s = e
    return out


def session_fraction(ts_list: list[int], open_t: time, close_t: time) -> float:
    """Share of bar timestamps that fall inside the NSE session in IST. Well under 1.0
    means the epoch isn't what we think (e.g. IST wall time encoded as UTC)."""
    if not ts_list:
        return 1.0
    inside = sum(open_t <= datetime.fromtimestamp(t, IST).time() < close_t for t in ts_list)
    return inside / len(ts_list)


async def fetch_history(store: Store, dhan: DhanClient, instruments: list[Instrument], days: int,
                        now: datetime, *, concurrency: int = 4, open_t: time = time(9, 15),
                        close_t: time = time(15, 30), retry_pause_s: float = 30,
                        sleep=asyncio.sleep) -> dict:
    end = now.astimezone(IST)
    start = end - timedelta(days=days)
    windows = chunks(start, end)
    queue: asyncio.Queue[Instrument] = asyncio.Queue()
    for ins in instruments:
        queue.put_nowait(ins)
    report = {"bars": 0, "failed": [], "empty": [], "out_of_session": {}, "retried": 0}
    failed_ins: list[Instrument] = []

    async def worker() -> None:
        while True:
            try:
                ins = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            rows = []
            try:
                for s, e in windows:
                    rows += await dhan.intraday(ins, s, e, interval=1)
            except FetchError as e:
                report["failed"].append(f"{ins.symbol}: {e}")
                failed_ins.append(ins)
                log.warning("history %s failed: %s", ins.symbol, e)
                continue
            rows = sorted(dict(rows).items())  # windows share a boundary minute; keep one bar per ts
            if not rows:
                report["empty"].append(ins.symbol)
                continue
            frac = session_fraction([t for t, _ in rows], open_t, close_t)
            if frac < 0.95:
                report["out_of_session"][ins.symbol] = round(frac, 3)
            store.insert_bars([(ins.symbol, t, c) for t, c in rows])
            report["bars"] += len(rows)
            log.info("history %s: %d bars", ins.symbol, len(rows))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    if failed_ins:
        # Transient network errors (resets, timeouts) and rate limits are common on long runs:
        # one more pass after a pause (on 2026-09-28 an immediate retry hit the rate limit again).
        await sleep(retry_pause_s)
        report["retried"] = len(failed_ins)
        report["failed"] = []
        for ins in failed_ins:
            queue.put_nowait(ins)
        failed_ins.clear()
        await asyncio.gather(*(worker() for _ in range(min(concurrency, 2))))
    return report
