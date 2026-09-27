import json
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from newsalert.signals import Alert
from newsalert.store import Store
from newsalert.web.app import SiteInfo, create_app, parse_results
from newsalert.web.auth import COOKIE, PasswordCheck, SessionSigner

PW = "correct horse battery"
T0 = 1_790_000_000
PARAMS = {"move_threshold": 0.015, "move_window_min": 15, "ma_fast_min": 5, "ma_slow_min": 60,
          "ma_confirm_frac": 0.5, "corr_min": 0.6, "corr_returns": 30, "cooldown_min": 30}
TICKERS = {"ALPHA": {"name": "Alpha Industries Ltd.", "sector": "Metals"},
           "BETA": {"name": "Beta Bank Ltd.", "sector": "Financial Services"}}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def add_alert(store, symbol="ALPHA", ts=T0, direction=1, news=None, corr=0.8, residual=0.02):
    a = Alert(symbol, ts, direction, 0.03 * direction, ts - 900, 100.0, 100 * (1 + 0.03 * direction),
              index_move=0.01, corr=corr, beta=1.0, fast_sma=102.0, slow_sma=100.5, fast_move=0.02 * direction,
              residual=residual)
    aid = store.insert_alert(a, mode="demo", run_id="r", variant="filtered", received_ns=1, sent_ns=2, delivered=True)
    store.set_alert_news(aid, news or [])
    return aid


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "alerts.db")
    store = Store(db)
    results = tmp_path / "RESULTS.md"
    results.write_text(open("docs/RESULTS.md").read())
    clock = Clock()
    prices = {"ALPHA": [(T0 - 1800 + 60 * i, 100 + i * 0.1) for i in range(90)],
              "NIFTY50": [(T0 - 1800 + 60 * i, 25000.0) for i in range(90)]}
    market = {"open": True, "now_ts": T0 + 600, "simulated": True}
    app = create_app(db_path=db, info=SiteInfo("demo", "Test replay", "NIFTY 50", "NIFTY50", "₹", "Asia/Kolkata",
                                               TICKERS, PARAMS),
                     password=PW, prices=lambda s, a, b: [p for p in prices.get(s, []) if a < p[0] <= b],
                     market=lambda: market, results_path=str(results), static_dir=str(tmp_path / "dist"),
                     push_poll_s=0.02, clock=clock)
    return TestClient(app), store, clock


def login(client, pw=PW):
    return client.post("/api/login", json={"password": pw})


# --- password check ---------------------------------------------------------------

PROTECTED = ["/api/me", "/api/alerts", "/api/alerts/1", "/api/status", "/api/results", "/api/stream"]


@pytest.mark.parametrize("path", PROTECTED)
def test_every_data_endpoint_requires_login(env, path):
    client, _, _ = env
    assert client.get(path).status_code == 401


def test_login_sets_strict_httponly_cookie_and_grants_access(env):
    client, store, _ = env
    add_alert(store)
    r = login(client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and PW.lower() not in cookie
    assert client.get("/api/alerts").status_code == 200
    client.post("/api/logout")
    client.cookies.clear()
    assert client.get("/api/alerts").status_code == 401


def test_wrong_password_and_forged_or_expired_cookies_rejected(env):
    client, _, clock = env
    assert login(client, "nope").status_code == 401
    client.cookies.set(COOKIE, "eyJleHAiOiA5OTk5OTk5OTk5fQ.forged")
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    assert login(client).status_code == 204
    good = client.cookies.get(COOKIE)
    payload, mac = good.split(".")
    client.cookies.set(COOKIE, payload[:-2] + "AA." + mac)  # tampered payload
    assert client.get("/api/me").status_code == 401
    client.cookies.set(COOKIE, good)
    assert client.get("/api/me").status_code == 200
    clock.t += 13 * 3600  # past the 12 h session lifetime
    assert client.get("/api/me").status_code == 401


def test_lockout_after_repeated_failures(env):
    client, _, clock = env
    for _ in range(5):
        assert login(client, "guess").status_code == 401
    r = login(client)  # right password, but locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    clock.t += 61
    assert login(client).status_code == 204


def test_password_check_unit():
    c = Clock()
    pc = PasswordCheck("s3cret", max_failures=2, lockout_s=10, clock=c)
    assert pc.check("s3cret") and not pc.check("x") and not pc.check("y")
    assert pc.locked_for() == 10 and not pc.check("s3cret")
    with pytest.raises(ValueError):
        PasswordCheck("")
    s = SessionSigner(b"k" * 32, ttl_s=5, clock=c)
    tok = s.issue()
    assert s.valid(tok) and not SessionSigner(b"j" * 32, clock=c).valid(tok)


# --- data ------------------------------------------------------------------------------

def test_alert_list_filters_and_search(env):
    client, store, _ = env
    a1 = add_alert(store, "ALPHA", T0, 1, news=[{"source": "NSE", "headline": "Alpha wins large order",
                                                 "url": "https://x/a", "published": "p"}])
    a2 = add_alert(store, "BETA", T0 + 60, -1)
    a3 = add_alert(store, "ALPHA", T0 + 120, -1)
    login(client)
    ids = lambda **q: [i["id"] for i in client.get("/api/alerts", params=q).json()["items"]]
    assert ids() == [a3, a2, a1]
    assert ids(symbol="alpha") == [a3, a1]
    assert ids(sector="Financial Services") == [a2]
    assert ids(direction="up") == [a1]
    assert ids(q="large order") == [a1]          # matches news headline
    assert ids(q="beta bank") == [a2]            # matches company name
    page = client.get("/api/alerts", params={"limit": 2}).json()
    assert page["more"] and [i["id"] for i in page["items"]] == [a3, a2]
    assert ids(before_id=a2) == [a1]
    item = client.get("/api/alerts").json()["items"][-1]
    assert item["sector"] == "Metals" and item["name"] == "Alpha Industries Ltd."
    assert "corr 0.80" in item["reasons"]["corr"]["text"] and "rose 2.00%" in item["reasons"]["ma"]["text"]
    down = client.get("/api/alerts", params={"symbol": "BETA"}).json()["items"][0]
    assert "fell 2.00%" in down["reasons"]["ma"]["text"]
    assert not re.search(r"-\d", down["reasons"]["ma"]["text"] + down["reasons"]["corr"]["text"])  # real minus signs


def test_detail_has_context_clipped_to_now_and_news_without_article_text(env):
    client, store, _ = env
    aid = add_alert(store, news=[{"source": "BusinessLine", "headline": "H", "url": "https://x/1",
                                  "published": "p", "text": "FULL ARTICLE BODY"}])
    login(client)
    d = client.get(f"/api/alerts/{aid}").json()
    assert d["news"] == [{"headline": "H", "source": "BusinessLine", "url": "https://x/1", "published": "p"}]
    assert "FULL ARTICLE BODY" not in json.dumps(d)
    assert d["context"]["end"] == T0 + 600                   # clipped to the (simulated) current time
    assert all(t <= T0 + 600 for t, _ in d["context"]["prices"]) and d["context"]["index"]
    assert client.get("/api/alerts/999").status_code == 404


def test_status_and_results(env):
    client, store, _ = env
    store.set_status("cycle", {"at": T0, "ok": 501, "failed": 0})
    login(client)
    st = client.get("/api/status").json()
    assert st["mode"] == "demo" and st["market"]["open"] and st["cycle"]["value"]["ok"] == 501
    sections = {s["key"]: s for s in client.get("/api/results").json()["sections"]}
    nse = sections["nse"]["false_alert_rows"]
    assert [(r["false"], r["n"], r["rate"]) for r in nse] == [(2496, 9555, 26.1), (2050, 8190, 25.0)]
    us = sections["us"]["false_alert_rows"]
    assert [(r["variant"], r["false"], r["n"], r["rate"]) for r in us] == [
        ("Without filters", 761, 2715, 28.0), ("With MA + correlation filters", 637, 2402, 26.5)]


def test_parse_results_handles_missing_sections():
    assert parse_results("# Results\n") == []


# --- push channel ----------------------------------------------------------------------

def read_events(resp, n):
    """Parse SSE frames from a streaming response until n 'alert' events arrive."""
    events, cur = [], {}
    for line in resp.iter_lines():
        if line == "":
            if cur.get("event") == "alert":
                events.append(cur)
                if len(events) >= n:
                    break
            cur = {}
        elif not line.startswith(":") and ":" in line:
            k, v = line.split(":", 1)
            cur[k] = v.strip()
    return events


def test_stream_delivers_backlog_since_id(env):
    client, store, _ = env
    a1, a2 = add_alert(store), add_alert(store, "BETA", T0 + 60)
    login(client)
    with client.stream("GET", "/api/stream", params={"since": 0, "max_events": 3}) as r:  # 2 alerts + status
        assert r.headers["content-type"].startswith("text/event-stream")
        ev = read_events(r, 2)
    assert [int(e["id"].split(":p")[1]) for e in ev] == [a1, a2]
    assert json.loads(ev[1]["data"])["symbol"] == "BETA"


def test_stream_resumes_from_last_event_id(env):
    client, store, _ = env
    a1, a2 = add_alert(store), add_alert(store, "BETA", T0 + 60)
    login(client)
    with client.stream("GET", "/api/stream", params={"max_events": 2}, headers={"Last-Event-ID": str(a1)}) as r:
        ev = read_events(r, 1)
    assert int(ev[0]["id"].split(":p")[1]) == a2


def test_stream_pushes_alert_written_after_connect(env, tmp_path):
    client, store, _ = env
    add_alert(store)                                   # existing backlog is not replayed by default
    login(client)
    writer = Store(store.conn.execute("PRAGMA database_list").fetchone()[2])   # another connection, like live mode

    def later():
        time.sleep(0.2)
        add_alert(writer, "BETA", T0 + 300, -1)

    threading.Thread(target=later).start()
    start = time.monotonic()
    with client.stream("GET", "/api/stream", params={"max_events": 2}) as r:  # status, then the alert
        ev = read_events(r, 1)
    assert json.loads(ev[0]["data"])["symbol"] == "BETA" and ev[0]["event"] == "alert"
    assert time.monotonic() - start < 3


def test_stream_sends_status_events(env):
    client, store, _ = env
    login(client)
    got = None
    with client.stream("GET", "/api/stream", params={"max_events": 1}) as r:
        cur = {}
        for line in r.iter_lines():
            if line.startswith("event:"):
                cur["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and cur.get("event") == "status":
                got = json.loads(line.split(":", 1)[1])
                break
    assert got["mode"] == "demo" and "market" in got


# --- static frontend ---------------------------------------------------------------------

def test_spa_fallback_and_no_path_traversal(env, tmp_path):
    client, _, _ = env
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "app.js").write_text("js")
    (tmp_path / "secret.txt").write_text("nope")
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/alerts/12").text == "<html>app</html>"      # client-side route
    assert client.get("/app.js").text == "js"
    assert "nope" not in client.get("/../secret.txt").text
    assert "nope" not in client.get("/%2e%2e/secret.txt").text
    assert client.get("/api/nothing").status_code in (401, 404)


def test_close_sma_values_are_shown_distinctly():
    from newsalert.web.app import _pair
    assert _pair(60.4512, 60.4498) == ("60.451", "60.450")
    assert _pair(102.0, 100.5) == ("102.00", "100.50")


def test_token_status_falls_back_when_live_has_not_run(tmp_path):
    db = str(tmp_path / "a.db")
    Store(db)
    fb = {"value": {"state": "valid", "expires_at": "2026-09-28T22:03:00+05:30", "auto_refresh": True, "error": None},
          "updated_at": T0}
    app = create_app(db_path=db, info=SiteInfo("live", "t", "NIFTY 50", "NIFTY50", "₹", "Asia/Kolkata", TICKERS, PARAMS),
                     password=PW, prices=lambda *a: [], market=lambda: {"open": False}, static_dir=None,
                     token_fallback=lambda: fb)
    c = TestClient(app)
    login(c)
    assert c.get("/api/status").json()["token"] == fb
    Store(db).set_status("token", {"state": "error", "error": "x"})   # live-mode status wins once present
    assert c.get("/api/status").json()["token"]["value"]["state"] == "error"


# --- news-first -----------------------------------------------------------------------------

def add_news(store, nid, ticker="ALPHA", direction="up", event="order/contract win", created=T0, headline="Alpha wins order",
             relation="direct", source="businessline"):
    store.conn.execute("INSERT INTO news_alerts (id, item_id, created_at, published_at, source, event_type, confidence, "
                       "headline, url, classifier) VALUES (?,?,?,?,?,?,0.8,?,?,'gemini')",
                       (nid, nid, created, created - 120, source, event, headline, f"https://example.test/{nid}"))
    store.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason) "
                       "VALUES (?,?,?,?,'medium','big order')", (nid, ticker, relation, direction))
    store.conn.commit()


def test_news_list_filters_detail_and_links(env):
    client, store, _ = env
    add_news(store, 1, "ALPHA", "up", headline="Alpha wins order")
    add_news(store, 2, "BETA", "down", event="regulatory action", headline="Beta Bank penalised")
    pid = add_alert(store, "ALPHA", T0 + 1200)
    store.link_price_alert(pid, "ALPHA", T0 + 1200)
    login(client)
    ids = lambda **q: [i["id"] for i in client.get("/api/news", params=q).json()["items"]]
    assert ids() == [2, 1]
    assert ids(symbol="alpha") == [1] and ids(direction="down") == [2]
    assert ids(sector="Financial Services") == [2] and ids(event_type="regulatory action") == [2]
    assert ids(q="penalised") == [2] and ids(q="alpha industries") == [1]
    n = client.get("/api/news").json()["items"][1]
    assert n["latency_s"] == 120 and n["stocks"][0]["name"] == "Alpha Industries Ltd."
    assert n["linked_price_alerts"][0]["id"] == pid and n["linked_price_alerts"][0]["minutes_after"] == 20
    d = client.get("/api/news/1").json()
    assert d["context"]["symbol"] == "ALPHA" and d["context"]["prices"] and d["context"]["marker"] == T0
    price = client.get(f"/api/alerts/{pid}").json()
    assert price["linked_news"][0]["news_alert_id"] == 1 and price["linked_news"][0]["headline"] == "Alpha wins order"
    assert client.get("/api/news/99").status_code == 404
    assert "order/contract win" in client.get("/api/me").json()["event_types"]


def test_stream_pushes_news_and_resumes_both_cursors(env):
    client, store, _ = env
    add_news(store, 1)
    a1 = add_alert(store)
    login(client)
    with client.stream("GET", "/api/stream", params={"since": 0, "since_news": 0, "max_events": 3}) as r:
        evs, cur = [], {}
        for line in r.iter_lines():
            if line == "":
                if cur.get("event") in ("news", "alert"):
                    evs.append(cur)
                cur = {}
                if len(evs) == 2:
                    break
            elif ":" in line and not line.startswith(":"):
                k, v = line.split(":", 1)
                cur[k] = v.strip()
    assert [e["event"] for e in evs] == ["news", "alert"] and evs[1]["id"] == f"n1:p{a1}"
    add_news(store, 2, "BETA")
    with client.stream("GET", "/api/stream", params={"max_events": 1}, headers={"Last-Event-ID": f"n1:p{a1}"}) as r:
        got = None
        for line in r.iter_lines():
            if line.startswith("data:") and '"stocks"' in line:
                got = json.loads(line[5:])
                break
    assert got["id"] == 2 and got["stocks"][0]["ticker"] == "BETA"


def test_status_includes_news_service(env):
    client, store, _ = env
    store.set_status("news", {"last_poll_at": T0, "pending": 3, "gemini_configured": False})
    login(client)
    assert client.get("/api/status").json()["news"]["value"]["pending"] == 3
