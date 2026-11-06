from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import httpx
import pytest

from breakout_runner.client import TapetideApi, TapetideClient, TokenPool
from breakout_runner.client.endpoints import PriceBar
from breakout_runner.data.bars import (
    CORPORATE_ACTION,
    HISTORY_SHORT,
    INSUFFICIENT,
    RAW_MISSING,
    build_history,
    correct_volumes,
    fetch_daily_bars,
    resample_weekly,
)
from breakout_runner.data.calendar import TradingCalendar, load_calendar

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
D = dt.date.fromisoformat
# Real closures seen in the ACMESOLAR daily rows.
CAL = TradingCalendar([D("2026-09-14"), D("2026-10-02"), D("2026-10-20")])


def daily_fixture():
    with open(FIXTURES / "ACMESOLAR_daily.csv") as fh:
        return [
            PriceBar(
                r["date"],
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["raw_close"]),
                int(r["volume"]),
            )
            for r in csv.DictReader(fh)
        ]


def bar(date, close=100.0, raw=None, volume=1000):
    return PriceBar(date, close, close, close, close, close if raw is None else raw, volume)


def series(start, weeks, **kw):
    first = D(start)
    rows = []
    for w in range(weeks):
        for d in range(5):
            rows.append(bar((first + dt.timedelta(days=7 * w + d)).isoformat(), **kw))
    return rows


def test_full_scan_day_is_friday_normally():
    assert CAL.is_full_scan_day(D("2026-09-18"))
    assert not CAL.is_full_scan_day(D("2026-09-17"))


def test_friday_holiday_moves_the_scan_to_thursday():
    assert CAL.is_full_scan_day(D("2026-10-01"))  # Friday 2 Oct is closed
    assert not CAL.is_full_scan_day(D("2026-10-02"))


def test_special_weekend_session_counts_as_a_session_but_keeps_friday_scan():
    sunday = D("2026-02-01")
    cal = TradingCalendar([], [sunday])
    assert cal.is_session(sunday)
    assert cal.is_full_scan_day(D("2026-01-30"))
    assert not cal.is_full_scan_day(sunday)


def test_window_end_is_the_session_before_the_next_scan():
    assert CAL.next_session(D("2026-09-18")) == D("2026-09-21")
    assert CAL.window_end(D("2026-09-18")) == D("2026-09-24")
    # The next scan moves to Thursday 1 Oct, so the window ends Wednesday.
    assert CAL.window_end(D("2026-09-25")) == D("2026-09-30")


def test_resample_matches_real_weekly_sums_and_sessions(cfg):
    """The same weeks as Tapetide's own weekly bars in ACMESOLAR_weekly.csv."""
    daily, _ = correct_volumes(daily_fixture(), cfg.data, cfg.indicators)
    weekly = {w.week: w for w in resample_weekly(daily, CAL)}
    short = weekly[D("2026-09-14")]
    assert short.sessions == 4 and short.volume == 19818031
    assert (short.open, short.high, short.low, short.close) == (402.15, 440.7, 402.15, 435.25)
    assert weekly[D("2026-09-21")].volume == 17968650 and weekly[D("2026-09-21")].sessions == 5
    assert weekly[D("2026-10-26")].volume == 7705017


def test_partial_week_is_labelled_on_5_nov(cfg):
    daily, _ = correct_volumes(daily_fixture(), cfg.data, cfg.indicators)
    weekly = resample_weekly(daily, CAL)
    latest = weekly[-1]
    assert latest.week == D("2026-11-02") and latest.sessions == 4
    assert latest.complete is False and latest.volume == 2415108 and latest.close == 407.9
    assert weekly[-2].complete is True


def test_friday_data_completes_the_week(cfg):
    rows = daily_fixture() + [bar("2026-11-06", 410.0)]
    weekly = resample_weekly(correct_volumes(rows, cfg.data, cfg.indicators)[0], CAL)
    assert weekly[-1].complete is True and weekly[-1].sessions == 5


def test_late_symbol_is_not_final(cfg):
    rows = daily_fixture()
    assert build_history("X", rows, CAL, D("2026-11-06"), cfg.data, cfg.indicators).final is False
    assert build_history("X", rows, CAL, D("2026-11-05"), cfg.data, cfg.indicators).final is True


def test_bonus_inside_baseline_puts_old_volume_in_todays_terms(cfg):
    # A 1:1 bonus: before it, the raw price is twice the adjusted close and shares are half.
    before = series("2026-06-01", 5, close=100.0, raw=200.0, volume=1000)
    after = series("2026-07-06", 5, close=100.0, raw=100.0, volume=2000)
    daily, flags = correct_volumes(before + after, cfg.data, cfg.indicators)
    assert CORPORATE_ACTION in flags
    assert {d.volume for d in daily} == {2000.0}
    assert daily[0].raw_volume == 1000


def test_constant_factor_changes_nothing(cfg):
    daily, flags = correct_volumes(series("2026-06-01", 5), cfg.data, cfg.indicators)
    assert not flags and {d.volume for d in daily} == {1000}


def test_small_rounding_in_factor_stays_within_tolerance(cfg):
    rows = series("2026-06-01", 2) + series("2026-06-15", 2, raw=100.2)
    assert correct_volumes(rows, cfg.data, cfg.indicators)[1] == set()


def test_null_raw_close_is_flagged_never_substituted(cfg):
    rows = series("2026-06-01", 5)
    rows[10] = PriceBar(rows[10].date, 100.0, 100.0, 100.0, 100.0, None, 1000)
    daily, flags = correct_volumes(rows, cfg.data, cfg.indicators)
    assert RAW_MISSING in flags
    assert daily[10].raw_close is None


def test_null_bars_beyond_limit_are_insufficient_and_not_zero_filled(cfg):
    rows = series("2026-01-05", 40)
    for i in (-3, -2):
        rows[i] = PriceBar(rows[i].date, None, None, None, None, None, None)
    h = build_history("X", rows, TradingCalendar([]), D("2026-10-09"), cfg.data, cfg.indicators)
    assert h.quality == INSUFFICIENT
    assert all(d.volume > 0 for d in h.daily)


def test_new_listing_is_history_short(cfg):
    rows = series("2026-09-07", 8)
    h = build_history("X", rows, TradingCalendar([]), D("2026-10-30"), cfg.data, cfg.indicators)
    assert h.quality == HISTORY_SHORT and HISTORY_SHORT in h.flags


@pytest.fixture
def api_for(cfg):
    def build(handler):
        pool = TokenPool.from_config(cfg.tapetide, {"TAPETIDE_TOKEN": "t"})
        client = TapetideClient(
            "https://t.example",
            cfg.http,
            pool,
            transport=httpx.MockTransport(handler),
            sleep=lambda _: None,
        )
        return TapetideApi(client)

    return build


def _page(rows, first, has_more=True, cursor=None):
    window = {"has_more": has_more, "next_cursor": cursor, "first_date": first}
    meta = {"window": window, "coverage": {"last_stored_date": "2026-11-05"}}
    return httpx.Response(200, json={"data": rows, "meta": meta})


def _row(date):
    return dict(date=date, open=1, high=1, low=1, close=1, raw_close=1, volume=1)


def test_paging_covers_a_window_the_first_page_cut_short(api_for):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if len(seen) == 1:
            return _page([_row("2026-10-01")], "2026-10-01", True, "2026-09-30")
        return _page([_row("2026-09-01")], "2026-09-01", True, "2026-08-31")

    rows = fetch_daily_bars(api_for(handler), "X", 70, D("2026-11-05"))
    assert [r.date for r in rows] == ["2026-09-01", "2026-10-01"]
    assert seen[1]["end_date"] == "2026-09-30"
    assert int(seen[1]["days"]) == 34


def test_full_window_makes_one_call_even_though_older_data_exists(api_for):
    calls = []

    def handler(request):
        calls.append(1)
        return _page([_row("2026-08-28")], "2026-08-28", True, "2026-08-27")

    fetch_daily_bars(api_for(handler), "X", 70, D("2026-11-05"))
    assert len(calls) == 1


def test_history_end_stops_paging(api_for):
    calls = []

    def handler(request):
        calls.append(1)
        return _page([_row("2026-10-20")], "2026-10-20", False, None)

    assert len(fetch_daily_bars(api_for(handler), "X", 840, D("2026-11-05"))) == 1
    assert len(calls) == 1


def test_calendar_cache_saves_the_unit(tmp_path, api_for, cfg):
    calls = []

    def handler(request):
        calls.append(1)
        rows = [
            {"date": "2026-11-09", "name": "x", "exchange": "NSE", "type": "FULL"},
            {"date": "2026-11-10", "name": "y", "exchange": "NSE", "type": "PARTIAL"},
        ]
        return httpx.Response(200, json={"data": rows, "meta": {"next_cursor": None}})

    api = api_for(handler)
    cache = tmp_path / "h.json"
    cal = load_calendar(api, cache, D("2026-11-05"), cfg.data)
    assert D("2026-11-09") in cal.holidays and D("2026-11-10") not in cal.holidays
    load_calendar(api, cache, D("2026-11-20"), cfg.data)
    assert len(calls) == 1
    load_calendar(api, cache, D("2026-12-31"), cfg.data)
    assert len(calls) == 2
