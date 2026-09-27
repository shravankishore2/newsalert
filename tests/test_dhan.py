from datetime import datetime, time

import httpx
import pytest

from newsalert.auth import DhanAuth
from newsalert.clients import DhanClient, FetchError, Instrument, RateLimited
from newsalert.history import chunks, fetch_history, session_fraction
from newsalert.market import IST
from newsalert.ratelimit import RateLimiter
from newsalert.store import Store

from fakes import CLIENT_ID, PIN, SECRET, FakeClock, FakeDhan

T = datetime(2026, 9, 28, 10, 0, tzinfo=IST)


def make(fake, clock, handler=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or fake.handler))
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock)
    quote = RateLimiter([(1, 1.0)], clock=clock, sleep=clock.sleep)
    data = RateLimiter([(4, 1.0)], clock=clock, sleep=clock.sleep)
    return http, DhanClient(http, auth, quote, data)


async def test_ltp_batches_at_most_1000_and_respects_1_per_second():
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    ins = [Instrument(f"S{i}", str(1000 + i), "NSE_EQ", "EQUITY") for i in range(1500)]
    ins.insert(0, Instrument("NIFTY50", "13", "IDX_I", "INDEX"))
    fake.prices = {(x.segment, x.security_id): 10.0 + i for i, x in enumerate(ins)}
    http, dhan = make(fake, clock)
    async with http:
        prices = await dhan.ltp(ins)
    assert len(fake.ltp_calls) == 2
    assert [sum(len(v) for v in c.values()) for c in fake.ltp_calls] == [1000, 501]
    assert fake.ltp_calls[0]["IDX_I"] == [13]
    assert fake.ltp_at[1] - fake.ltp_at[0] >= 1.0      # second batch waited for the 1/s limit
    assert len(prices) == 1501 and prices["NIFTY50"] == 10.0


@pytest.mark.parametrize("resp", [
    httpx.Response(429),
    httpx.Response(400, json={"errorCode": "DH-904", "errorMessage": "Too many requests"}),
])
async def test_rate_limit_responses_pause_limiter(resp):
    clock = FakeClock(T)
    fake = FakeDhan(clock)

    def h(req):
        return fake.handler(req) if req.url.host == "auth.dhan.co" else resp

    http, dhan = make(fake, clock, h)
    async with http:
        with pytest.raises(RateLimited):
            await dhan.ltp([Instrument("A", "1", "NSE_EQ", "EQUITY")])
    assert dhan.quote_limiter._paused_until > clock()


async def test_other_errors_raise_fetcherror():
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    fake.fail_ltp = True
    http, dhan = make(fake, clock)
    async with http:
        with pytest.raises(FetchError):
            await dhan.ltp([Instrument("A", "1", "NSE_EQ", "EQUITY")])


async def test_intraday_request_format_and_parse():
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    t0 = int(datetime(2026, 9, 25, 9, 15, tzinfo=IST).timestamp())
    fake.intraday["2885"] = {"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [2900.5, 2901.0],
                             "volume": [10, 20], "timestamp": [t0, t0 + 60]}
    http, dhan = make(fake, clock)
    async with http:
        bars = await dhan.intraday(Instrument("RELIANCE", "2885", "NSE_EQ", "EQUITY"),
                                   datetime(2026, 9, 25, 9, 15, tzinfo=IST), datetime(2026, 9, 25, 15, 30, tzinfo=IST))
    assert bars == [(t0, 2900.5), (t0 + 60, 2901.0)]
    assert fake.intraday_calls == [{"securityId": "2885", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                                    "interval": "1", "oi": False, "fromDate": "2026-09-25 09:15:00",
                                    "toDate": "2026-09-25 15:30:00"}]


def test_history_chunks_never_exceed_90_days():
    s, e = datetime(2026, 1, 1, tzinfo=IST), datetime(2026, 7, 20, tzinfo=IST)
    cs = chunks(s, e)
    assert cs[0][0] == s and cs[-1][1] == e and len(cs) == 3
    assert all((b - a).days <= 90 for a, b in cs)
    assert all(cs[i][1] == cs[i + 1][0] for i in range(len(cs) - 1))


def test_session_fraction_flags_shifted_timestamps():
    day = datetime(2026, 9, 25, 9, 15, tzinfo=IST)
    good = [int(day.timestamp()) + 60 * i for i in range(375)]
    assert session_fraction(good, time(9, 15), time(15, 30)) == 1.0
    shifted = [t + 19800 for t in good]  # IST wall time mistakenly encoded as UTC
    assert session_fraction(shifted, time(9, 15), time(15, 30)) < 0.2


async def test_fetch_history_stores_bars_and_reports_problems():
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    day = int(datetime(2026, 9, 25, 9, 15, tzinfo=IST).timestamp())
    fake.intraday["13"] = {"timestamp": [day, day + 60], "close": [25000.0, 25010.0]}
    noon = int(datetime(2026, 9, 25, 12, 0, tzinfo=IST).timestamp())
    # IST wall time encoded as UTC: 12:00 IST reads as 17:30 IST, after the close
    fake.intraday["2885"] = {"timestamp": [noon + 19800, noon + 19860], "close": [1.0, 2.0]}
    ins = [Instrument("NIFTY50", "13", "IDX_I", "INDEX"), Instrument("RELIANCE", "2885", "NSE_EQ", "EQUITY"),
           Instrument("EMPTY", "9", "NSE_EQ", "EQUITY")]
    store = Store(":memory:")
    http, dhan = make(fake, clock)
    async with http:
        rep = await fetch_history(store, dhan, ins, days=120, now=T)
    assert rep["bars"] == 4 and rep["empty"] == ["EMPTY"] and list(rep["out_of_session"]) == ["RELIANCE"]
    assert len(fake.intraday_calls) == 3 * 2  # 120 days -> two <=90-day windows per instrument
    assert store.bar_summary("bars")[:2] == (4, 2)


@pytest.mark.parametrize("resp", [
    httpx.Response(400, json={"errorType": "Order_Error", "errorCode": "DH-906", "errorMessage": "Invalid Token"}),
    httpx.Response(401, json={"data": {"808": "Authentication Failed - Client ID or Token invalid"}, "status": "failed"}),
    httpx.Response(400, json={"status": "failed", "remarks": {"error_code": "807"}}),
])
async def test_token_errors_as_dhan_actually_sends_them(resp):
    """Bodies as returned by Dhan for a bad token (DH-906 variant captured 2026-09-27)."""
    from newsalert.clients import TokenRejected
    clock = FakeClock(T)
    fake = FakeDhan(clock)

    def h(req):
        return fake.handler(req) if req.url.host == "auth.dhan.co" else resp

    http, dhan = make(fake, clock, h)
    async with http:
        with pytest.raises(TokenRejected):
            await dhan.ltp([Instrument("A", "1", "NSE_EQ", "EQUITY")])
    assert dhan.auth.token is None
