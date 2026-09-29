"""News ingest -> classify -> alert pipeline (runs 24/7 as its own service).

- Polls each RSS feed with conditional GET (ETag / If-Modified-Since), so an unchanged feed
  costs one small 304. NSE's announcements feed empties at midnight IST, so the archive
  only exists because we poll through the day.
- Stores every item once (keyed by guid/link) with published and fetched times.
  NSE: never stores NSE's text; the rule classifier runs in memory and only derived
  fields (symbol, event type, label, link, times) are kept.
  BusinessLine: headline + RSS summary (never article text).
- Items older than `max_age_min` when first seen are archived but not alerted or sent to
  Gemini (a first poll would otherwise flood alerts with stale news and burn quota).
- A classified item becomes a news alert if its event type isn't "other" and at least one
  affected stock is in tickers.csv.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable

import httpx

from ..market import IST
from ..store import Store
from .gemini import GeminiClassifier, GeminiError, ItemIn, QuotaExhausted
from .models import Classification, EventType
from .rules import classify_nse

log = logging.getLogger(__name__)

UA = "newsalert/0.3 (personal, non-commercial)"
RULE_CONFIDENCE = 0.5   # rule-based classifications carry a fixed, stated confidence
NSE_DUP_WINDOW_S = 1800  # NSE often posts the same announcement twice (PDF + XBRL data file)


def company_key(name: str) -> str:
    """Normalised company name for exact matching: 'Adani Ports & SEZ Limited' ~ 'Adani Ports & SEZ Ltd.'."""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(limited|ltd|the)\b", " ", s)
    return " ".join(s.split())


@dataclass
class Feed:
    name: str
    source: str          # "nse" | "businessline"
    url: str
    etag: str | None = None
    last_modified: str | None = None
    health: dict = field(default_factory=dict)


def _ts(pub: str) -> float | None:
    s = (pub or "").strip()
    try:
        return datetime.strptime(s, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST).timestamp()   # NSE (IST)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).timestamp()                                          # RFC 822
    except (TypeError, ValueError):
        return None


def _clean(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()
    return t[:limit] if t else None


class NewsService:
    def __init__(self, *, http: httpx.AsyncClient, store: Store, feeds: list[Feed], tickers: dict[str, str],
                 gemini: GeminiClassifier | None, poll_s: float = 180, max_age_min: float = 120,
                 expectations=None,
                 min_confidence: float = 0.0, mode: str = "live",
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.http, self.store, self.feeds, self.tickers = http, store, feeds, tickers
        self.gemini, self.poll_s, self.max_age_s = gemini, poll_s, max_age_min * 60
        self.min_confidence, self.mode, self.clock, self.sleep = min_confidence, mode, clock, sleep
        self.last_error: str | None = None
        # NSE item titles are the company name; used when the link carries no symbol (XBRL, debt, odd filenames)
        self._by_name = {company_key(n): sym for sym, n in tickers.items() if n}
        self.last_poll_at: float | None = None
        self.expectations = expectations          # optional FinnhubEarnings
        self._results_queue: list[tuple[str, float]] = []

    # -- ingest -------------------------------------------------------------------------------
    async def poll_feed(self, feed: Feed) -> int:
        headers = {"User-Agent": UA}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified
        now = self.clock()
        try:
            resp = await self.http.get(feed.url, headers=headers, follow_redirects=True)
        except httpx.HTTPError as e:
            feed.health.update(ok=False, error=type(e).__name__, checked_at=now)
            return 0
        if resp.status_code == 304:
            feed.health.update(ok=True, error=None, checked_at=now, last_ok_at=now, new=0)
            return 0
        if resp.status_code != 200:
            feed.health.update(ok=False, error=f"HTTP {resp.status_code}", checked_at=now)
            return 0
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            feed.health.update(ok=False, error="bad XML", checked_at=now)
            return 0
        feed.etag, feed.last_modified = resp.headers.get("etag"), resp.headers.get("last-modified")
        new = 0
        items = root.findall(".//item")
        for it in items:
            new += self._store_item(feed, it, now)
        self.store.conn.commit()
        feed.health.update(ok=True, error=None, checked_at=now, last_ok_at=now, items=len(items), new=new)
        return new

    def _store_item(self, feed: Feed, it: ET.Element, now: float) -> int:
        link = (it.findtext("link") or "").strip()
        key = (it.findtext("guid") or "").strip() or link
        if not key:
            return 0
        if self.store.conn.execute("SELECT 1 FROM news_items WHERE key=?", (key,)).fetchone():
            return 0
        published = _ts(it.findtext("pubDate") or "")
        stale = published is not None and now - published > self.max_age_s
        if feed.source == "nse":
            m = re.search(r"/corporate/([A-Z0-9&\-]+)_", link)
            symbol = m.group(1) if m else None
            if symbol not in self.tickers:        # name in memory only; just the resolved symbol is stored
                symbol = self._by_name.get(company_key(it.findtext("title") or ""), symbol)
            rr = classify_nse(it.findtext("description") or "")       # NSE text used in memory only
            in_universe = symbol in self.tickers
            if not in_universe or rr.event_type is EventType.other or stale:
                status = "skipped"
            else:
                status = "classified"
            cls = {"event_type": rr.event_type.value, "confidence": RULE_CONFIDENCE,
                   "affected": ([{"ticker": symbol, "relation": "direct", "direction": rr.direction,
                                  "strength": rr.strength, "reason": rr.label}] if in_universe else []),
                   "rejected_tickers": [] if in_universe or not symbol else [symbol]}
            cur = self.store.conn.execute(
                """INSERT INTO news_items (source, feed, key, url, symbol_hint, headline, summary, label,
                   action_kind, action_date, content_hash, published_at, fetched_at, status, classifier,
                   classification, classified_at, error)
                   VALUES ('nse',?,?,?,?,NULL,NULL,?,?,?,NULL,?,?,?,'rules',?,?,?)""",
                (feed.name, key, link, symbol, rr.label, rr.action_kind, rr.action_date, published, now, status,
                 json.dumps(cls), now, "stale" if stale else ("not in universe" if not in_universe else None)))
            if status == "classified":
                self._alert(cur.lastrowid)
            return 1
        headline = _clean(it.findtext("title"), 300)
        summary = _clean(it.findtext("description"), 500)
        if not headline:
            return 0
        h = hashlib.sha256(f"{headline}\n{summary or ''}".encode()).hexdigest()
        self.store.conn.execute(
            """INSERT INTO news_items (source, feed, key, url, headline, summary, content_hash,
               published_at, fetched_at, status, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (feed.source, feed.name, key, link, headline, summary, h, published, now,
             "skipped" if stale else "pending", "stale" if stale else None))
        return 1

    # -- classify -------------------------------------------------------------------------------
    def _apply(self, item_id: int, c: Classification | None, rejected: list[str], error: str | None) -> None:
        now = self.clock()
        if c is None:
            self.store.conn.execute("UPDATE news_items SET status='unclassified', classifier='gemini', attempts=attempts+2, "
                                    "classified_at=?, error=? WHERE id=?", (now, error, item_id))
            return
        payload = c.model_dump(mode="json") | {"rejected_tickers": rejected}
        self.store.conn.execute("UPDATE news_items SET status='classified', classifier='gemini', classification=?, "
                                "classified_at=?, error=NULL WHERE id=?", (json.dumps(payload), now, item_id))
        self._alert(item_id)

    async def classify_pending(self) -> int:
        """Classify pending headlines: cache first, then Gemini within quota. Returns number decided."""
        done = 0
        # 1) cache hits (same headline+summary seen in another feed) - no API call
        rows = self.store.conn.execute(
            """SELECT n.id, c.classification, c.rejected FROM news_items n JOIN classification_cache c
               ON c.content_hash = n.content_hash WHERE n.status='pending'""").fetchall()
        for item_id, cls, rej in rows:
            self._apply(item_id, Classification.model_validate_json(cls), json.loads(rej or "[]"), None)
            done += 1
        self.store.conn.commit()
        if self.gemini is None:
            return done
        # 2) Gemini, batch by batch, while quota allows
        while self.gemini.can_request():
            rows = self.store.conn.execute(
                "SELECT id, headline, summary, content_hash FROM news_items WHERE status='pending' "
                "ORDER BY published_at DESC, id DESC LIMIT ?", (self.gemini.batch_size,)).fetchall()
            if not rows:
                break
            hashes = {r[0]: r[3] for r in rows}
            try:
                outs = await self.gemini.classify([ItemIn(r[0], r[1], r[2]) for r in rows])
            except QuotaExhausted as e:
                self.last_error = str(e)
                break
            except GeminiError as e:
                self.last_error = str(e)
                log.warning("gemini: %s", e)
                break
            self.last_error = None                # a successful call clears an earlier outage
            for o in outs:
                self._apply(o.id, o.classification, o.rejected_tickers, o.error)
                if o.classification is not None:
                    self.store.conn.execute(
                        "INSERT OR IGNORE INTO classification_cache VALUES (?,?,?,?,?)",
                        (hashes[o.id], o.classification.model_dump_json(), json.dumps(o.rejected_tickers),
                         self.gemini.model, self.clock()))
                done += 1
            self.store.conn.commit()
            if not outs:
                break
        return done

    # -- alerts ------------------------------------------------------------------------------------
    def _alert(self, item_id: int) -> int | None:
        row = self.store.conn.execute(
            "SELECT source, url, headline, label, published_at, classification, classifier FROM news_items WHERE id=?",
            (item_id,)).fetchone()
        source, url, headline, label, published, cls_json, classifier = row
        cls = json.loads(cls_json)
        stocks = [s for s in cls.get("affected", []) if s["ticker"] in self.tickers]
        if cls["event_type"] == "other" or not stocks or cls.get("confidence", 0) < self.min_confidence:
            return None
        if self.store.conn.execute("SELECT 1 FROM news_alerts WHERE item_id=?", (item_id,)).fetchone():
            return None
        if source == "nse" and self.store.conn.execute(
                """SELECT 1 FROM news_alerts a JOIN news_alert_stocks s ON s.news_alert_id = a.id
                   WHERE a.source = 'nse' AND a.event_type = ? AND s.ticker = ? AND a.created_at >= ?""",
                (cls["event_type"], stocks[0]["ticker"], self.clock() - NSE_DUP_WINDOW_S)).fetchone():
            return None                           # same filing seen again (PDF + XBRL copies)
        cur = self.store.conn.execute(
            """INSERT INTO news_alerts (item_id, mode, created_at, published_at, source, event_type, confidence,
               headline, url, classifier) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item_id, self.mode, self.clock(), published, source, cls["event_type"], cls.get("confidence"),
             headline if source != "nse" else f"{stocks[0]['ticker']}: {label}", url, classifier))
        for s in stocks:
            self.store.conn.execute(
                "INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason) "
                "VALUES (?,?,?,?,?,?)",
                (cur.lastrowid, s["ticker"], s["relation"], s.get("direction"), s.get("strength"), s.get("reason")))
        if cls["event_type"] == "results" and self.expectations is not None:
            direct = [s["ticker"] for s in stocks if s["relation"] == "direct"] or [stocks[0]["ticker"]]
            self._results_queue.append((direct[0], published or self.clock()))
        return cur.lastrowid

    async def fetch_expectations(self) -> int:
        """Look up earnings expectations for companies that just reported (optional, Finnhub)."""
        if self.expectations is None:
            return 0
        from datetime import datetime as _dt
        n, seen = 0, set()
        while self._results_queue:
            sym, ts = self._results_queue.pop(0)
            day = _dt.fromtimestamp(ts, IST).date()
            if (sym, day) in seen:
                continue
            seen.add((sym, day))
            if await self.expectations.fetch(sym, day) is not None:
                n += 1
        return n

    # -- status + loop ----------------------------------------------------------------------------
    def publish_status(self) -> None:
        q = lambda sql: self.store.conn.execute(sql).fetchone()[0]
        day_start = datetime.fromtimestamp(self.clock(), IST).replace(hour=0, minute=0, second=0).timestamp()
        g = self.gemini
        self.store.set_status("news", {
            "last_poll_at": self.last_poll_at,
            "feeds": {f.name: f.health for f in self.feeds},
            "items_today": self.store.conn.execute("SELECT COUNT(*) FROM news_items WHERE fetched_at >= ?",
                                                   (day_start,)).fetchone()[0],
            "pending": q("SELECT COUNT(*) FROM news_items WHERE status='pending'"),
            "unclassified": q("SELECT COUNT(*) FROM news_items WHERE status='unclassified'"),
            "alerts_today": self.store.conn.execute("SELECT COUNT(*) FROM news_alerts WHERE created_at >= ?",
                                                    (day_start,)).fetchone()[0],
            "gemini": None if g is None else {
                "model": g.model, "used_today": g.used_today(), "daily_cap": g.rpd,
                "paused_until": g.paused_until if g.paused_until > self.clock() else None,
                "last_quota": g.last_quota or None},
            "gemini_configured": g is not None,
            "expectations": None if self.expectations is None else {
                "provider": "finnhub", "last_error": self.expectations.last_error},
            "last_error": self.last_error,
        }, now=self.clock())

    async def run_once(self) -> None:
        for f in self.feeds:
            await self.poll_feed(f)
        self.last_poll_at = self.clock()
        await self.classify_pending()
        await self.fetch_expectations()
        self.publish_status()

    async def run(self, max_polls: int | None = None) -> None:
        n = 0
        while max_polls is None or n < max_polls:
            try:
                await self.run_once()
            except Exception as e:   # keep the service alive; the error shows in the status bar
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("news poll failed")
                self.publish_status()
            n += 1
            if max_polls is None or n < max_polls:
                await self.sleep(self.poll_s)
