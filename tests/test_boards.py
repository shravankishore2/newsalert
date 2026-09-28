"""Refined NSE rules, corporate actions, grounded figures, expectations and the two boards."""

import json
import sqlite3
from datetime import date, datetime

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from newsalert.market import IST, MarketCalendar
from newsalert.news.expectations import FinnhubEarnings, surprise_pct, verdict
from newsalert.news.models import Classification, figures_grounded
from newsalert.news.rules import classify_nse
from newsalert.ratelimit import RateLimiter
from newsalert.store import Store
from newsalert.web.app import SiteInfo, create_app
from newsalert.web.board import actions_board, results_board

CAL = MarketCalendar.from_config(yaml.safe_load(open("config.yaml"))["market"])
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=IST).timestamp()
PW = "a-test-password"
TICKERS = {"ALPHA": {"name": "Alpha Industries Ltd.", "sector": "Metals"},
           "BETA": {"name": "Beta Bank Ltd.", "sector": "Financial Services"}}
PARAMS = {"move_threshold": 0.015, "move_window_min": 15, "ma_fast_min": 5, "ma_slow_min": 60,
          "ma_confirm_frac": 0.5, "corr_min": 0.6, "corr_returns": 30, "cooldown_min": 30}
INFO = SiteInfo("live", "t", "NIFTY 50", "NIFTY50", "₹", "Asia/Kolkata", TICKERS, PARAMS)


# --- refined NSE rules -------------------------------------------------------------------------

@pytest.mark.parametrize("subject,event,direction", [
    ("the Trading Window closure pursuant to SEBI", "other", None),
    ("Board comments on fine levied by the exchanges", "regulatory action", "down"),
    ("Action(s) initiated or orders passed", "regulatory action", "down"),
    ('a press release titled "Alpha secures order worth Rs 500 crore"', "order/contract win", "up"),
    ("Allotment of 1,200 shares under ESOP", "other", None),
    ("Allotment of 5,00,000 shares on preferential basis", "capital raise", "down"),
    ("Resignation of Chief Financial Officer", "management change", "down"),
    ("Resignation of Mr A, Vice President - Legal", "management change", None),
    ("Credit Rating - outlook revised to Negative", "rating change", "down"),
    ("Credit Rating - reaffirmed", "rating change", None),
    ("Record Date for Bonus Issue", "dividend/buyback", "up"),
    ("Sub-division of equity shares", "dividend/buyback", "up"),
    ("Intimation of record date for maturity of commercial paper", "other", None),
    ("Outcome of Board Meeting - Financial Results", "results", None),
])
def test_refined_rules(subject, event, direction):
    r = classify_nse(f"Alpha Industries Limited has informed the Exchange about {subject}")
    assert (r.event_type.value, r.direction) == (event, direction)


def test_corporate_action_kind_and_date():
    r = classify_nse("X Ltd has informed the Exchange about Intimation of Record Date for Bonus Issue. "
                     "Record date is 10-Oct-2026")
    assert (r.action_kind, r.action_date, r.label) == ("bonus", "2026-10-10", "Bonus issue")
    r = classify_nse("X has informed the Exchange about Record date for interim dividend. The record date is October 15, 2026")
    assert (r.action_kind, r.action_date) == ("dividend", "2026-10-15")
    r = classify_nse("X has informed the Exchange about Intimation of Record Date. Record Date: 01/11/2026")
    assert (r.action_kind, r.action_date) == ("record date", "2026-11-01")
    assert classify_nse("X has informed the Exchange about record date for interest payment on NCDs").action_kind is None


# --- grounded Gemini figures -------------------------------------------------------------------

def test_results_figures_grounded_in_text():
    c = Classification.model_validate({"event_type": "results", "confidence": 0.8, "affected": [],
                                       "results": {"period": "Q2 FY27", "profit_cr": 520, "profit_yoy_pct": 12,
                                                   "revenue_yoy_pct": 9}})
    g = figures_grounded(c, "Alpha Q2 net profit rises 12% to ₹520 crore")
    assert g.results.profit_yoy_pct == 12 and g.results.profit_cr == 520
    assert g.results.revenue_yoy_pct is None                   # "9%" isn't in the text: dropped
    other = Classification.model_validate({"event_type": "guidance", "confidence": 0.5, "affected": [],
                                           "results": {"profit_cr": 1}})
    assert figures_grounded(other, "x").results is None         # figures only for results events


# --- expectations -----------------------------------------------------------------------------------

def test_surprise_and_verdict_thresholds():
    assert surprise_pct(11, 10) == pytest.approx(10) and surprise_pct(1, 0) is None
    assert [verdict(x) for x in (-15, -5, 0, 5, 15, None)] == ["strong miss", "miss", "in line", "beat", "strong beat", None]


async def test_finnhub_lookup_nearest_report_and_store():
    store = Store(":memory:")
    seen = {}

    def h(req):
        seen.update(dict(req.url.params))
        seen["token"] = req.headers.get("x-finnhub-token")
        return httpx.Response(200, json={"earningsCalendar": [
            {"date": "2026-07-20", "epsEstimate": 9.0, "epsActual": 9.5, "symbol": "ALPHA.NS"},
            {"date": "2026-09-28", "epsEstimate": 10.0, "epsActual": 11.5, "revenueEstimate": 5e9,
             "revenueActual": 5.2e9, "quarter": 2, "year": 2027, "symbol": "ALPHA.NS"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        f = FinnhubEarnings(http, "fk", store, limiter=RateLimiter([(100, 60.0)]))
        e = await f.fetch("ALPHA", date(2026, 9, 28))
    assert e.report_date == "2026-09-28" and e.eps_actual == 11.5
    assert seen["symbol"] == "ALPHA.NS" and seen["international"] == "true" and seen["token"] == "fk"
    assert store.conn.execute("SELECT COUNT(*) FROM earnings_expectations").fetchone()[0] == 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"earningsCalendar": []}))) as http:
        assert await FinnhubEarnings(http, "fk", store).fetch("BETA", date(2026, 9, 28)) is None


# --- boards -------------------------------------------------------------------------------------------

def seed(store: Store):
    def news(nid, ticker, source, headline, created, results=None, classifier="gemini", abn=None):
        store.conn.execute("INSERT INTO news_items (id, source, feed, key, url, headline, published_at, fetched_at, "
                           "status, classifier, classification) VALUES (?,?,?,?,?,?,?,?,'classified',?,?)",
                           (nid, source, "f", f"k{nid}", f"https://example.test/{nid}",
                            None if source == "nse" else headline, created - 60, created, classifier,
                            json.dumps({"event_type": "results", "confidence": 0.8, "affected": [], "results": results})))
        store.conn.execute("INSERT INTO news_alerts (id, item_id, created_at, published_at, source, event_type, confidence, "
                           "headline, url, classifier) VALUES (?,?,?,?,?,'results',0.8,?,?,?)",
                           (nid, nid, created, created - 60, source, headline, f"https://example.test/{nid}", classifier))
        store.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, reason, abn_15m, "
                           "abn_1h, abn_close, evaluated_at) VALUES (?,?, 'direct', 'up', 'Profit beat', ?,?,?,?)",
                           (nid, ticker, *(abn or (None, None, None)), NOW if abn else None))
    news(1, "ALPHA", "businessline", "Alpha Q2 profit rises 12% to ₹520 crore", NOW - 3600,
         results={"period": "Q2 FY27", "profit_cr": 520, "profit_yoy_pct": 12})
    news(2, "ALPHA", "nse", "ALPHA: Financial results", NOW - 3000, classifier="rules")
    news(3, "BETA", "nse", "BETA: Financial results", NOW - 2 * 86400, classifier="rules", abn=(0.004, 0.012, 0.031))
    store.conn.commit()


def price_fn():
    open_ = datetime(2026, 9, 28, 9, 15, tzinfo=IST).timestamp()
    data = {"ALPHA": [(int(open_ + 60 * i), 100 + 0.02 * i) for i in range(375)],
            "NIFTY50": [(int(open_ + 60 * i), 25000.0) for i in range(375)]}
    return lambda s, a, b: [(t, p) for t, p in data.get(s, []) if a < t <= b]


def test_results_board_bases_and_merging():
    store = Store(":memory:")
    seed(store)
    cards = results_board(store.conn, INFO, price_fn(), CAL, "NIFTY50", NOW, days=7)
    alpha = next(c for c in cards if c["ticker"] == "ALPHA")
    assert len(alpha["sources"]) == 2                                  # BusinessLine + NSE merged into one card
    assert alpha["basis"] == "yoy" and alpha["gauge"]["mode"] == "reaction"
    assert alpha["figures"]["profit_yoy_pct"] == 12 and alpha["verdict"] is None
    assert alpha["briefing"] == "Alpha Q2 profit rises 12% to ₹520 crore"   # figures live in the table, not repeated
    assert alpha["reaction"]["provisional"] and alpha["reaction"]["abn_15m"] > 0   # market open: from live prices
    beta = next(c for c in cards if c["ticker"] == "BETA")
    assert beta["basis"] == "reaction" and not beta["reaction"]["provisional"]
    assert beta["gauge"]["value"] == pytest.approx(3.1) and beta["gauge"]["label"] == "strong positive reaction"
    assert "filed its financial results with NSE" in beta["briefing"]
    # with expectations, the gauge switches to surprise and a verdict
    store.conn.execute("INSERT INTO earnings_expectations VALUES ('ALPHA','2026-09-28',2,2027,10,11.5,NULL,NULL,'finnhub',?)", (NOW,))
    alpha = next(c for c in results_board(store.conn, INFO, price_fn(), CAL, "NIFTY50", NOW) if c["ticker"] == "ALPHA")
    assert alpha["basis"] == "expectations" and alpha["verdict"] == "strong beat"
    assert alpha["gauge"]["value"] == pytest.approx(15) and "Finnhub" in alpha["basis_label"]


def test_actions_board_dedupes_and_limits_to_universe():
    store = Store(":memory:")
    rows = [("ALPHA", "dividend", "2026-10-15"), ("ALPHA", "dividend", "2026-10-15"), ("BETA", "bonus", None),
            ("ZZZZ", "split", "2026-10-01"), ("BETA", "record date", "2026-09-01")]
    for i, (sym, kind, when) in enumerate(rows):
        store.conn.execute("INSERT INTO news_items (source, feed, key, url, symbol_hint, label, action_kind, action_date, "
                           "published_at, fetched_at, status) VALUES ('nse','f',?,?,?,?,?,?,?,?,'skipped')",
                           (f"k{i}", f"https://example.test/{i}", sym, kind.title(), kind, when, NOW - i * 60, NOW - i * 60))
    items = actions_board(store.conn, INFO, NOW)
    assert [(a["ticker"], a["kind"], a["action_date"]) for a in items] == [
        ("ALPHA", "dividend", "2026-10-15"), ("BETA", "bonus", None), ("BETA", "record date", "2026-09-01")]
    assert items[0]["upcoming"] and not items[2]["upcoming"]


def test_board_endpoints_require_login(tmp_path):
    db = str(tmp_path / "a.db")
    store = Store(db)
    seed(store)
    app = create_app(db_path=db, info=INFO, password=PW, prices=price_fn(), calendar=CAL, static_dir=None,
                     market=lambda: {"open": True, "now_ts": NOW})
    c = TestClient(app)
    assert c.get("/api/board/results").status_code == 401 and c.get("/api/board/actions").status_code == 401
    assert c.post("/api/login", json={"password": PW}).status_code == 204
    body = c.get("/api/board/results").json()
    assert {x["ticker"] for x in body["items"]} == {"ALPHA", "BETA"} and body["expectations_configured"] is False
    assert c.get("/api/board/actions").json() == {"items": []}


def test_news_items_migration_adds_action_columns(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT NOT NULL, feed TEXT NOT NULL, "
                 "key TEXT NOT NULL UNIQUE, url TEXT NOT NULL, fetched_at REAL NOT NULL, status TEXT NOT NULL)")
    conn.commit()
    conn.close()
    cols = {r[1] for r in Store(p).conn.execute("PRAGMA table_info(news_items)")}
    assert {"action_kind", "action_date"} <= cols
