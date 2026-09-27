"""Dhan access-token management.

Dhan access tokens last 24 hours. A new one is generated with
POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=..&pin=..&totp=..
using a TOTP computed locally from the secret shown when TOTP is set up in Dhan.

The client ID, PIN, TOTP secret and access token are never logged: error messages
are built without the request URL (which carries the PIN and TOTP), and
`install_redaction` masks these values in any log record as a second line of defence.
"""

from __future__ import annotations

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
from typing import Callable

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
                 clock: Callable[[], float] = time.time):
        self.http, self.client_id, self._pin, self._secret = http, client_id, pin, totp_secret
        self.cache_path = Path(cache_path) if cache_path else None
        self.auth_url, self.refresh_margin, self.clock = auth_url, refresh_margin, clock
        self.token: Token | None = self._load()
        self._last_totp = ""

    # -- secrets hygiene ----------------------------------------------------
    def secret_values(self) -> list[str]:
        vals = [self.client_id, self._pin, self._secret, self._last_totp]
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

    async def generate(self) -> Token:
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
            # Deliberately omit the URL and body: they can contain the PIN, TOTP or token.
            raise AuthError(f"token request rejected: HTTP {resp.status_code}")
        expiry = self._parse_expiry(body.get("expiryTime"))
        self.token = Token(body["accessToken"], expiry)
        self._save()
        log.info("Dhan access token refreshed; valid until %s", expiry.isoformat(timespec="minutes"))
        return self.token

    def _parse_expiry(self, s: str | None) -> datetime:
        if s:
            try:
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=IST)  # Dhan returns IST without offset
            except ValueError:
                pass
        return self.now() + timedelta(hours=24)

    async def ensure(self) -> str:
        """Return a usable access token, generating a new one if missing or near expiry."""
        if self.needs_refresh():
            await self.generate()
        return self.token.access_token

    def invalidate(self) -> None:
        """Called when Dhan rejects the token (e.g. DH-901); next ensure() regenerates."""
        self.token = None
