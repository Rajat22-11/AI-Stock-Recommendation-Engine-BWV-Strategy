"""Daily bars become weekly bars here: real session counts, corrected volume, quality flags."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..client.endpoints import PriceBar, TapetideApi
from ..config.models import Data, Indicators
from ..units import DAYS_PER_WEEK
from .calendar import TradingCalendar, week_monday

CORPORATE_ACTION = "corporate_action_in_window"
RAW_MISSING = "raw_missing"
HISTORY_SHORT = "history_short"
INSUFFICIENT = "insufficient"
OK = "ok"


@dataclass(frozen=True)
class DailyBar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    raw_close: float | None
    volume: float  # in today's share terms when a corporate action was corrected
    raw_volume: int


@dataclass(frozen=True)
class WeeklyBar:
    week: dt.date  # Monday, matching Tapetide's own weekly label
    open: float
    high: float
    low: float
    close: float
    volume: float
    sessions: int
    last_date: dt.date
    complete: bool = True

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class SymbolHistory:
    symbol: str
    daily: list[DailyBar]
    weekly: list[WeeklyBar]
    data_through: dt.date | None
    final: bool
    quality: str = OK
    flags: set[str] = field(default_factory=set)

    @property
    def completed(self) -> list[WeeklyBar]:
        return [w for w in self.weekly if w.complete]

    @property
    def partial(self) -> WeeklyBar | None:
        return next((w for w in reversed(self.weekly) if not w.complete), None)


def fetch_daily_bars(api: TapetideApi, symbol: str, days: int, today: dt.date) -> list[PriceBar]:
    """Page backwards until the window is covered or the stock's history ends."""
    wanted_start = today - dt.timedelta(days=days)
    rows: dict[str, PriceBar] = {}
    remaining = days
    end_date: str | None = None
    while True:
        page = api.price(symbol, remaining, end_date)
        rows.update({bar.date: bar for bar in page.bars})
        if not (page.bars and page.has_more and page.next_cursor and page.first_date):
            break
        # A full window ends within a week of its start (weekends and holidays).
        slack = dt.date.fromisoformat(page.first_date) - wanted_start
        if slack.days <= DAYS_PER_WEEK:
            break
        end_date = page.next_cursor
        remaining = (dt.date.fromisoformat(end_date) - wanted_start).days
        if remaining <= 0:
            break
    return [rows[d] for d in sorted(rows)]


def _usable(bar: PriceBar) -> bool:
    return None not in (bar.open, bar.high, bar.low, bar.close, bar.volume)


def _factor(bar: PriceBar) -> float | None:
    if bar.raw_close is None or bar.close in (None, 0):
        return None  # null means no raw print: never substitute close
    return bar.raw_close / bar.close


def _window_start(last: dt.date, weeks: int) -> dt.date:
    return week_monday(last) - dt.timedelta(days=weeks * DAYS_PER_WEEK)


def correct_volumes(
    bars: list[PriceBar], data: Data, ind: Indicators
) -> tuple[list[DailyBar], set[str]]:
    """D-08. Prices are back-adjusted but volume is as printed, so a split or bonus inside the
    baseline fakes a volume multiple. Express old volume in today's share terms."""
    flags: set[str] = set()
    usable = [b for b in bars if _usable(b)]
    if not usable:
        return [], flags
    last = dt.date.fromisoformat(usable[-1].date)

    baseline_from = _window_start(last, ind.volume_baseline_weeks).isoformat()
    if any(b.raw_close is None and b.date >= baseline_from for b in usable):
        flags.add(RAW_MISSING)

    window_weeks = max(ind.pivot_lookback_weeks, ind.volume_baseline_weeks)
    window_from = _window_start(last, window_weeks).isoformat()
    factors = [f for b in usable if b.date >= window_from and (f := _factor(b)) is not None]
    latest = next((f for b in reversed(usable) if (f := _factor(b)) is not None), None)
    adjust = False
    if factors and min(factors) > 0:
        spread_pct = (max(factors) - min(factors)) / min(factors) * 100
        adjust = spread_pct > data.corp_action_tolerance_pct
    if adjust:
        flags.add(CORPORATE_ACTION)

    daily = []
    for bar in usable:
        factor = _factor(bar)
        scale = factor / latest if adjust and factor is not None and latest else 1
        daily.append(
            DailyBar(
                date=dt.date.fromisoformat(bar.date),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                raw_close=bar.raw_close,
                volume=bar.volume * scale,
                raw_volume=bar.volume,
            )
        )
    return daily, flags


def resample_weekly(daily: list[DailyBar], calendar: TradingCalendar) -> list[WeeklyBar]:
    """Group by Monday-based week. Sessions are the rows that exist, not a calendar guess."""
    groups: dict[dt.date, list[DailyBar]] = {}
    for bar in daily:
        groups.setdefault(week_monday(bar.date), []).append(bar)
    data_through = daily[-1].date if daily else None
    weekly = []
    for monday in sorted(groups):
        rows = groups[monday]
        last_session = calendar.last_weekday_session(monday)
        weekly.append(
            WeeklyBar(
                week=monday,
                open=rows[0].open,
                high=max(r.high for r in rows),
                low=min(r.low for r in rows),
                close=rows[-1].close,
                volume=sum(r.volume for r in rows),
                sessions=len(rows),
                last_date=rows[-1].date,
                complete=last_session is None or data_through >= last_session,
            )
        )
    return weekly


def build_history(
    symbol: str,
    bars: list[PriceBar],
    calendar: TradingCalendar,
    asof: dt.date,
    data: Data,
    ind: Indicators,
) -> SymbolHistory:
    daily, flags = correct_volumes(bars, data, ind)
    weekly = resample_weekly(daily, calendar)
    data_through = daily[-1].date if daily else None
    history = SymbolHistory(
        symbol=symbol,
        daily=daily,
        weekly=weekly,
        data_through=data_through,
        final=data_through is not None and data_through >= asof,
        flags=flags,
    )
    if data_through is not None:
        baseline_from = _window_start(data_through, ind.volume_baseline_weeks).isoformat()
        nulls = sum(1 for b in bars if not _usable(b) and b.date >= baseline_from)
        if nulls > data.max_null_bars:
            history.quality = INSUFFICIENT
    if not daily:
        history.quality = INSUFFICIENT
    elif history.quality == OK and len(history.completed) < ind.min_pivot_weeks:
        history.quality = HISTORY_SHORT
        history.flags.add(HISTORY_SHORT)
    return history
