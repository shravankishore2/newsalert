"""Optional earnings expectations from Finnhub's free earnings calendar.

Why Finnhub: its terms allow personal use ("All plan listed on Finnhub website is strictly
for personal use"; no sharing of data or derived results), which fits a single-user,
password-protected dashboard. `/calendar/earnings` is not marked premium in Finnhub's API
spec (free tier: "1 month of historical earnings and new updates") and takes
`international=true`. Whether Indian (NSE) symbols are actually returned on the free tier
is UNVERIFIED until a key is configured; with no key this module is simply off and the
results board falls back to stated figures or the stock reaction, and says so.

Response fields used (per Finnhub's docs): date, epsActual, epsEstimate, revenueActual,
revenueEstimate, quarter, year, symbol.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import httpx

from ..ratelimit import RateLimiter

log = logging.getLogger(__name__)


@dataclass
class Expectation:
    symbol: str
    report_date: str
    eps_estimate: float | None
    eps_actual: float | None
    revenue_estimate: float | None
    revenue_actual: float | None
    quarter: int | None
    year: int | None


def surprise_pct(actual: float | None, estimate: float | None) -> float | None:
    """(actual - estimate) / |estimate| in percent; None when either is missing or estimate is 0."""
    if actual is None or estimate is None or estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100


def verdict(surprise: float | None) -> str | None:
    """Five-step verdict used by the gauge: strong miss .. strong beat."""
    if surprise is None:
        return None
    if surprise <= -10:
        return "strong miss"
    if surprise < -2:
        return "miss"
    if surprise <= 2:
        return "in line"
    if surprise < 10:
        return "beat"
    return "strong beat"


class FinnhubEarnings:
    def __init__(self, http: httpx.AsyncClient, api_key: str, store, *,
                 base_url: str = "https://finnhub.io/api/v1", per_minute: int = 30,
                 symbol_suffix: str = ".NS", clock: Callable[[], float] = time.time,
                 limiter: RateLimiter | None = None):
        self.http, self._key, self.store, self.base_url = http, api_key, store, base_url
        self.suffix, self.clock = symbol_suffix, clock
        self.limiter = limiter or RateLimiter([(per_minute, 60.0)])
        self.last_error: str | None = None

    async def fetch(self, symbol: str, around: date) -> Expectation | None:
        """Look up the report nearest `around` (±3 days) and store it. None if not covered."""
        await self.limiter.acquire()
        params = {"from": (around - timedelta(days=3)).isoformat(), "to": (around + timedelta(days=1)).isoformat(),
                  "symbol": f"{symbol}{self.suffix}", "international": "true"}
        try:
            resp = await self.http.get(f"{self.base_url}/calendar/earnings", params=params,
                                       headers={"X-Finnhub-Token": self._key})
        except httpx.HTTPError as e:
            self.last_error = f"finnhub: {type(e).__name__}"
            return None
        if resp.status_code != 200:
            self.last_error = f"finnhub: HTTP {resp.status_code}"
            return None
        try:
            rows = (resp.json() or {}).get("earningsCalendar") or []
        except ValueError:
            self.last_error = "finnhub: bad JSON"
            return None
        self.last_error = None
        rows = [r for r in rows if r.get("date")]
        if not rows:
            return None
        best = min(rows, key=lambda r: abs((date.fromisoformat(r["date"]) - around).days))
        e = Expectation(symbol, best["date"], best.get("epsEstimate"), best.get("epsActual"),
                        best.get("revenueEstimate"), best.get("revenueActual"), best.get("quarter"), best.get("year"))
        self.store.conn.execute(
            "INSERT OR REPLACE INTO earnings_expectations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e.symbol, e.report_date, e.quarter, e.year, e.eps_estimate, e.eps_actual,
             e.revenue_estimate, e.revenue_actual, "finnhub", self.clock()))
        self.store.conn.commit()
        return e
