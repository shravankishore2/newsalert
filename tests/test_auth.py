import base64
import logging
import os
import stat
from datetime import datetime, timedelta

import httpx
import pytest

from newsalert.auth import AuthError, DhanAuth, totp
from newsalert.clients import DhanClient, Instrument, TokenRejected
from newsalert.market import IST
from newsalert.ratelimit import RateLimiter

from fakes import CLIENT_ID, PIN, SECRET, FakeClock, FakeDhan

T = datetime(2026, 9, 28, 8, 45, tzinfo=IST)


def test_totp_matches_rfc6238_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238 appendix B (SHA1), last 6 of the 8-digit values
    assert totp(secret, 59) == "287082"
    assert totp(secret, 1111111109) == "081804"
    assert totp(secret, 1234567890) == "005924"
    assert totp(secret.lower().rstrip("="), 59) == "287082"  # lowercase, unpadded secrets work


def make(tmp_path, fake, clock, **kw):
    http = fake.client()
    return http, DhanAuth(http, CLIENT_ID, PIN, SECRET, tmp_path / "tok.json", clock=clock, **kw)


async def test_generate_sends_totp_and_caches_token_privately(tmp_path):
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http, auth = make(tmp_path, fake, clock)
    async with http:
        tok = await auth.generate()
    assert tok.access_token == "tok-1"
    assert fake.auth_calls == [{"dhanClientId": CLIENT_ID, "pin": PIN, "totp": totp(SECRET, clock())}]
    assert tok.expiry == T + timedelta(hours=24) and tok.expiry.tzinfo is not None
    mode = stat.S_IMODE(os.stat(tmp_path / "tok.json").st_mode)
    assert mode == 0o600
    # a new process picks the cached token up without calling Dhan
    _, again = make(tmp_path, fake, clock)
    assert again.token.access_token == "tok-1" and not again.needs_refresh()


async def test_ensure_refreshes_only_near_expiry(tmp_path):
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http, auth = make(tmp_path, fake, clock, refresh_margin=timedelta(hours=2))
    async with http:
        await auth.ensure()
        clock.t += 21 * 3600          # 3h left: still fine
        assert await auth.ensure() == "tok-1"
        clock.t += 2 * 3600           # 1h left: inside the margin
        assert await auth.ensure() == "tok-2"
    assert fake.token_n == 2


async def test_cached_token_for_other_client_is_ignored(tmp_path):
    (tmp_path / "tok.json").write_text('{"client_id": "someone-else", "access_token": "x", "expiry": "2030-01-01T00:00:00+05:30"}')
    clock = FakeClock(T)
    _, auth = make(tmp_path, FakeDhan(clock), clock)
    assert auth.token is None


@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(401, json={"status": "error", "echo": str(req.url)}),
    lambda req: (_ for _ in ()).throw(httpx.ConnectError(f"cannot reach {req.url}", request=req)),
])
async def test_auth_errors_never_contain_secrets(tmp_path, handler):
    clock = FakeClock(T)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock)
    async with http:
        with pytest.raises(AuthError) as ei:
            await auth.generate()
    msg = str(ei.value) + repr(ei.value) + str(ei.value.__cause__ or "")
    for secret in (CLIENT_ID, PIN, SECRET, totp(SECRET, clock())):
        assert secret not in msg


async def test_secrets_redacted_from_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)  # includes httpx's "HTTP Request: POST <url with pin/totp>"
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http, auth = make(tmp_path, fake, clock)
    auth.install_redaction(logging.getLogger())
    async with http:
        await auth.generate()
        logging.getLogger("x").info("debug dump: %s %s", auth.token.access_token, SECRET)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP Request" in text  # httpx did log the URL...
    for secret in (CLIENT_ID, PIN, SECRET, totp(SECRET, clock()), "tok-1"):
        assert secret not in text  # ...but every secret in it was masked


async def test_rejected_token_is_regenerated_on_next_request(tmp_path):
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    fake.prices[("NSE_EQ", "2885")] = 2900.0
    http, auth = make(tmp_path, fake, clock)
    dhan = DhanClient(http, auth, RateLimiter([(100, 1.0)]), RateLimiter([(100, 1.0)]))
    ins = [Instrument("RELIANCE", "2885", "NSE_EQ", "EQUITY")]
    async with http:
        assert await dhan.ltp(ins) == {"RELIANCE": 2900.0}
        fake.reject_token_once = True          # e.g. token revoked by a login elsewhere
        with pytest.raises(TokenRejected):
            await dhan.ltp(ins)
        assert auth.token is None
        assert await dhan.ltp(ins) == {"RELIANCE": 2900.0}
    assert fake.token_n == 2
