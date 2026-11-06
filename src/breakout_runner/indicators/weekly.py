"""Pure indicators on weekly bars (I-rules). No I/O, no clock. `t` indexes the signal week."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from statistics import mean, median

from ..config.models import Data, Indicators
from ..data.bars import DailyBar, WeeklyBar
from ..units import RUPEES_PER_CRORE

TIGHT = "tight"
NORMAL = "normal"
LOOSE = "loose"
NOT_A_BASE = "not_a_base"
TIGHT_DRY = "tight_dry"
UNKNOWN_BASE = "unknown"
# Best first; ranking uses the position in this list.
BASE_QUALITY_ORDER = (TIGHT_DRY, NORMAL, LOOSE, NOT_A_BASE, UNKNOWN_BASE)


@dataclass(frozen=True)
class Correction:
    pct: float
    peak: float
    peak_date: dt.date
    trough: float
    trough_date: dt.date
    weeks_taken: int


@dataclass(frozen=True)
class HigherLow:
    price: float
    date: dt.date


@dataclass(frozen=True)
class Extension:
    threshold: float | None
    peaks: tuple[float, ...]


@dataclass(frozen=True)
class Metrics:
    week: dt.date
    close: float
    pivot: float | None
    breakout_close: bool
    already_broke_out: bool
    prior_ath_just_above: bool
    baseline: float | None
    vol_mult: float | None
    sessions: int
    smas: dict[int, float | None]
    sma13: float | None
    sma13_slope: float | None
    sma13_rising: bool | None
    dist_sma_pct: float | None
    stacked: bool | None
    close_loc: float | None
    correction: Correction | None
    first_higher_low: HigherLow | None
    base_range_pct: float | None
    base_class: str
    base_quality: str
    high_52w: float | None
    below_high_pct: float | None
    structure_ok: bool | None
    extension: Extension


def normalised_volume(bar: WeeklyBar, session_base: int) -> float:
    return bar.volume * session_base / bar.sessions


def pivot_of(bars: list[WeeklyBar], t: int, weeks: int) -> float | None:
    window = bars[max(0, t - weeks) : t]
    return max(b.high for b in window) if window else None


def baseline_of(bars: list[WeeklyBar], t: int, weeks: int, session_base: int) -> float | None:
    window = bars[max(0, t - weeks) : t]
    if not window:
        return None
    value = median(normalised_volume(b, session_base) for b in window)
    return value if value > 0 else None


def sma_of(bars: list[WeeklyBar], t: int, weeks: int) -> float | None:
    if t + 1 < weeks:
        return None
    return mean(b.close for b in bars[t - weeks + 1 : t + 1])


def close_location(bar: WeeklyBar) -> float | None:
    return (bar.close - bar.low) / bar.range if bar.range > 0 else None


def dist_pct(close: float, sma: float | None) -> float | None:
    return (close / sma - 1) * 100 if sma else None


def is_pivot_low(bars: list[WeeklyBar], k: int, side: int) -> bool:
    if k - side < 0 or k + side >= len(bars):
        return False
    return all(
        bars[k].low < bars[k - i].low and bars[k].low < bars[k + i].low for i in range(1, side + 1)
    )


def is_pivot_high(bars: list[WeeklyBar], k: int, side: int) -> bool:
    if k - side < 0 or k + side >= len(bars):
        return False
    return all(
        bars[k].high > bars[k - i].high and bars[k].high > bars[k + i].high
        for i in range(1, side + 1)
    )


def correction_of(
    bars: list[WeeklyBar], t: int, weeks: int, acceptable_pct: float
) -> Correction | None:
    """I-06: the latest drawdown reaching `acceptable_pct`, else the deepest one in the window.
    The deepest alone can pick a base from before the current one."""
    window = bars[max(0, t - weeks) : t]
    deepest: Correction | None = None
    latest: Correction | None = None
    segment: Correction | None = None
    peak_bar: WeeklyBar | None = None
    peak_position = 0
    for position, bar in enumerate(window):
        if peak_bar is None or bar.high > peak_bar.high:
            peak_bar = bar
            peak_position = position
            segment = None
        pct = (peak_bar.high - bar.low) / peak_bar.high * 100
        if segment is not None and pct <= segment.pct:
            continue
        segment = Correction(
            pct=pct,
            peak=peak_bar.high,
            peak_date=peak_bar.week,
            trough=bar.low,
            trough_date=bar.week,
            weeks_taken=position - peak_position,
        )
        if deepest is None or pct > deepest.pct:
            deepest = segment
        if pct >= acceptable_pct:
            latest = segment
    return latest or deepest


def first_higher_low_of(
    bars: list[WeeklyBar], t: int, correction: Correction | None, side: int
) -> HigherLow | None:
    """I-07: first confirmed pivot low after the trough and above it."""
    if correction is None:
        return None
    visible = bars[: t + 1]
    for k, bar in enumerate(visible):
        if bar.week <= correction.trough_date:
            continue
        if bar.low > correction.trough and is_pivot_low(visible, k, side):
            return HigherLow(bar.low, bar.week)
    return None


def higher_low_line(
    bars: list[WeeklyBar], t: int, trough: float | None, side: int
) -> HigherLow | None:
    """S4-05: the line that ends the trade, as it stood before the signal week: the latest
    confirmed pivot low below last week's close and not below the base trough. Equal counts:
    in a clean uptrend the latest swing low is also the deepest drawdown in the window, and
    breaking it must still end the trade."""
    if t < 1:
        return None
    visible = bars[:t]
    reference = bars[t - 1].close
    for k in range(len(visible) - 1, -1, -1):
        bar = visible[k]
        if not is_pivot_low(visible, k, side) or bar.low >= reference:
            continue
        if trough is None or bar.low >= trough:
            return HigherLow(bar.low, bar.week)
    return None


def base_range_of(bars: list[WeeklyBar], t: int, weeks: int) -> float | None:
    window = bars[max(0, t - weeks) : t]
    if not window:
        return None
    low = min(b.low for b in window)
    return (max(b.high for b in window) - low) / low * 100 if low > 0 else None


def classify_base(range_pct: float | None, cfg: Indicators) -> str:
    if range_pct is None:
        return NOT_A_BASE
    bands = cfg.base_bands
    if range_pct <= bands.tight_max:
        return TIGHT
    if range_pct <= bands.normal_max:
        return NORMAL
    if range_pct <= bands.loose_max:
        return LOOSE
    return NOT_A_BASE


def base_quality_of(
    bars: list[WeeklyBar], t: int, base_class: str, baseline: float | None, ind: Indicators, sb: int
) -> str:
    """I-19 / calibration 9. Tight only ranks highest when volume is also drying up."""
    if base_class != TIGHT:
        return base_class
    window = bars[max(0, t - ind.base_window_weeks) : t]
    if baseline and window and mean(normalised_volume(b, sb) for b in window) <= baseline:
        return TIGHT_DRY
    return NORMAL


def _rising(values: list[float], count: int) -> bool:
    recent = values[-count:]
    return all(a < b for a, b in zip(recent, recent[1:], strict=False))


def structure_of(bars: list[WeeklyBar], t: int, ind: Indicators) -> bool | None:
    """I-14: last N pivot highs and last N pivot lows both rising; None when too few exist."""
    side = ind.pivot_low_side_bars
    visible = bars[: t + 1]
    first = max(0, t - ind.structure_lookback_weeks + 1)
    highs = [visible[k].high for k in range(first, t + 1) if is_pivot_high(visible, k, side)]
    lows = [visible[k].low for k in range(first, t + 1) if is_pivot_low(visible, k, side)]
    n = ind.hh_hl_pivots
    if len(highs) < n or len(lows) < n:
        return None
    return _rising(highs, n) and _rising(lows, n)


def extension_of(bars: list[WeeklyBar], t: int, ind: Indicators) -> Extension:
    """I-11: this stock's own overextension threshold, from confirmed peaks only."""
    sma_weeks = ind.sma_weeks[0]
    first = max(sma_weeks - 1, t - ind.overext_lookback_weeks)
    series: dict[int, float] = {}
    for k in range(first, t + 1):
        d = dist_pct(bars[k].close, sma_of(bars, k, sma_weeks))
        if d is not None:
            series[k] = d
    w = ind.peak_window_weeks
    peaks = []
    for k, d in series.items():
        if k + w > t or d < ind.min_peak_dist_pct:
            continue  # unconfirmed until the right side of the window has printed
        around = [series[i] for i in range(k - w, k + w + 1) if i in series]
        if d == max(around):
            peaks.append(d)
    recent = tuple(peaks[-ind.overext_peaks_to_measure :])
    if len(recent) < ind.overext_min_peaks:
        return Extension(None, recent)
    return Extension(median(recent), recent)


def compute_metrics(bars: list[WeeklyBar], t: int, ind: Indicators, data: Data) -> Metrics:
    bar = bars[t]
    base = data.session_base
    pivot = pivot_of(bars, t, ind.pivot_lookback_weeks)
    previous_pivot = pivot_of(bars, t - 1, ind.pivot_lookback_weeks) if t >= 1 else None
    baseline = baseline_of(bars, t, ind.volume_baseline_weeks, base)
    smas = {w: sma_of(bars, t, w) for w in ind.sma_weeks}
    sma13 = smas[ind.sma_weeks[0]]
    earlier = t - ind.slope_lookback_weeks
    sma13_before = sma_of(bars, earlier, ind.sma_weeks[0]) if earlier >= 0 else None
    slope = sma13 - sma13_before if sma13 is not None and sma13_before is not None else None
    values = [smas[w] for w in ind.sma_weeks]
    stacked = (
        None if None in values else all(a > b for a, b in zip(values, values[1:], strict=False))
    )
    correction = correction_of(
        bars, t, ind.correction_lookback_weeks, ind.acceptable_correction_pct
    )
    base_range = base_range_of(bars, t, ind.base_window_weeks)
    base_class = classify_base(base_range, ind)
    high_window = bars[max(0, t - ind.pivot_lookback_weeks + 1) : t + 1]
    high_52w = max(b.high for b in high_window)
    ath_cap = pivot * (1 + ind.ath_proximity_pct / 100) if pivot else None
    return Metrics(
        week=bar.week,
        close=bar.close,
        pivot=pivot,
        breakout_close=pivot is not None and bar.close > pivot,
        already_broke_out=(
            previous_pivot is not None and t >= 1 and bars[t - 1].close > previous_pivot
        ),
        prior_ath_just_above=(
            ath_cap is not None and any(pivot < b.high <= ath_cap for b in bars[:t])
        ),
        baseline=baseline,
        vol_mult=normalised_volume(bar, base) / baseline if baseline else None,
        sessions=bar.sessions,
        smas=smas,
        sma13=sma13,
        sma13_slope=slope,
        sma13_rising=None if slope is None else slope > 0,
        dist_sma_pct=dist_pct(bar.close, sma13),
        stacked=stacked,
        close_loc=close_location(bar),
        correction=correction,
        first_higher_low=first_higher_low_of(bars, t, correction, ind.pivot_low_side_bars),
        base_range_pct=base_range,
        base_class=base_class,
        base_quality=base_quality_of(bars, t, base_class, baseline, ind, base),
        high_52w=high_52w,
        below_high_pct=(high_52w - bar.close) / high_52w * 100 if high_52w else None,
        structure_ok=structure_of(bars, t, ind),
        extension=extension_of(bars, t, ind),
    )


def wilder_atr(daily: list[DailyBar], period: int) -> float | None:
    """I-09: Wilder's smoothing, seeded with the mean of the first `period` true ranges."""
    if len(daily) < period + 1:
        return None
    ranges = [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(daily, daily[1:], strict=False)
    ]
    atr = mean(ranges[:period])
    for tr in ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_pct_of(
    daily: list[DailyBar], weekly: list[WeeklyBar], ind: Indicators
) -> tuple[float | None, bool]:
    """(atr_pct, from_weekly). Falls back to weekly ATR over the configured ratio."""
    atr = wilder_atr(daily, ind.atr_period)
    if atr is not None:
        return atr / daily[-1].close * 100, False
    if len(weekly) < ind.atr_period + 1:
        return None, False
    proxies = [
        DailyBar(w.last_date, w.open, w.high, w.low, w.close, None, w.volume, int(w.volume))
        for w in weekly
    ]
    weekly_atr = wilder_atr(proxies, ind.atr_period)
    return weekly_atr / ind.atr_weekly_to_daily_ratio / weekly[-1].close * 100, True


def median_turnover_cr(daily: list[DailyBar], sessions: int) -> float | None:
    """I-10: adjusted close times today's-terms volume is a true traded value."""
    window = daily[-sessions:]
    if not window:
        return None
    return median(b.close * b.volume for b in window) / RUPEES_PER_CRORE


def liquidity_tier(turnover_cr: float | None, ind: Indicators) -> str | None:
    if turnover_cr is None:
        return None
    return "thin" if turnover_cr < ind.thin_turnover_cr else "liquid"


def circuit_suspect(daily: list[DailyBar], ind: Indicators) -> bool:
    """I-12: no circuit-band route exists, so count locked-looking sessions in the bars."""
    window = daily[-(ind.circuit_window_sessions + 1) :]
    locked = 0
    for previous, bar in zip(window, window[1:], strict=False):
        change = abs(bar.close / previous.close - 1) * 100 if previous.close else 0
        at_extreme = bar.close in (bar.high, bar.low)
        if bar.high == bar.low or (at_extreme and change >= ind.circuit_pct_min):
            locked += 1
    return locked >= ind.circuit_sessions_flag
