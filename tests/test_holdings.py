from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from breakout_runner.client.endpoints import SignalRow
from breakout_runner.data.bars import DailyBar, WeeklyBar
from breakout_runner.indicators.weekly import compute_metrics
from breakout_runner.rules.holdings import (
    HoldingInputs,
    Position,
    add_size,
    evaluate_holding,
    find_flag,
    flag_breakout,
    is_absorption,
    is_failure_bar,
    scope_positions,
    signal3_at,
)
from breakout_runner.rules.midweek import (
    FridayItem,
    distribution_flags,
    evaluate_midweek,
    new_high_names,
    read_friday_state,
    week_to_date,
)
from breakout_runner.rules.types import Verdict

D = dt.date.fromisoformat
FIRST = D("2024-01-01")


def wk(i, o, h, l, c, v=1000.0, s=5):  # noqa: E741
    day = FIRST + dt.timedelta(weeks=i)
    return WeeklyBar(day, o, h, l, c, v, s, day)


def steady(n=80, start=100.0, step=0.5, volume=1000.0):
    """A calm uptrend, so nothing fires unless a test adds it."""
    bars = []
    for i in range(n):
        c = start + i * step
        bars.append(wk(i, c - 0.2, c + 1, c - 1, c, volume))
    return bars


def position(**over):
    values = dict(
        position_id=1,
        signal_id=10,
        symbol="AAA",
        status="open",
        pivot=120.0,
        fill_date=D("2025-01-06"),
        fill_price=125.0,
        qty_initial=800,
        qty_open=800,
        avg_cost=125.0,
        stop_current=115.0,
        trim_price=156.25,
        trimmed=False,
    )
    return Position(**(values | over))


def run(bars, cfg, *, full_scan=True, p=None, **over):
    t = len(bars) - 1
    m = compute_metrics(bars, t, cfg.indicators, cfg.data)
    i = HoldingInputs(
        position=p or position(),
        bars=bars,
        metrics=m,
        signal_date=D("2026-09-18"),
        full_scan=full_scan,
        **over,
    )
    return evaluate_holding(i, cfg)


def test_scope_filters_status_book_and_exclusions():
    rows = [
        {"position_id": 1, "signal_id": 1, "symbol": "A", "status": "open"},
        {"position_id": 2, "signal_id": 2, "symbol": "B", "status": "pending"},
        {"position_id": 3, "signal_id": 3, "symbol": "C", "status": "closed"},
        {"position_id": 4, "signal_id": 4, "symbol": "D", "status": "open"},
        {
            "position_id": 5,
            "signal_id": 5,
            "symbol": "E",
            "status": "open",
            "signal_date": D("2026-09-01"),
        },
    ]
    kept = scope_positions(rows, D("2026-10-04"), {4})
    assert [p.symbol for p in kept] == ["A", "B"]


def test_pending_position_gets_no_verdict(cfg):
    assert run(steady(), cfg, p=position(status="pending", fill_price=None)) is None


def test_quiet_week_is_hold_with_null_stop_and_line_prices(cfg):
    d = run(steady(), cfg)
    assert d.verdict == Verdict.HOLD and d.stop is None and d.provisional is False
    assert d.setup == "holding"
    assert "higher-low line" in d.notes and "156.25" in d.notes


def test_gain_level_reached_is_annotated_not_a_trim(cfg):
    bars = steady()
    p = position(trim_price=bars[-1].high - 1)
    d = run(bars, cfg, p=p)
    assert d.verdict != Verdict.TRIM
    assert "reached (the ledger trims)" in d.notes


def with_peaks(extra):
    """Three past runs give an own threshold around 15-20% above the SMA."""
    closes = []
    for _ in range(3):
        closes += [100.0] * 13 + [106, 112, 118, 116, 108, 102, 100]
    closes += [100.0] * 13 + extra
    return [wk(i, c, c + 1, c - 1, c) for i, c in enumerate(closes)]


def test_overextension_trim_is_final_only_on_the_full_scan(cfg):
    bars = with_peaks([120.0])
    bars[-1] = replace(bars[-1], volume=1000.0)
    d = run(bars, cfg)
    assert d.verdict == Verdict.TRIM and d.stage == 2 and d.provisional is False
    assert d.raw["own_threshold"] is not None and "own threshold" in d.notes
    assert run(bars, cfg, full_scan=False).provisional is True


def test_trim_not_repeated_once_trimmed(cfg):
    bars = with_peaks([120.0])
    d = run(bars, cfg, p=position(trimmed=True, qty_open=560))
    assert d.verdict != Verdict.TRIM


def test_signal1_parabolic_on_heavy_volume_reduces(cfg):
    bars = with_peaks([120.0])
    bars[-1] = replace(bars[-1], volume=3000.0)
    d = run(bars, cfg)
    assert d.verdict == Verdict.REDUCE and d.stage == 4 and "S4-01" in d.raw["rules_fired"]


def test_failure_bar_shapes(cfg):
    ex = cfg.exit
    previous = wk(0, 100, 110, 95, 105)
    inside = [previous, wk(1, 100, 108, 96, 104)]
    assert is_failure_bar(inside, 1, ex) == "inside bar"
    doji = [previous, wk(1, 112, 120, 100, 112.5)]
    assert is_failure_bar(doji, 1, ex) == "doji"
    hammer = [previous, wk(1, 112, 130, 111, 114)]
    assert is_failure_bar(hammer, 1, ex) == "inverted hammer"
    plain = [previous, wk(1, 100, 130, 99, 128)]
    assert is_failure_bar(plain, 1, ex) is None


def test_signal2_after_signal1_reduces(cfg):
    bars = with_peaks([120.0])
    bars[-1] = replace(bars[-1], volume=3000.0)
    last = bars[-1]
    bars.append(
        replace(wk(len(bars), 121, 122, 119, 120.5, 2000.0), week=last.week + dt.timedelta(weeks=1))
    )
    d = run(bars, cfg)
    assert d.verdict == Verdict.REDUCE and "S4-02" in d.raw["rules_fired"]


def test_signal3_distribution_chop_counts_wide_weeks(cfg):
    bars = steady(70, step=0.0, volume=1000.0)
    n = len(bars)
    bars += [
        wk(n, 100, 112, 90, 104, 2000.0),
        wk(n + 1, 104, 116, 92, 98, 2000.0),
        wk(n + 2, 98, 112, 88, 101, 2000.0),
        wk(n + 3, 101, 115, 90, 100, 2000.0),
        wk(n + 4, 100, 102, 98, 101, 1000.0),
        wk(n + 5, 101, 103, 99, 100, 1000.0),
    ]
    t = len(bars) - 1
    assert signal3_at(bars, t, cfg) >= cfg.exit.signal3_min_wide_weeks
    d = run(bars, cfg)
    assert d.verdict == Verdict.REDUCE and "S4-03" in d.raw["rules_fired"]


def test_signal4_capitulation_exits_and_names_the_line(cfg):
    bars = steady(70)
    n = len(bars)
    last = bars[-1].close
    bars += [
        wk(n, last, last + 1, last - 6, last - 2),
        wk(n + 1, last - 2, last + 2, last - 3, last + 1),
        wk(n + 2, last + 1, last + 2, last - 20, last - 15, 3000.0),
    ]
    d = run(bars, cfg)
    assert d.verdict == Verdict.EXIT and d.stage == 4 and "S4-05" in d.raw["rules_fired"]
    assert d.raw["higher_low_line"] is not None
    assert "Signal 4" in d.notes


def test_absorption_week_does_not_count(cfg):
    bars = steady(70)
    n = len(bars)
    last = bars[-1].close
    bars += [
        wk(n, last, last + 1, last - 6, last - 2),
        wk(n + 1, last - 2, last + 2, last - 3, last + 1),
        # A heavy down week that closes in the top of its range.
        wk(n + 2, last + 1, last + 1.5, last - 30, last - 2, 3000.0),
    ]
    assert is_absorption(bars, len(bars) - 1, cfg) is True
    d = run(bars, cfg)
    assert d.verdict != Verdict.EXIT


def flag_bars(last_close, volume, last_volume=None):
    bars = steady(70, step=1.0, volume=1000.0)
    n = len(bars)
    top = bars[-1].close
    for k in range(5):
        bars.append(wk(n + k, top, top + 1, top - 2, top - 0.5, 400.0))
    bars.append(wk(n + 5, top, last_close + 0.5, top - 1, last_close, last_volume or volume))
    return bars


def test_inside_flag_is_hold_with_trigger_never_add(cfg):
    bars = flag_bars(169.0, 400.0)
    assert find_flag(bars, len(bars) - 1, cfg) is not None
    # Room to add, and the low is near the SMA, yet inside a flag it must stay a HOLD.
    d = run(bars, cfg, p=position(qty_open=500))
    assert d.verdict == Verdict.HOLD and d.alert_trigger is not None


def test_flag_breakout_adds_buys_back_trimmed_shares(cfg):
    bars = flag_bars(175.0, 1500.0)
    assert flag_breakout(bars, len(bars) - 1, cfg) is not None
    p = position(qty_initial=800, qty_open=560, trimmed=True)
    d = run(bars, cfg, p=p)
    assert d.verdict == Verdict.ADD and "S3-02" in d.raw["rules_fired"]
    # 560 shares at 175 are already 9.8% of capital, so the 10% cap binds before the 240 sold.
    assert d.size_pct == pytest.approx(0.2, abs=0.01)


def test_flag_breakout_on_thin_volume_does_not_add(cfg):
    bars = flag_bars(175.0, 450.0)
    assert flag_breakout(bars, len(bars) - 1, cfg) is None


def test_cap_reached_means_no_add(cfg):
    bars = flag_bars(175.0, 1500.0)
    capital = cfg.ledger_mirror.paper_capital
    full = int(capital * 0.10 / 175.0)
    p = position(qty_initial=full + 100, qty_open=full, trimmed=True)
    assert add_size(p, 175.0, cfg) <= 0.01
    d = run(bars, cfg, p=replace(p, qty_open=full + 1))
    assert d.verdict != Verdict.ADD


def test_never_trimmed_position_has_nothing_to_buy_back(cfg):
    assert add_size(position(), 175.0, cfg) == 0


def test_sma_refill_adds_without_a_volume_condition(cfg):
    bars = steady(70, step=0.6, volume=1000.0)
    t = len(bars) - 1
    sma = compute_metrics(bars, t, cfg.indicators, cfg.data).sma13
    # Heavy volume just before keeps this from reading as a dried-up flag; the signal week's
    # own volume is tiny because the refill has no volume condition.
    bars[t - 2] = replace(bars[t - 2], volume=5000.0)
    bars[t - 1] = replace(bars[t - 1], volume=5000.0)
    bars[t] = wk(t, sma + 1, sma + 3, sma * 1.005, sma + 2, 10.0)
    p = position(qty_initial=800, qty_open=560, trimmed=True)
    d = run(bars, cfg, p=p)
    assert d.verdict == Verdict.ADD and "S3-03" in d.raw["rules_fired"]


def test_kill_switch_is_flagged_never_an_exit(cfg):
    bars = steady()
    last = bars[-1]
    bars[-1] = replace(last, open=100.0, low=99.0, volume=3000.0)
    p = position(pivot=120.0)
    d = run(bars, cfg, p=p, surveillance=("asm:ST-ASM:1",), pledge_jump_pp=6.0)
    assert d.verdict != Verdict.EXIT
    flags = d.raw["review_flags"]
    assert any("surveillance" in f for f in flags)
    assert any("pledge" in f for f in flags)
    assert any("gap below pivot" in f for f in flags)


def test_midweek_holding_rows_are_provisional_and_labelled(cfg):
    d = run(steady(), cfg, full_scan=False)
    assert d.provisional is True and "PARTIAL" in d.notes


def daily_week(volumes, closes, start="2026-11-02"):
    first = D(start)
    return [
        DailyBar(first + dt.timedelta(days=i), c, c + 1, c - 1, c, c, float(v), v)
        for i, (v, c) in enumerate(zip(volumes, closes, strict=True))
    ]


def friday(**over):
    values = dict(
        signal_id=99,
        symbol="AAA",
        verdict="WAIT_ALERT",
        signal_date=D("2026-10-30"),
        trigger=100.0,
        stop=92.0,
        req_vol_abs=10000,
        setup="A_base",
    )
    return FridayItem(**(values | over))


def partial_week(close, volume, sessions=2, high=None, low=None):
    return WeeklyBar(
        D("2026-11-02"),
        close,
        high if high is not None else close + 1,
        low if low is not None else close - 1,
        close,
        float(volume),
        sessions,
        D("2026-11-03"),
        complete=False,
    )


def metrics_for(week, cfg):
    bars = steady(70)
    bars.append(week)
    return compute_metrics(bars, len(bars) - 1, cfg.indicators, cfg.data)


def test_run_rate_40_percent_at_2_of_5_projects_100(cfg):
    week = partial_week(95.0, 4000)
    wtd = week_to_date(friday(), week, metrics_for(week, cfg), [], cfg)
    assert wtd.pct_of_required == 40 and wtd.projected_pct_of_required == 100


def test_trigger_crossed_on_pace_is_a_provisional_entry_row(cfg):
    week = partial_week(101.0, 6000, sessions=3)
    d = evaluate_midweek(friday(), week, metrics_for(week, cfg), [], D("2026-11-04"), cfg)
    assert d.verdict == Verdict.ENTRY and d.provisional is True
    assert "PARTIAL (3 sessions)" in d.notes and d.raw["friday_signal_id"] == 99


def test_trigger_crossed_behind_pace_is_wait(cfg):
    week = partial_week(101.0, 500, sessions=3)
    d = evaluate_midweek(friday(), week, metrics_for(week, cfg), [], D("2026-11-04"), cfg)
    assert d.verdict == Verdict.WAIT and d.provisional is True


def test_near_trigger_is_listed_with_the_alert(cfg):
    week = partial_week(97.6, 1000)
    wtd = week_to_date(friday(), week, metrics_for(week, cfg), [], cfg)
    assert wtd.to_trigger_pct == pytest.approx(2.4) and wtd.near_trigger is True
    d = evaluate_midweek(friday(), week, metrics_for(week, cfg), [], D("2026-11-03"), cfg)
    assert d.verdict == Verdict.WAIT_ALERT and "set the alert" in d.notes


def test_far_name_keeps_its_friday_stance(cfg):
    week = partial_week(80.0, 1000)
    d = evaluate_midweek(
        friday(verdict="WAIT"), week, metrics_for(week, cfg), [], D("2026-11-03"), cfg
    )
    assert d.verdict == Verdict.WAIT


def test_distribution_after_breakout_close_near_low(cfg):
    week = partial_week(100.5, 1000, high=110.0, low=99.0)
    assert distribution_flags([], week, 105.0, cfg) == [
        "close near the week's low after a breakout"
    ]


def test_distribution_heavy_down_days(cfg):
    earlier = daily_week([1000] * 5, [100, 100, 100, 100, 100], start="2026-10-26")
    this = daily_week([2000, 2000], [98, 96])
    week = partial_week(96.0, 4000)
    assert "2 heavy down days" in distribution_flags(earlier + this, week, None, cfg)


def row(id_, symbol, verdict, date, **over):
    return (
        dict(
            id=id_,
            symbol=symbol,
            verdict=verdict,
            signal_date=D(date),
            alert_trigger=None,
            pivot=100.0,
            stop=92.0,
            req_vol_abs=1000,
            setup="A_base",
        )
        | over
    )


def test_friday_state_uses_latest_scan_and_exclusions():
    rows = [
        row(1, "OLD", "WAIT", "2026-10-23"),
        row(2, "AAA", "WAIT_ALERT", "2026-10-30", alert_trigger=105.0),
        row(3, "AAA", "WAIT", "2026-10-30"),
        row(4, "PILOT", "ENTRY", "2026-10-30"),
        row(5, "HELD", "HOLD", "2026-10-30"),
    ]
    items, stale = read_friday_state(rows, D("2026-11-03"), {4}, 10)
    assert not stale
    assert [(i.symbol, i.signal_id) for i in items] == [("AAA", 3)]


def test_stale_or_missing_friday_state_is_reported():
    assert read_friday_state([], D("2026-11-03"), set(), 10) == ([], True)
    rows = [row(1, "OLD", "WAIT", "2026-10-01")]
    assert read_friday_state(rows, D("2026-11-03"), set(), 10) == ([], True)


def test_new_highs_exclude_tracked_names():
    def signal(symbol):
        return SignalRow(symbol, "2026-11-03", ["near_52w_high"], 1.0, 1.0, 1.0, 1.0, 1.0)

    rows = [signal("NEW"), signal("AAA")]
    assert new_high_names(rows, {"AAA"}) == ["NEW"]


def test_gain_note_follows_the_configured_trim(cfg, raw):
    from breakout_runner.config.loader import parse_config

    raw["ledger_mirror"]["trim_multiple"] = 1.3
    changed = parse_config(raw)
    assert "+30% level" in run(steady(), changed).notes
