import logging
from datetime import date, datetime, time, timezone

import yaml

from newsalert.market import IST, MarketCalendar

CAL = MarketCalendar.from_config(yaml.safe_load(open("config.yaml"))["market"])


def ist(*a):
    return datetime(*a, tzinfo=IST)


def test_holidays_and_weekends_are_not_trading_days():
    assert CAL.is_trading_day(date(2026, 10, 1))        # Thursday
    assert not CAL.is_trading_day(date(2026, 10, 2))    # Gandhi Jayanti (Friday)
    assert not CAL.is_trading_day(date(2026, 10, 3))    # Saturday
    assert not CAL.is_trading_day(date(2026, 11, 10))   # Diwali-Balipratipada
    assert not CAL.is_trading_day(date(2026, 12, 25))   # Christmas


def test_session_bounds_in_ist():
    assert not CAL.is_open(ist(2026, 10, 1, 9, 14, 59))
    assert CAL.is_open(ist(2026, 10, 1, 9, 15))
    assert CAL.is_open(ist(2026, 10, 1, 15, 29, 59))
    assert not CAL.is_open(ist(2026, 10, 1, 15, 30))
    assert not CAL.is_open(ist(2026, 10, 2, 11, 0))     # holiday, mid-session time
    # instants given in UTC are converted: 05:00 UTC = 10:30 IST, 10:05 UTC = 15:35 IST
    assert CAL.is_open(datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc))
    assert not CAL.is_open(datetime(2026, 10, 1, 10, 5, tzinfo=timezone.utc))


def test_next_open_skips_holiday_and_weekend():
    # Thursday after the close -> Friday is a holiday, then the weekend -> Monday 09:15
    assert CAL.next_open(ist(2026, 10, 1, 16, 0)) == ist(2026, 10, 5, 9, 15)
    assert CAL.next_open(ist(2026, 10, 1, 8, 0)) == ist(2026, 10, 1, 9, 15)
    during = ist(2026, 10, 1, 11, 0)
    assert CAL.next_open(during) == during


def test_unknown_year_warns_and_assumes_weekdays_trade(caplog):
    cal = MarketCalendar({2026: []}, time(9, 15), time(15, 30))
    with caplog.at_level(logging.WARNING):
        assert cal.is_trading_day(date(2027, 1, 26))    # would be Republic Day, but 2027 isn't configured
        assert cal.is_trading_day(date(2027, 1, 27))
    assert sum("no NSE holiday list for 2027" in r.getMessage() for r in caplog.records) == 1


def _run_window(monkeypatch, now):
    import newsalert.__main__ as m

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now

    monkeypatch.setattr(m, "datetime", FixedDT)
    return m.main(["is-trading-window", "--until", "15:35"])


def test_trading_window_for_systemd(monkeypatch):
    assert _run_window(monkeypatch, ist(2026, 9, 28, 9, 10)) == 0     # Monday before the open
    assert _run_window(monkeypatch, ist(2026, 9, 28, 15, 36)) == 1    # after the cutoff
    assert _run_window(monkeypatch, ist(2026, 10, 2, 9, 10)) == 1     # Gandhi Jayanti holiday
    assert _run_window(monkeypatch, ist(2026, 10, 3, 9, 10)) == 1     # Saturday
