from datetime import date

import httpx
import pytest

from newsalert.clients import (FetchError, FinnhubClient, NewsApiClient, RateLimited,
                               TelegramClient, _retry_after)
from newsalert.ratelimit import RateLimiter
from newsalert.store import Store


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def limiter():
    return RateLimiter([(1000, 1.0)])


async def test_finnhub_quote_ok_and_token_in_header():
    seen = {}

    def h(req):
        seen["token"] = req.headers.get("X-Finnhub-Token")
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"c": 101.5, "t": 1700000000, "pc": 100})

    async with client(h) as http:
        q = await FinnhubClient(http, "KEY", limiter()).quote("AAPL")
    assert (q.symbol, q.price, q.ts) == ("AAPL", 101.5, 1700000000)
    assert seen["token"] == "KEY" and "token" not in seen["params"]


@pytest.mark.parametrize("resp", [
    httpx.Response(500),
    httpx.Response(200, json={"c": 0, "t": 0}),       # unknown symbol
    httpx.Response(200, text="not json"),
])
async def test_finnhub_quote_failures_raise(resp):
    async with client(lambda req: resp) as http:
        with pytest.raises(FetchError):
            await FinnhubClient(http, "K", limiter()).quote("ZZZZ")


async def test_finnhub_transport_error_raises_fetcherror():
    def h(req):
        raise httpx.ConnectTimeout("boom")
    async with client(h) as http:
        with pytest.raises(FetchError):
            await FinnhubClient(http, "K", limiter()).quote("AAPL")


async def test_finnhub_429_pauses_limiter():
    rl = limiter()
    async with client(lambda req: httpx.Response(429, headers={"Retry-After": "7"})) as http:
        with pytest.raises(RateLimited) as ei:
            await FinnhubClient(http, "K", rl).quote("AAPL")
    assert ei.value.retry_after == 7
    assert rl._paused_until > 0


def test_retry_after_parsing():
    assert _retry_after(httpx.Response(429, headers={"X-Ratelimit-Reset": "1030"}), now=1000) == 30
    assert _retry_after(httpx.Response(429)) == 60


async def test_company_news_sorted_and_limited():
    items = [{"headline": f"h{i}", "url": f"u{i}", "datetime": i} for i in range(5)]
    async with client(lambda req: httpx.Response(200, json=items)) as http:
        news = await FinnhubClient(http, "K", limiter()).company_news("AAPL", date(2026, 9, 25), 2)
    assert [n["headline"] for n in news] == ["h4", "h3", "h2"]


async def test_newsapi_daily_budget_enforced():
    store = Store(":memory:")
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, json={"articles": [{"title": "t", "url": "u", "publishedAt": "x"}]})

    async with client(h) as http:
        na = NewsApiClient(http, "K", store, daily_budget=2)
        d = date(2026, 9, 25)
        await na.headlines("AAPL", "Apple Inc.", d, 3)
        await na.headlines("AAPL", "Apple Inc.", d, 3)
        with pytest.raises(FetchError):
            await na.headlines("AAPL", "Apple Inc.", d, 3)
        assert na.budget_left(date(2026, 9, 26)) == 2  # resets per day
    assert len(calls) == 2
    assert calls[0].headers["X-Api-Key"] == "K"


async def test_telegram_send_and_error():
    def ok(req):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    async with client(ok) as http:
        assert await TelegramClient(http, "T", "C").send("hi") == 42

    def bad(req):
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})
    async with client(bad) as http:
        with pytest.raises(FetchError, match="chat not found"):
            await TelegramClient(http, "T", "C").send("hi")
