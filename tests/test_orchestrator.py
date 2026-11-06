"""Whole runs against a fake API: mode selection, ledger order, shedding, failure and reruns."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import psycopg
import pytest
import yaml

from breakout_runner.client import AllTokensExhausted
from breakout_runner.client.endpoints import (
    CorporateAction,
    HeatmapRow,
    LiveQuote,
    PriceBar,
    PricePage,
    ScreenPage,
    ScreenRow,
    SignalRow,
)
from breakout_runner.compare import AGREE, DIFFER, ONLY_CLAUDE, compare_rows, render_report
from breakout_runner.data.calendar import TradingCalendar
from breakout_runner.orchestrator import (
    FAILED,
    MIDWEEK,
    PARTIAL,
    SUCCEEDED,
    WEEKLY_SCAN,
    RunContext,
    decide_mode,
    execute,
    run_full_scan,
    run_late_sweep,
    run_midweek,
)
from breakout_runner.writer.tracker import Tracker

D = dt.date.fromisoformat
FRIDAY = D("2026-11-06")
ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("BREAKOUT_TEST_DB_URL")
SCHEMA = Path(__file__).parent / "db" / "tracker_schema.sql"
VERSION = "runner-2026-11-06-abcd1234"
pg = pytest.mark.skipif(not URL, reason="BREAKOUT_TEST_DB_URL is not set")


def weekly_path(last: list[float], volumes: list[float] | None = None) -> list[tuple[float, float]]:
    """Rise, a deep correction, a recovery, a tight base, then the given final weeks."""
    path = [100 + i * 2.5 for i in range(41)]
    path += [200 - i * 3.5 for i in range(20)]
    path += [130 + i * 2.4 for i in range(25)]
    path += [186, 188, 190, 187, 189, 191, 188, 190, 185, 186, 187, 188, 189, 195, 188, 187, 186]
    path += last
    vols = [120000.0] * (len(path) - len(last)) + (volumes or [120000.0] * len(last))
    return list(zip(path, vols, strict=True))


def daily_bars(path: list[tuple[float, float]], through: dt.date = FRIDAY) -> list[PriceBar]:
    monday = D("2026-11-02") - dt.timedelta(weeks=len(path) - 1)
    bars = []
    for w, (close, volume) in enumerate(path):
        for i in range(5):
            day = monday + dt.timedelta(weeks=w, days=i)
            if day > through:
                continue
            bars.append(
                PriceBar(
                    date=day.isoformat(),
                    open=close,
                    high=round(close * 1.01, 2),
                    low=round(close * 0.99, 2),
                    close=close,
                    raw_close=close,
                    volume=int(volume),
                )
            )
    return bars


BREAKOUT = weekly_path([215.0], [300000.0])
ALERT = weekly_path([189.0], [80000.0])
SECOND_WEEK = weekly_path([205.0, 208.0], [300000.0, 300000.0])
FALLEN = weekly_path([120.0])


def screen_row(symbol: str) -> ScreenRow:
    return ScreenRow(
        rank=1,
        symbol=symbol,
        name=symbol,
        ltp=200.0,
        pe=20.0,
        market_cap_cr=5000.0,
        net_profit_qtr_cr=50.0,
        profit_growth_qtr_pct=25.0,
        sales_qtr_cr=500.0,
        sales_growth_qtr_pct=20.0,
        roce_pct=25.0,
        roe_pct=None,
        sort_value=25.0,
        ratio_values={"Down from 52w high": None, "DMA 50": None, "Current price": 200.0},
    )


class FakeClient:
    def refresh_usage(self):
        pass


class FakeApi:
    def __init__(self, q1=(), q2=(), bars=None, exhaust_on=None, signals=()):
        self.client = FakeClient()
        self.q1, self.q2 = list(q1), list(q2)
        self.bars = bars or {}
        self.calls: list[str] = []
        self.exhaust_on = exhaust_on
        self.signal_rows = list(signals)
        self.actions: list[CorporateAction] = []
        self.quotes: dict[str, float] = {}

    def screen_field_names(self):
        return {
            "Down from 52w high",
            "Current price",
            "DMA 50",
            "DMA 200",
            "Volume 1week average",
            "Volume 1year average",
            "Market Capitalization",
            "Debt to equity",
            "Pledged percentage",
            "Return on capital employed",
        }

    def screen_fundamental(self, query, sort, order, limit, cursor=None):
        self.calls.append("screen")
        rows = self.q2 if "Down from 52w high > 1.5" in query else self.q1
        return ScreenPage([screen_row(s) for s in rows], len(rows), None)

    def price(self, symbol, days, end_date=None):
        self.calls.append(f"price:{symbol}")
        if symbol == self.exhaust_on:
            raise AllTokensExhausted(1793989800)
        bars = daily_bars(self.bars[symbol])
        return PricePage(bars, bars[-1].date, bars[0].date, False, None, bars[0].date)

    def heatmap(self, slug, limit):
        self.calls.append("heatmap")
        return [HeatmapRow(f"U{i}", i * 1.0, i * 1.0, i * 1.0, "2026-11-06") for i in range(100)]

    def surveillance(self, limit):
        self.calls.append("surveillance")
        return []

    def corporate_actions(self, limit):
        self.calls.append("corporate_actions")
        return self.actions

    def fundamentals(self, symbol, metrics, period_type, start=None):
        self.calls.append(f"fundamentals:{symbol}")
        return []

    def shareholding(self, symbol):
        return {}

    def promoter_pledge(self, symbol):
        self.calls.append(f"pledge:{symbol}")
        return []

    def market_signals(self, signal, min_market_cap_cr, limit):
        self.calls.append("signals")
        return self.signal_rows

    def live_quotes(self, symbols):
        self.calls.append("quotes")
        return [LiveQuote(s, self.quotes.get(s), None, None, None, None, "closed") for s in symbols]


class FakePool:
    def __init__(self, remaining=None):
        self.remaining = remaining

    def combined_remaining(self):
        return self.remaining

    def ensure_capacity(self, need):
        pass

    def usage_report(self):
        return {"primary": {"total": 1}}

    def redact(self, text):
        return text


def context(cfg, api, tracker, today=FRIDAY, pool=None, calendar=None):
    return RunContext(
        config=cfg,
        api=api,
        pool=pool or FakePool(),
        tracker=tracker,
        calendar=calendar or TradingCalendar([]),
        today=today,
        now=dt.datetime(2026, 11, 6, 14, 0, tzinfo=dt.UTC),
        version=VERSION,
        sha256="abcd",
        trigger_id="ci",
        session_id="1",
    )


def shadow():
    return Tracker(None, live=False)


def signals(tracker):
    return {r["symbol"]: r for r in tracker.outbox.get("signals", [])}


# Mode selection, with the date standing in for a faked clock.
def test_friday_is_a_full_scan_and_thursday_a_check_in(cfg):
    cal = TradingCalendar([])
    assert decide_mode(FRIDAY, cal, cfg) == WEEKLY_SCAN
    assert decide_mode(D("2026-11-05"), cal, cfg) == MIDWEEK


def test_friday_holiday_moves_the_scan_to_thursday(cfg):
    cal = TradingCalendar([FRIDAY])
    assert decide_mode(D("2026-11-05"), cal, cfg) == WEEKLY_SCAN
    assert decide_mode(FRIDAY, cal, cfg) is None


def test_weekend_and_mid_week_holiday_skip(cfg):
    assert decide_mode(D("2026-11-07"), TradingCalendar([]), cfg) is None
    assert decide_mode(D("2026-11-04"), TradingCalendar([D("2026-11-04")]), cfg) is None


def test_unconfigured_weekday_skips(cfg):
    schedule = cfg.schedule.model_copy(update={"midweek_days": ["TUE"]})
    config = cfg.model_copy(update={"schedule": schedule})
    assert decide_mode(D("2026-11-03"), TradingCalendar([]), config) == MIDWEEK
    assert decide_mode(D("2026-11-04"), TradingCalendar([]), config) is None


def test_full_scan_writes_entry_and_alert_without_touching_a_database(cfg):
    tracker = shadow()
    api = FakeApi(["NEW"], ["NEAR"], {"NEW": BREAKOUT, "NEAR": ALERT})
    state = execute(context(cfg, api, tracker), run_full_scan)
    rows = signals(tracker)
    assert state.status == SUCCEEDED, state.problems
    assert rows["NEW"]["verdict"] == "ENTRY" and rows["NEW"]["provisional"] is True
    assert rows["NEW"]["window_start"] == D("2026-11-09")
    assert rows["NEW"]["window_end"] == D("2026-11-12")
    assert "review_packet" in rows["NEW"]["raw"]
    assert rows["NEAR"]["verdict"] == "WAIT_ALERT"
    assert len(tracker.outbox["ledger_calls"]) == 2
    assert len(tracker.outbox["signal_rationale"]) == 2
    assert tracker.outbox["runs"][0]["status"] == SUCCEEDED
    assert tracker.outbox["reviews"][0]["period"] == "weekly"


def test_screen_rows_with_null_ratios_are_still_verified_from_bars(cfg):
    tracker = shadow()
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT})
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert any("Q1 drift 0.0% over 1 of 1" in note for note in state.notes)


def test_a_name_failing_the_screen_on_bars_makes_the_run_watch_only(cfg):
    tracker = shadow()
    bad = ["BAD"]
    good = ["NEW"]
    api = FakeApi(good + bad, [], {"NEW": BREAKOUT, "BAD": FALLEN})
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert state.status == PARTIAL
    assert any("Q1 failed re-verification" in p for p in state.problems)
    assert signals(tracker)["NEW"]["verdict"] == "WAIT"
    assert signals(tracker)["NEW"]["raw"]["rules_fired"] == ["U-04"]


def test_second_week_breakout_is_a_wait(cfg):
    tracker = shadow()
    api = FakeApi(["TWICE"], [], {"TWICE": SECOND_WEEK})
    execute(context(cfg, api, tracker), run_full_scan)
    assert signals(tracker)["TWICE"]["verdict"] == "WAIT"
    assert signals(tracker)["TWICE"]["raw"]["rules_fired"] == ["S1-14"]


def test_excluded_invit_costs_no_bar_call(cfg):
    tracker = shadow()
    api = FakeApi(["CAPINVIT", "NEW"], [], {"NEW": BREAKOUT})
    execute(context(cfg, api, tracker), run_full_scan)
    assert "price:CAPINVIT" not in api.calls and "CAPINVIT" not in signals(tracker)


def test_corporate_actions_warn_and_suggest(cfg):
    tracker = shadow()
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT})
    api.actions = [CorporateAction("TRUST", "IV", "dividend", "2026-11-10", None, None)]
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert any("Add to universe.exclude_symbols: TRUST" in n for n in state.notes)


def test_all_tokens_exhausted_closes_partial_and_exits_non_zero(cfg):
    tracker = shadow()
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT}, exhaust_on="NEW")
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert state.status == PARTIAL
    assert any("exhausted" in p for p in state.problems)
    assert tracker.outbox["runs"][0]["status"] == PARTIAL


def test_incomplete_friday_bar_is_listed_for_the_late_sweep(cfg):
    tracker = shadow()
    path = {"LATE": BREAKOUT}
    api = FakeApi(["LATE"], [], path)
    api.price = lambda symbol, days, end_date=None: _page(path[symbol], D("2026-11-05"))
    execute(context(cfg, api, tracker), run_full_scan)
    row = signals(tracker)["LATE"]
    assert row["verdict"] == "WAIT" and row["provisional"] is True
    assert '"not_final"' in tracker.outbox["runs"][0]["notes"]
    assert '"LATE"' in tracker.outbox["runs"][0]["notes"]


def _page(path, through):
    bars = daily_bars(path, through)
    return PricePage(bars, bars[-1].date, bars[0].date, False, None, bars[0].date)


def test_quota_short_sheds_in_config_order(cfg):
    tracker = shadow()
    api = FakeApi(["NEW"], ["NEAR"], {"NEW": BREAKOUT, "NEAR": ALERT})
    reserves = cfg.quota.reserve_calls + cfg.quota.reserve_for_claude
    state = execute(context(cfg, api, tracker, pool=FakePool(reserves + 2)), run_full_scan)
    assert any(n.startswith("Shed:") for n in state.notes)
    assert "price:NEAR" not in api.calls
    assert "surveillance" not in api.calls and "corporate_actions" not in api.calls


# Mid-week
def friday_row(symbol, trigger=197.0, verdict="WAIT_ALERT", sid=1):
    return {
        "id": sid,
        "symbol": symbol,
        "verdict": verdict,
        "signal_date": D("2026-10-30"),
        "alert_trigger": trigger,
        "pivot": trigger,
        "stop": 180.0,
        "req_vol_abs": 500000,
        "setup": "A_base",
    }


class ReadTracker(Tracker):
    def __init__(self, friday=(), positions=(), last=None):
        super().__init__(None, live=False)
        self.friday = list(friday)
        self.positions = list(positions)
        self.last = last

    def friday_rows(self, today, max_age_days):
        return self.friday

    def position_rows(self):
        return self.positions

    def book_started_on(self):
        return D("2026-10-04")

    def last_run(self, run_type):
        return self.last


def position(symbol="HELD"):
    return {
        "position_id": 1,
        "signal_id": 9,
        "symbol": symbol,
        "status": "open",
        "signal_date": D("2026-10-30"),
        "pivot": 170.0,
        "fill_date": D("2026-11-02"),
        "fill_price": 175.0,
        "qty_initial": 100,
        "qty_open": 100,
        "avg_cost": 175.0,
        "stop_current": 161.0,
        "trim_price": 218.75,
        "trimmed": False,
    }


def test_check_in_is_provisional_and_runs_no_screen(cfg):
    tracker = ReadTracker([friday_row("NEAR")], [position()])
    api = FakeApi(bars={"NEAR": ALERT, "HELD": ALERT})
    today = D("2026-11-04")
    api.price = lambda s, d, e=None: _page(api.bars[s], today)
    state = execute(context(cfg, api, tracker, today=today), run_midweek)
    rows = signals(tracker)
    assert state.status == SUCCEEDED, state.problems
    assert "screen" not in api.calls
    assert rows["NEAR"]["provisional"] is True and rows["NEAR"]["source"] == MIDWEEK
    assert rows["HELD"]["provisional"] is True and rows["HELD"]["verdict"] == "HOLD"


def test_budget_sheds_new_highs_first_then_quotes_the_shed_names(cfg):
    quota = cfg.quota.model_copy(update={"midweek_budget_calls": 2})
    config = cfg.model_copy(update={"quota": quota})
    friday = [friday_row(f"N{i}", sid=i + 1) for i in range(4)]
    tracker = ReadTracker(friday)
    api = FakeApi(bars={f"N{i}": ALERT for i in range(4)})
    today = D("2026-11-04")

    def price(s, d, e=None):
        api.calls.append(f"price:{s}")
        return _page(api.bars[s], today)

    api.price = price
    api.quotes = {"N2": 195.0, "N3": 150.0}
    state = execute(context(config, api, tracker, today=today), run_midweek)
    assert "signals" not in api.calls and api.calls.count("quotes") == 1
    assert sum(c.startswith("price:") for c in api.calls) == 2
    assert any("N2 195.0 vs trigger" in n for n in state.notes)
    assert not any(n.startswith("N3") for n in state.notes)


def test_stale_friday_state_covers_positions_only(cfg):
    tracker = ReadTracker([], [position()])
    api = FakeApi(bars={"HELD": ALERT})
    today = D("2026-11-04")
    api.price = lambda s, d, e=None: _page(api.bars[s], today)
    state = execute(context(cfg, api, tracker, today=today), run_midweek)
    assert any("open positions only" in n for n in state.notes)
    assert set(signals(tracker)) == {"HELD"}


def test_new_high_names_are_marked_as_not_from_friday(cfg):
    tracker = ReadTracker([friday_row("NEAR")])
    row = SignalRow("FRESH", "2026-11-03", ["near_52w_high"], 10.0, 1.0, 2.0, 11.0, 2000.0)
    api = FakeApi(bars={"NEAR": ALERT}, signals=[row])
    today = D("2026-11-04")
    api.price = lambda s, d, e=None: _page(api.bars[s], today)
    state = execute(context(cfg, api, tracker, today=today), run_midweek)
    assert any("not on Friday's list: FRESH" in n for n in state.notes)


# Late sweep
def test_sweep_with_nothing_pending_spends_nothing(cfg):
    tracker = ReadTracker(last={"notes": '{"not_final": []}', "data_as_of": FRIDAY})
    api = FakeApi()
    state = execute(context(cfg, api, tracker, today=D("2026-11-07")), run_late_sweep)
    assert api.calls == [] and state.notes == ["Nothing to sweep"]


def test_sweep_evaluates_only_the_listed_symbols(cfg):
    notes = '{"not_final": [{"symbol": "LATE", "screen": "Q1", "grade": "A"}]}'
    tracker = ReadTracker(last={"notes": notes, "data_as_of": FRIDAY})
    api = FakeApi(bars={"LATE": BREAKOUT, "OTHER": BREAKOUT})
    state = execute(context(cfg, api, tracker, today=D("2026-11-07")), run_late_sweep)
    assert state.status == SUCCEEDED, state.problems
    assert "price:LATE" in api.calls and "price:OTHER" not in api.calls
    row = signals(tracker)["LATE"]
    assert row["verdict"] == "ENTRY" and row["signal_date"] == FRIDAY


# Against the tracker's real ledger
@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("drop schema public cascade")
        admin.execute("create schema public")
        admin.execute(SCHEMA.read_text("utf-8"))
    connection = psycopg.connect(URL)
    yield connection
    connection.close()


def _count(conn, table):
    found = conn.execute(f"select count(*) from {table}").fetchone()[0]
    conn.commit()
    return found


@pg
def test_live_run_then_rerun_inserts_no_new_signals(conn, cfg):
    tracker = Tracker(conn, live=True)
    api = FakeApi(["NEW"], ["NEAR"], {"NEW": BREAKOUT, "NEAR": ALERT})
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert state.status == SUCCEEDED, state.problems
    assert _count(conn, "signals") == 2 and _count(conn, "signal_rationale") == 2
    assert _count(conn, "reviews") == 1 and _count(conn, "skill_versions") == 1
    ledger = conn.execute("select value #>> '{}' from config where key = 'processed_through'")
    assert ledger.fetchone()[0] == "2026-11-06"
    conn.commit()

    again = Tracker(conn, live=True)
    second = execute(context(cfg, FakeApi(["NEW"], ["NEAR"], api.bars), again), run_full_scan)
    assert second.counts == {"unchanged": 2}
    assert _count(conn, "signals") == 2


@pg
def test_second_ledger_call_is_skipped_when_signals_fail(conn, cfg):
    tracker = Tracker(conn, live=True)
    calls = []
    original = tracker.process_ledger
    tracker.process_ledger = lambda through: (calls.append(through), original(through))[1]

    def fail(*args, **kwargs):
        raise RuntimeError("signals table unavailable")

    tracker.write_signal = fail
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT})
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert state.status == FAILED and len(calls) == 1
    found = conn.execute("select status, summary from runs").fetchone()
    conn.commit()
    assert found[0] == "failed" and "signals table unavailable" in found[1]
    assert _count(conn, "signals") == 0


@pg
def test_ledger_mismatch_aborts_before_any_write(conn, cfg):
    conn.execute("update config set value = '9' where key = 'stop_cap_pct'")
    conn.commit()
    tracker = Tracker(conn, live=True)
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT})
    state = execute(context(cfg, api, tracker), run_full_scan)
    assert state.status == FAILED and api.calls == []
    assert _count(conn, "runs") == 0


@pg
def test_real_ledger_fills_a_promoted_entry_and_manages_it(conn, cfg):
    tracker = Tracker(conn, live=True)
    api = FakeApi(["NEW"], [], {"NEW": BREAKOUT})
    execute(context(cfg, api, tracker), run_full_scan)
    conn.execute("set local tracker.allow_signal_edit = 'on'")
    conn.execute("update signals set created_at = now() - interval '3 days'")
    conn.commit()
    promotion = context(cfg, None, Tracker(conn, live=True), today=D("2026-11-08"))
    promotion.pool = None
    promotion.now = dt.datetime.now(dt.UTC)
    from breakout_runner.orchestrator import run_promotion

    assert execute(promotion, run_promotion).status == SUCCEEDED
    conn.execute(
        "insert into price_bars values ('NEW', '2026-11-09', 220, 222, 218, 221, 1000, 't')"
    )
    conn.commit()
    conn.execute("select process_ledger('2026-11-09')")
    row = conn.execute("select status, fill_price, stop_current from positions").fetchone()
    conn.commit()
    assert row[0] == "open" and float(row[1]) == 220.0 and float(row[2]) == 202.4


# Shadow report
def test_compare_reports_each_kind_of_difference(cfg):
    runner = [
        {"symbol": "A", "verdict": "ENTRY", "stop": 100.0, "raw": {"rules_fired": ["S1-03"]}},
        {
            "symbol": "B",
            "verdict": "WAIT",
            "vol_mult": 1.8,
            "raw": {"rules_fired": ["S1-07"]},
        },
    ]
    claude = [
        {"symbol": "A", "verdict": "ENTRY_REDUCED", "stop": 100.5},
        {"symbol": "B", "verdict": "ENTRY_REDUCED", "vol_mult": 1.8},
        {"symbol": "C", "verdict": "HOLD"},
    ]
    report = {r.symbol: r for r in compare_rows(runner, claude, cfg)}
    assert report["A"].status == AGREE
    assert report["B"].status == DIFFER and report["B"].rules == ("S1-07",)
    assert report["C"].status == ONLY_CLAUDE
    text = render_report(list(report.values()))
    assert text.startswith("1 of 3") and "S1-07" in text


def test_workflow_crons_match_the_config(cfg):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "run.yml").read_text("utf-8"))
    triggers = workflow[True] if True in workflow else workflow["on"]
    crons = {entry["cron"] for entry in triggers["schedule"]}
    schedule = cfg.schedule
    assert crons == {
        schedule.cron_utc,
        schedule.late_sweep.cron_utc,
        schedule.promotion_sweep.cron_utc,
    }
