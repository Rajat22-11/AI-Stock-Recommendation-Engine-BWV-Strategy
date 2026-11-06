"""Stages 2-4 (S2, S3, S4 rules) for paper positions read from the tracker.

The ledger owns the mechanics: it stops out on the daily low and trims at fill x 1.25 by
itself, so this module only emits signals, and HOLD rows carry a null stop because any
higher `signals.stop` would raise the position's stop.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import mean
from typing import Any

from ..config.models import Add, Config, Exit
from ..data.bars import WeeklyBar
from ..indicators.weekly import (
    Metrics,
    baseline_of,
    close_location,
    dist_pct,
    higher_low_line,
    normalised_volume,
    sma_of,
)
from ..units import STAGE_ADD, STAGE_EXIT, STAGE_TRIM
from .types import Decision, SetupKind, Verdict

OPEN = "open"
PENDING = "pending"


@dataclass(frozen=True)
class Position:
    position_id: int
    signal_id: int
    symbol: str
    status: str
    pivot: float | None = None
    fill_date: dt.date | None = None
    fill_price: float | None = None
    qty_initial: int | None = None
    qty_open: int = 0
    avg_cost: float | None = None
    stop_current: float | None = None
    trim_price: float | None = None
    trimmed: bool = False


def scope_positions(
    rows: list[dict[str, Any]], book_started_on: dt.date, excluded_signal_ids: set[int]
) -> list[Position]:
    """P-01: only the current book. Pilot-book signals are not ours to manage."""
    positions = []
    for row in rows:
        if row["status"] not in (OPEN, PENDING):
            continue
        if row["signal_id"] in excluded_signal_ids:
            continue
        signal_date = row.get("signal_date")
        if signal_date is not None and signal_date < book_started_on:
            continue
        positions.append(
            Position(
                position_id=row["position_id"],
                signal_id=row["signal_id"],
                symbol=row["symbol"],
                status=row["status"],
                pivot=row.get("pivot"),
                fill_date=row.get("fill_date"),
                fill_price=row.get("fill_price"),
                qty_initial=row.get("qty_initial"),
                qty_open=row.get("qty_open") or 0,
                avg_cost=row.get("avg_cost"),
                stop_current=row.get("stop_current"),
                trim_price=row.get("trim_price"),
                trimmed=bool(row.get("trimmed")),
            )
        )
    return positions


@dataclass(frozen=True)
class HoldingInputs:
    position: Position
    bars: list[WeeklyBar]
    metrics: Metrics
    signal_date: dt.date
    full_scan: bool
    liquidity_tier: str | None = None
    surveillance: tuple[str, ...] | None = None
    pledge_jump_pp: float | None = None


def _dist_at(bars: list[WeeklyBar], k: int, weeks: int) -> float | None:
    return dist_pct(bars[k].close, sma_of(bars, k, weeks)) if k >= 0 else None


def _vol_at(bars: list[WeeklyBar], k: int, config: Config) -> float | None:
    baseline = baseline_of(
        bars, k, config.indicators.volume_baseline_weeks, config.data.session_base
    )
    return normalised_volume(bars[k], config.data.session_base) / baseline if baseline else None


def is_absorption(bars: list[WeeklyBar], k: int, config: Config) -> bool:
    """S4-04: heavy selling that closes in the upper third is absorption, not distribution."""
    if k < 1 or bars[k].close >= bars[k - 1].close:
        return False
    vol = _vol_at(bars, k, config)
    loc = close_location(bars[k])
    ex = config.exit
    return (
        vol is not None
        and loc is not None
        and vol >= ex.absorption_min_volume_multiple
        and loc >= ex.absorption_close_location_min
    )


def signal1_at(bars: list[WeeklyBar], k: int, threshold: float | None, config: Config) -> bool:
    """S4-01: extended by this stock's standards and expanding further on heavy volume."""
    weeks = config.indicators.sma_weeks[0]
    dist, before = _dist_at(bars, k, weeks), _dist_at(bars, k - 1, weeks)
    vol = _vol_at(bars, k, config)
    if None in (dist, before, vol):
        return False
    limit = threshold if threshold is not None else config.exit.fallback_extension_pct
    return dist >= limit and dist > before and vol >= config.exit.signal1_min_volume_multiple


def is_failure_bar(bars: list[WeeklyBar], t: int, ex: Exit) -> str | None:
    bar, previous = bars[t], bars[t - 1]
    if bar.range <= 0:
        return None
    if bar.high <= previous.high and bar.low >= previous.low:
        return "inside bar"
    body = abs(bar.close - bar.open)
    if body <= ex.doji_body_to_range * bar.range:
        return "doji"
    wick = bar.high - max(bar.open, bar.close)
    loc = close_location(bar)
    if wick >= ex.hammer_wick_to_body * body and loc <= ex.hammer_close_location_max:
        return "inverted hammer"
    return None


def signal2_at(
    bars: list[WeeklyBar], t: int, threshold: float | None, config: Config
) -> str | None:
    """S4-02: a failure bar on heavy volume soon after a Signal 1 week."""
    ex = config.exit
    if t < 1:
        return None
    vol = _vol_at(bars, t, config)
    if vol is None or vol < ex.signal2_min_volume_multiple:
        return None
    recent = range(max(0, t - ex.failure_lookback_weeks), t)
    if not any(signal1_at(bars, k, threshold, config) for k in recent):
        return None
    return is_failure_bar(bars, t, ex)


def signal3_at(bars: list[WeeklyBar], t: int, config: Config) -> int | None:
    """S4-03: wide directionless weeks on elevated volume. Returns the count of wide weeks."""
    ex = config.exit
    window = ex.signal3_window_weeks
    if t - window < 0:
        return None
    net = abs(bars[t].close / bars[t - window].close - 1) * 100
    if net > ex.chop_net_flat_pct:
        return None
    wide = 0
    for k in range(t - window + 1, t + 1):
        earlier = bars[max(0, k - ex.signal3_range_avg_weeks) : k]
        if not earlier or is_absorption(bars, k, config):
            continue
        vol = _vol_at(bars, k, config)
        average = mean(b.range for b in earlier)
        if (
            vol is not None
            and vol >= config.add.flag.min_vol_multiple
            and bars[k].range >= ex.signal3_wide_range_multiple * average
        ):
            wide += 1
    return wide if wide >= ex.signal3_min_wide_weeks else None


@dataclass(frozen=True)
class Flag:
    high: float
    low: float
    weeks: int
    avg_volume: float


def find_flag(bars: list[WeeklyBar], end: int, config: Config) -> Flag | None:
    """S3-01: longest qualifying tight, dried-up pause near the highs ending at `end`."""
    cfg: Add = config.add
    ind = config.indicators
    base = config.data.session_base
    sma_weeks = ind.sma_weeks[0]
    if end < 0:
        return None
    sma_now = sma_of(bars, end, sma_weeks)
    earlier = end - ind.slope_lookback_weeks
    sma_before = sma_of(bars, earlier, sma_weeks) if earlier >= 0 else None
    if sma_now is None or sma_before is None or sma_now <= sma_before:
        return None
    high_window = bars[max(0, end - ind.pivot_lookback_weeks + 1) : end + 1]
    high_52w = max(b.high for b in high_window)
    for weeks in range(cfg.flag.max_weeks, cfg.flag.min_weeks - 1, -1):
        first = end - weeks + 1
        if first < 0:
            continue
        window = bars[first : end + 1]
        high = max(b.high for b in window)
        low = min(b.low for b in window)
        baseline = baseline_of(bars, first, ind.volume_baseline_weeks, base)
        average = mean(normalised_volume(b, base) for b in window)
        if (
            low > 0
            and (high - low) / low * 100 <= cfg.flag.max_range_pct
            and (high_52w - window[-1].close) / high_52w * 100 <= cfg.flag.max_below_high_pct
            and baseline is not None
            and average <= baseline
        ):
            return Flag(high, low, weeks, average)
    return None


def flag_breakout(bars: list[WeeklyBar], t: int, config: Config) -> Flag | None:
    """S3-02: the flag sits before the signal week; the signal week closes above it."""
    flag = find_flag(bars, t - 1, config)
    if flag is None or bars[t].close <= flag.high:
        return None
    baseline = baseline_of(
        bars, t, config.indicators.volume_baseline_weeks, config.data.session_base
    )
    vol = _vol_at(bars, t, config)
    if not baseline or vol is None:
        return None
    cfg = config.add.flag
    needed = max(cfg.breakout_vs_flag_avg * flag.avg_volume / baseline, cfg.min_vol_multiple)
    return flag if vol >= needed else None


def sma_refill(bars: list[WeeklyBar], m: Metrics, t: int, config: Config) -> bool:
    """S3-03: a weekly close back above a rising SMA after the low came near it. Volume is
    deliberately not a condition; it made no difference in the skill's backtest."""
    if m.sma13 is None or m.sma13_rising is not True:
        return False
    near = abs(bars[t].low / m.sma13 - 1) * 100 <= config.add.sma_refill.low_within_sma_pct
    return near and bars[t].close > m.sma13


def add_size(p: Position, close: float, config: Config) -> float:
    """S3-04: buys back trimmed shares, never past the position cap."""
    ledger = config.ledger_mirror
    current = p.qty_open * close / ledger.paper_capital * 100
    trimmed = max(0, (p.qty_initial or 0) - p.qty_open) * close / ledger.paper_capital * 100
    return max(0, min(trimmed, ledger.position_cap_pct - current))


def review_flags(i: HoldingInputs, config: Config) -> list[str]:
    """S4-07: what the runner can see of the kill switch. It flags; a person decides."""
    flags = []
    ks = config.kill_switch
    if ks.alert_on_surveillance and i.surveillance:
        flags.append("surveillance: " + ", ".join(i.surveillance))
    if i.pledge_jump_pp is not None and i.pledge_jump_pp >= ks.pledge_jump_pp:
        flags.append(f"pledge up {i.pledge_jump_pp} pp")
    bar = i.bars[-1]
    vol = i.metrics.vol_mult
    if (
        i.position.pivot is not None
        and bar.open < i.position.pivot
        and vol is not None
        and vol >= ks.gap_below_pivot_min_volume_multiple
    ):
        flags.append(f"gap below pivot {i.position.pivot}")
    return flags


def evaluate_holding(i: HoldingInputs, config: Config) -> Decision | None:
    """Exit sequence first, then trim, then add, else HOLD. Pending positions get no verdict."""
    p = i.position
    if p.status != OPEN or p.fill_price is None:
        return None
    bars, m = i.bars, i.metrics
    t = len(bars) - 1
    decimals = config.output.price_decimals
    threshold = m.extension.threshold
    provisional = not i.full_scan

    def fmt(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.{decimals}f}"

    def rnd(value: float | None) -> float | None:
        return None if value is None else round(value, decimals)

    trough = m.correction.trough if m.correction else None
    line = higher_low_line(bars, t, trough, config.indicators.pivot_low_side_bars)
    flags = review_flags(i, config)
    hit_trim = p.trim_price is not None and bars[t].high >= p.trim_price
    trim_state = "reached" if (hit_trim or p.trimmed) else "not reached"
    gain_pct = round((config.ledger_mirror.trim_multiple - 1) * 100, decimals)
    lines = (
        f"Pivot {fmt(p.pivot)}; +{gain_pct:g}% level {fmt(p.trim_price)} {trim_state} "
        "(the ledger trims); "
        f"higher-low line {fmt(line.price if line else None)}; "
        f"stop {fmt(p.stop_current)}; distance from SMA {fmt(m.dist_sma_pct)}%."
    )
    if provisional:
        label = f"{config.data.partial_week_label} ({m.sessions} sessions)"
        lines = f"{label}, provisional, confirm on the weekly close. {lines}"

    def decision(verdict: Verdict, stage: int, what: str, rules: list[str], **cols) -> Decision:
        return Decision(
            symbol=p.symbol,
            signal_date=i.signal_date,
            verdict=verdict,
            provisional=provisional,
            setup=SetupKind.HOLDING.value,
            stage=stage,
            notes=f"{what} {lines}",
            raw={
                "rules_fired": rules,
                "position_id": p.position_id,
                "origin_signal_id": p.signal_id,
                "own_threshold": rnd(threshold),
                "peaks": [rnd(x) for x in m.extension.peaks],
                "higher_low_line": rnd(line.price) if line else None,
                "review_flags": flags,
                "already_trimmed": p.trimmed,
            },
            signal_close=m.close,
            pivot=p.pivot,
            vol_mult=rnd(m.vol_mult),
            sma13=rnd(m.sma13),
            sma13_rising=m.sma13_rising,
            dist_sma_pct=rnd(m.dist_sma_pct),
            close_loc=rnd(m.close_loc),
            liquidity_tier=i.liquidity_tier,
            **cols,
        )

    ex = config.exit
    vol = m.vol_mult
    if (
        line is not None
        and bars[t].close < line.price
        and vol is not None
        and vol >= ex.signal4_min_volume_multiple
        and not is_absorption(bars, t, config)
    ):
        what = f"Signal 4: close below the higher low {fmt(line.price)} on {fmt(vol)}x."
        return decision(Verdict.EXIT, STAGE_EXIT, what, ["S4-05"])

    if signal1_at(bars, t, threshold, config):
        what = f"Signal 1: extended and expanding on {fmt(vol)}x. Reduce."
        return decision(Verdict.REDUCE, STAGE_EXIT, what, ["S4-01", "S2-02"])

    failure = signal2_at(bars, t, threshold, config)
    if failure:
        what = f"Signal 2: {failure} on {fmt(vol)}x after a parabolic push. Reduce."
        return decision(Verdict.REDUCE, STAGE_EXIT, what, ["S4-02"])

    wide = signal3_at(bars, t, config)
    if wide is not None:
        what = f"Signal 3: {wide} wide directionless weeks on elevated volume. Reduce."
        return decision(Verdict.REDUCE, STAGE_EXIT, what, ["S4-03"])

    if (
        not p.trimmed
        and threshold is not None
        and m.dist_sma_pct is not None
        and m.dist_sma_pct >= threshold
    ):
        peaks = ", ".join(fmt(x) for x in m.extension.peaks)
        what = f"Overextended: {fmt(m.dist_sma_pct)}% vs own threshold {fmt(threshold)}% ({peaks})."
        return decision(Verdict.TRIM, STAGE_TRIM, what, ["S2-01"])

    size = add_size(p, m.close, config)
    flag = flag_breakout(bars, t, config)
    if flag is not None and size > 0:
        what = f"Flag breakout above {fmt(flag.high)} on {fmt(vol)}x."
        return decision(
            Verdict.ADD,
            STAGE_ADD,
            what,
            ["S3-02", "S3-04"],
            size_pct=rnd(size),
            alert_trigger=flag.high,
        )
    # Never add inside a flag: this check comes before the refill on purpose.
    inside = find_flag(bars, t, config)
    if inside is not None:
        what = f"Inside a flag; add only on a weekly close above {fmt(inside.high)}."
        return decision(
            Verdict.HOLD, STAGE_ADD, what, ["S3-01", "S4-08"], alert_trigger=inside.high
        )
    if sma_refill(bars, m, t, config) and size > 0:
        what = f"SMA refill: close back above the rising 13-week SMA {fmt(m.sma13)}."
        return decision(Verdict.ADD, STAGE_ADD, what, ["S3-03", "S3-04"], size_pct=rnd(size))
    return decision(Verdict.HOLD, STAGE_EXIT, "No stage rule fired.", ["S4-08"])
