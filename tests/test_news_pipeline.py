"""News-first pipeline: ingest, NSE rules, Gemini (mocked), cache, alerts, links, event study."""

import json
import math
from datetime import datetime, timedelta

import httpx
import pytest
import yaml

from newsalert.market import IST, MarketCalendar
from newsalert.news.evaluate import binom_two_sided, evaluate_pending, evaluate_stock, event_window, hit_stats, render_news_results
from newsalert.news.gemini import GeminiClassifier, QuotaExhausted
from newsalert.news.ingest import Feed, NewsService
from newsalert.news.models import Classification
from newsalert.news.rules import classify_nse
from newsalert.ratelimit import RateLimiter
from newsalert.store import Store

CAL = MarketCalendar.from_config(yaml.safe_load(open("config.yaml"))["market"])
TICKERS = {"ALPHA": "Alpha Industries Ltd.", "BETA": "Beta Steel Ltd.", "GAMMA": "Gamma Motors Ltd."}
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=IST).timestamp()
NSE_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
BL_URL = "https://www.thehindubusinessline.com/companies/feeder/default.rss"
GEMINI = "generativelanguage.googleapis.com"


def nse_xml(items):
    body = "".join(
        f"<item><title>{co}</title><link>https://nsearchives.nseindia.com/corporate/{sym}_28092026{i:02d}0000_x.pdf</link>"
        f"<description>{co} has informed the Exchange about {subj}</description><pubDate>{pub}</pubDate></item>"
        for i, (sym, co, subj, pub) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>".encode()


def bl_xml(items):
    body = "".join(f"<item><title>{t}</title><link>https://www.thehindubusinessline.com/a/{i}</link><guid>bl-{i}</guid>"
                   f"<description>{d}</description><pubDate>{p}</pubDate></item>" for i, (t, d, p) in enumerate(items))
    return f"<rss><channel>{body}</channel></rss>".encode()


def rfc(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%a, %d %b %Y %H:%M:%S +0530")


class Server:
    """Serves feeds (with ETag) and a scripted Gemini from memory; records calls."""

    def __init__(self):
        self.feeds = {}
        self.gemini_replies = []      # list of (status, body-or-text) consumed in order
        self.gemini_calls = []
        self.feed_calls = []

    def handler(self, req):
        if req.url.host == GEMINI:
            self.gemini_calls.append(json.loads(req.content))
            assert req.headers.get("x-goog-api-key") == "test-key"
            status, reply = self.gemini_replies.pop(0)
            if status != 200:
                return httpx.Response(status, json=reply)
            text = reply if isinstance(reply, str) else json.dumps(reply)
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
        url = str(req.url)
        self.feed_calls.append((url, req.headers.get("if-none-match")))
        body = self.feeds[url]
        etag = f'"{hash(body)}"'
        if req.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"etag": etag, "content-type": "application/xml"})


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make(server, clock, with_gemini=True, rpd=100):
    store = Store(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    g = None
    if with_gemini:
        g = GeminiClassifier(http, "test-key", model="gemini-test", tickers=TICKERS, store=store, rpm=1000, rpd=rpd,
                             clock=clock, limiter=RateLimiter([(1000, 60.0)]))
    svc = NewsService(http=http, store=store, tickers=TICKERS, gemini=g, clock=clock,
                      feeds=[Feed("NSE announcements", "nse", NSE_URL), Feed("BusinessLine companies", "businessline", BL_URL)])
    return store, http, svc


def item_json(i, event="order/contract win", stocks=(("ALPHA", "direct", "up"),), conf=0.8):
    return {"id": i, "event_type": event, "confidence": conf,
            "affected": [{"ticker": t, "relation": r, "direction": d, "strength": "medium", "reason": "why"} for t, r, d in stocks]}


# --- NSE: rules, never stored text -------------------------------------------------------------------

async def test_nse_items_classified_by_rules_and_text_never_stored():
    s, c = Server(), Clock()
    pub = datetime.fromtimestamp(NOW - 120, IST).strftime("%d-%b-%Y %H:%M:%S")
    s.feeds[NSE_URL] = nse_xml([("ALPHA", "Alpha Industries Limited", "Bagging/Receiving of orders/contracts SECRETPHRASE", pub),
                                ("ALPHA", "Alpha Industries Limited", "Trading Window closure", pub),
                                ("ZZZZ", "Not In Universe Ltd", "Bagging/Receiving of orders", pub)])
    s.feeds[BL_URL] = bl_xml([])
    store, http, svc = make(s, c)
    async with http:
        await svc.run_once()
    dump = "\n".join(str(r) for r in store.conn.execute("SELECT * FROM news_items").fetchall())
    assert "SECRETPHRASE" not in dump and "Alpha Industries Limited" not in dump and "has informed" not in dump
    rows = store.conn.execute("SELECT symbol_hint, status, label, headline FROM news_items ORDER BY id").fetchall()
    assert rows == [("ALPHA", "classified", "Order/contract win", None), ("ALPHA", "skipped", "Other filing", None),
                    ("ZZZZ", "skipped", "Order/contract win", None)]
    a = store.conn.execute("SELECT source, event_type, headline, classifier FROM news_alerts").fetchall()
    assert a == [("nse", "order/contract win", "ALPHA: Order/contract win", "rules")]
    assert s.gemini_calls == []          # NSE never goes to the LLM


def test_rule_directions():
    assert classify_nse("X has informed the Exchange about Credit Rating downgraded").direction == "down"
    assert classify_nse("X has informed the Exchange about Financial Results").direction is None
    assert classify_nse("X has informed the Exchange regarding insolvency petition").event_type.value == "fraud/legal"


# --- BusinessLine: Gemini, validation, retry, cache, universe -------------------------------------

async def test_gemini_batch_validates_and_rejects_unknown_tickers():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([("Alpha wins Rs 500 cr order", "Order from railways", rfc(NOW - 60)),
                              ("Beta Steel cuts prices", "Pressure on margins", rfc(NOW - 60))])
    store, http, svc = make(s, c)
    ids = []
    async with http:
        for f in svc.feeds:
            await svc.poll_feed(f)
        ids = [r[0] for r in store.conn.execute("SELECT id FROM news_items ORDER BY published_at DESC, id DESC")]
        s.gemini_replies = [(200, {"items": [
            item_json(ids[0], stocks=(("ALPHA", "direct", "up"), ("NOTREAL", "competitor", "down"))),
            item_json(ids[1], event="guidance", stocks=(("BETA", "direct", "down"), ("GAMMA", "customer", "up")))]})]
        await svc.classify_pending()
    body = s.gemini_calls[0]
    assert body["generationConfig"]["responseMimeType"] == "application/json" and "responseSchema" in body["generationConfig"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "ALPHA: Alpha Industries Ltd." in prompt and "Alpha wins Rs 500 cr order" in prompt
    cls = json.loads(store.conn.execute("SELECT classification FROM news_items WHERE id=?", (ids[0],)).fetchone()[0])
    assert [a["ticker"] for a in cls["affected"]] == ["ALPHA"] and cls["rejected_tickers"] == ["NOTREAL"]
    stocks = store.conn.execute("SELECT ticker, relation, direction FROM news_alert_stocks ORDER BY id").fetchall()
    assert ("GAMMA", "customer", "up") in stocks and all(t != "NOTREAL" for t, _, _ in stocks)


async def test_invalid_json_retried_once_then_unclassified():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([("Alpha news", "x", rfc(NOW - 60)), ("Beta news", "y", rfc(NOW - 60))])
    store, http, svc = make(s, c)
    async with http:
        for f in svc.feeds:
            await svc.poll_feed(f)
        a, b = [r[0] for r in store.conn.execute("SELECT id FROM news_items ORDER BY published_at DESC, id DESC")]
        s.gemini_replies = [
            (200, "{not json"),                                    # batch invalid
            (200, {"items": [item_json(a)]}),                      # item a retried alone: valid
            (200, {"items": [{"id": b, "event_type": "BOGUS"}]}),  # item b retried alone: still invalid
        ]
        await svc.classify_pending()
    st = dict(store.conn.execute("SELECT id, status FROM news_items").fetchall())
    assert st == {a: "classified", b: "unclassified"} and len(s.gemini_calls) == 3


async def test_never_classified_twice_cache_and_status():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([("Alpha wins order", "Order", rfc(NOW - 60))])
    store, http, svc = make(s, c)
    async with http:
        await svc.poll_feed(svc.feeds[1])
        (i1,) = store.conn.execute("SELECT id FROM news_items").fetchone()
        s.gemini_replies = [(200, {"items": [item_json(i1)]})]
        await svc.classify_pending()
        await svc.classify_pending()                      # nothing pending: no call
        # same headline+summary appears in another feed (different guid): served from cache
        s.feeds["https://bl/markets"] = bl_xml([]).replace(b"<channel>", b"<channel><item><title>Alpha wins order</title>"
                                                           b"<link>https://bl/x</link><guid>other</guid><description>Order"
                                                           b"</description><pubDate>" + rfc(NOW - 30).encode() + b"</pubDate></item>")
        svc.feeds.append(Feed("BL markets", "businessline", "https://bl/markets"))
        await svc.poll_feed(svc.feeds[2])
        await svc.classify_pending()
    assert len(s.gemini_calls) == 1
    assert [r[0] for r in store.conn.execute("SELECT status FROM news_items")] == ["classified", "classified"]
    assert store.conn.execute("SELECT COUNT(*) FROM news_alerts").fetchone()[0] == 2


async def test_quota_daily_cap_and_429_leave_items_pending():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([("A", "a", rfc(NOW - 60))])
    store, http, svc = make(s, c, rpd=1)
    async with http:
        await svc.poll_feed(svc.feeds[1])
        s.gemini_replies = [(429, {"error": {"code": 429, "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaMetric": "generate_content_free_tier_requests", "quotaValue": "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}]}})]
        await svc.classify_pending()
        assert svc.gemini.paused_until == NOW + 37 and svc.gemini.last_quota["quotaValue"] == "20"
        assert svc.gemini.used_today() == 1 and not svc.gemini.can_request()   # daily cap of 1 reached
        c.t += 40
        await svc.classify_pending()                       # still capped for the day: no call
    assert store.conn.execute("SELECT status FROM news_items").fetchone()[0] == "pending"
    assert len(s.gemini_calls) == 1


async def test_no_gemini_key_leaves_pending_and_stale_items_skipped():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([("Fresh", "x", rfc(NOW - 60)), ("Old", "y", rfc(NOW - 5 * 3600))])
    store, http, svc = make(s, c, with_gemini=False)
    async with http:
        await svc.run_once()
    assert sorted(r[0] for r in store.conn.execute("SELECT status FROM news_items")) == ["pending", "skipped"]
    st = store.get_status()["news"]["value"]
    assert st["gemini_configured"] is False and st["pending"] == 1


async def test_conditional_get_uses_etag():
    s, c = Server(), Clock()
    s.feeds[NSE_URL] = nse_xml([])
    s.feeds[BL_URL] = bl_xml([])
    store, http, svc = make(s, c)
    async with http:
        await svc.poll_feed(svc.feeds[0])
        await svc.poll_feed(svc.feeds[0])
    assert s.feed_calls[0][1] is None and s.feed_calls[1][1] is not None
    assert svc.feeds[0].health["ok"] and svc.feeds[0].health["new"] == 0


def test_classification_schema_rejects_bad_values():
    with pytest.raises(Exception):
        Classification.model_validate({"event_type": "results", "confidence": 1.5, "affected": []})
    with pytest.raises(Exception):
        Classification.model_validate({"event_type": "results", "confidence": 0.5,
                                       "affected": [{"ticker": "A", "relation": "friend", "direction": "up",
                                                     "strength": "low", "reason": "x"}]})
    c = Classification.model_validate({"event_type": "results", "confidence": 0.5, "affected": [
        {"ticker": "alpha.ns", "relation": "direct", "direction": "up", "strength": "low", "reason": "x"}]})
    assert c.affected[0].ticker == "ALPHA"


# --- linking price alerts ------------------------------------------------------------------------

def test_price_alert_linked_within_60_minutes_after_news():
    store = Store(":memory:")
    store.conn.execute("INSERT INTO news_alerts (id, item_id, created_at, source, event_type, url, classifier) "
                       "VALUES (1, 1, ?, 'nse', 'results', 'u', 'rules')", (NOW,))
    store.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation) VALUES (1, 'ALPHA', 'direct')")
    assert store.link_price_alert(10, "ALPHA", int(NOW + 1800)) == [1]
    assert store.link_price_alert(11, "ALPHA", int(NOW + 3700)) == []     # too late
    assert store.link_price_alert(12, "ALPHA", int(NOW - 60)) == []       # before the news
    assert store.link_price_alert(13, "BETA", int(NOW + 60)) == []        # other stock
    assert store.conn.execute("SELECT price_alert_id, minutes_after FROM price_news_links").fetchall() == [(10, 30.0)]


# --- event study ----------------------------------------------------------------------------------------

def series(start, n, f):
    return [(int(start + 60 * i), f(i)) for i in range(n)]


def price_fn(data):
    return lambda sym, a, b: [(t, p) for t, p in data.get(sym, []) if a < t <= b]


def test_event_window_in_and_out_of_hours():
    t, rule, close = event_window(CAL, datetime(2026, 9, 28, 10, 0, tzinfo=IST).timestamp())
    assert rule == "alert time" and close == datetime(2026, 9, 28, 15, 30, tzinfo=IST).timestamp()
    t, rule, _ = event_window(CAL, datetime(2026, 10, 1, 20, 0, tzinfo=IST).timestamp())   # Thu evening, Fri holiday
    assert rule == "next open" and t == datetime(2026, 10, 5, 9, 15, tzinfo=IST).timestamp()


def test_evaluate_stock_abnormal_returns():
    open_ = datetime(2026, 9, 28, 9, 15, tzinfo=IST).timestamp()
    data = {"ALPHA": series(open_, 375, lambda i: 100 + 0.01 * i),      # +3.74% by the close
            "NIFTY50": series(open_, 375, lambda i: 25000 * (1 + 0.00001 * i))}
    alert = datetime(2026, 9, 28, 10, 0, tzinfo=IST).timestamp()
    r = evaluate_stock(CAL, price_fn(data), "NIFTY50", "ALPHA", alert)
    p0 = 100 + 0.01 * 45
    assert r["t0_rule"] == "alert time"
    assert r["ret_15m"] == pytest.approx((100 + 0.01 * 60) / p0 - 1)
    assert r["abn_1h"] == pytest.approx(r["ret_1h"] - r["idx_1h"])
    assert r["ret_close"] == pytest.approx((100 + 0.01 * 374) / p0 - 1)
    late = evaluate_stock(CAL, price_fn(data), "NIFTY50", "ALPHA", datetime(2026, 9, 28, 15, 0, tzinfo=IST).timestamp())
    assert "abn_1h" not in late and "abn_15m" in late                         # +1 h would pass the close
    overnight = evaluate_stock(CAL, price_fn(data), "NIFTY50", "ALPHA", datetime(2026, 9, 27, 22, 0, tzinfo=IST).timestamp())
    assert overnight["t0_rule"] == "next open" and overnight["ret_15m"] == pytest.approx(100.15 / 100 - 1)


def test_hit_stats_and_binomial():
    st = hit_stats([("up", 0.01), ("up", -0.01), ("down", -0.02), ("down", 0.0), (None, 0.03), ("up", None)])
    assert (st["n"], st["hits"], st["ties"], st["missing"], st["no_direction"]) == (3, 2, 1, 1, 1)
    assert binom_two_sided(5, 10) == pytest.approx(1.0)
    assert binom_two_sided(9, 10) == pytest.approx(0.021484375)
    assert math.isnan(binom_two_sided(0, 0))


def test_results_section_marks_small_samples_and_reports_latency():
    store = Store(":memory:")
    for i in range(3):
        store.conn.execute("INSERT INTO news_alerts (id, item_id, created_at, published_at, source, event_type, url, classifier) "
                           "VALUES (?, ?, ?, ?, 'businessline', 'order/contract win', 'u', 'gemini')", (i + 1, i + 1, NOW + 240, NOW))
        store.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, abn_15m, evaluated_at) "
                           "VALUES (?, 'ALPHA', 'direct', 'up', ?, ?)", (i + 1, 0.01 if i < 2 else -0.01, NOW))
    text = render_news_results(store, generated=datetime(2026, 9, 28))
    assert "| All directional calls | +15 min | 3 | 66.7%" in text and "**too few**" in text
    assert "| businessline | 3 | 4.0 min | 4.0 min |" in text
    assert "next session open" in text or "next open" in text


async def test_evaluate_pending_waits_for_close():
    store = Store(":memory:")
    open_ = datetime(2026, 9, 28, 9, 15, tzinfo=IST).timestamp()
    data = {"ALPHA": series(open_, 375, lambda i: 100.0 + i * 0.01), "NIFTY50": series(open_, 375, lambda i: 25000.0)}
    store.conn.execute("INSERT INTO news_alerts (id, item_id, created_at, source, event_type, url, classifier) "
                       "VALUES (1, 1, ?, 'nse', 'order/contract win', 'u', 'rules')", (open_ + 3600,))
    store.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction) VALUES (1,'ALPHA','direct','up')")
    assert evaluate_pending(store, CAL, price_fn(data), "NIFTY50", now=open_ + 4 * 3600) == 0      # session still open
    assert evaluate_pending(store, CAL, price_fn(data), "NIFTY50", now=open_ + 7 * 3600) == 1
    abn = store.conn.execute("SELECT abn_15m, abn_close, t0_rule FROM news_alert_stocks").fetchone()
    assert abn[0] > 0 and abn[1] > 0 and abn[2] == "alert time"
