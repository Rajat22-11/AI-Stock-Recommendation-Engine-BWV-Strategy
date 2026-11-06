"""Contract tests on a real Postgres built from tests/db/tracker_schema.sql (live snapshot).
Set BREAKOUT_TEST_DB_URL to a disposable database: every test drops and rebuilds its schema."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import psycopg
import pytest

from breakout_runner.data.bars import DailyBar
from breakout_runner.data.calendar import TradingCalendar
from breakout_runner.orchestrator import FAILED, RunContext, execute, run_promotion
from breakout_runner.rules.types import Decision, Verdict
from breakout_runner.writer.reviews import outcome_tag, write_trade_reviews, write_weekly_review
from breakout_runner.writer.rows import AUTHOR, InvalidRow, content_hash, signal_row
from breakout_runner.writer.tracker import (
    INSERTED,
    SUPERSEDED,
    UNCHANGED,
    RunInProgress,
    Tracker,
)
from breakout_runner.writer.why import Review, apply_review, promote_due, review_packet

URL = os.environ.get("BREAKOUT_TEST_DB_URL")
SCHEMA = Path(__file__).parent / "db" / "tracker_schema.sql"
D = dt.date.fromisoformat
FRIDAY = D("2026-11-06")
VERSION = "runner-2026-11-06-abcd1234"

pg = pytest.mark.skipif(not URL, reason="BREAKOUT_TEST_DB_URL is not set")


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute("drop schema public cascade")
        admin.execute("create schema public")
        admin.execute(SCHEMA.read_text("utf-8"))
    connection = psycopg.connect(URL)
    yield connection
    connection.close()


@pytest.fixture
def tracker(conn):
    return Tracker(conn, live=True)


def open_run(tracker, run_type="weekly_scan", data_as_of=FRIDAY):
    run_id = tracker.open_run(
        run_type, "ci", "1", data_as_of, VERSION, "abcd", D("2026-11-06"), {"config_sha256": "abcd"}
    )
    return run_id


def entry(symbol="AAA", close=100.0, **over) -> Decision:
    values = dict(
        symbol=symbol,
        signal_date=FRIDAY,
        verdict=Verdict.ENTRY,
        provisional=True,
        setup="A_base",
        stage=1,
        notes="n",
        raw={"rules_fired": ["S1-03"], "conviction": {"level": "medium"}},
        signal_close=close,
        pivot=95.0,
        entry_low=close,
        entry_high=105.0,
        stop=96.6,
        stop_basis="cap8",
        trim_price=131.25,
        size_pct=10.0,
        risk_pct=8.0,
        priority_rank=1,
        window_start=D("2026-11-09"),
        window_end=D("2026-11-12"),
        rationale={"thesis": "t", "catalyst": "unverified", "risks": "r"},
    )
    return Decision(**{**values, **over})


def put(tracker, run_id, decision, source="weekly_scan"):
    row = signal_row(decision, source, VERSION)
    result = tracker.write_signal(row, run_id, set(), decision.rationale or None, 3)
    tracker.commit()
    return result


def bar(day, o=100.0, h=102.0, lo=99.0, c=101.0, v=1000):
    return DailyBar(
        date=D(day), open=o, high=h, low=lo, close=c, raw_close=c, volume=float(v), raw_volume=v
    )


def rows(conn, sql, *params):
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(sql, params)
        found = cur.fetchall()
    conn.commit()
    return found


CAL = TradingCalendar([])


@pg
def test_rerun_inserts_nothing(tracker, conn):
    run_id = open_run(tracker)
    assert put(tracker, run_id, entry()).action == INSERTED
    assert put(tracker, run_id, entry()).action == UNCHANGED
    assert rows(conn, "select count(*) n from signals")[0]["n"] == 1
    assert rows(conn, "select count(*) n from signal_rationale")[0]["n"] == 1


@pg
def test_changed_content_supersedes_and_keeps_the_old_row(tracker, conn):
    run_id = open_run(tracker)
    first = put(tracker, run_id, entry())
    second = put(tracker, run_id, entry(close=101.0))
    assert second.action == SUPERSEDED
    found = rows(conn, "select id, supersedes from signals order by id")
    assert found == [
        {"id": first.signal_id, "supersedes": None},
        {"id": second.signal_id, "supersedes": first.signal_id},
    ]


@pg
def test_excluded_row_is_not_superseded(tracker, conn):
    run_id = open_run(tracker)
    first = put(tracker, run_id, entry())
    row = signal_row(entry(close=101.0), "weekly_scan", VERSION)
    result = tracker.write_signal(row, run_id, {first.signal_id})
    tracker.commit()
    assert result.action == INSERTED
    assert rows(conn, "select supersedes from signals where id = %s", result.signal_id) == [
        {"supersedes": None}
    ]


@pg
def test_append_only_trigger_still_applies(tracker, conn):
    run_id = open_run(tracker)
    put(tracker, run_id, entry())
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("update signals set notes = 'x'")
    conn.rollback()


@pg
def test_every_validated_verdict_is_accepted_by_the_table(tracker):
    run_id = open_run(tracker)
    for i, verdict in enumerate(Verdict):
        put(tracker, run_id, entry(symbol=f"S{i}", verdict=verdict, rationale={}))


def test_invalid_verdict_is_rejected_before_any_insert():
    d = entry()
    d.verdict = type("V", (), {"value": "NO-GO"})()
    with pytest.raises(InvalidRow):
        signal_row(d, "weekly_scan", VERSION)


@pg
def test_duplicate_trigger_is_refused_and_old_own_row_is_closed(tracker, conn):
    run_id = open_run(tracker)
    with pytest.raises(RunInProgress):
        tracker.guard("weekly_scan", FRIDAY, 45)
    conn.execute("update runs set started_at = now() - interval '2 hours' where id = %s", [run_id])
    conn.commit()
    tracker.guard("weekly_scan", FRIDAY, 45)
    assert rows(conn, "select status from runs where id = %s", run_id) == [{"status": "failed"}]


@pg
def test_guard_leaves_another_writers_stuck_row_alone(tracker, conn):
    conn.execute(
        "insert into runs(run_type, data_as_of, status, started_at) "
        "values ('weekly_scan', %s, 'running', now() - interval '20 days')",
        [FRIDAY],
    )
    conn.commit()
    tracker.guard("weekly_scan", FRIDAY, 45)
    assert rows(conn, "select status from runs") == [{"status": "running"}]


@pg
def test_crash_closes_the_run_as_failed_and_discards_open_work(tracker, conn):
    ctx = RunContext(
        config=None,
        api=None,
        pool=None,
        tracker=tracker,
        calendar=CAL,
        today=FRIDAY,
        now=dt.datetime(2026, 11, 6, 19, 30, tzinfo=dt.UTC),
        version=VERSION,
        sha256="abcd",
    )

    def crashing(c):
        c.run_id = open_run(c.tracker)
        put_no_commit = signal_row(entry(), "weekly_scan", VERSION)
        c.tracker.write_signal(put_no_commit, c.run_id, set())
        raise RuntimeError("boom")

    state = execute(ctx, crashing)
    assert state.status == FAILED
    found = rows(conn, "select status, summary, finished_at is not null closed from runs")
    assert found == [{"status": "failed", "summary": "Failed: boom", "closed": True}]
    assert rows(conn, "select count(*) n from signals")[0]["n"] == 0


@pg
def test_transcription_check_and_incremental_upsert(tracker, conn):
    daily = [bar("2026-11-04", c=100.0), bar("2026-11-05", c=101.0)]
    assert tracker.upsert_bars("AAA", daily, 0.05) is True
    tracker.commit()
    moved = [bar("2026-11-05", c=110.0), bar("2026-11-06", c=111.0)]
    assert tracker.upsert_bars("AAA", moved, 0.05) is False
    tracker.commit()
    found = rows(conn, "select bar_date, close from price_bars order by bar_date")
    assert [float(r["close"]) for r in found] == [100.0, 101.0, 111.0]


def _ledger_ready(tracker, conn):
    run_id = open_run(tracker)
    first = put(tracker, run_id, entry())
    return run_id, first


@pg
def test_provisional_entry_never_becomes_a_position(tracker, conn):
    _ledger_ready(tracker, conn)
    tracker.process_ledger(FRIDAY)
    tracker.commit()
    assert rows(conn, "select count(*) n from positions")[0]["n"] == 0


@pg
def test_wednesday_review_does_not_fill_on_monday(tracker, conn, cfg):
    run_id, first = _ledger_ready(tracker, conn)
    bars = [
        bar("2026-11-09", o=100, h=104, lo=99, c=103),
        bar("2026-11-10", o=103, h=104, lo=102, c=103),
        bar("2026-11-11", o=103, h=104, lo=102, c=103),
        bar("2026-11-12", o=103, h=104, lo=102, c=103),
    ]
    tracker.upsert_bars("AAA", bars, 0.05)
    review = Review(why_grade="B", why_text="clean results")
    result = apply_review(tracker, first.signal_id, review, run_id, D("2026-11-11"), CAL, cfg)
    tracker.process_ledger(D("2026-11-12"))
    tracker.commit()
    position = rows(conn, "select status, fill_date, signal_id from positions")
    assert position == [
        {"status": "open", "fill_date": D("2026-11-12"), "signal_id": result.signal_id}
    ]


@pg
def test_grade_c_halves_size_and_keeps_the_original(tracker, conn, cfg):
    run_id, first = _ledger_ready(tracker, conn)
    review = Review(why_grade="C", why_text="weak", catalyst="order win")
    result = apply_review(tracker, first.signal_id, review, run_id, D("2026-11-06"), CAL, cfg)
    tracker.commit()
    new = rows(conn, "select * from signals where id = %s", result.signal_id)[0]
    assert new["verdict"] == "ENTRY_REDUCED" and new["provisional"] is False
    assert float(new["size_pct"]) == 5.0 and new["why_grade"] == "C"
    old = rows(conn, "select provisional, verdict from signals where id = %s", first.signal_id)
    assert old == [{"provisional": True, "verdict": "ENTRY"}]
    rationale = rows(conn, "select catalyst from signal_rationale where signal_id = %s", new["id"])
    assert rationale == [{"catalyst": "order win"}]


@pg
def test_rerun_after_promotion_does_not_undo_it(tracker, conn, cfg):
    run_id, first = _ledger_ready(tracker, conn)
    review = Review(why_grade="A", why_text="good")
    apply_review(tracker, first.signal_id, review, run_id, D("2026-11-06"), CAL, cfg)
    tracker.commit()
    assert put(tracker, run_id, entry()).action == UNCHANGED


def _age(conn, hours):
    conn.execute("set local tracker.allow_signal_edit = 'on'")
    conn.execute("update signals set created_at = now() - make_interval(hours => %s)", [hours])
    conn.commit()


@pg
def test_auto_reduced_promotes_after_grace_for_monday(tracker, conn, cfg):
    run_id, _ = _ledger_ready(tracker, conn)
    _age(conn, 49)
    sunday = D("2026-11-08")
    now = dt.datetime.now(dt.UTC)
    promoted, pending = promote_due(tracker, run_id, now, sunday, CAL, cfg)
    tracker.commit()
    assert promoted == ["AAA"] and pending == []
    new = rows(conn, "select * from signals where supersedes is not null")[0]
    assert new["verdict"] == "ENTRY_REDUCED" and new["window_start"] == D("2026-11-09")
    assert float(new["size_pct"]) == 5.0 and new["why_grade"] is None


@pg
def test_auto_reduced_waits_inside_grace(tracker, conn, cfg):
    run_id, _ = _ledger_ready(tracker, conn)
    now = dt.datetime.now(dt.UTC)
    assert promote_due(tracker, run_id, now, FRIDAY, CAL, cfg) == ([], ["AAA"])


@pg
def test_review_required_never_promotes(tracker, conn, cfg):
    run_id, _ = _ledger_ready(tracker, conn)
    _age(conn, 200)
    gate = cfg.why_gate.model_copy(update={"mode": "review_required"})
    config = cfg.model_copy(update={"why_gate": gate})
    now = dt.datetime.now(dt.UTC)
    assert promote_due(tracker, run_id, now, FRIDAY, CAL, config) == ([], ["AAA"])


@pg
def test_midweek_entry_is_never_auto_promoted(tracker, conn, cfg):
    run_id = open_run(tracker)
    put(tracker, run_id, entry(), source="midweek_checkin")
    _age(conn, 200)
    now = dt.datetime.now(dt.UTC)
    assert promote_due(tracker, run_id, now, FRIDAY, CAL, cfg) == ([], [])


@pg
def test_promotion_sweep_spends_no_api_calls(tracker, conn, cfg):
    _ledger_ready(tracker, conn)
    _age(conn, 49)
    ctx = RunContext(
        config=cfg,
        api=None,
        pool=None,
        tracker=tracker,
        calendar=CAL,
        today=D("2026-11-08"),
        now=dt.datetime.now(dt.UTC),
        version=VERSION,
        sha256="abcd",
    )
    state = execute(ctx, run_promotion)
    assert state.status == "succeeded"
    assert rows(conn, "select count(*) n from signals where not provisional")[0]["n"] == 1


def _closed_position(tracker, conn, run_id):
    first = put(tracker, run_id, entry(provisional=False))
    tracker.upsert_bars(
        "AAA",
        [bar("2026-11-09", o=100, h=101, lo=99, c=100), bar("2026-11-10", o=90, h=91, lo=88, c=89)],
        0.05,
    )
    tracker.process_ledger(D("2026-11-10"))
    tracker.commit()
    return first


@pg
def test_trade_review_uses_tracker_tag_once_and_never_edits_others(tracker, conn, cfg):
    run_id = open_run(tracker)
    first = _closed_position(tracker, conn, run_id)
    written = write_trade_reviews(tracker, run_id, D("2026-11-11"), cfg.reviews)
    tracker.commit()
    assert written == 1
    found = rows(conn, "select tag, author from trade_reviews")
    assert found == [{"tag": "gap_through_stop", "author": AUTHOR}]
    assert write_trade_reviews(tracker, run_id, D("2026-11-11"), cfg.reviews) == 0
    assert tracker.insert_trade_review(first.signal_id, "AAA", run_id, "gap_through_stop", "x") is (
        False
    )


@pg
def test_person_authored_review_is_left_alone(tracker, conn, cfg):
    run_id = open_run(tracker)
    first = _closed_position(tracker, conn, run_id)
    conn.execute(
        "insert into trade_reviews(signal_id, symbol, tag, lesson, author) "
        "values (%s, 'AAA', 'bad_entry', 'mine', 'rajat')",
        [first.signal_id],
    )
    conn.commit()
    assert write_trade_reviews(tracker, run_id, D("2026-11-11"), cfg.reviews) == 0
    assert rows(conn, "select lesson, author from trade_reviews") == [
        {"lesson": "mine", "author": "rajat"}
    ]


@pg
def test_loser_tag_maps_to_an_allowed_value(tracker, conn, cfg):
    run_id = open_run(tracker)
    put(tracker, run_id, entry(provisional=False))
    exit_signal = entry(
        verdict=Verdict.EXIT,
        provisional=False,
        signal_date=D("2026-11-09"),
        rationale={},
        entry_high=None,
    )
    put(tracker, run_id, exit_signal)
    tracker.upsert_bars(
        "AAA",
        [
            bar("2026-11-09", o=100, h=101, lo=99, c=100),
            bar("2026-11-10", o=99, h=99, lo=98, c=98),
        ],
        0.05,
    )
    tracker.process_ledger(D("2026-11-10"))
    tracker.commit()
    assert write_trade_reviews(tracker, run_id, D("2026-11-11"), cfg.reviews) == 1
    tracker.commit()
    assert rows(conn, "select tag from trade_reviews") == [{"tag": "other"}]


@pg
def test_old_wait_is_tagged_a_missed_runner(tracker, conn, cfg):
    run_id = open_run(tracker)
    wait = entry(verdict=Verdict.WAIT, provisional=False, signal_date=D("2026-10-01"), rationale={})
    first = put(tracker, run_id, wait)
    tracker.upsert_bars("AAA", [bar("2026-10-05", h=118.0)], 0.05)
    tracker.commit()
    assert write_trade_reviews(tracker, run_id, D("2026-11-06"), cfg.reviews) == 1
    tracker.commit()
    found = rows(conn, "select signal_id, tag from trade_reviews")
    assert found == [{"signal_id": first.signal_id, "tag": "missed_runner"}]


def test_outcome_tag_thresholds(cfg):
    assert outcome_tag({"max_up_4w_pct": 18.0}, cfg.reviews) == "missed_runner"
    assert outcome_tag({"max_up_4w_pct": 14.0}, cfg.reviews) is None
    assert outcome_tag({"max_up_4w_pct": 2.0, "max_down_4w_pct": -12.0}, cfg.reviews) == (
        "avoided_loser"
    )


@pg
def test_weekly_review_row_has_no_proposed_change(tracker, conn):
    run_id = open_run(tracker)
    write_weekly_review(tracker, run_id, FRIDAY, 1000000, 0, "Full scan.", 2)
    tracker.commit()
    row = rows(conn, "select period, proposed_change, proposal_status, stats from reviews")[0]
    assert row["period"] == "weekly" and row["proposed_change"] is None
    assert row["proposal_status"] == "none" and row["stats"]["equity"] == 1000000


@pg
def test_mirror_reads_live_config_keys(tracker, cfg):
    from breakout_runner.config.ledger import check_mirror

    check_mirror(cfg.ledger_mirror, tracker)


@pg
def test_non_live_tracker_cannot_write_even_by_accident(conn):
    conn.read_only = True
    tracker = Tracker(conn, live=False)
    run_id = open_run(tracker)
    row = signal_row(entry(), "weekly_scan", VERSION)
    tracker.write_signal(row, run_id, set(), {"thesis": "t"}, 3)
    tracker.upsert_bars("AAA", [bar("2026-11-06")], 0.05)
    tracker.process_ledger(FRIDAY)
    conn.commit()
    conn.read_only = False
    assert rows(conn, "select count(*) n from signals")[0]["n"] == 0
    assert rows(conn, "select count(*) n from runs")[0]["n"] == 0
    assert len(tracker.outbox["signals"]) == 1 and len(tracker.outbox["runs"]) == 1


def test_hash_ignores_run_and_version():
    base = {"symbol": "A", "signal_date": FRIDAY, "stop": 1.0}
    assert content_hash({**base, "run_id": 1}) == content_hash({**base, "run_id": 2})
    assert content_hash(base) != content_hash({**base, "stop": 2.0})


def test_review_rejects_a_number():
    with pytest.raises(ValueError):
        Review.model_validate_json(json.dumps({"why_grade": "A", "why_text": "x", "vol_mult": 9}))


def test_review_packet_drops_low_priority_fields_first():
    long = entry(notes="x" * 400)
    long.raw["flags"] = ["f" * 300]
    long.raw["data_gaps"] = ["g" * 300]
    full = review_packet(long, 10000)
    assert {"flags", "data_gaps", "numbers", "notes"} <= set(full)
    no_flags = review_packet(long, len(json.dumps(full)) - 1)
    assert "flags" not in no_flags and "data_gaps" in no_flags
    tight = review_packet(long, 500)
    assert len(json.dumps(tight)) <= 500
    assert tight["symbol"] == "AAA" and tight["questions"]
    assert "flags" not in tight and "data_gaps" not in tight and "notes" not in tight
