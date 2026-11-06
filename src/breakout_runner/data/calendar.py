"""D-03: sessions are weekdays minus holidays, plus listed special sessions."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path

from ..client.endpoints import Holiday, TapetideApi
from ..config.models import Data
from ..units import DAYS_PER_WEEK, LAST_DAY_OF_LAST_MONTH, LAST_MONTH, WEEKDAY_FRIDAY

ONE_DAY = dt.timedelta(days=1)
FULL_CLOSURE = "FULL"


def week_monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


class TradingCalendar:
    def __init__(self, holidays: Iterable[dt.date], special_sessions: Iterable[dt.date] = ()):
        self.holidays = frozenset(holidays)
        self.special_sessions = frozenset(special_sessions)

    def is_session(self, day: dt.date) -> bool:
        if day in self.special_sessions:
            return True
        return day.weekday() <= WEEKDAY_FRIDAY and day not in self.holidays

    def week_sessions(self, day: dt.date) -> list[dt.date]:
        monday = week_monday(day)
        days = (monday + dt.timedelta(days=i) for i in range(DAYS_PER_WEEK))
        return [d for d in days if self.is_session(d)]

    def last_weekday_session(self, day: dt.date) -> dt.date | None:
        """Last Mon-Fri session of the week. This decides the scan day and bar completeness:
        a weekend special session (Muhurat, budget day) cannot hold back Friday's scan,
        and cron only fires on weekdays anyway."""
        sessions = [d for d in self.week_sessions(day) if d.weekday() <= WEEKDAY_FRIDAY]
        return sessions[-1] if sessions else None

    def is_full_scan_day(self, day: dt.date) -> bool:
        return self.last_weekday_session(day) == day

    def next_session(self, day: dt.date) -> dt.date:
        day += ONE_DAY
        while not self.is_session(day):
            day += ONE_DAY
        return day

    def previous_session(self, day: dt.date) -> dt.date:
        day -= ONE_DAY
        while not self.is_session(day):
            day -= ONE_DAY
        return day

    def last_session_on_or_before(self, day: dt.date) -> dt.date:
        return day if self.is_session(day) else self.previous_session(day)

    def next_full_scan_day(self, day: dt.date) -> dt.date:
        day += ONE_DAY
        while not self.is_full_scan_day(day):
            day += ONE_DAY
        return day

    def window_end(self, signal_date: dt.date) -> dt.date:
        """S1-09: the session before the next full scan."""
        return self.previous_session(self.next_full_scan_day(signal_date))

    def sessions_elapsed(self, day: dt.date) -> int:
        return sum(1 for d in self.week_sessions(day) if d <= day)


def closed_days(rows: Iterable[Holiday]) -> list[dt.date]:
    return [dt.date.fromisoformat(r.date) for r in rows if (r.type or FULL_CLOSURE) == FULL_CLOSURE]


def load_calendar(api: TapetideApi, cache: Path, today: dt.date, cfg: Data) -> TradingCalendar:
    """Holidays cost a unit, so they are cached for calendar_cache_days."""
    if cache.exists():
        stored = json.loads(cache.read_text("utf-8"))
        age = (today - dt.date.fromisoformat(stored["fetched_on"])).days
        if 0 <= age <= cfg.calendar_cache_days:
            holidays = [dt.date.fromisoformat(d) for d in stored["holidays"]]
            return TradingCalendar(holidays, cfg.special_sessions)
    start = dt.date(today.year - 1, 1, 1)
    end = dt.date(today.year + 1, LAST_MONTH, LAST_DAY_OF_LAST_MONTH)
    holidays = closed_days(api.holidays(start, end))
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_on": today.isoformat(), "holidays": [d.isoformat() for d in holidays]}
    cache.write_text(json.dumps(payload), "utf-8")
    return TradingCalendar(holidays, cfg.special_sessions)
