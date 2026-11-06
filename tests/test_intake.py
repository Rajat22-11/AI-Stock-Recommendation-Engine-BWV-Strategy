from __future__ import annotations

import datetime as dt
import random
from dataclasses import replace

import httpx
import pytest

from breakout_runner.client import TapetideApi, TapetideClient, TokenPool
from breakout_runner.client.endpoints import CorporateAction, ScreenRow, SurveillanceRow
from breakout_runner.config.models import LocalRatio, Predicate
from breakout_runner.data.bars import DailyBar
from breakout_runner.indicators.weekly import Extension, Metrics
from breakout_runner.intake.screens import (
    Q1,
    Q2,
    Candidate,
    UnknownRatio,
    build_query,
    corporate_action_warnings,
    grade_row,
    local_ratios,
    merge_candidates,
    non_equity_symbols,
    post_filter_reason,
    rank_candidates,
    ratio_names,
    run_screen,
    surveillance_state,
    validate_ratio_names,
    verify_rows,
)

D = dt.date.fromisoformat


def row(symbol="AAA", **over):
    values = dict(
        rank=1,
        symbol=symbol,
        name=symbol,
        ltp=100.0,
        pe=20.0,
        market_cap_cr=5000.0,
        net_profit_qtr_cr=50.0,
        profit_growth_qtr_pct=25.0,
        sales_qtr_cr=500.0,
        sales_growth_qtr_pct=20.0,
        roce_pct=25.0,
        roe_pct=None,
        sort_value=25.0,
        ratio_values={},
    )
    return ScreenRow(**(values | over))


def metrics(**over):
    values = dict(
        week=D("2026-09-14"),
        close=100.0,
        pivot=98.0,
        breakout_close=True,
        already_broke_out=False,
        prior_ath_just_above=False,
        baseline=1000.0,
        vol_mult=2.5,
        sessions=5,
        smas={13: 90.0, 26: 80.0, 39: 70.0, 52: 60.0},
        sma13=90.0,
        sma13_slope=2.0,
        sma13_rising=True,
        dist_sma_pct=11.1,
        stacked=True,
        close_loc=0.8,
        correction=None,
        first_higher_low=None,
        base_range_pct=15.0,
        base_class="normal",
        base_quality="normal",
        high_52w=100.0,
        below_high_pct=0.0,
        structure_ok=True,
        extension=Extension(None, ()),
    )
    return Metrics(**(values | over))


def test_query_text_comes_from_config(cfg, raw):
    from breakout_runner.config.loader import parse_config

    text = build_query(cfg.screens.trigger_q1, cfg.screens.common)
    assert "Down from 52w high < 4 AND\n" in text
    assert "Current price > DMA 50 AND\n" in text
    assert text.endswith("Pledged percentage < 10")
    assert "*" not in text
    raw["screens"]["trigger_q1"][0]["value"] = 3
    changed = parse_config(raw)
    assert "Down from 52w high < 3 AND" in build_query(
        changed.screens.trigger_q1, changed.screens.common
    )


def test_q2_has_both_bounds(cfg):
    text = build_query(cfg.screens.alert_q2, cfg.screens.common)
    assert "Down from 52w high > 1.5" in text and "Down from 52w high < 5" in text


def test_unknown_ratio_aborts(cfg):
    catalogue = ratio_names(cfg.screens)
    validate_ratio_names(cfg.screens, catalogue)
    with pytest.raises(UnknownRatio, match="DMA 200"):
        validate_ratio_names(cfg.screens, catalogue - {"DMA 200"})


def _api(cfg, handler):
    pool = TokenPool.from_config(cfg.tapetide, {"TAPETIDE_TOKEN": "t"})
    return TapetideApi(
        TapetideClient(
            "https://t.example",
            cfg.http,
            pool,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
    )


def _screen_row(symbol):
    r = row(symbol)
    return {
        "rank": 1,
        "symbol": symbol,
        "name": symbol,
        "ltp": r.ltp,
        "pe": r.pe,
        "market_cap_cr": r.market_cap_cr,
        "dividend_yield_pct": 0,
        "net_profit_qtr_cr": r.net_profit_qtr_cr,
        "profit_growth_qtr_pct": r.profit_growth_qtr_pct,
        "sales_qtr_cr": r.sales_qtr_cr,
        "sales_growth_qtr_pct": r.sales_growth_qtr_pct,
        "roce_pct": r.roce_pct,
        "roe_pct": r.roe_pct,
        "sort_value": r.sort_value,
        "ratio_values": {},
    }


def test_short_pages_continue_until_cursor_ends(cfg):
    pages = [
        {"data": [_screen_row("A"), _screen_row("B")], "meta": {"next_cursor": "c1"}},
        {"data": [_screen_row("C")], "meta": {"next_cursor": None}},
    ]
    cursors = []

    def handler(request):
        cursors.append((request.url.params.get("cursor"), request.url.params["limit"]))
        return httpx.Response(200, json=pages.pop(0))

    rows = run_screen(_api(cfg, handler), "q", cfg.screens)
    assert [r.symbol for r in rows] == ["A", "B", "C"]
    assert cursors == [(None, "50"), ("c1", "50")]


def test_row_cap_stops_paging(cfg):
    def handler(request):
        rows = [_screen_row(f"S{i}") for i in range(60)]
        return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": "more"}})

    assert len(run_screen(_api(cfg, handler), "q", cfg.screens)) == cfg.screens.max_rows


PREDS = [
    Predicate(ratio="Down from 52w high", op="<", value=4),
    Predicate(ratio="Current price", op=">", ref="DMA 50"),
]


def test_verify_pass_and_null_is_unknown(cfg):
    good = row(
        "G", ratio_values={"Down from 52w high": 2.0, "Current price": 100.0, "DMA 50": 90.0}
    )
    null = row(
        "N", ratio_values={"Down from 52w high": None, "Current price": 100.0, "DMA 50": 90.0}
    )
    result = verify_rows([good, null], PREDS, cfg.screens)
    assert result.mismatched == () and result.unknown == 1 and result.trusted


def test_current_price_falls_back_to_ltp(cfg):
    r = row("L", ltp=50.0, ratio_values={"Down from 52w high": 2.0, "DMA 50": 90.0})
    assert verify_rows([r], PREDS, cfg.screens).mismatched == ("L",)


def test_drift_above_limit_is_untrusted(cfg):
    good = row(
        "G", ratio_values={"Down from 52w high": 2.0, "Current price": 100.0, "DMA 50": 90.0}
    )
    bad = row("B", ratio_values={"Down from 52w high": 9.0, "Current price": 100.0, "DMA 50": 90.0})
    result = verify_rows([good] * 8 + [bad] * 2, PREDS, cfg.screens)
    assert result.drift_pct == 20 and result.trusted is False


@pytest.mark.parametrize(
    "over, expected",
    [
        ({}, "A"),
        ({"profit_growth_qtr_pct": 10.0}, "B"),
        ({"roce_pct": 11.0}, "C"),
        ({"sales_growth_qtr_pct": -1.0}, "C"),
        ({"roce_pct": None}, "unknown"),
        ({"net_profit_qtr_cr": -1.0}, None),
        ({"profit_growth_qtr_pct": -31.0}, None),
    ],
)
def test_fundamental_grade(cfg, over, expected):
    assert grade_row(row(**over), cfg.fundamental_grade) == expected


def test_trigger_screen_wins_when_a_name_is_in_both(cfg):
    candidates, dropped = merge_candidates(
        [row("A")], [row("A"), row("B"), row("BAD", net_profit_qtr_cr=-5.0)], cfg.fundamental_grade
    )
    assert {c.symbol: c.screen for c in candidates} == {"A": Q1, "B": Q2}
    assert dropped == ["BAD"]


def cand(symbol="AAA", screen=Q1, grade="A", **over):
    return Candidate(symbol, screen, grade, row(symbol), metrics=metrics(**over))


def test_volume_far_above_the_band_is_not_filtered(cfg):
    assert post_filter_reason(cand(vol_mult=9.0), cfg) is None


def test_trigger_extension_cap(cfg):
    c = cand(close=104.0, sma13=90.0)
    assert post_filter_reason(c, cfg) == "too far above the 13-week SMA"


def test_alert_extension_cap_is_tighter(cfg):
    assert post_filter_reason(cand(screen=Q2, close=100.0, sma13=90.0), cfg) is not None
    assert post_filter_reason(cand(screen=Q1, close=100.0, sma13=90.0), cfg) is None


def test_trigger_atr_reaches_the_risk_bands_but_alert_cap_applies(cfg):
    c = cand()
    c.atr_pct = 6.0
    assert post_filter_reason(c, cfg) is None
    c = cand(screen=Q2, close=95.0)
    c.atr_pct = 6.0
    assert post_filter_reason(c, cfg) == "ATR above the screen cap"


def test_pullback_and_price_and_turnover_filters(cfg):
    assert post_filter_reason(cand(below_high_pct=11.0), cfg) == "pullback, not a breakout"
    assert post_filter_reason(cand(close=20.0), cfg) == "below minimum price"
    c = cand()
    c.turnover_cr = 0.5
    assert post_filter_reason(c, cfg) == "below minimum turnover"


def test_ranking_order_and_rank_numbers():
    pool = [
        cand("Z_ALERT", Q2, "A", base_quality="tight_dry", dist_sma_pct=1.0),
        cand("LOOSE", Q1, "A", base_quality="loose", dist_sma_pct=1.0),
        cand("FAR", Q1, "A", base_quality="tight_dry", dist_sma_pct=9.0),
        cand("NEAR_C", Q1, "C", base_quality="tight_dry", dist_sma_pct=2.0),
        cand("NEAR_A", Q1, "A", base_quality="tight_dry", dist_sma_pct=2.0),
    ]
    ranked = rank_candidates(pool)
    assert [c.symbol for c in ranked] == ["NEAR_A", "NEAR_C", "FAR", "LOOSE", "Z_ALERT"]
    assert [c.rank for c in ranked] == [1, 2, 3, 4, 5]


def test_rank_is_independent_of_input_order():
    pool = [cand(f"S{i}", dist_sma_pct=float(i % 3)) for i in range(20)]
    expected = [c.symbol for c in rank_candidates(list(pool))]
    shuffled = list(pool)
    random.Random(7).shuffle(shuffled)
    assert [c.symbol for c in rank_candidates(shuffled)] == expected


def surv(framework="asm", as_of="2026-11-04", symbol="AAA"):
    return SurveillanceRow(symbol, framework, "ST-ASM", 1, as_of, "2026-10-01")


def test_empty_surveillance_is_unknown_not_clear(cfg):
    state = surveillance_state([], D("2026-11-05"), cfg.surveillance)
    assert state.known is False and state.flags_for("AAA") is None


def test_stale_surveillance_is_unknown(cfg):
    state = surveillance_state([surv(as_of="2026-10-01")], D("2026-11-05"), cfg.surveillance)
    assert state.known is False


def test_surveillance_membership_and_clear(cfg):
    rows = [surv("asm"), surv("esm"), replace(surv("gsm"), symbol="BBB")]
    state = surveillance_state(rows, D("2026-11-05"), cfg.surveillance)
    assert state.known and state.flags_for("CLEAN") == ()
    assert state.flags_for("AAA") == ("asm:ST-ASM:1", "esm:ST-ASM:1")
    assert state.flags_for("BBB") == ("gsm:ST-ASM:1",)


def _daily(closes, highs=None, volumes=None):
    start = D("2026-01-01")
    return [
        DailyBar(
            date=start + dt.timedelta(days=i),
            open=c,
            high=(highs or closes)[i],
            low=c,
            close=c,
            raw_close=c,
            volume=(volumes or [100.0] * len(closes))[i],
            raw_volume=0,
        )
        for i, c in enumerate(closes)
    ]


def test_all_null_rows_are_not_trusted(cfg):
    """Real screen rows carried null DMA and 52-week values, which used to pass as zero drift."""
    empty = row("E", ltp=None, ratio_values={"Down from 52w high": None, "DMA 50": None})
    result = verify_rows([empty] * 3, PREDS, cfg.screens)
    assert result.verified == 0 and result.drift_pct == 0 and result.trusted is False


def test_no_rows_stay_trusted(cfg):
    assert verify_rows([], PREDS, cfg.screens).trusted is True


def test_bars_supply_the_ratio_the_screen_left_null(cfg):
    null = row("N", ltp=100.0, ratio_values={"Down from 52w high": None, "DMA 50": None})
    local = {"N": {"Down from 52w high": 9.0, "DMA 50": 90.0, "Current price": 100.0}}
    result = verify_rows([null], PREDS, cfg.screens, local)
    assert result.mismatched == ("N",) and result.verified == 1 and result.trusted is False


def test_local_ratios_from_bars():
    specs = {
        "Current price": LocalRatio(kind="last_close", sessions=1),
        "DMA": LocalRatio(kind="sma_close", sessions=2),
        "Down": LocalRatio(kind="below_high_pct", sessions=3),
        "Vol": LocalRatio(kind="avg_volume", sessions=2),
        "Long": LocalRatio(kind="sma_close", sessions=99),
    }
    daily = _daily([100.0, 110.0, 90.0], highs=[100.0, 120.0, 90.0], volumes=[10.0, 20.0, 40.0])
    values = local_ratios(daily, specs)
    assert values["Current price"] == 90.0 and values["DMA"] == 100.0 and values["Vol"] == 30.0
    assert values["Down"] == pytest.approx(25.0)
    assert "Long" not in values


def test_excluded_symbol_is_dropped_before_grading(cfg):
    candidates, dropped = merge_candidates(
        [row("CAPINVIT"), row("KEEP")], [], cfg.fundamental_grade, exclude={"CAPINVIT"}
    )
    assert [c.symbol for c in candidates] == ["KEEP"] and dropped == ["CAPINVIT"]


def test_configured_ratio_names_exist_in_the_screens(cfg):
    assert set(cfg.screens.local_ratios) <= ratio_names(cfg.screens)


def _action(symbol, action_type="split", series="EQ"):
    return CorporateAction(symbol, series, action_type, "2026-11-16", 10, 1)


def test_split_warning_only_for_names_we_hold(cfg):
    actions = [_action("HELD"), _action("OTHER"), _action("HELD", "dividend")]
    warned = corporate_action_warnings(actions, {"HELD"}, cfg.corporate_actions)
    assert warned == {"HELD": ["split 10:1 ex 2026-11-16"]}


def test_non_equity_series_are_named(cfg):
    actions = [_action("TRUST", "dividend", "IV"), _action("REIT", "other", "RR"), _action("EQTY")]
    assert non_equity_symbols(actions, cfg.corporate_actions) == {"TRUST", "REIT"}
