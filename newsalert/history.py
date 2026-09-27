"""Download 1-minute bars from Yahoo Finance (yfinance) for replay.

Finnhub's /stock/candle endpoint is premium-only, so replay history comes from
yfinance instead. Yahoo serves 1-minute bars for roughly the last 30 days, at most
8 days per request.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from .store import Store

log = logging.getLogger(__name__)


def to_yahoo(symbol: str) -> str:
    return symbol.replace(".", "-")  # BRK.B -> BRK-B


def fetch_history(store: Store, symbols: list[str], days: int, *, chunk_days: int = 7,
                  batch: int = 100, now: datetime | None = None, download=None) -> int:
    """Fetch bars into `store`. `download` defaults to yfinance.download (injectable for tests)."""
    if download is None:
        import yfinance as yf
        download = yf.download
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).date()
    end = now.date() + timedelta(days=1)
    total = 0
    for i in range(0, len(symbols), batch):
        group = symbols[i:i + batch]
        ymap = {to_yahoo(s): s for s in group}
        d0 = start
        while d0 < end:
            d1 = min(d0 + timedelta(days=chunk_days), end)
            df = download(list(ymap), start=d0.isoformat(), end=d1.isoformat(), interval="1m",
                          group_by="ticker", auto_adjust=False, progress=False, threads=True)
            rows = []
            if df is not None and len(df):
                for ysym, sym in ymap.items():
                    try:
                        closes = df[ysym]["Close"] if df.columns.nlevels > 1 else df["Close"]
                    except KeyError:
                        continue
                    for ts, px in closes.items():
                        if px is not None and not math.isnan(px):
                            rows.append((sym, int(ts.timestamp()), float(px)))
            store.insert_bars(rows)
            total += len(rows)
            log.info("bars %s..%s symbols %d-%d: %d rows", d0, d1, i, i + len(group), len(rows))
            d0 = d1
    return total
