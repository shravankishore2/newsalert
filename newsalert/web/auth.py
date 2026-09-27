"""Single-user password login with an HMAC-signed session cookie.

The password comes from DASHBOARD_PASSWORD. Sessions are signed with a per-process random
key unless DASHBOARD_SECRET is set, so restarting the server logs everyone out.
Failed logins are throttled.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable

COOKIE = "newsalert_session"


class SessionSigner:
    def __init__(self, key: bytes | None = None, ttl_s: float = 12 * 3600,
                 clock: Callable[[], float] = time.time):
        self.key = key or secrets.token_bytes(32)
        self.ttl_s, self.clock = ttl_s, clock

    def _mac(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.key, payload, hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(
            {"exp": self.clock() + self.ttl_s, "n": secrets.token_hex(8)}).encode()).decode().rstrip("=")
        return f"{payload}.{self._mac(payload.encode())}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie or cookie.count(".") != 1:
            return False
        payload, mac = cookie.split(".")
        if not hmac.compare_digest(mac, self._mac(payload.encode())):
            return False
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return float(data["exp"]) > self.clock()
        except (ValueError, KeyError, TypeError):
            return False


class PasswordCheck:
    """Constant-time password comparison with a lockout after repeated failures."""

    def __init__(self, password: str, max_failures: int = 5, window_s: float = 300,
                 lockout_s: float = 60, clock: Callable[[], float] = time.time):
        if not password:
            raise ValueError("DASHBOARD_PASSWORD is empty")
        self._digest = hashlib.sha256(password.encode()).digest()
        self.max_failures, self.window_s, self.lockout_s, self.clock = max_failures, window_s, lockout_s, clock
        self._failures: list[float] = []
        self._locked_until = 0.0

    def locked_for(self) -> float:
        return max(0.0, self._locked_until - self.clock())

    def check(self, attempt: str) -> bool:
        if self.locked_for() > 0:
            return False
        ok = hmac.compare_digest(hashlib.sha256(attempt.encode()).digest(), self._digest)
        now = self.clock()
        if ok:
            self._failures.clear()
            return True
        self._failures = [t for t in self._failures if now - t < self.window_s] + [now]
        if len(self._failures) >= self.max_failures:
            self._locked_until = now + self.lockout_s
            self._failures.clear()
        return False
