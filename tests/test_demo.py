import math
from datetime import datetime

import yaml

from newsalert.demo import MAX_GAP_SLEEP_S, DemoDriver
from newsalert.market import IST
from newsalert.store import Store

CFG = yaml.safe_load(open("config.yaml"))
DATASET = {"label": "Synthetic test replay", "timezone": "Asia/Kolkata", "index_symbol": "NIFTY50"}


def build_history(path, days=2):
    s = Store(path)
    rows = []
    for d in range(days):
        start = int(datetime(2026, 9, 21 + d, 9, 15, tzinfo=IST).timestamp())
        for i in range(375):
            ts = start + 60 * i
            rows.append(("NIFTY50", ts, 25000 * (1 + 0.0005 * math.sin(i))))
            # ALPHA: sustained 3% climb at 11:15 each day that holds
            rows.append(("ALPHA", ts, 100 * (1 + 0.001 * math.sin(i * 1.3)) * (1 + 0.003 * min(max(i - 120, 0), 10))))
    s.insert_bars(rows)
    return s


class Sleeper:
    def __init__(self):
        self.total = 0.0
        self.calls = []

    async def __call__(self, s):
        self.calls.append(s)
        self.total += s


async def test_demo_replays_into_labelled_demo_db(tmp_path):
    build_history(str(tmp_path / "h.db"))
    sleep = Sleeper()
    d = DemoDriver(history_db=str(tmp_path / "h.db"), demo_db=str(tmp_path / "demo.db"), dataset=DATASET,
                   alerts_cfg=CFG["alerts"], speed=60, warm_days=1, sleep=sleep)
    await d.run()
    out = Store(str(tmp_path / "demo.db"))
    rows = out.conn.execute("SELECT mode, symbol, fast_sma FROM alerts ORDER BY id").fetchall()
    assert rows and all(r[0] == "demo" and r[1] == "ALPHA" and r[2] is not None for r in rows)
    st = out.get_status()
    assert st["mode"]["value"]["mode"] == "demo" and st["replay"]["value"]["finished"]
    assert st["replay"]["value"]["dataset"] == "Synthetic test replay"
    assert st["token"]["value"]["state"] == "not used"
    assert st["news"]["value"]["archived_alerts"] == 0
    # day 1 replays instantly; day 2 is paced at 60x (374 one-minute steps = 374 s) plus one capped overnight gap
    assert max(sleep.calls) <= MAX_GAP_SLEEP_S
    assert abs(sleep.total - (374 + MAX_GAP_SLEEP_S)) < 1e-6


async def test_demo_prices_never_show_the_future(tmp_path):
    build_history(str(tmp_path / "h.db"), days=1)
    d = DemoDriver(history_db=str(tmp_path / "h.db"), demo_db=str(tmp_path / "demo.db"), dataset=DATASET,
                   alerts_cfg=CFG["alerts"], warm_days=0, sleep=Sleeper())
    start = int(datetime(2026, 9, 21, 9, 15, tzinfo=IST).timestamp())
    d.sim_ts = start + 600
    pts = d.prices("ALPHA", start, start + 3600)
    assert pts and pts[-1][0] == start + 600
    assert d.market_state()["simulated"] is True


async def test_demo_db_is_rebuilt_each_start(tmp_path):
    build_history(str(tmp_path / "h.db"), days=1)
    stale = Store(str(tmp_path / "demo.db"))
    stale.set_status("mode", {"mode": "live"})
    stale.close()
    DemoDriver(history_db=str(tmp_path / "h.db"), demo_db=str(tmp_path / "demo.db"), dataset=DATASET,
               alerts_cfg=CFG["alerts"], sleep=Sleeper())
    assert Store(str(tmp_path / "demo.db")).get_status() == {}


async def test_demo_replays_archived_news_with_prices_on_archive_days(tmp_path):
    build_history(str(tmp_path / "h.db"), days=3)                      # 21, 22, 23 Sep
    archive = Store(str(tmp_path / "live.db"))
    day3_1100 = datetime(2026, 9, 23, 11, 0, tzinfo=IST).timestamp()  # ALPHA climbs from 11:15 each day
    archive.conn.execute("INSERT INTO news_alerts (id, item_id, mode, created_at, published_at, source, event_type, "
                         "confidence, headline, url, classifier) VALUES (1, 1, 'live', ?, ?, 'businessline', "
                         "'order/contract win', 0.8, 'Alpha wins order', 'https://example.test/a', 'gemini')",
                         (day3_1100, day3_1100 - 60))
    archive.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction, strength, reason) "
                         "VALUES (1, 'ALPHA', 'direct', 'up', 'medium', 'order')")
    archive.conn.commit()
    sleep = Sleeper()
    d = DemoDriver(history_db=str(tmp_path / "h.db"), demo_db=str(tmp_path / "demo.db"), dataset=DATASET,
                   alerts_cfg=CFG["alerts"], speed=60, sleep=sleep, news_db=str(tmp_path / "live.db"))
    assert sorted(str(x) for x in d.days) == ["2026-09-22", "2026-09-23"]      # news day + one warm-up day
    await d.run()
    out = Store(str(tmp_path / "demo.db"))
    news = out.conn.execute("SELECT id, mode, headline, created_at FROM news_alerts").fetchall()
    assert news == [(1, "demo", "Alpha wins order", day3_1100)]
    day3 = [r for r in out.conn.execute("SELECT id, ts FROM alerts WHERE symbol='ALPHA'")
            if datetime.fromtimestamp(r[1], IST).date().day == 23]
    links = out.conn.execute("SELECT price_alert_id, news_alert_id FROM price_news_links").fetchall()
    assert day3 and links == [(day3[0][0], 1)]
    assert out.get_status()["replay"]["value"]["news_archive"]["emitted"] == 1
    assert all(datetime.fromtimestamp(t, IST).date().day != 21 for (t,) in out.conn.execute("SELECT ts FROM alerts"))


async def test_demo_copies_items_and_action_filings_for_the_boards(tmp_path):
    import json
    build_history(str(tmp_path / "h.db"), days=2)                       # 21, 22 Sep
    archive = Store(str(tmp_path / "live.db"))
    t = datetime(2026, 9, 22, 11, 0, tzinfo=IST).timestamp()
    archive.conn.execute("INSERT INTO news_items (id, source, feed, key, url, headline, published_at, fetched_at, status, "
                         "classifier, classification) VALUES (5, 'businessline', 'f', 'k5', 'https://example.test/5', "
                         "'Alpha Q2 profit up 12%', ?, ?, 'classified', 'gemini', ?)",
                         (t - 60, t, json.dumps({"event_type": "results", "confidence": 0.8, "affected": [],
                                                 "results": {"profit_yoy_pct": 12}})))
    archive.conn.execute("INSERT INTO news_alerts (id, item_id, mode, created_at, published_at, source, event_type, confidence, "
                         "headline, url, classifier) VALUES (1, 5, 'live', ?, ?, 'businessline', 'results', 0.8, "
                         "'Alpha Q2 profit up 12%', 'https://example.test/5', 'gemini')", (t, t - 60))
    archive.conn.execute("INSERT INTO news_alert_stocks (news_alert_id, ticker, relation, direction) VALUES (1,'ALPHA','direct','up')")
    archive.conn.execute("INSERT INTO news_items (source, feed, key, url, symbol_hint, label, action_kind, action_date, "
                         "published_at, fetched_at, status) VALUES ('nse','f','k9','https://example.test/9','ALPHA',"
                         "'Dividend','dividend','2026-10-15',?,?,'skipped')", (t + 600, t + 600))
    archive.conn.commit()
    d = DemoDriver(history_db=str(tmp_path / "h.db"), demo_db=str(tmp_path / "demo.db"), dataset=DATASET,
                   alerts_cfg=CFG["alerts"], sleep=Sleeper(), news_db=str(tmp_path / "live.db"))
    await d.run()
    out = Store(str(tmp_path / "demo.db"))
    item_id = out.conn.execute("SELECT item_id FROM news_alerts").fetchone()[0]
    cls = json.loads(out.conn.execute("SELECT classification FROM news_items WHERE id=?", (item_id,)).fetchone()[0])
    assert cls["results"]["profit_yoy_pct"] == 12
    assert out.conn.execute("SELECT symbol_hint, action_kind, action_date FROM news_items WHERE action_kind IS NOT NULL"
                            ).fetchall() == [("ALPHA", "dividend", "2026-10-15")]
