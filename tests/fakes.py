"""Shared offline fakes: a fake clock and an in-memory Dhan/Telegram/RSS server."""

import json
from datetime import datetime
from urllib.parse import parse_qs

import httpx

from newsalert.auth import totp
from newsalert.market import IST

CLIENT_ID, PIN, SECRET = "1100001234", "246810", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


class FakeClock:
    """Wall clock (epoch seconds) that only moves when someone sleeps."""

    def __init__(self, start: datetime):
        self.t = start.timestamp()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += max(s, 0)


class FakeDhan:
    """Serves Dhan auth, LTP, intraday, Telegram and RSS from memory, and records calls."""

    def __init__(self, clock=None):
        self.clock = clock
        self.prices: dict[tuple[str, str], float] = {}
        self.missing: set[tuple[str, str]] = set()
        self.fail_ltp = False
        self.reject_token_once = False
        self.token_n = 0
        self.ltp_calls: list[dict] = []
        self.ltp_at: list[float] = []   # clock time of each LTP request
        self.auth_calls: list[dict] = []
        self.sent: list[dict] = []
        self.intraday: dict[str, dict] = {}
        self.intraday_calls: list[dict] = []
        self.rss: dict[str, bytes] = {}
        self.rss_calls: list[str] = []

    def token(self) -> str:
        return f"tok-{self.token_n}"

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.url.host == "auth.dhan.co":
            q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
            self.auth_calls.append(q)
            now = self.clock() if self.clock else None
            if q.get("dhanClientId") != CLIENT_ID or q.get("pin") != PIN or q.get("totp") != totp(SECRET, now):
                return httpx.Response(401, json={"status": "error"})
            self.token_n += 1
            exp = datetime.fromtimestamp((now or 0) + 86400, IST).replace(tzinfo=None)
            return httpx.Response(200, json={"dhanClientId": CLIENT_ID, "accessToken": self.token(),
                                             "expiryTime": exp.isoformat(timespec="milliseconds")})
        if req.url.host == "api.telegram.org":
            self.sent.append(json.loads(req.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if url in self.rss:
            self.rss_calls.append(url)
            return httpx.Response(200, content=self.rss[url], headers={"content-type": "application/xml"})
        if req.url.host == "api.dhan.co":
            if req.headers.get("access-token") != self.token() or req.headers.get("client-id") != CLIENT_ID:
                return httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "invalid token"})
            if self.reject_token_once:
                self.reject_token_once = False
                return httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "expired"})
            body = json.loads(req.content)
            if req.url.path.endswith("/marketfeed/ltp"):
                self.ltp_calls.append(body)
                self.ltp_at.append(self.clock() if self.clock else 0.0)
                if self.fail_ltp:
                    return httpx.Response(500)
                data = {}
                for seg, ids in body.items():
                    for sid in ids:
                        k = (seg, str(sid))
                        if k in self.prices and k not in self.missing:
                            data.setdefault(seg, {})[str(sid)] = {"last_price": self.prices[k]}
                return httpx.Response(200, json={"data": data, "status": "success"})
            if req.url.path.endswith("/charts/intraday"):
                self.intraday_calls.append(body)
                return httpx.Response(200, json=self.intraday.get(body["securityId"], {"timestamp": [], "close": []}))
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
