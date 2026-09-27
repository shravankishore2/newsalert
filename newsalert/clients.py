"""Thin async API clients. Each takes an injected httpx.AsyncClient so tests can
swap in httpx.MockTransport and run offline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from .ratelimit import RateLimiter


class FetchError(Exception):
    """A fetch failed; the caller skips this ticker for this cycle."""


class RateLimited(FetchError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: int  # exchange timestamp of the last trade (epoch s)


def _retry_after(resp: httpx.Response, now: float | None = None, default: float = 60.0) -> float:
    """Seconds to back off after a 429. Retry-After is seconds; X-Ratelimit-Reset is epoch seconds."""
    try:
        if "Retry-After" in resp.headers:
            return max(1.0, float(resp.headers["Retry-After"]))
        if "X-Ratelimit-Reset" in resp.headers:
            now = time.time() if now is None else now
            return min(default, max(1.0, float(resp.headers["X-Ratelimit-Reset"]) - now))
    except ValueError:
        pass
    return default


class FinnhubClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, limiter: RateLimiter,
                 base_url: str = "https://finnhub.io/api/v1"):
        self.http, self.api_key, self.limiter, self.base_url = http, api_key, limiter, base_url

    async def _get(self, path: str, params: dict) -> object:
        await self.limiter.acquire()
        try:
            resp = await self.http.get(f"{self.base_url}{path}",
                                       params=params, headers={"X-Finnhub-Token": self.api_key})
        except httpx.HTTPError as e:
            raise FetchError(f"{path}: {e!r}") from e
        if resp.status_code == 429:
            wait = _retry_after(resp)
            self.limiter.pause(wait)
            raise RateLimited(wait)
        if resp.status_code != 200:
            raise FetchError(f"{path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise FetchError(f"{path}: bad JSON") from e

    async def quote(self, symbol: str) -> Quote:
        data = await self._get("/quote", {"symbol": symbol})
        # Finnhub returns zeros for unknown symbols instead of an error.
        if not isinstance(data, dict) or not data.get("c") or not data.get("t"):
            raise FetchError(f"quote {symbol}: empty response")
        return Quote(symbol, float(data["c"]), int(data["t"]))

    async def company_news(self, symbol: str, today: date, lookback_days: int, limit: int = 3) -> list[dict]:
        data = await self._get("/company-news", {
            "symbol": symbol, "from": (today - timedelta(days=lookback_days)).isoformat(),
            "to": today.isoformat()})
        if not isinstance(data, list):
            raise FetchError(f"company-news {symbol}: unexpected response")
        items = sorted(data, key=lambda x: x.get("datetime", 0), reverse=True)[:limit]
        return [{"source": "finnhub", "headline": x.get("headline", ""), "url": x.get("url", ""),
                 "published": x.get("datetime")} for x in items]


class NewsApiClient:
    """NewsAPI free plan: 100 requests/day, enforced here via a persisted daily counter."""

    def __init__(self, http: httpx.AsyncClient, api_key: str, store, daily_budget: int,
                 base_url: str = "https://newsapi.org/v2"):
        self.http, self.api_key, self.store = http, api_key, store
        self.daily_budget, self.base_url = daily_budget, base_url

    def budget_left(self, today: date) -> int:
        return self.daily_budget - self.store.usage("newsapi", today.isoformat())

    async def headlines(self, symbol: str, name: str, today: date, lookback_days: int,
                        limit: int = 3) -> list[dict]:
        if self.budget_left(today) <= 0:
            raise FetchError("newsapi daily budget exhausted")
        self.store.add_usage("newsapi", today.isoformat())
        q = f'"{name}"' if name else symbol
        try:
            resp = await self.http.get(f"{self.base_url}/everything", headers={"X-Api-Key": self.api_key},
                                       params={"q": q, "from": (today - timedelta(days=lookback_days)).isoformat(),
                                               "sortBy": "publishedAt", "language": "en", "pageSize": limit})
        except httpx.HTTPError as e:
            raise FetchError(f"newsapi: {e!r}") from e
        if resp.status_code != 200:
            raise FetchError(f"newsapi: HTTP {resp.status_code}")
        return [{"source": "newsapi", "headline": a.get("title", ""), "url": a.get("url", ""),
                 "published": a.get("publishedAt")} for a in resp.json().get("articles", [])[:limit]]


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, token: str, chat_id: str,
                 base_url: str = "https://api.telegram.org"):
        self.http, self.token, self.chat_id, self.base_url = http, token, chat_id, base_url

    async def send(self, text: str, reply_to: int | None = None) -> int:
        """Send a message; returns Telegram's message_id."""
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            resp = await self.http.post(f"{self.base_url}/bot{self.token}/sendMessage", json=payload)
        except httpx.HTTPError as e:
            raise FetchError(f"telegram: {e!r}") from e
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("ok"):
            raise FetchError(f"telegram: HTTP {resp.status_code} {body.get('description', '')}")
        return body["result"]["message_id"]
