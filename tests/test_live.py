import json
from datetime import datetime, timedelta

import yaml

from newsalert.auth import DhanAuth
from newsalert.clients import DhanClient, Instrument, RssFeed
from newsalert.config import Ticker
from newsalert.live import LiveMonitor
from newsalert.market import IST, MarketCalendar
from newsalert.ratelimit import RateLimiter
from newsalert.signals import Engine, Params
from newsalert.store import Store

from fakes import CLIENT_ID, PIN, SECRET, FakeClock, FakeDhan

CAL = MarketCalendar.from_config(yaml.safe_load(open("config.yaml"))["market"])
NIFTY = Instrument("NIFTY50", "13", "IDX_I", "INDEX")
NSE_FEED = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_XML = b"""<rss><channel>
<item><title>Alpha Industries Limited</title><link>https://nsearchives.nseindia.com/corporate/ALPHA_01102026101500_order.pdf</link>
<description>Alpha Industries Limited has informed the Exchange about a large order win</description><pubDate>01-Oct-2026 10:15:00</pubDate></item>
</channel></rss>"""


def setup(start, *, params=None, lead=30):
    clock = FakeClock(start)
    fake = FakeDhan(clock)
    fake.prices = {("IDX_I", "13"): 25000.0, ("NSE_EQ", "101"): 100.0, ("NSE_EQ", "202"): 50.0}
    fake.rss[NSE_FEED] = NSE_XML
    http = fake.client()
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock)
    dhan = DhanClient(http, auth, RateLimiter([(1, 1.0)], clock=clock, sleep=clock.sleep),
                      RateLimiter([(5, 1.0)], clock=clock, sleep=clock.sleep))
    params = params or Params(warmup_returns=3, ma_enabled=False, corr_enabled=False, move_window_min=5)
    mon = LiveMonitor(
        tickers=[Ticker("ALPHA", "Alpha Industries Ltd.", "101"), Ticker("BETA", "Beta Ltd.", "202")],
        index=NIFTY, engine=Engine(params, "NIFTY50"), dhan=dhan, auth=auth,
        feeds=[RssFeed(http, "NSE announcements", NSE_FEED, clock=clock)],
        store=Store(":memory:"), calendar=CAL, cycle_s=60, token_refresh_lead_min=lead,
        clock=clock, sleep=clock.sleep)
    return clock, fake, http, mon


async def test_one_batched_ltp_request_covers_every_ticker():
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 10, 0, tzinfo=IST))
    async with http:
        s = await mon.run_cycle()
    assert len(fake.ltp_calls) == 1
    assert fake.ltp_calls[0] == {"IDX_I": [13], "NSE_EQ": [101, 202]}
    assert (s.ok, s.failed) == (3, 0)


async def test_alert_stored_with_reasons_missing_ticker_skipped_and_news_attached():
    params = Params(warmup_returns=3, ma_enabled=True, ma_fast_min=2, ma_slow_min=10, corr_enabled=False,
                    move_window_min=5)
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 10, 0, tzinfo=IST), params=params)
    async with http:
        for _ in range(8):
            await mon.run_cycle()
            clock.t += 60
        for px in (103.0, 105.0):                     # ALPHA climbs 5% over two cycles
            fake.prices[("NSE_EQ", "101")] = px
            fake.prices[("NSE_EQ", "202")] = 60.0     # BETA +20%, but missing from the reply
            fake.missing = {("NSE_EQ", "202")}
            s = await mon.run_cycle()
            clock.t += 60
        await mon.drain()
    assert s.failures == ["BETA"] and s.failed == 1
    assert mon.engine.series["BETA"].px[-1] == 50.0   # nothing fed for the missing ticker
    row = mon.store.conn.execute(
        "SELECT symbol, delivered, latency_ms, news, fast_sma, slow_sma, fast_move FROM alerts").fetchone()
    assert row[0] == "ALPHA" and row[1] == 1 and row[2] > 0
    news = json.loads(row[3])
    assert "order win" in news[0]["headline"] and set(news[0]) == {"source", "headline", "url", "published"}
    assert row[4] > row[5] and row[6] > 0              # why the MA filter passed is stored
    st = mon.store.get_status()
    assert st["cycle"]["value"]["missing"] == ["BETA"] and st["feeds"]["value"]["NSE announcements"]["ok"]


async def test_failed_request_skips_whole_cycle():
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 10, 0, tzinfo=IST))
    fake.fail_ltp = True
    async with http:
        s = await mon.run_cycle()
    assert (s.ok, s.failed) == (0, 3) and mon.engine.series == {}


async def test_rss_feed_cached_between_alerts():
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 10, 0, tzinfo=IST))
    feed = mon.feeds[0]
    async with http:
        await feed.items()
        clock.t += 120
        await feed.items()        # within 5-min TTL: no request
        clock.t += 400
        await feed.items()
    assert len(fake.rss_calls) == 2


async def test_scheduler_skips_holiday_weekend_and_refreshes_token_before_open():
    # Thursday 2026-10-01 after the close. Friday 10-02 is a holiday, then the weekend.
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 15, 31, tzinfo=IST))
    async with http:
        await mon.run(stop_after=datetime(2026, 10, 5, 9, 20, tzinfo=IST))
    ltp_times = [datetime.fromtimestamp(t, IST) for t in fake.ltp_at]
    # token generated once, 30 min before Monday's open, not on the holiday
    assert fake.token_n == 1
    assert fake.auth_calls and mon.auth.token.expiry > datetime(2026, 10, 5, 15, 30, tzinfo=IST)
    first = ltp_times[0]
    assert first == datetime(2026, 10, 5, 9, 15, tzinfo=IST)
    assert all(t.date() == first.date() and t.time() >= first.time() for t in ltp_times)
    assert len(ltp_times) == 5    # 09:15..09:19, one per 60 s cycle


async def test_scheduler_does_not_regenerate_a_token_that_outlives_the_session():
    clock, fake, http, mon = setup(datetime(2026, 10, 5, 8, 0, tzinfo=IST))
    async with http:
        await mon.auth.generate()     # made at 08:00, valid until 08:00 tomorrow
        await mon.run(stop_after=datetime(2026, 10, 5, 9, 16, tzinfo=IST))
    assert fake.token_n == 1


async def test_token_rejection_published_to_status():
    clock, fake, http, mon = setup(datetime(2026, 10, 1, 10, 0, tzinfo=IST))
    fake.reject_token_once = True
    async with http:
        await mon.run_cycle()
    st = mon.store.get_status()
    assert st["token"]["value"]["state"] == "error" and "token rejected" in st["token"]["value"]["error"]
    assert st["cycle"]["value"]["error"]
