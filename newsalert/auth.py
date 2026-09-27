"""Dhan access-token management.

Dhan access tokens last 24 hours. A new one is generated with
POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=..&pin=..&totp=..
using a TOTP computed locally from the secret shown when TOTP is set up in Dhan.

The client ID, PIN, TOTP secret and access token are never logged: error messages
are built without the request URL (which carries the PIN and TOTP), and
`install_redaction` masks these values in any log record as a second line of defence.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .market import IST

log = logging.getLogger(__name__)


def totp(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (HMAC-SHA1), as used by authenticator apps."""
    s = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(s + "=" * (-len(s) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


class AuthError(Exception):
    pass


def token_from_jwt(jwt: str, client_id: str) -> "Token":
    """Wrap an access token pasted from Dhan Web. Reads `exp` and `dhanClientId` from the JWT
    payload (not verified; Dhan does that) so expiry is known without an API call."""
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        exp = datetime.fromtimestamp(int(claims["exp"]), IST)
    except (IndexError, ValueError, KeyError, TypeError):
        raise AuthError("DHAN_ACCESS_TOKEN is not a readable Dhan JWT") from None
    if str(claims.get("dhanClientId", client_id)) != str(client_id):
        raise AuthError("DHAN_ACCESS_TOKEN belongs to a different client ID than DHAN_CLIENT_ID")
    return Token(jwt, exp)


@dataclass
class Token:
    access_token: str
    expiry: datetime  # timezone-aware

    def valid_for(self, now: datetime) -> timedelta:
        return self.expiry - now


class _Redact(logging.Filter):
    def __init__(self, secrets: Callable[[], list[str]]):
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = msg
        for s in self._secrets():
            if s and len(s) >= 4:
                red = red.replace(s, "***")
        if red != msg:
            record.msg, record.args = red, None
        return True


class DhanAuth:
    def __init__(self, http: httpx.AsyncClient, client_id: str, pin: str, totp_secret: str,
                 cache_path: str | Path | None, *, auth_url: str = "https://auth.dhan.co/app/generateAccessToken",
                 refresh_margin: timedelta = timedelta(hours=2),
                 clock: Callable[[], float] = time.time, manual_token: str = "",
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.http, self.client_id, self._pin, self._secret = http, client_id, pin, totp_secret
        self.cache_path = Path(cache_path) if cache_path else None
        self.auth_url, self.refresh_margin, self.clock = auth_url, refresh_margin, clock
        self._sleep = sleep
        self.token: Token | None = self._load()
        self._last_totp = ""
        self._manual = manual_token
        self._warned_manual = False
        if manual_token:
            try:
                t = token_from_jwt(manual_token, client_id)
            except AuthError as e:
                if not self.can_generate:
                    raise
                # PIN + TOTP can mint a fresh token, so a stale or mismatched paste isn't fatal.
                log.warning("ignoring DHAN_ACCESS_TOKEN: %s", e)
                t = None
            if t is not None and (self.token is None or t.expiry > self.token.expiry):
                self.token = t

    # -- secrets hygiene ----------------------------------------------------
    def secret_values(self) -> list[str]:
        vals = [self.client_id, self._pin, self._secret, self._last_totp, self._manual]
        if self.token:
            vals.append(self.token.access_token)
        return vals

    def install_redaction(self, logger: logging.Logger | None = None) -> None:
        f = _Redact(self.secret_values)
        for h in (logger or logging.getLogger()).handlers:
            h.addFilter(f)

    # -- cache ----------------------------------------------------------------
    def _load(self) -> Token | None:
        if not self.cache_path or not self.cache_path.exists():
            return None
        try:
            d = json.loads(self.cache_path.read_text())
            if d.get("client_id") != self.client_id:
                return None
            return Token(d["access_token"], datetime.fromisoformat(d["expiry"]))
        except (ValueError, KeyError):
            return None

    def _save(self) -> None:
        if not self.cache_path or not self.token:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"client_id": self.client_id, "access_token": self.token.access_token,
                       "expiry": self.token.expiry.isoformat()}, f)
        os.replace(tmp, self.cache_path)

    # -- token lifecycle --------------------------------------------------------
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), IST)

    def needs_refresh(self) -> bool:
        return self.token is None or self.token.valid_for(self.now()) < self.refresh_margin

    @property
    def can_generate(self) -> bool:
        return bool(self._pin and self._secret)

    async def generate(self) -> Token:
        if not self.can_generate:
            raise AuthError("cannot generate a Dhan token: DHAN_PIN and DHAN_TOTP_SECRET are not set")
        # Dhan accepts only the current 30 s code. A code computed in the last few seconds of its
        # window can expire in flight ("Invalid TOTP"), so wait for a fresh window in that case.
        left = 30 - (self.clock() % 30)
        if left < 3:
            await self._sleep(left + 0.5)
        self._last_totp = totp(self._secret, self.clock())
        params = {"dhanClientId": self.client_id, "pin": self._pin, "totp": self._last_totp}
        try:
            resp = await self.http.post(self.auth_url, params=params)
        except httpx.HTTPError as e:
            raise AuthError(f"token request failed: {type(e).__name__}") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("accessToken"):
            # Never echo the URL (it carries the PIN and TOTP). Dhan's own reason fields are
            # included, with every credential value masked in case the body echoes one.
            raise AuthError(f"token request rejected: HTTP {resp.status_code}{self._reason(body)}")
        expiry = self._parse_expiry(body.get("expiryTime"))
        self.token = Token(body["accessToken"], expiry)
        self._save()
        log.info("Dhan access token refreshed; valid until %s", expiry.isoformat(timespec="minutes"))
        return self.token

    def _reason(self, body) -> str:
        if not isinstance(body, dict):
            return ""
        keys = ("status", "errorType", "errorCode", "errorMessage", "message", "remarks", "error")
        parts = [f"{k}={body[k]}" for k in keys if body.get(k) not in (None, "", {})]
        if not parts and body:
            parts = [f"fields={sorted(body)}"]
        text = "; ".join(str(p) for p in parts)[:300]
        for secret in self.secret_values():
            if secret and len(secret) >= 4:
                text = text.replace(secret, "***")
        return f" ({text})" if text else ""

    def _parse_expiry(self, s: str | None) -> datetime:
        if s:
            try:
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=IST)  # Dhan returns IST without offset
            except ValueError:
                pass
        return self.now() + timedelta(hours=24)

    async def ensure(self) -> str:
        """Return a usable access token, generating a new one if missing or near expiry.
        Without TOTP credentials, a pasted token is used until it expires."""
        if self.needs_refresh():
            if self.can_generate:
                await self.generate()
            elif self.token is not None and self.token.expiry > self.now():
                if not self._warned_manual:
                    self._warned_manual = True
                    log.warning("Dhan token expires %s and cannot be renewed automatically "
                                "(no DHAN_PIN/DHAN_TOTP_SECRET)", self.token.expiry.isoformat(timespec="minutes"))
            else:
                raise AuthError("Dhan access token expired or rejected; paste a new DHAN_ACCESS_TOKEN "
                                "or set DHAN_PIN and DHAN_TOTP_SECRET for automatic refresh")
        return self.token.access_token

    def reload_cache(self) -> bool:
        """Adopt a newer token another process (e.g. the 08:30 refresh job) wrote to the shared
        cache. Returns True if the in-memory token changed."""
        cached = self._load()
        if cached and (self.token is None or (cached.access_token != self.token.access_token
                                              and cached.expiry > self.token.expiry)):
            self.token = cached
            return True
        return False

    def invalidate(self) -> None:
        """Called when Dhan rejects the token (e.g. DH-901). If another process has cached a
        different, newer token, use that; otherwise the next ensure() regenerates."""
        rejected = self.token.access_token if self.token else None
        self.token = None
        cached = self._load()
        if cached and cached.access_token != rejected and cached.expiry > self.now():
            self.token = cached
