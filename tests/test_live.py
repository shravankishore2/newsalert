import json

import httpx

from newsalert.clients import FinnhubClient, TelegramClient
from newsalert.config import Ticker
from newsalert.live import LiveMonitor
from newsalert.ratelimit import RateLimiter
from newsalert.signals import Engine, Params
from newsalert.store import Store

T0 = 1_700_000_000


class FakeMarket:
    """Serves Finnhub /quote and /company-news and Telegram sendMessage from memory."""

    def __init__(self):
        self.prices = {"SPY": 400.0, "AAA": 100.0, "BBB": 50.0}
        self.ts = T0
        self.fail = set()
        self.sent = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.telegram.org":
            body = json.loads(req.content)
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if req.url.path.endswith("/company-news"):
            return httpx.Response(200, json=[{"headline": "AAA wins contract", "url": "u", "datetime": 1}])
        sym = req.url.params["symbol"]
        if sym in self.fail:
            return httpx.Response(503)
        return httpx.Response(200, json={"c": self.prices[sym], "t": self.ts})


def make(market, store):
    http = httpx.AsyncClient(transport=httpx.MockTransport(market.handler))
    params = Params(warmup_returns=3, ma_enabled=False, corr_enabled=False, move_window_min=5,
                    max_ref_staleness_min=10)
    mon = LiveMonitor(
        tickers=[Ticker("AAA", "Alpha"), Ticker("BBB", "Beta")], index_symbol="SPY",
        engine=Engine(params, "SPY"),
        finnhub=FinnhubClient(http, "K", RateLimiter([(1000, 1.0)])),
        telegram=TelegramClient(http, "T", "C"), newsapi=None, store=store, workers=2)
    return http, mon


async def test_cycle_alerts_skips_failures_and_records_latency():
    m, store = FakeMarket(), Store(":memory:")
    http, mon = make(m, store)
    async with http:
        for i in range(8):  # one quote per minute, flat
            m.ts = T0 + 60 * i
            s = await mon.run_cycle()
            assert s.failed == 0 and s.alerts == 0
        # AAA jumps 5%, but BBB's fetch fails this cycle
        m.ts += 60
        m.prices["AAA"] = 105.0
        m.prices["BBB"] = 60.0
        m.fail = {"BBB"}
        s = await mon.run_cycle()
        assert (s.alerts, s.failed) == (1, 1)
        await mon.drain()

    assert "BBB" not in {a[0] for a in store.conn.execute("SELECT symbol FROM alerts")}
    # BBB's failed cycle fed nothing to the engine
    assert mon.engine.series["BBB"].px[-1] == 50.0
    row = store.conn.execute("SELECT symbol, delivered, latency_ms, news FROM alerts").fetchone()
    assert row[0] == "AAA" and row[1] == 1 and row[2] > 0
    assert json.loads(row[3])[0]["headline"] == "AAA wins contract"
    assert "AAA" in m.sent[0]["text"] and "+5.00%" in m.sent[0]["text"]
    assert m.sent[1]["reply_to_message_id"] == 1  # news follows as a reply


async def test_unchanged_quote_counts_as_stale_not_update():
    m, store = FakeMarket(), Store(":memory:")
    http, mon = make(m, store)
    async with http:
        await mon.run_cycle()
        s = await mon.run_cycle()  # same timestamps: market closed
    assert s.stale == 3 and s.ok == 3


async def test_telegram_failure_still_stores_undelivered_alert():
    m, store = FakeMarket(), Store(":memory:")
    http, mon = make(m, store)

    orig = m.handler

    def failing(req):
        if req.url.host == "api.telegram.org":
            return httpx.Response(502)
        return orig(req)

    m.handler = failing
    http, mon = make(m, store)
    async with http:
        for i in range(8):
            m.ts = T0 + 60 * i
            await mon.run_cycle()
        m.ts += 60
        m.prices["AAA"] = 105.0
        await mon.run_cycle()
        await mon.drain()
    assert store.conn.execute("SELECT delivered, sent_ns FROM alerts").fetchall() == [(0, None)]


def test_jobs_interleave_index():
    m = FakeMarket()
    _, mon = make(m, Store(":memory:"))
    mon.index_every = 1
    assert mon._jobs() == ["SPY", "AAA", "SPY", "BBB"]
