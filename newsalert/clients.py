"""Thin async API clients. Each takes an injected httpx.AsyncClient so tests can
swap in httpx.MockTransport and run offline."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from .auth import DhanAuth
from .market import IST
from .ratelimit import RateLimiter


class FetchError(Exception):
    """A fetch failed; the caller skips the affected tickers for this cycle."""


class RateLimited(FetchError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class TokenRejected(FetchError):
    """Dhan says the access token is invalid or expired."""


# Dhan error codes: DH-901 invalid/expired token; 807/809 token expired/invalid on data
# APIs; DH-904 too many requests.
_TOKEN_CODES = {"DH-901", "807", "809"}
_RATE_CODES = {"DH-904", "805"}


def _dhan_error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    code = body.get("errorCode")  # trading-style errors: {"errorCode": "DH-901", ...}
    remarks = body.get("remarks")
    if not code and isinstance(remarks, dict):  # data-API errors: {"remarks": {"error_code": "807"}}
        code = remarks.get("error_code")
    return str(code) if code else None


@dataclass(frozen=True)
class Instrument:
    symbol: str
    security_id: str
    segment: str       # NSE_EQ | IDX_I
    instrument: str    # EQUITY | INDEX


class DhanClient:
    """Dhan market data. Quote APIs (LTP): 1 request/s, up to 1000 instruments per request.
    Data APIs (historical): 5 requests/s, 100,000 per day."""

    MAX_LTP_INSTRUMENTS = 1000

    def __init__(self, http: httpx.AsyncClient, auth: DhanAuth, quote_limiter: RateLimiter,
                 data_limiter: RateLimiter, base_url: str = "https://api.dhan.co/v2"):
        self.http, self.auth = http, auth
        self.quote_limiter, self.data_limiter, self.base_url = quote_limiter, data_limiter, base_url

    async def _post(self, path: str, body: dict, limiter: RateLimiter) -> dict:
        token = await self.auth.ensure()
        await limiter.acquire()
        try:
            resp = await self.http.post(f"{self.base_url}{path}", json=body, headers={
                "access-token": token, "client-id": self.auth.client_id,
                "Accept": "application/json", "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            raise FetchError(f"{path}: {type(e).__name__}") from None
        code = _dhan_error_code(resp) if resp.status_code != 200 else None
        if resp.status_code == 429 or code in _RATE_CODES:
            wait = float(resp.headers.get("Retry-After", 5) or 5)
            limiter.pause(wait)
            raise RateLimited(wait)
        if resp.status_code in (401, 403) or code in _TOKEN_CODES:
            self.auth.invalidate()
            raise TokenRejected(f"{path}: token rejected ({code or resp.status_code})")
        if resp.status_code != 200:
            raise FetchError(f"{path}: HTTP {resp.status_code} {code or ''}".strip())
        try:
            data = resp.json()
        except ValueError:
            raise FetchError(f"{path}: bad JSON") from None
        if not isinstance(data, dict):
            raise FetchError(f"{path}: unexpected response")
        return data

    async def ltp(self, instruments: list[Instrument]) -> dict[str, float]:
        """Last traded price for every instrument, batched. Returns {symbol: price}; symbols
        missing from Dhan's reply are simply absent (caller skips them this cycle)."""
        out: dict[str, float] = {}
        for i in range(0, len(instruments), self.MAX_LTP_INSTRUMENTS):
            chunk = instruments[i:i + self.MAX_LTP_INSTRUMENTS]
            body: dict[str, list[int]] = {}
            by_key = {}
            for ins in chunk:
                body.setdefault(ins.segment, []).append(int(ins.security_id))
                by_key[(ins.segment, str(ins.security_id))] = ins.symbol
            data = (await self._post("/marketfeed/ltp", body, self.quote_limiter)).get("data") or {}
            for seg, rows in data.items():
                if not isinstance(rows, dict):
                    continue
                for sid, row in rows.items():
                    sym = by_key.get((seg, str(sid)))
                    px = row.get("last_price") if isinstance(row, dict) else None
                    if sym and isinstance(px, (int, float)) and px > 0:
                        out[sym] = float(px)
        return out

    async def intraday(self, ins: Instrument, start: datetime, end: datetime, interval: int = 1) -> list[tuple[int, float]]:
        """1-minute (or other interval) closes between start and end (at most 90 days apart)."""
        fmt = "%Y-%m-%d %H:%M:%S"
        data = await self._post("/charts/intraday", {
            "securityId": str(ins.security_id), "exchangeSegment": ins.segment,
            "instrument": ins.instrument, "interval": str(interval), "oi": False,
            "fromDate": start.astimezone(IST).strftime(fmt), "toDate": end.astimezone(IST).strftime(fmt),
        }, self.data_limiter)
        ts, close = data.get("timestamp") or [], data.get("close") or []
        if len(ts) != len(close):
            raise FetchError(f"intraday {ins.symbol}: timestamp/close length mismatch")
        return [(int(t), float(c)) for t, c in zip(ts, close) if c is not None]


# --- news --------------------------------------------------------------------

_SUFFIX = re.compile(r"\b(ltd|limited|inc|corp|corporation|co|company|india|\(india\))\.?$", re.I)


def short_name(name: str) -> str:
    """'Reliance Industries Ltd.' -> 'Reliance Industries' (used to match headlines)."""
    n = name.strip()
    for _ in range(3):
        n2 = _SUFFIX.sub("", n).strip(" .,")
        if n2 == n or not n2:
            break
        n = n2
    return n


@dataclass(frozen=True)
class NewsItem:
    source: str
    headline: str
    url: str
    published: str
    symbol_hint: str = ""  # NSE feed: symbol embedded in the link


class RssFeed:
    """Polite RSS reader: fetched only when an alert needs it, cached for `ttl_s`."""

    def __init__(self, http: httpx.AsyncClient, name: str, url: str, ttl_s: float = 300,
                 user_agent: str = "newsalert/0.2 (personal, non-commercial)",
                 clock=time.monotonic):
        self.http, self.name, self.url, self.ttl_s, self.ua, self.clock = http, name, url, ttl_s, user_agent, clock
        self._items: list[NewsItem] = []
        self._fetched_at: float | None = None

    async def items(self) -> list[NewsItem]:
        now = self.clock()
        if self._fetched_at is not None and now - self._fetched_at < self.ttl_s:
            return self._items
        try:
            resp = await self.http.get(self.url, headers={"User-Agent": self.ua}, follow_redirects=True)
        except httpx.HTTPError as e:
            raise FetchError(f"{self.name}: {type(e).__name__}") from None
        if resp.status_code != 200:
            raise FetchError(f"{self.name}: HTTP {resp.status_code}")
        self._items = self.parse(resp.content)
        self._fetched_at = now
        return self._items

    def parse(self, content: bytes) -> list[NewsItem]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            raise FetchError(f"{self.name}: bad XML") from None
        out = []
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            m = re.search(r"/corporate/([A-Z0-9&\-]+)_", link)
            out.append(NewsItem(self.name, (it.findtext("title") or "").strip() + (
                f" — {(it.findtext('description') or '').strip()}" if m else ""),
                link, _norm_date(it.findtext("pubDate") or ""), m.group(1) if m else ""))
        return out


def _norm_date(s: str) -> str:
    s = s.strip()
    for parse in (lambda x: datetime.strptime(x, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST),  # NSE
                  parsedate_to_datetime):                                                # RFC 822
        try:
            return parse(s).isoformat()
        except (ValueError, TypeError):
            continue
    return s


def match_news(items: list[NewsItem], symbol: str, name: str, limit: int = 3) -> list[NewsItem]:
    """NSE items match on the symbol in the link; other feeds on the company's short name."""
    sn = short_name(name)
    pat = re.compile(rf"\b{re.escape(sn)}\b", re.I) if len(sn) >= 3 else None
    hits = [i for i in items if (i.symbol_hint == symbol) or
            (not i.symbol_hint and pat is not None and pat.search(i.headline))]
    return sorted(hits, key=lambda i: i.published, reverse=True)[:limit]


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, token: str, chat_id: str,
                 base_url: str = "https://api.telegram.org"):
        self.http, self.token, self.chat_id, self.base_url = http, token, chat_id, base_url

    async def send(self, text: str, reply_to: int | None = None) -> int:
        """Send a message; returns Telegram's message_id."""
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            resp = await self.http.post(f"{self.base_url}/bot{self.token}/sendMessage", json=payload)
        except httpx.HTTPError as e:
            raise FetchError(f"telegram: {type(e).__name__}") from None
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("ok"):
            raise FetchError(f"telegram: HTTP {resp.status_code} {body.get('description', '')}")
        return body["result"]["message_id"]
