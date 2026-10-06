"""Guest view: key gate, read-only, rate limits, noindex, and no third-party text or raw prices."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from newsalert.signals import Alert
from newsalert.store import Store
from newsalert.web import guest as gv
from newsalert.web.app import SiteInfo, create_app

T0 = 1_790_000_000
PW = "a-test-password"
TICKERS = {"ALPHA": {"name": "Alpha Industries Ltd.", "sector": "Metals"},
           "BETA": {"name": "Beta Bank Ltd.", "sector": "Financial Services"}}
PARAMS = {"move_threshold": 0.015, "move_window_min": 15, "ma_fast_min": 5, "ma_slow_min": 60,
          "ma_confirm_frac": 0.5, "corr_min": 0.6, "corr_returns": 30, "cooldown_min": 30}

# Third-party text and raw prices planted in the database: none of it may reach a guest.
HEADLINE = "Alpha Industries bags colossal Zanzibar ferry contract"
SUMMARY = "The quixotic Zanzibar deal adds sizeable revenue visibility for the shipbuilder"
GEMINI_REASON = "Alpha secured a Zanzibar ferry contract that should lift sentiment"
NSE_LABEL = "ALPHA: Order/contract win"
SECRET_WORDS = ("zanzibar", "colossal", "quixotic", "shipbuilder", "ferry", "bags", "sentiment")
RAW_PRICES = [1234.56, 1240.12, 1251.78, 25432.1, 25510.9, 987.65, 1017.28]


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "alerts.db")
    store = Store(db)
    c = store.conn
    # BusinessLine item + Gemini alert on ALPHA (direct) and BETA (sector peer)
    c.execute("INSERT INTO news_items (id, source, feed, key, url, headline, summary, published_at, fetched_at, "
              "status) VALUES (1, 'businessline', 'bl', 'k1', 'https://www.thehindubusinessline.com/a/1', ?, ?, ?, ?, "
              "'classified')",
              (HEADLINE, SUMMARY, T0 - 120, T0 - 60))
    c.execute("INSERT INTO news_alerts (id, item_id, mode, created_at, published_at, source, event_type, confidence, "
              "headline, url, classifier) VALUES (1, 1, 'live', ?, ?, 'businessline', 'order/contract win', 0.85, ?, "
              "'https://www.thehindubusinessline.com/a/1', 'gemini')", (T0, T0 - 120, HEADLINE))
    c.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason) "
              "VALUES (1, 'ALPHA', 'direct', 'up', 'medium', ?)", (GEMINI_REASON,))
    c.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason, "
              "ret_15m, abn_15m) VALUES (1, 'BETA', 'sector peer', 'up', 'low', ?, 0.004, 0.002)", (GEMINI_REASON,))
    # NSE filing (rules), with an event-study result already in
    c.execute("INSERT INTO news_items (id, source, feed, key, url, symbol_hint, label, fetched_at, status) "
              "VALUES (2, 'nse', 'nse', 'k2', 'https://nsearchives.nseindia.com/corporate/ALPHA_x.pdf', 'ALPHA', "
              "'Order/contract win', ?, 'classified')", (T0 + 30,))
    c.execute("INSERT INTO news_alerts (id, item_id, mode, created_at, published_at, source, event_type, confidence, "
              "headline, url, classifier) VALUES (2, 2, 'live', ?, NULL, 'nse', 'order/contract win', NULL, ?, "
              "'https://nsearchives.nseindia.com/corporate/ALPHA_x.pdf', 'rules')", (T0 + 60, NSE_LABEL))
    c.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason, "
              "ret_close, abn_close) VALUES (2, 'ALPHA', 'direct', 'up', 'medium', 'Order/contract win', 0.0123, 0.0101)")
    # a price alert linked to the BusinessLine alert
    a = Alert("ALPHA", T0 + 600, 1, 0.0302, T0 - 300, RAW_PRICES[5], RAW_PRICES[6], index_move=0.003, corr=0.8,
              beta=1.0, fast_sma=RAW_PRICES[1], slow_sma=RAW_PRICES[0], fast_move=0.02, residual=0.027)
    pid = store.insert_alert(a, mode="live", run_id="r", variant="filtered", received_ns=1, sent_ns=2, delivered=True)
    store.set_alert_news(pid, [{"headline": HEADLINE, "source": "BusinessLine", "url": "u", "published": "p"}])
    c.execute("INSERT INTO price_news_links VALUES (?, 1, 'ALPHA', 10.0)", (pid,))
    c.commit()
    quotes = {"ALPHA": [(T0 - 60, RAW_PRICES[0]), (T0 + 300, RAW_PRICES[1]), (T0 + 900, RAW_PRICES[2])],
              "NIFTY50": [(T0 - 60, RAW_PRICES[3]), (T0 + 900, RAW_PRICES[4])]}
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>QuantRadar</title><div id=root></div>")
    key_file = tmp_path / "guest_key"
    key_file.write_text("the-guest-key\n")
    status = {"open": True, "now_ts": T0 + 1000, "simulated": False, "timezone": "Asia/Kolkata"}
    app = create_app(db_path=db, info=SiteInfo("live", "NSE test", "NIFTY 50", "NIFTY50", "₹", "Asia/Kolkata",
                                               TICKERS, PARAMS),
                     password=PW, prices=lambda s, a, b: [p for p in quotes.get(s, []) if a < p[0] <= b],
                     market=lambda: status, static_dir=str(dist), push_poll_s=0.02,
                     clock=lambda: T0 + 1000, guest_key_file=str(key_file))
    return TestClient(app), key_file


K = {"k": "the-guest-key"}


def leaks(text: str) -> list[str]:
    """Every planted headline/summary/reason word or raw price found in `text`."""
    low = text.lower()
    found = [w for w in SECRET_WORDS if w in low]
    for line in (HEADLINE, SUMMARY, GEMINI_REASON, NSE_LABEL):
        if line.lower() in low:
            found.append(line)
    for p in RAW_PRICES:
        for form in {f"{p}", f"{p:.2f}", f"{p:.1f}", f"{p:,.2f}", f"{int(p)}"}:
            if re.search(rf"(?<![\d.]){re.escape(form)}(?![\d])", text):
                found.append(form)
    if "₹" in text:
        found.append("₹")
    return found


def numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from numbers(v)


def assert_clean(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    assert leaks(text) == []
    if not isinstance(payload, str):
        for x in numbers(payload):
            assert all(abs(x - p) > 1e-6 for p in RAW_PRICES), f"raw price {x} reached the guest API"


# --- no third-party text, no raw prices --------------------------------------------------------

def test_the_planted_text_and_prices_are_in_the_owner_api(env):
    """Guard for the tests below: the owner's API really does carry the planted text and prices."""
    client, _ = env
    assert client.post("/api/login", json={"password": PW}).status_code == 204
    body = client.get("/api/news").text + client.get("/api/news/1").text + client.get("/api/alerts").text
    assert HEADLINE in body and SUMMARY in body and GEMINI_REASON in body and "1234.56" in body


def test_guest_news_has_no_headline_summary_reason_or_prices(env):
    client, _ = env
    r = client.get("/guest/api/news", params=K)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert_clean(r.json())


def test_guest_news_fields_are_the_whitelist(env):
    client, _ = env
    for n in client.get("/guest/api/news", params=K).json()["items"]:
        assert tuple(n) == gv.GUEST_NEWS_FIELDS
        for s in n["stocks"]:
            assert tuple(s) == gv.GUEST_STOCK_FIELDS
        for link in n["linked_price_moves"]:
            assert tuple(link) == gv.GUEST_LINK_FIELDS


def test_guest_news_shows_what_it_should(env):
    client, _ = env
    bl, nse = sorted(client.get("/guest/api/news", params=K).json()["items"], key=lambda n: n["id"])
    assert (bl["event_type"], bl["source_name"], bl["url"]) == (
        "order/contract win", "BusinessLine", "https://www.thehindubusinessline.com/a/1")
    alpha, beta = bl["stocks"]
    assert (alpha["ticker"], alpha["direction"], alpha["strength"]) == ("ALPHA", "up", "medium")
    # move since the alert: last price at/before the alert -> latest, as a fraction
    assert alpha["move_since_alert"] == pytest.approx(RAW_PRICES[2] / RAW_PRICES[0] - 1)
    assert (beta["ret_15m"], beta["abn_15m"]) == (0.004, 0.002)
    assert bl["linked_price_moves"] == [{"symbol": "ALPHA", "direction": 1, "move": 0.0302, "minutes_after": 10.0}]
    assert bl["reasoning"] == ("Gemini read the article as order/contract win, 85% confident. Expected short-term "
                               "move: ALPHA (direct): up, medium strength. Also flagged: BETA (sector peer): up, "
                               "low strength.")
    assert nse["source_name"] == "NSE" and nse["classifier"] == "rules"
    assert nse["url"] is None          # NSE requires written permission to link (guest.LINKABLE_SOURCES)
    assert nse["reasoning"] == "Exchange-filing rule for order/contract win. Expected short-term move: ALPHA (direct): up, medium strength."
    assert nse["stocks"][0]["ret_close"] == 0.0123 and nse["stocks"][0]["move_since_alert"] is None


def test_guest_meta_and_stream_are_clean(env):
    client, _ = env
    meta = client.get("/guest/api/meta", params=K)
    assert meta.status_code == 200
    assert_clean(meta.json())
    assert set(meta.json()["status"]) == {"server_time", "market", "news"}
    with client.stream("GET", "/guest/api/stream", params={**K, "max_events": 3},
                       headers={"Last-Event-ID": "0"}) as r:
        body = "".join(r.iter_text())
    assert body.count("event: news") == 2 and "event: alert" not in body
    assert_clean(body)
    for frame in re.findall(r"^data: (.*)$", body, re.M):
        assert_clean(json.loads(frame))


def test_unsafe_links_are_dropped():
    assert gv._safe_url("javascript:alert(1)") is None
    assert gv._safe_url("https://www.nseindia.com/x") == "https://www.nseindia.com/x"


# --- key gate ------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/guest", "/guest/api/news", "/guest/api/meta", "/guest/api/stream"])
def test_wrong_or_missing_key_is_404(env, path):
    client, _ = env
    assert client.get(path).status_code == 404
    assert client.get(path, params={"k": "nope"}).status_code == 404


def test_page_with_key_is_served_noindex(env):
    client, _ = env
    r = client.get("/guest", params=K)
    assert r.status_code == 200 and "<div id=root>" in r.text
    assert "noindex" in r.headers["x-robots-tag"] and r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/guest/api/news", params=K).headers["x-robots-tag"] == gv.ROBOTS
    assert client.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"


def test_no_key_file_means_no_guest_view(env):
    client, key_file = env
    key_file.unlink()
    assert client.get("/guest", params=K).status_code == 404


def test_rotation_takes_effect_without_restart(env):
    client, key_file = env
    assert client.get("/guest/api/news", params=K).status_code == 200
    new = gv.GuestKey(key_file).rotate()
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    assert client.get("/guest/api/news", params=K).status_code == 404
    assert client.get("/guest/api/news", params={"k": new}).status_code == 200


def test_guest_key_grants_nothing_else(env):
    client, _ = env
    for path in ("/api/news", "/api/alerts", "/api/status", "/api/me", "/api/stream", "/api/news/1"):
        assert client.get(path, params=K).status_code == 401
    assert "set-cookie" not in client.get("/guest", params=K).headers


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_guest_is_read_only(env, method):
    client, _ = env
    assert getattr(client, method)("/guest/api/news", params=K).status_code == 405
    assert getattr(client, method)("/guest", params=K).status_code == 405
    assert getattr(client, method)("/guest", params={"k": "nope"}).status_code == 404


def test_rate_limited(tmp_path):
    t = [0.0]
    tight = gv.RateWindow(3, 60, clock=lambda: t[0])
    key_file = tmp_path / "k2"
    key_file.write_text("kk")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "index.html").write_text("x")
    app = create_app(db_path=str(tmp_path / "none.db"), info=SiteInfo("live", "t", "N", "N", "₹", "Asia/Kolkata"),
                     password=PW, prices=lambda *a: [], market=lambda: {}, static_dir=str(tmp_path / "d"),
                     guest_key_file=str(key_file), guest_rate=tight)
    c = TestClient(app)
    assert [c.get("/guest", params={"k": "kk"}).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert c.get("/api/health").status_code == 200        # only guest paths are limited
    t[0] = 61
    assert c.get("/guest", params={"k": "kk"}).status_code == 200


def test_wrong_key_guesses_are_capped(env):
    client, _ = env
    codes = [client.get("/guest", params={"k": f"guess{i}"}).status_code for i in range(22)]
    assert codes[:20] == [404] * 20 and codes[20:] == [429, 429]


def test_stream_slots_cap_per_client():
    s = gv.StreamSlots(per_client=2, total=3)
    assert [s.take("a"), s.take("a"), s.take("a")] == [True, True, False]
    assert s.take("b") and not s.take("c")
    s.give("a")
    assert s.take("c")
