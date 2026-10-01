import base64
from pathlib import Path
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


def _jwt(client_id, exp):
    import json as _j
    enc = lambda d: base64.urlsafe_b64encode(_j.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS512'})}.{enc({'dhanClientId': client_id, 'exp': exp})}.sig"


async def test_pasted_token_used_until_expiry_then_clear_error(tmp_path):
    from newsalert.auth import token_from_jwt
    clock = FakeClock(T)
    exp = int((T + timedelta(hours=10)).timestamp())
    jwt = _jwt(CLIENT_ID, exp)
    assert token_from_jwt(jwt, CLIENT_ID).expiry == T + timedelta(hours=10)
    fake = FakeDhan(clock)
    http = fake.client()
    auth = DhanAuth(http, CLIENT_ID, "", "", None, clock=clock, manual_token=jwt)
    assert not auth.can_generate
    async with http:
        assert await auth.ensure() == jwt
        clock.t += 9 * 3600                  # inside refresh margin, but can't refresh: keep using it
        assert await auth.ensure() == jwt
        clock.t += 2 * 3600                  # expired
        with pytest.raises(AuthError, match="DHAN_ACCESS_TOKEN") as ei:
            await auth.ensure()
    assert jwt not in str(ei.value) and fake.auth_calls == []


def test_pasted_token_for_other_client_rejected():
    from newsalert.auth import token_from_jwt
    with pytest.raises(AuthError, match="different client ID") as ei:
        token_from_jwt(_jwt("999", 2_000_000_000), CLIENT_ID)
    assert "999" not in str(ei.value)
    with pytest.raises(AuthError):
        token_from_jwt("not-a-jwt", CLIENT_ID)


def test_pasted_token_is_redacted(tmp_path, caplog):
    jwt = _jwt(CLIENT_ID, 2_000_000_000)
    auth = DhanAuth(None, CLIENT_ID, "", "", None, clock=FakeClock(T), manual_token=jwt)
    caplog.set_level(logging.INFO)
    auth.install_redaction(logging.getLogger())
    logging.getLogger("x").info("using %s", jwt)
    assert jwt not in caplog.text


async def test_rejection_reason_shown_but_secrets_masked(tmp_path):
    clock = FakeClock(T)
    code = totp(SECRET, clock())

    def h(req):
        return httpx.Response(200, json={"status": "error", "message": f"Invalid pin {PIN} or totp {code} for {CLIENT_ID}"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock)
    async with http:
        with pytest.raises(AuthError) as ei:
            await auth.generate()
    msg = str(ei.value)
    assert "Invalid pin" in msg and "status=error" in msg
    for secret in (CLIENT_ID, PIN, SECRET, code):
        assert secret not in msg


def test_mismatched_pasted_token_ignored_when_totp_available(caplog):
    other = _jwt("999", 2_000_000_000)
    with caplog.at_level(logging.WARNING):
        auth = DhanAuth(None, CLIENT_ID, PIN, SECRET, None, clock=FakeClock(T), manual_token=other)
    assert auth.token is None and auth.can_generate
    assert "ignoring DHAN_ACCESS_TOKEN" in caplog.text and other not in caplog.text
    with pytest.raises(AuthError):   # without TOTP credentials it is still an error
        DhanAuth(None, CLIENT_ID, "", "", None, clock=FakeClock(T), manual_token=other)


async def test_totp_not_sent_in_last_seconds_of_its_window(tmp_path):
    """A code computed at :28-:30 would expire in flight; generate() waits for the next window."""
    clock = FakeClock(datetime.fromtimestamp((int(T.timestamp()) // 30) * 30 + 28.5, IST))
    fake = FakeDhan(clock)
    http = fake.client()
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock, sleep=clock.sleep)
    async with http:
        await auth.generate()
    assert clock.sleeps == [2.0]                                  # 1.5 s left + 0.5 s margin
    assert fake.auth_calls[0]["totp"] == totp(SECRET, clock())    # code from the new window
    assert int(clock()) % 30 < 3


async def test_running_process_adopts_token_refreshed_by_another(tmp_path):
    """The 08:30 refresh job writes a new token to the shared cache; the monitor that has been
    running since boot picks it up at pre-open, and on rejection prefers it over regenerating."""
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http = fake.client()
    monitor = DhanAuth(http, CLIENT_ID, PIN, SECRET, tmp_path / "tok.json", clock=clock)
    job = DhanAuth(http, CLIENT_ID, PIN, SECRET, tmp_path / "tok.json", clock=clock)
    async with http:
        await monitor.generate()                       # tok-1, held in memory by the monitor
        clock.t += 3600
        await job.generate()                           # tok-2, written to the cache by the job
        assert monitor.token.access_token == "tok-1"
        assert monitor.reload_cache() and monitor.token.access_token == "tok-2"
        assert not monitor.reload_cache()              # nothing newer: no change
        monitor.token = type(monitor.token)("tok-1", monitor.token.expiry)   # pretend it still had tok-1
        monitor.invalidate()                           # tok-1 rejected -> use cached tok-2, no new call
        assert monitor.token.access_token == "tok-2" and fake.token_n == 2
        monitor.invalidate()                           # tok-2 rejected too -> nothing usable cached
        assert monitor.token is None


async def test_invalid_totp_retried_with_next_code(tmp_path):
    clock = FakeClock(T + timedelta(seconds=5))      # 5 s into a 30 s window
    replies = [{"status": "error", "message": "Invalid TOTP"}, {"status": "error", "message": "Invalid TOTP"}]
    codes = []

    def h(req):
        from urllib.parse import parse_qs
        codes.append(parse_qs(req.url.query.decode())["totp"][0])
        if replies:
            return httpx.Response(200, json=replies.pop(0))
        return httpx.Response(200, json={"accessToken": "tok-ok", "expiryTime": "2026-09-29T08:45:00"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    auth = DhanAuth(http, CLIENT_ID, PIN, SECRET, None, clock=clock, sleep=clock.sleep)
    async with http:
        tok = await auth.generate()
    assert tok.access_token == "tok-ok" and len(codes) == 3
    assert len(set(codes)) == 3                      # a new code each time, never a resend
    replies[:] = [{"status": "error", "message": "Invalid PIN"}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http2:
        auth2 = DhanAuth(http2, CLIENT_ID, PIN, SECRET, None, clock=clock, sleep=clock.sleep)
        with pytest.raises(AuthError, match="Invalid PIN"):
            await auth2.generate()                   # other errors are not retried


# -- one token source: only the VM generates --------------------------------------------------
async def test_generation_is_refused_off_the_token_authority(tmp_path, monkeypatch):
    """Generating a token kills the VM's; anywhere without DHAN_TOKEN_AUTHORITY=1 it refuses,
    whichever path asks (explicit generate or ensure() on a stale token)."""
    import newsalert.auth as A
    monkeypatch.delenv(A.TOKEN_AUTHORITY_ENV)
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http, auth = make(tmp_path, fake, clock)
    async with http:
        with pytest.raises(AuthError, match="i-know-this-kills-the-vm-token"):
            await auth.generate()
        with pytest.raises(AuthError, match="refusing to generate"):
            await auth.ensure()
    assert fake.auth_calls == []                       # Dhan's token endpoint was never called


async def test_override_flag_allows_it(tmp_path, monkeypatch):
    import newsalert.auth as A
    monkeypatch.delenv(A.TOKEN_AUTHORITY_ENV)
    A.allow_generation_here()
    clock = FakeClock(T)
    fake = FakeDhan(clock)
    http, auth = make(tmp_path, fake, clock)
    async with http:
        assert (await auth.generate()).access_token == "tok-1"


def test_cli_token_command_refuses_without_the_flag(tmp_path, monkeypatch, capsys):
    """`newsalert token --force` on a machine that isn't the token source fails before any
    network call (the conftest blocks sockets, so reaching Dhan would error differently)."""
    import newsalert.auth as A
    from newsalert.__main__ import main
    config = Path(__file__).resolve().parents[1] / "config.yaml"
    monkeypatch.delenv(A.TOKEN_AUTHORITY_ENV)
    for k, v in {"DHAN_CLIENT_ID": CLIENT_ID, "DHAN_PIN": PIN, "DHAN_TOTP_SECRET": SECRET,
                 "DHAN_ACCESS_TOKEN": ""}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)                        # token cache path resolves inside tmp
    rc = main(["--config", str(config), "token", "--force"])
    err = capsys.readouterr().err
    assert rc == 1 and "refusing to generate" in err and A.OVERRIDE_FLAG in err
    assert not (tmp_path / "data" / "dhan_token.json").exists()


def test_override_flag_is_accepted_anywhere_on_the_command_line(monkeypatch):
    import newsalert.__main__ as M
    import newsalert.auth as A
    seen = {}
    async def fake_cmd(args, cfg):
        seen["override"] = A._override
        return 0
    monkeypatch.setattr(M, "cmd_token", fake_cmd)
    config = Path(__file__).resolve().parents[1] / "config.yaml"
    M.main(["--config", str(config), "token", A.OVERRIDE_FLAG])
    assert seen["override"] is True
