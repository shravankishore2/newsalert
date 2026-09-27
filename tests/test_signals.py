import math

from newsalert.signals import Engine, Params

IDX = "SPY"
T0 = 1_700_000_000


def params(**kw):
    base = dict(move_window_min=15, move_threshold=0.015, max_ref_staleness_min=10, cooldown_min=30,
                warmup_returns=30, ma_fast_min=5, ma_slow_min=60, ma_confirm_frac=0.5,
                corr_returns=30, corr_min=0.6, index_max_staleness_min=3)
    base.update(kw)
    return Params(**base)


def feed(engine, minutes, tick, idx):
    """Feed one bar per minute; tick(i) and idx(i) give prices. Returns alerts."""
    out = []
    for i in range(minutes):
        ts = T0 + 60 * i
        engine.on_price(IDX, ts, idx(i))
        a = engine.on_price("AAA", ts, tick(i))
        if a:
            out.append((i, a))
    return out


def wiggle(i, amp=0.001):
    return 1 + amp * math.sin(i * 1.7)


def test_no_alert_below_threshold():
    e = Engine(params(), IDX)
    assert feed(e, 200, lambda i: 100 * wiggle(i), lambda i: 400 * wiggle(i + 3)) == []


def test_sustained_idiosyncratic_move_alerts_with_filters():
    e = Engine(params(), IDX)
    # flat for 100 min, then a steady 3% climb over 10 min that holds; index flat
    tick = lambda i: 100 * wiggle(i) * (1 + 0.003 * min(max(i - 100, 0), 10))
    alerts = feed(e, 140, tick, lambda i: 400 * wiggle(i + 3, 0.0003))
    assert len(alerts) == 1
    i, a = alerts[0]
    assert a.direction == 1 and a.move >= 0.015 and 100 <= i <= 115


def test_single_print_spike_blocked_by_ma_but_not_unfiltered():
    tick = lambda i: 100 * wiggle(i) * (1.03 if i == 100 else 1)
    idx = lambda i: 400 * wiggle(i + 3, 0.0003)
    assert feed(Engine(params(), IDX), 140, tick, idx) == []
    raw = feed(Engine(params(ma_enabled=False, corr_enabled=False), IDX), 140, tick, idx)
    assert [i for i, _ in raw] == [100]


def test_large_single_print_spike_passes_ma_filter():
    """Documented limit: the 5-min fast SMA absorbs a one-bar spike only up to about
    fast_min * confirm_frac * threshold (~3.75%). Bigger spikes pass the MA filter."""
    tick = lambda i: 100 * wiggle(i) * (1.05 if i == 100 else 1)
    idx = lambda i: 400 * wiggle(i + 3, 0.0003)
    assert [i for i, _ in feed(Engine(params(), IDX), 140, tick, idx)] == [100]


def test_market_wide_move_blocked_by_correlation():
    # ticker tracks the index 1:1 (plus tiny noise), then both rise 3%
    base = lambda i: wiggle(i, 0.002) * (1 + 0.003 * min(max(i - 100, 0), 10))
    tick = lambda i: 100 * base(i) * (1 + 0.0001 * math.cos(i))
    idx = lambda i: 400 * base(i)
    assert feed(Engine(params(), IDX), 140, tick, idx) == []
    raw = feed(Engine(params(ma_enabled=False, corr_enabled=False), IDX), 140, tick, idx)
    assert len(raw) == 1


def test_stale_reference_after_gap_no_alert():
    e = Engine(params(ma_enabled=False, corr_enabled=False), IDX)
    for i in range(60):
        e.on_price("AAA", T0 + 60 * i, 100.0 + 0.01 * (i % 2))
    # 3-hour hole (failed fetches / overnight), then a price 5% higher: reference is stale
    assert e.on_price("AAA", T0 + 60 * 60 + 3 * 3600, 105.0) is None


def test_stale_index_skips_filtered_alert():
    e = Engine(params(), IDX)
    tick = lambda i: 100 * wiggle(i) * (1 + 0.003 * min(max(i - 100, 0), 10))
    for i in range(141):
        ts = T0 + 60 * i
        if i < 90:  # index feed stops at minute 90
            e.on_price(IDX, ts, 400 * wiggle(i + 3, 0.0003))
        assert e.on_price("AAA", ts, tick(i)) is None


def test_cooldown_limits_repeat_alerts():
    e = Engine(params(ma_enabled=False, corr_enabled=False, cooldown_min=30), IDX)
    tick = lambda i: 100 * (1 + 0.004 * max(i - 40, 0))  # keeps rising
    alerts = feed(e, 120, tick, lambda i: 400.0)
    gaps = [b[0] - a[0] for a, b in zip(alerts, alerts[1:])]
    assert alerts and all(g >= 30 for g in gaps)


def test_out_of_order_or_duplicate_updates_ignored():
    e = Engine(params(), IDX)
    e.on_price("AAA", T0, 100.0)
    s = e.series["AAA"]
    e.on_price("AAA", T0, 150.0)
    e.on_price("AAA", T0 - 60, 150.0)
    assert s.px == [100.0]


def test_filter_rejection_starts_cooldown():
    """A spike the MA filter rejects must not re-fire a few bars later as a new alert."""
    e = Engine(params(corr_enabled=False), IDX)
    # 3% spike at minute 100, then the price stays up from 101 onwards
    tick = lambda i: 100 * wiggle(i) * (1.03 if i >= 100 else 1)
    alerts = feed(e, 125, tick, lambda i: 400.0)
    assert e.rejected >= 1
    assert alerts == []  # without the cooldown-on-reject, this fires a few bars later


def test_correlation_evaluable_at_live_cadence():
    """Regression: at live cadence the index updates ~5x/min but each ticker only every
    ~10 min, so 30 ticker returns span ~5 h. The index series used to be pruned to ~2 h,
    which made the correlation filter unevaluable and the filtered engine silent."""
    e = Engine(params(), IDX)
    for k in range(int(6.5 * 3600 / 12)):          # one session, index every 12 s
        ts = T0 + 12 * k
        e.on_price(IDX, ts, 400 * wiggle(k, 0.0003))
        if k % 50 == 0:                             # ticker every 600 s
            e.on_price("AAA", ts, 100 * wiggle(k))
    assert len(e.series["AAA"].ts) > 31
    assert e._returns(e.series["AAA"], 30) is not None
