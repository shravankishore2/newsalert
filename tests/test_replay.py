import math
from datetime import datetime, timezone

import yaml

from newsalert.replay import classify, percentile, render_results, replay, wilson
from newsalert.signals import Alert
from newsalert.store import Store

T0 = 1_700_000_000


def alert(direction=1, ref=100.0, price=103.0):
    return Alert("AAA", T0, direction, price / ref - 1, T0 - 900, ref, price)


def fwd(prices):
    return [(T0 + 60 * (i + 1), p) for i, p in enumerate(prices)]


def test_classify_false_true_and_unevaluable():
    w, gap = 1800, 600
    held = fwd([103.0] * 30)
    assert classify(alert(), held, w, 0.5, gap) is False
    # gives back exactly half (101.5) -> not "more than half"
    assert classify(alert(), fwd([101.5] + [103.0] * 29), w, 0.5, gap) is False
    assert classify(alert(), fwd([101.4] + [103.0] * 29), w, 0.5, gap) is True
    # down move reversing up
    assert classify(alert(-1, 100, 97), fwd([98.6] + [97.0] * 29), w, 0.5, gap) is True
    # window not covered (alert near the close)
    assert classify(alert(), fwd([103.0] * 10), w, 0.5, gap) is None
    assert classify(alert(), [], w, 0.5, gap) is None


def test_stats_helpers():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([5], 0.95) == 5
    lo, hi = wilson(10, 100)
    assert 0.05 < lo < 0.1 < hi < 0.18
    assert math.isnan(wilson(0, 0)[0])


def test_replay_end_to_end_writes_results(tmp_path):
    cfg = yaml.safe_load(open("config.yaml"))
    src = Store(":memory:")
    rows = []
    for i in range(300):
        ts = T0 + 60 * i
        spy = 400 * (1 + 0.0005 * math.sin(i))
        rows.append(("SPY", ts, spy))
        # AAA: sustained 3% climb at minute 120 that holds
        a = 100 * (1 + 0.001 * math.sin(i * 1.3)) * (1 + 0.003 * min(max(i - 120, 0), 10))
        rows.append(("AAA", ts, a))
        # BBB: one-minute 3% spike at 200 that fully reverses
        b = 50 * (1 + 0.001 * math.cos(i * 1.1)) * (1.03 if i == 200 else 1)
        rows.append(("BBB", ts, b))
    src.insert_bars(rows)
    sink = Store(tmp_path / "r.db")
    run_id, res = replay(src, "bars", cfg["alerts"], cfg["replay"], "SPY", sink)

    u, f = res["unfiltered"], res["filtered"]
    assert {a.symbol for a in u.alerts} == {"AAA", "BBB"}
    assert {a.symbol for a in f.alerts} == {"AAA"}   # MA filter drops the spike
    assert u.verdicts[("BBB", T0 + 60 * 200)] is True
    assert f.false == 0 and f.evaluable == 1
    assert len(f.latencies_ms) == 1 and f.latencies_ms[0] > 0
    assert len(sink.alerts(run_id, "unfiltered")) == 2

    text = render_results(source_desc="synthetic", summary=src.bar_summary("bars"), trading_days=1, results=res,
                          alerts_cfg=cfg["alerts"], replay_cfg=cfg["replay"], live_latencies=[],
                          min_sample=100, generated=datetime(2026, 9, 27, tzinfo=timezone.utc),
                          run_id=run_id, elapsed_s=1)
    assert "1/2 = 50.0%" in text                 # unfiltered false-alert rate
    assert "0/1 = 0.0%" in text                  # filtered
    assert text.index("## 1. Alert latency") < text.index("## 2. False-alert rate")
    assert "Sample-size warning" in text
    assert "Not measured" in text                # no live latency data
    assert "30 minutes" in text and "more than 50%" in text
