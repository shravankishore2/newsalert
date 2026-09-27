"""NSE trading calendar: session hours in IST, weekends and trading holidays."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class MarketCalendar:
    def __init__(self, holidays: dict[int, list[date]], open_t: time = time(9, 15),
                 close_t: time = time(15, 30), tz: ZoneInfo = IST):
        """holidays: {year: [dates]} for the years the calendar knows about."""
        self.holidays = {d for ds in holidays.values() for d in ds}
        self.years = set(holidays)
        self.open_t, self.close_t, self.tz = open_t, close_t, tz
        self._warned: set[int] = set()

    @classmethod
    def from_config(cls, m: dict) -> "MarketCalendar":
        hol = {int(y): [date.fromisoformat(str(d)) for d in ds] for y, ds in (m.get("holidays") or {}).items()}
        return cls(hol, time.fromisoformat(m["open"]), time.fromisoformat(m["close"]), ZoneInfo(m["timezone"]))

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if d.year not in self.years and d.year not in self._warned:
            self._warned.add(d.year)
            log.warning("no NSE holiday list for %d in config.yaml; treating all weekdays as trading days", d.year)
        return d not in self.holidays

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return self.is_trading_day(local.date()) and self.open_t <= local.time() < self.close_t

    def session(self, d: date) -> tuple[datetime, datetime]:
        return (datetime.combine(d, self.open_t, self.tz), datetime.combine(d, self.close_t, self.tz))

    def next_trading_day(self, d: date) -> date:
        """First trading day on or after d."""
        for _ in range(30):
            if self.is_trading_day(d):
                return d
            d += timedelta(days=1)
        raise RuntimeError("no trading day within 30 days; check the holiday list")

    def next_open(self, now: datetime) -> datetime:
        """Start of the current session if open now, else the next session's open."""
        local = now.astimezone(self.tz)
        d = local.date()
        if self.is_trading_day(d) and local.time() < self.close_t:
            start, _ = self.session(d)
            return max(start, local) if local.time() >= self.open_t else start
        return self.session(self.next_trading_day(d + timedelta(days=1)))[0]
