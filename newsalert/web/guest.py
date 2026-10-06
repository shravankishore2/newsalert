"""Read-only guest view (/guest?k=<key>): live news alerts without third-party text or raw prices.

What a guest sees for each news alert is built from a whitelist (GUEST_NEWS_FIELDS, GUEST_STOCK_FIELDS):
event type, tickers, direction, strength, a reasoning line we write ourselves from those
structured fields, times, the source's name and, where the source's terms allow it, a link to the original. Never the headline,
the BusinessLine summary, Gemini's free-text reason (it paraphrases the headline closely) or
any Dhan price: moves are fractions (shown as %), computed on the server.

The key is the only secret. It lives in a file outside the repo (mode 600, written by
`python -m newsalert guest rotate`) and is re-read whenever the file changes, so rotating
takes effect at once; no file = no guest view (every /guest URL answers 404).

    python -m newsalert guest rotate   # new key; the old link stops working; prints the link
    python -m newsalert guest show     # print the current link
    python -m newsalert guest off      # delete the key
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_KEY_FILE = "~/.quantradar_guest_key"
DEFAULT_PUBLIC_URL = "https://quantradar.68-233-96-25.sslip.io"
BANNER = ("Read-only demo. No headline or filing text is shown: open the original at the source. "
          "Moves are shown as %, never prices.")
ROBOTS = "noindex, nofollow, noarchive"

SOURCE_NAME = {"nse": "NSE", "businessline": "BusinessLine"}
# Sources whose originals a public page may link to. BusinessLine's terms grant "a limited, revocable
# and non exclusive right to create hyperlinks to the home page or any other page". NSE's Hyperlinking
# Policy says "Prior written permission is required before hyperlinks are directed from any third-party
# website to this Website" (checked 2026-10-06), so NSE filings show the source name, no link, until
# NSE grants permission (request: nsewebmaster@nse.co.in).
LINKABLE_SOURCES = frozenset({"businessline"})
GUEST_NEWS_FIELDS = ("id", "created_at", "published_at", "latency_s", "source", "source_name", "url",
                     "event_type", "classifier", "confidence", "reasoning", "stocks", "linked_price_moves")
GUEST_STOCK_FIELDS = ("ticker", "name", "sector", "relation", "direction", "strength",
                      "move_since_alert", "ret_15m", "ret_1h", "ret_close", "abn_15m", "abn_1h", "abn_close")
GUEST_LINK_FIELDS = ("symbol", "direction", "move", "minutes_after")


# -- key -----------------------------------------------------------------------------------

class GuestKey:
    """The current key, re-read when the file's mtime or size changes."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path).expanduser() if path else None
        self._stamp: tuple[int, int] | None = None
        self._key: str | None = None
        self._lock = threading.Lock()

    def current(self) -> str | None:
        if self.path is None:
            return None
        try:
            st = self.path.stat()
        except OSError:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp != self._stamp:
                try:
                    self._key = self.path.read_text().strip() or None
                except OSError:
                    self._key = None
                self._stamp = stamp
            return self._key

    def matches(self, given: str | None) -> bool:
        key = self.current()
        return bool(key and given) and hmac.compare_digest(given.encode(), key.encode())

    def rotate(self) -> str:
        assert self.path is not None
        key = secrets.token_urlsafe(24)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
        os.replace(tmp, self.path)
        return key


def link(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/guest?k={key}"


# -- rate limits ---------------------------------------------------------------------------

class RateWindow:
    """At most `n` hits per `per` seconds for each client (sliding window)."""

    def __init__(self, n: int, per: float, clock: Callable[[], float] = time.monotonic):
        self.n, self.per, self.clock = n, per, clock
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, who: str) -> tuple[bool, float]:
        now = self.clock()
        with self.lock:
            q = self.hits.setdefault(who, deque())
            while q and now - q[0] >= self.per:
                q.popleft()
            if len(q) >= self.n:
                return False, self.per - (now - q[0])
            q.append(now)
            if len(self.hits) > 5000:                        # forget idle clients
                for k in [k for k, v in self.hits.items() if not v]:
                    del self.hits[k]
            return True, 0.0


class StreamSlots:
    """Caps open guest push streams, per client and in total (each holds a connection and a poll loop)."""

    def __init__(self, per_client: int = 3, total: int = 60):
        self.per_client, self.total = per_client, total
        self.open: dict[str, int] = {}
        self.lock = threading.Lock()

    def take(self, who: str) -> bool:
        with self.lock:
            if self.open.get(who, 0) >= self.per_client or sum(self.open.values()) >= self.total:
                return False
            self.open[who] = self.open.get(who, 0) + 1
            return True

    def give(self, who: str) -> None:
        with self.lock:
            n = self.open.get(who, 0) - 1
            if n > 0:
                self.open[who] = n
            else:
                self.open.pop(who, None)


# -- the sanitised alert -------------------------------------------------------------------

def _safe_url(url: str | None) -> str | None:
    try:
        u = urlsplit(url or "")
    except ValueError:
        return None
    return url if u.scheme in ("http", "https") and u.netloc else None


def _effect(s: dict) -> str:
    d = s.get("direction")
    if d not in ("up", "down"):
        return f"{s['ticker']} ({s['relation']}): no clear direction"
    return f"{s['ticker']} ({s['relation']}): {d}" + (f", {s['strength']} strength" if s.get("strength") else "")


def reasoning(n: dict, stocks: list[dict]) -> str:
    """The model's call in our own words, from structured fields only (never the headline or
    Gemini's free-text reason)."""
    if n["classifier"] == "rules":
        how = f"Exchange-filing rule for {n['event_type']}"
    else:
        conf = f", {round(n['confidence'] * 100)}% confident" if n.get("confidence") is not None else ""
        how = f"Gemini read the article as {n['event_type']}{conf}"
    if not stocks:
        return f"{how}. No listed stock flagged as materially affected."
    main = next((s for s in stocks if s["relation"] == "direct"), stocks[0])
    text = f"{how}. Expected short-term move: {_effect(main)}."
    others = [s for s in stocks if s is not main]
    if others:
        text += " Also flagged: " + "; ".join(_effect(s) for s in others) + "."
    return text


def _move_since(prices, symbol: str, t: float, now_ts: float) -> float | None:
    """Fractional move from the last price at or before the alert to the latest one."""
    try:
        px = prices(symbol, int(t) - 900, int(now_ts))
    except Exception:                                        # a missing table/symbol: just no number
        return None
    before = [p for ts, p in px if ts <= t]
    after = [p for ts, p in px if ts > t]
    if not before or not after or not before[-1]:
        return None
    return after[-1] / before[-1] - 1


def guest_news(n: dict, prices=None, now_ts: float | None = None) -> dict:
    """One news alert as `_news_json` builds it -> the guest whitelist."""
    stocks = []
    for s in n.get("stocks") or []:
        g = {k: s.get(k) for k in GUEST_STOCK_FIELDS}
        g["move_since_alert"] = (_move_since(prices, s["ticker"], n["created_at"], now_ts)
                                 if prices is not None and now_ts is not None and s.get("ret_close") is None
                                 and s["relation"] == "direct" else None)
        stocks.append(g)
    out = {
        "id": n["id"], "created_at": n["created_at"], "published_at": n.get("published_at"),
        "latency_s": n.get("latency_s"), "source": n["source"],
        "source_name": SOURCE_NAME.get(n["source"], n["source"]),
        "url": _safe_url(n.get("url")) if n["source"] in LINKABLE_SOURCES else None,
        "event_type": n["event_type"], "classifier": n["classifier"], "confidence": n.get("confidence"),
        "reasoning": reasoning(n, stocks), "stocks": stocks,
        "linked_price_moves": [{k: l.get(k) for k in GUEST_LINK_FIELDS} for l in n.get("linked_price_alerts") or []],
    }
    assert tuple(out) == GUEST_NEWS_FIELDS
    return out


def guest_status(status: dict) -> dict:
    """Market state and news-feed health, nothing about tokens, quotas or errors."""
    m = status.get("market") or {}
    nv = ((status.get("news") or {}).get("value")) or {}
    feeds = nv.get("feeds") or {}
    return {"server_time": status.get("server_time"),
            "market": {"open": bool(m.get("open")), "next_open": m.get("next_open"), "timezone": m.get("timezone")},
            "news": {"last_poll_at": nv.get("last_poll_at"), "alerts_today": nv.get("alerts_today"),
                     "feeds_ok": sum(1 for v in feeds.values() if v.get("ok")), "feeds": len(feeds)}}
