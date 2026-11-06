"""Regression cases: real bars for Claude's 30 Oct 2026 scan, with Claude's recorded numbers.
A change that moves a measured value or a verdict here must be explained, not absorbed."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from breakout_runner.data.bars import WeeklyBar
from breakout_runner.indicators.weekly import compute_metrics
from breakout_runner.rules.entry import EntryInputs, evaluate_entry

D = dt.date.fromisoformat
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "cases_2026-10-30.json"
CASES = json.loads(FIXTURE.read_text("utf-8"))
AS_OF = D(CASES["as_of"])
NAMES = sorted(CASES["cases"])
# Tolerances cover rounding in Claude's two-decimal rows, not differences in method.
PRICE_TOLERANCE = 0.01
PCT_TOLERANCE = 0.06


def bars_of(name):
    return [
        WeeklyBar(
            week=D(w[0]),
            open=w[1],
            high=w[2],
            low=w[3],
            close=w[4],
            volume=w[5],
            sessions=w[6],
            last_date=D(w[7]),
        )
        for w in CASES["cases"][name]["weekly"]
    ]


def metrics_of(name, cfg):
    bars = bars_of(name)
    return compute_metrics(bars, len(bars) - 1, cfg.indicators, cfg.data)


@pytest.mark.parametrize("name", NAMES)
def test_measures_match_claudes_recorded_row(name, cfg):
    want = CASES["cases"][name]["claude"]
    m = metrics_of(name, cfg)
    assert m.close == pytest.approx(want["close"], abs=PRICE_TOLERANCE)
    assert m.pivot == pytest.approx(want["pivot"], abs=PRICE_TOLERANCE)
    assert m.sma13 == pytest.approx(want["sma13"], abs=PRICE_TOLERANCE)
    assert m.dist_sma_pct == pytest.approx(want["dist_sma_pct"], abs=PCT_TOLERANCE)
    assert m.base_range_pct == pytest.approx(want["base_range_pct"], abs=PCT_TOLERANCE)
    if want["vol_mult"] is not None:
        assert m.vol_mult == pytest.approx(want["vol_mult"], abs=PCT_TOLERANCE)


@pytest.mark.parametrize("name", NAMES)
def test_verdict_is_claudes_or_an_explained_difference(name, cfg):
    case = CASES["cases"][name]
    decision = evaluate_entry(
        EntryInputs(
            symbol=name,
            metrics=metrics_of(name, cfg),
            signal_date=AS_OF,
            bar_complete=True,
            window_start=D("2026-11-02"),
            window_end=D("2026-11-05"),
            atr_pct=case["atr_pct"],
        ),
        cfg,
    )
    verdict = decision.verdict.value if decision else None
    assert verdict == case["runner_verdict"]
    assert case["why_different"] or verdict == case["claude"]["verdict"]


def test_corporate_action_correction_is_the_only_volume_difference(cfg):
    assert metrics_of("MBAPL", cfg).vol_mult == pytest.approx(1.10, abs=0.01)
