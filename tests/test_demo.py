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
    rows = out.conn.execute("SELECT mode, symbol, news, fast_sma FROM alerts ORDER BY id").fetchall()
    assert rows and all(r[0] == "demo" and r[1] == "ALPHA" and r[2] == "[]" and r[3] is not None for r in rows)
    st = out.get_status()
    assert st["mode"]["value"]["mode"] == "demo" and st["replay"]["value"]["finished"]
    assert st["replay"]["value"]["dataset"] == "Synthetic test replay"
    assert st["token"]["value"]["state"] == "not used"
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
