"""Mid-week check-in: provisional, week-to-date, reading Friday's rows instead of a file."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import median
from typing import Any

from ..client.endpoints import SignalRow
from ..config.models import Config
from ..data.bars import DailyBar, WeeklyBar
from ..indicators.weekly import Metrics, close_location
from .types import Decision, SetupKind, Verdict

CARRIED = {
    "ENTRY": Verdict.WAIT_ALERT,
    "ENTRY_REDUCED": Verdict.WAIT_ALERT,
    "WAIT_ALERT": Verdict.WAIT_ALERT,
    "WAIT": Verdict.WAIT,
}


@dataclass(frozen=True)
class FridayItem:
    signal_id: int
    symbol: str
    verdict: str
    signal_date: dt.date
    trigger: float | None
    stop: float | None
    req_vol_abs: int | None
    setup: str | None = None


def read_friday_state(
    rows: list[dict[str, Any]], today: dt.date, excluded_signal_ids: set[int], max_age_days: int
) -> tuple[list[FridayItem], bool]:
    """Latest full-scan rows, newest per symbol. Returns (items, stale)."""
    usable = [r for r in rows if r["id"] not in excluded_signal_ids]
    if not usable:
        return [], True
    latest = max(r["signal_date"] for r in usable)
    if (today - latest).days > max_age_days:
        return [], True
    items: dict[str, FridayItem] = {}
    for row in sorted(usable, key=lambda r: r["id"]):
        if row["signal_date"] != latest or row["verdict"] not in CARRIED:
            continue
        items[row["symbol"]] = FridayItem(
            signal_id=row["id"],
            symbol=row["symbol"],
            verdict=row["verdict"],
            signal_date=row["signal_date"],
            trigger=row.get("alert_trigger") or row.get("pivot"),
            stop=row.get("stop"),
            req_vol_abs=row.get("req_vol_abs"),
            setup=row.get("setup"),
        )
    return list(items.values()), False


@dataclass(frozen=True)
class WeekToDate:
    high: float
    low: float
    close: float
    volume: float
    sessions: int
    to_trigger_pct: float | None
    to_stop_pct: float | None
    crossed: bool
    crossed_on_multiple: float | None
    pct_of_required: float | None
    projected_pct_of_required: float | None
    near_trigger: bool
    distribution: list[str]


def distribution_flags(
    daily: list[DailyBar], week: WeeklyBar, trigger: float | None, config: Config
) -> list[str]:
    cfg = config.midweek.distribution
    flags = []
    this_week = [b for b in daily if b.date >= week.week]
    earlier = [b for b in daily if b.date < week.week][-cfg.daily_baseline_sessions :]
    if earlier and this_week:
        typical = median(b.volume for b in earlier)
        closes = [earlier[-1].close, *(b.close for b in this_week)]
        heavy = sum(
            1
            for previous, bar in zip(closes, this_week, strict=False)
            if bar.close < previous and bar.volume >= cfg.heavy_day_volume_multiple * typical
        )
        if heavy >= cfg.heavy_down_days_flag:
            flags.append(f"{heavy} heavy down days")
    loc = close_location(week)
    if (
        trigger is not None
        and week.high > trigger
        and loc is not None
        and loc <= cfg.week_close_location_max
    ):
        flags.append("close near the week's low after a breakout")
    return flags


def week_to_date(
    item: FridayItem, week: WeeklyBar, metrics: Metrics, daily: list[DailyBar], config: Config
) -> WeekToDate:
    trigger, stop = item.trigger, item.stop
    required = item.req_vol_abs
    base = config.data.session_base
    pct = projected = None
    if required:
        pct = week.volume / required * 100
        projected = week.volume * base / week.sessions / required * 100
    crossed = trigger is not None and week.close > trigger
    to_trigger = (trigger - week.close) / trigger * 100 if trigger else None
    return WeekToDate(
        high=week.high,
        low=week.low,
        close=week.close,
        volume=week.volume,
        sessions=week.sessions,
        to_trigger_pct=to_trigger,
        to_stop_pct=(week.close - stop) / week.close * 100 if stop else None,
        crossed=crossed,
        crossed_on_multiple=metrics.vol_mult if crossed else None,
        pct_of_required=pct,
        projected_pct_of_required=projected,
        near_trigger=(
            to_trigger is not None and 0 <= to_trigger <= config.midweek.near_trigger_pct
        ),
        distribution=distribution_flags(daily, week, trigger, config),
    )


def evaluate_midweek(
    item: FridayItem,
    week: WeeklyBar,
    metrics: Metrics,
    daily: list[DailyBar],
    signal_date: dt.date,
    config: Config,
    liquidity_tier: str | None = None,
) -> Decision:
    """Always provisional and labelled. Friday's own row is never touched."""
    wtd = week_to_date(item, week, metrics, daily, config)
    decimals = config.output.price_decimals

    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.{decimals}f}"

    def rnd(value: float | None) -> float | None:
        return None if value is None else round(value, decimals)

    label = f"{config.data.partial_week_label} ({wtd.sessions} sessions)"
    projected = wtd.projected_pct_of_required
    if wtd.crossed and projected is not None and projected >= 100:
        verdict, what = Verdict.ENTRY, f"Crossed {fmt(item.trigger)} on pace for the volume."
    elif wtd.crossed:
        verdict, what = Verdict.WAIT, f"Crossed {fmt(item.trigger)} but volume is behind pace."
    elif wtd.near_trigger:
        verdict = Verdict.WAIT_ALERT
        what = f"Within {fmt(wtd.to_trigger_pct)}% of {fmt(item.trigger)}: set the alert."
    else:
        verdict = CARRIED[item.verdict]
        what = f"{fmt(wtd.to_trigger_pct)}% from the trigger {fmt(item.trigger)}."
    notes = (
        f"{label}, provisional, confirm on the weekly close. {what} "
        f"Week high {fmt(wtd.high)}, low {fmt(wtd.low)}, last {fmt(wtd.close)}. "
        f"Volume {fmt(wtd.pct_of_required)}% of required, projected {fmt(projected)}%."
    )
    if wtd.distribution:
        notes = f"{notes} Distribution: {'; '.join(wtd.distribution)}."
    risk = None
    if item.stop and wtd.close > item.stop:
        risk = (wtd.close - item.stop) / wtd.close * 100
    return Decision(
        symbol=item.symbol,
        signal_date=signal_date,
        verdict=verdict,
        provisional=True,
        setup=item.setup or SetupKind.OTHER.value,
        stage=1,
        notes=notes,
        raw={
            "rules_fired": ["O-05", "I-17"],
            "friday_signal_id": item.signal_id,
            "sessions_elapsed": wtd.sessions,
            "to_trigger_pct": rnd(wtd.to_trigger_pct),
            "to_stop_pct": rnd(wtd.to_stop_pct),
            "pct_of_required": rnd(wtd.pct_of_required),
            "projected_pct_of_required": rnd(projected),
            "crossed": wtd.crossed,
            "distribution": wtd.distribution,
        },
        signal_close=wtd.close,
        pivot=metrics.pivot,
        alert_trigger=item.trigger,
        stop=item.stop,
        stop_basis="cap8" if item.stop else None,
        risk_pct=rnd(risk),
        vol_mult=rnd(metrics.vol_mult),
        req_vol_abs=item.req_vol_abs,
        sma13=rnd(metrics.sma13),
        sma13_rising=metrics.sma13_rising,
        dist_sma_pct=rnd(metrics.dist_sma_pct),
        close_loc=rnd(metrics.close_loc),
        liquidity_tier=liquidity_tier,
    )


def new_high_names(rows: list[SignalRow], tracked: set[str]) -> list[str]:
    """52-week-high names not on Friday's list, for the summary, marked as such."""
    return sorted({r.symbol for r in rows if r.symbol not in tracked})
