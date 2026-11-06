from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from breakout_runner.client.endpoints import FundamentalRow
from breakout_runner.config.loader import parse_config
from breakout_runner.data.bars import HISTORY_SHORT, RAW_MISSING, WeeklyBar
from breakout_runner.indicators.rs import RsResult
from breakout_runner.indicators.weekly import compute_metrics
from breakout_runner.rules.entry import (
    NO_BASE,
    EntryInputs,
    classify_setup,
    conviction_of,
    evaluate_entry,
    is_triggered,
    required_volume,
    size_of,
)
from breakout_runner.rules.fundamentals import analyse, holdings_trend, yoy_series
from breakout_runner.rules.types import Verdict

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
D = dt.date.fromisoformat


def load_weekly():
    with open(FIXTURES / "ACMESOLAR_weekly.csv") as fh:
        return [
            WeeklyBar(
                week=D(r["date"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                sessions=int(r["sessions"]) if r["sessions"] else 5,
                last_date=D(r["date"]),
            )
            for r in csv.DictReader(fh)
        ]


@pytest.fixture
def bars():
    return load_weekly()


def at(bars, date, cfg):
    t = next(i for i, b in enumerate(bars) if b.week == D(date))
    return compute_metrics(bars, t, cfg.indicators, cfg.data)


def inputs(m, **over):
    values = dict(
        symbol="ACMESOLAR",
        metrics=m,
        signal_date=D("2026-09-18"),
        bar_complete=True,
        window_start=D("2026-09-21"),
        window_end=D("2026-09-24"),
        atr_pct=3.5,
        liquidity_tier="liquid",
        rs=RsResult(92.0, "available"),
        accelerating=True,
        holdings_rising=True,
        surveillance=(),
        grade="A",
        screen="Q1",
        priority_rank=1,
    )
    return EntryInputs(**(values | over))


def test_acmesolar_is_a_provisional_entry_with_the_confirmed_stop(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg)), cfg)
    assert d.verdict == Verdict.ENTRY and d.provisional is True and d.why_grade is None
    assert d.setup == "A_base" and d.stage == 1
    assert d.entry_low == 435.25 and d.entry_high == 457.01
    assert d.stop == 420.45  # entry_high x 0.92
    assert d.stop_basis == "cap8"
    assert d.trim_price == 571.26
    assert d.risk_pct == 8.0
    assert d.size_pct == 10.0
    assert d.vol_mult == pytest.approx(2.55, abs=0.005)
    assert d.window_start == D("2026-09-21") and d.window_end == D("2026-09-24")


def test_acmesolar_loose_base_lowers_conviction_only(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    d = evaluate_entry(inputs(m), cfg)
    assert "loose base on low volume" in d.raw["conviction"]["notches"]
    assert d.verdict == Verdict.ENTRY


def test_evidence_lists_rules_and_numbers(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg)), cfg)
    assert "S1-03" in d.raw["rules_fired"]
    assert d.raw["measures"]["correction"]["pct"] == pytest.approx(39.59, abs=0.01)
    assert d.raw["measures"]["first_higher_low"]["price"] == 216.34
    assert "457.01" in d.notes and "420.45" in d.notes
    assert "do not chase" in d.notes


def test_identical_inputs_give_byte_identical_output(bars, cfg):
    i = inputs(at(bars, "2026-09-14", cfg))
    a, b = (
        json.dumps(evaluate_entry(i, cfg).columns(), sort_keys=True, default=str) for _ in range(2)
    )
    assert a == b


def test_stop_config_changes_the_output(bars, cfg, raw):
    raw["ledger_mirror"]["stop_cap_pct"] = 10
    changed = parse_config(raw)
    d = evaluate_entry(inputs(at(bars, "2026-09-14", changed)), changed)
    assert d.stop == round(457.01 * 0.9, 2)


def test_entry_zone_config_changes_the_output(bars, cfg, raw):
    raw["risk"]["entry_zone_max_above_close_pct"] = 3
    changed = parse_config(raw)
    d = evaluate_entry(inputs(at(bars, "2026-09-14", changed)), changed)
    assert d.entry_high == round(435.25 * 1.03, 2)


def test_volume_minimum_config_changes_the_verdict(bars, cfg, raw):
    raw["volume"]["breakout_min_multiple"] = 3.0
    raw["volume"]["marginal_below"] = 1.5
    changed = parse_config(raw)
    d = evaluate_entry(inputs(at(bars, "2026-09-14", changed)), changed)
    assert d.verdict == Verdict.WAIT


# Decision table, one test per row.


def test_row1_history_short_is_wait(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg), quality=HISTORY_SHORT), cfg)
    assert d.verdict == Verdict.WAIT and HISTORY_SHORT in d.notes and d.provisional is False


def test_row1_raw_missing_blocks_the_trigger_unless_allowed(bars, cfg, raw):
    m = at(bars, "2026-09-14", cfg)
    i = inputs(m, flags=frozenset({RAW_MISSING}))
    d = evaluate_entry(i, cfg)
    assert d.verdict == Verdict.WAIT and "raw_close: missing in baseline" in d.raw["data_gaps"]
    raw["data"]["allow_raw_missing_trigger"] = True
    assert evaluate_entry(i, parse_config(raw)).verdict == Verdict.ENTRY


def test_row2_atr_above_limit_is_no_go(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg), atr_pct=7.5), cfg)
    assert d.verdict == Verdict.NO_GO and "S1-05" in d.raw["rules_fired"]


def test_row3_no_base_without_trigger_is_no_go(bars, cfg):
    m = replace(
        at(bars, "2026-10-26", cfg),
        correction=None,
        stacked=False,
    )
    assert classify_setup(m, cfg) == NO_BASE
    assert evaluate_entry(inputs(m), cfg).verdict == Verdict.NO_GO


def test_row5_light_volume_breakout_is_wait_with_requirements(bars, cfg):
    m = replace(at(bars, "2026-09-14", cfg), vol_mult=1.4)
    d = evaluate_entry(inputs(m), cfg)
    assert d.verdict == Verdict.WAIT and "false-start" in d.notes
    # A loose base asks for the higher multiple; the absolute is a full-week share count.
    assert d.req_vol_mult == 3.0
    assert d.req_vol_abs == required_volume(m, 3.0)


def test_req_vol_abs_is_multiple_times_the_weekly_baseline(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    assert required_volume(m, 2.0) == pytest.approx(2 * m.baseline, abs=1)


def test_row6_within_alert_distance_is_wait_alert(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    near = replace(m, close=m.pivot * 0.97, breakout_close=False, vol_mult=0.8)
    d = evaluate_entry(inputs(near), cfg)
    assert d.verdict == Verdict.WAIT_ALERT and d.alert_trigger == m.pivot
    assert d.stop == round(m.pivot * 0.92, 2) and d.risk_pct == 8.0
    assert d.provisional is False and d.rationale["catalyst"] == "unverified"


def test_row8_far_below_the_pivot_writes_nothing(bars, cfg):
    assert evaluate_entry(inputs(at(bars, "2026-10-26", cfg)), cfg) is None


def test_partial_bar_never_triggers(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    assert is_triggered(m, True, cfg) is True
    assert is_triggered(m, False, cfg) is False
    assert evaluate_entry(inputs(m, bar_complete=False), cfg).verdict != Verdict.ENTRY


def test_setup_b_for_a_shallow_uptrend(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    shallow = replace(
        m,
        correction=replace(m.correction, pct=18.0),
        stacked=True,
        base_range_pct=15.0,
        base_class="normal",
        below_high_pct=4.0,
    )
    assert classify_setup(shallow, cfg) == "B_flag"
    d = evaluate_entry(inputs(shallow), cfg)
    assert d.verdict == Verdict.ENTRY and d.setup == "B_flag"


def test_missing_higher_low_lowers_conviction_not_the_setup(bars, cfg):
    m = replace(at(bars, "2026-09-14", cfg), first_higher_low=None)
    d = evaluate_entry(inputs(m), cfg)
    assert d.verdict == Verdict.ENTRY
    assert "no higher low yet" in d.raw["conviction"]["notches"]


def test_atr_half_band_halves_size_and_lowers_conviction(bars, cfg):
    i = inputs(at(bars, "2026-09-14", cfg), atr_pct=6.0)
    d = evaluate_entry(i, cfg)
    assert d.verdict == Verdict.ENTRY and d.size_pct == 5.0
    assert "ATR in the half-size band" in d.raw["conviction"]["notches"]


def test_thin_stock_halves_size_and_warns(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg), liquidity_tier="thin"), cfg)
    assert d.size_pct == 5.0 and "not a guaranteed exit" in d.notes


def test_circuit_suspect_warns_too(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg), circuit_suspect=True), cfg)
    assert d.size_pct == 5.0 and "not a guaranteed exit" in d.notes


def test_multipliers_compound(bars, cfg):
    i = inputs(at(bars, "2026-09-14", cfg), atr_pct=6.0, liquidity_tier="thin")
    assert size_of(i, "high", cfg)[0] == 2.5


@pytest.mark.parametrize(
    "vol, note",
    [(6.0, "volume above the strong band"), (12.0, "volume above the weakest band")],
)
def test_high_volume_lowers_conviction_and_stays_an_entry(bars, cfg, vol, note):
    m = replace(at(bars, "2026-09-14", cfg), vol_mult=vol)
    d = evaluate_entry(inputs(m), cfg)
    assert d.verdict == Verdict.ENTRY and note in d.raw["conviction"]["notches"]


def test_unknown_signals_are_gaps_not_failures(bars, cfg):
    m = at(bars, "2026-09-14", cfg)
    i = inputs(
        m,
        rs=RsResult(None, "unavailable"),
        accelerating=None,
        holdings_rising=None,
        surveillance=None,
    )
    conv = conviction_of(i, "A_base", cfg)
    assert conv["signals"]["rs"] is None and conv["signals"]["accelerating"] is None
    d = evaluate_entry(i, cfg)
    assert d.verdict == Verdict.ENTRY
    gaps = d.raw["data_gaps"]
    assert "rs: unavailable" in gaps and "surveillance: unknown" in gaps
    assert "accelerating: unknown" in gaps


def test_conviction_counts_passing_signals(bars, cfg):
    m = replace(at(bars, "2026-09-14", cfg), base_class="normal", base_quality="normal")
    conv = conviction_of(inputs(m), "A_base", cfg)
    assert conv["level"] == "high" and conv["notches"] == []


def fr(metric, period, value, period_type="quarter"):
    return FundamentalRow(metric, period_type, period, "consolidated", value, "cr", period, None)


QUARTERS = [
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
    "2026-03-31",
    "2026-06-30",
    "2026-09-30",
]


def rows_for(metric, values):
    return [fr(metric, q, v) for q, v in zip(QUARTERS, values, strict=True)]


def test_yoy_is_year_on_year_by_period_not_sequential():
    values = dict(zip(QUARTERS, [100, 110, 120, 130, 110, 132, 150, 169], strict=True))
    points = [v for _, v in yoy_series(values) if v is not None]
    assert points == pytest.approx([10.0, 20.0, 25.0, 30.0])


def test_acceleration_detected(cfg):
    rows = rows_for("revenue", [100, 110, 120, 130, 110, 132, 150, 169])
    rows += rows_for("eps", [10, 11, 12, 13, 11, 13.2, 15, 16.9])
    rows += rows_for("net_profit", [10, 11, 12, 13, 11, 13.2, 15, 16.9])
    trend = analyse(rows, cfg.fundamentals)
    assert trend.accelerating is True and trend.earnings_metric == "eps"


def test_slowing_growth_is_not_accelerating(cfg):
    rows = rows_for("revenue", [100, 110, 120, 130, 130, 143, 150, 136])
    rows += rows_for("net_profit", [10, 11, 12, 13, 13, 14.3, 15, 13.6])
    rows += rows_for("eps", [10, 11, 12, 13, 13, 14.3, 15, 13.6])
    assert analyse(rows, cfg.fundamentals).accelerating is False


def test_missing_latest_eps_falls_back_to_profit_and_never_zero(cfg):
    rows = rows_for("revenue", [100, 110, 120, 130, 110, 132, 150, 169])
    rows += rows_for("eps", [10, 11, 12, 13, 11, 13.2, 15, None])
    rows += rows_for("net_profit", [10, 11, 12, 13, 11, 13.2, 15, 16.9])
    trend = analyse(rows, cfg.fundamentals)
    assert trend.eps_provisional is True and trend.earnings_metric == "net_profit"
    assert trend.accelerating is True


def test_too_few_quarters_is_unknown(cfg):
    rows = [fr("revenue", QUARTERS[-1], 100.0), fr("net_profit", QUARTERS[-1], 10.0)]
    assert analyse(rows, cfg.fundamentals).accelerating is None


def test_corporate_action_avoids_unrestated_eps(cfg):
    rows = rows_for("revenue", [100, 110, 120, 130, 110, 132, 150, 169])
    rows += rows_for("eps", [10, 11, 12, 13, 5, 6, 7, 8])
    rows += rows_for("net_profit", [10, 11, 12, 13, 11, 13.2, 15, 16.9])
    trend = analyse(rows, cfg.fundamentals, corporate_action=True)
    assert trend.earnings_metric == "net_profit" and trend.earnings_accelerating is True


def test_quality_flags_and_derived_debt_to_equity(cfg):
    rows = [
        fr("net_profit", "2025-09-30", 10.0),
        fr("net_profit", "2026-09-30", 100.0),
        fr("other_income", "2026-09-30", 40.0),
        fr("borrowings", "2026-03-31", 300.0, "annual"),
        fr("share_capital", "2026-03-31", 50.0, "annual"),
        fr("reserves", "2026-03-31", 100.0, "annual"),
    ]
    trend = analyse(rows, cfg.fundamentals)
    assert trend.other_income_share_pct == 40.0
    assert trend.low_base is True
    assert trend.debt_to_equity == 2.0


def test_untrusted_screen_is_watch_only(bars, cfg):
    d = evaluate_entry(inputs(at(bars, "2026-09-14", cfg), screen_trusted=False), cfg)
    assert d.verdict == Verdict.WAIT and "watch-only" in d.notes


def test_note_text_follows_the_configured_stop(bars, cfg, raw):
    raw["ledger_mirror"]["stop_cap_pct"] = 10
    changed = parse_config(raw)
    d = evaluate_entry(inputs(at(bars, "2026-09-14", changed)), changed)
    assert "stop 10% below" in d.notes and "8%" not in d.notes
    assert "10% stop" in d.rationale["risks"]


def table(promoters, fii, dii):
    labels = ("Jun 2026", "Sep 2026")
    return {
        "Promoters": dict(zip(labels, promoters, strict=True)),
        "FIIs": dict(zip(labels, fii, strict=True)),
        "DIIs": dict(zip(labels, dii, strict=True)),
    }


def test_holdings_rising_when_promoters_hold_and_institutions_add(cfg):
    assert holdings_trend(table((50, 50), (10, 11), (5, 5.5)), cfg.holdings) == (True, [])


def test_holdings_not_rising_when_promoters_sell(cfg):
    assert holdings_trend(table((50, 49), (10, 11), (5, 5.5)), cfg.holdings) == (False, [])


def test_holdings_jump_is_a_reclassification_not_a_pass(cfg):
    rising, tags = holdings_trend(table((50, 50), (10, 18), (5, 5)), cfg.holdings)
    assert rising is None and tags == ["possible_reclassification"]


def test_holdings_quarters_sort_by_date_not_text(cfg):
    t = {
        "Promoters": {"Dec 2025": 50, "Mar 2026": 50, "Sep 2025": 90},
        "FIIs": {"Dec 2025": 10, "Mar 2026": 11, "Sep 2025": 1},
        "DIIs": {"Dec 2025": 5, "Mar 2026": 5, "Sep 2025": 1},
    }
    assert holdings_trend(t, cfg.holdings) == (True, [])


def test_missing_holdings_data_is_unknown(cfg):
    assert holdings_trend({}, cfg.holdings) == (None, [])
    assert holdings_trend({"Promoters": {"Sep 2026": 50}}, cfg.holdings) == (None, [])


def test_second_week_breakout_is_not_a_new_entry(bars, cfg):
    m = replace(at(bars, "2026-09-14", cfg), already_broke_out=True)
    d = evaluate_entry(inputs(m), cfg)
    assert d.verdict == Verdict.WAIT and d.provisional is False
    assert d.raw["rules_fired"] == ["S1-14"] and d.entry_high is None


def test_second_week_gate_is_a_config_switch(bars, cfg):
    setup = cfg.setup.model_copy(update={"block_second_week_breakout": False})
    m = replace(at(bars, "2026-09-14", cfg), already_broke_out=True)
    d = evaluate_entry(inputs(m), cfg.model_copy(update={"setup": setup}))
    assert d.verdict == Verdict.ENTRY
