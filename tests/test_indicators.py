from __future__ import annotations

import csv
import datetime as dt
from dataclasses import replace
from pathlib import Path

import pytest

from breakout_runner.client.endpoints import HeatmapRow
from breakout_runner.data.bars import DailyBar, WeeklyBar
from breakout_runner.indicators import rs as rs_mod
from breakout_runner.indicators.weekly import (
    atr_pct_of,
    circuit_suspect,
    compute_metrics,
    correction_of,
    extension_of,
    higher_low_line,
    median_turnover_cr,
    wilder_atr,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
D = dt.date.fromisoformat


def load_weekly(default_sessions=5):
    with open(FIXTURES / "ACMESOLAR_weekly.csv") as fh:
        return [
            WeeklyBar(
                week=D(r["date"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                sessions=int(r["sessions"]) if r["sessions"] else default_sessions,
                last_date=D(r["date"]),
            )
            for r in csv.DictReader(fh)
        ]


@pytest.fixture
def bars():
    return load_weekly()


def index_of(bars, date):
    return next(i for i, b in enumerate(bars) if b.week == D(date))


def g1(bars, cfg):
    return compute_metrics(bars, index_of(bars, "2026-09-14"), cfg.indicators, cfg.data)


def test_golden_pivot_and_breakout(bars, cfg):
    m = g1(bars, cfg)
    assert m.pivot == 430.5 and m.close == 435.25 and m.breakout_close is True


def test_golden_smas_and_distance(bars, cfg):
    m = g1(bars, cfg)
    assert m.smas[13] == pytest.approx(386.52, abs=0.01)
    assert m.smas[26] == pytest.approx(343.82, abs=0.01)
    assert m.smas[39] == pytest.approx(305.61, abs=0.01)
    assert m.smas[52] == pytest.approx(293.39, abs=0.01)
    assert m.dist_sma_pct == pytest.approx(12.6, abs=0.05)
    assert m.stacked is True and m.sma13_rising is True


def test_golden_close_location_correction_and_higher_low(bars, cfg):
    m = g1(bars, cfg)
    assert m.close_loc == pytest.approx(0.859, abs=0.001)
    assert m.correction.pct == pytest.approx(39.6, abs=0.05)
    assert m.correction.peak_date == D("2025-09-15")
    assert m.correction.trough_date == D("2026-01-26")
    assert m.correction.weeks_taken == 19
    assert m.first_higher_low.price == 216.34
    assert m.first_higher_low.date == D("2026-03-09")


def test_golden_base_range_is_loose(bars, cfg):
    m = g1(bars, cfg)
    assert m.base_range_pct == pytest.approx(24.9, abs=0.05)
    assert m.base_class == "loose" and m.base_quality == "loose"


def test_volume_multiple_under_the_rule_docs_assumption(bars, cfg):
    """Spec scenario: 4-session signal week, baseline weeks of 5 each."""
    t = index_of(bars, "2026-09-14")
    assumed = [replace(b, sessions=5) if t - 20 <= i < t else b for i, b in enumerate(bars)]
    m = compute_metrics(assumed, t, cfg.indicators, cfg.data)
    assert m.vol_mult == pytest.approx(2.59, abs=0.02)


def test_volume_multiple_with_real_sessions(bars, cfg):
    """Real rows: 27 Apr, 25 May and 22 Jun were also 4-session weeks, so the true value is
    2.55, outside the rule doc's 2.59 +/- 0.02. Neither raw 2.07 nor mean-based 1.88."""
    assert g1(bars, cfg).vol_mult == pytest.approx(2.55, abs=0.005)


def test_golden_g2_not_broken_out(bars, cfg):
    m = compute_metrics(bars, index_of(bars, "2026-10-26"), cfg.indicators, cfg.data)
    assert m.pivot == 476.9 and m.breakout_close is False
    assert (m.pivot - m.close) / m.pivot * 100 == pytest.approx(15.1, abs=0.05)


def test_too_few_bars_is_unknown_not_false(bars, cfg):
    m = compute_metrics(bars[:30], 29, cfg.indicators, cfg.data)
    assert m.smas[52] is None and m.stacked is None


def test_zero_range_close_location_is_null(bars, cfg):
    t = index_of(bars, "2026-09-14")
    flat = list(bars)
    flat[t] = replace(bars[t], high=400.0, low=400.0, close=400.0)
    assert compute_metrics(flat, t, cfg.indicators, cfg.data).close_loc is None


def weeks(lows, start="2026-01-05"):
    first = D(start)
    return [
        WeeklyBar(
            first + dt.timedelta(weeks=i),
            lo + 1,
            lo + 2,
            lo,
            lo + 1,
            1000.0,
            5,
            first + dt.timedelta(weeks=i),
        )
        for i, lo in enumerate(lows)
    ]


def test_no_higher_low_yet_is_reported_missing(cfg):
    falling = weeks([100 - i for i in range(30)])
    assert compute_metrics(falling, 29, cfg.indicators, cfg.data).first_higher_low is None


def test_higher_low_line_uses_last_confirmed_pivot_below_close(cfg):
    series = weeks([100, 90, 95, 92, 99, 98, 80])
    line = higher_low_line(series, 6, 50.0, 1)
    assert line.price == 92  # the low of 98 has no confirming bar before the signal week
    assert higher_low_line(series, 6, 95.0, 1) is None


def test_extension_threshold_from_own_peaks(cfg):
    closes = ([100.0] * 13 + [110, 125, 130, 118, 104, 100, 100]) * 3
    first = D("2024-01-01")
    series = [
        WeeklyBar(
            first + dt.timedelta(weeks=i),
            c,
            c + 1,
            c - 1,
            c,
            1000.0,
            5,
            first + dt.timedelta(weeks=i),
        )
        for i, c in enumerate(closes)
    ]
    ext = extension_of(series, len(series) - 1, cfg.indicators)
    assert ext.threshold is not None and ext.threshold > cfg.indicators.min_peak_dist_pct
    assert len(ext.peaks) >= cfg.indicators.overext_min_peaks


def test_extension_insufficient_history(bars, cfg):
    assert extension_of(bars[:16], 15, cfg.indicators).threshold is None


def daily(n, close=100.0, tr=4.0):
    first = D("2026-01-01")
    return [
        DailyBar(
            first + dt.timedelta(days=i),
            close,
            close + tr / 2,
            close - tr / 2,
            close,
            close,
            10000.0,
            10000,
        )
        for i in range(n)
    ]


def test_wilder_atr_constant_range(cfg):
    assert wilder_atr(daily(30), cfg.indicators.atr_period) == pytest.approx(4.0)
    assert wilder_atr(daily(5), cfg.indicators.atr_period) is None


def test_atr_pct_falls_back_to_weekly_with_flag(bars, cfg):
    pct, from_weekly = atr_pct_of(daily(5), bars, cfg.indicators)
    assert from_weekly is True and pct > 0
    pct, from_weekly = atr_pct_of(daily(30), bars, cfg.indicators)
    assert from_weekly is False and pct == pytest.approx(4.0)


def test_turnover_in_crore():
    assert median_turnover_cr(daily(30, 100.0), 20) == pytest.approx(0.1)


def test_circuit_suspect_on_repeated_locks(cfg):
    locked = []
    close = 100.0
    first = D("2026-01-01")
    for i in range(10):
        close *= 1.05
        locked.append(
            DailyBar(first + dt.timedelta(days=i), close, close, close, close, close, 100.0, 100)
        )
    assert circuit_suspect(locked, cfg.indicators) is True
    assert circuit_suspect(daily(30), cfg.indicators) is False


def test_property_price_scale_leaves_ratios(bars, cfg):
    t = index_of(bars, "2026-09-14")
    scaled = [
        replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3) for b in bars
    ]
    a, b = (compute_metrics(s, t, cfg.indicators, cfg.data) for s in (bars, scaled))
    assert a.vol_mult == pytest.approx(b.vol_mult)
    assert a.dist_sma_pct == pytest.approx(b.dist_sma_pct)
    assert a.correction.pct == pytest.approx(b.correction.pct)
    assert a.breakout_close == b.breakout_close and a.base_class == b.base_class


def test_property_volume_scale_leaves_multiple(bars, cfg):
    t = index_of(bars, "2026-09-14")
    scaled = [replace(b, volume=b.volume * 7) for b in bars]
    assert compute_metrics(scaled, t, cfg.indicators, cfg.data).vol_mult == pytest.approx(
        g1(bars, cfg).vol_mult
    )


def test_property_holiday_only_acts_through_normalisation(bars, cfg):
    """Dropping a baseline week to 4 sessions while scaling its volume by 4/5 is a no-op."""
    t = index_of(bars, "2026-09-14")
    changed = list(bars)
    changed[t - 3] = replace(bars[t - 3], sessions=4, volume=bars[t - 3].volume * 4 / 5)
    assert compute_metrics(changed, t, cfg.indicators, cfg.data).vol_mult == pytest.approx(
        g1(bars, cfg).vol_mult
    )


def test_rs_percentile_and_label(cfg):
    rows = [HeatmapRow(f"S{i}", float(i), float(i), float(i), "2026-11-05") for i in range(100)]
    rows.append(HeatmapRow("NULL", None, 1.0, 1.0, "2026-11-05"))
    scores = rs_mod.universe_scores(rows, cfg.rs)
    assert len(scores) == 100
    result = rs_mod.percentile(89.5, scores)
    assert result.rs == 90 and result.status == "available" and result.method == "approx_3_windows"


def test_rs_unavailable_when_inputs_missing():
    assert rs_mod.percentile(1.0, None).status == "unavailable"
    assert rs_mod.percentile(None, [1.0]).rs is None


def test_rs_own_score_uses_week_offsets(bars, cfg):
    t = index_of(bars, "2026-09-14")
    assert rs_mod.own_score(bars, t, cfg.rs) is not None
    assert rs_mod.own_score(bars, 10, cfg.rs) is None


def _week(i, high, low, close):
    return WeeklyBar(
        week=D("2024-01-01") + dt.timedelta(days=7 * i),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
        sessions=5,
        last_date=D("2024-01-05") + dt.timedelta(days=7 * i),
    )


def test_correction_prefers_the_latest_qualifying_drawdown():
    """ACMESOLAR on real history: an older 42.6% must not replace the current 39.6% base."""
    series = [(100, 90), (100, 57), (120, 100), (130, 120), (130, 80), (125, 100), (128, 110)]
    bars = [_week(i, h, lo, lo) for i, (h, lo) in enumerate(series)]
    c = correction_of(bars, len(bars), 104, 25.0)
    assert c.peak == 130 and c.trough == 80 and c.pct == pytest.approx(38.46, abs=0.01)


def test_correction_falls_back_to_the_deepest_when_none_qualifies():
    series = [(100, 90), (100, 80), (110, 100), (110, 95)]
    bars = [_week(i, h, lo, lo) for i, (h, lo) in enumerate(series)]
    assert correction_of(bars, len(bars), 104, 25.0).pct == pytest.approx(20.0)
