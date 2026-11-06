"""Command line entry. `check-config` validates locally and makes no network call."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from dotenv import find_dotenv, load_dotenv

from .client import TapetideApi, TapetideClient, TokenPool
from .compare import compare_rows, render_report
from .config.ledger import sync_mirror
from .config.loader import ConfigError, load_config
from .data.calendar import TradingCalendar, load_calendar
from .orchestrator import (
    PARTIAL,
    RUNNERS,
    SUCCEEDED,
    RunContext,
    decide_mode,
    execute,
    run_late_sweep,
    run_promotion,
)
from .writer.tracker import Tracker
from .writer.why import Review, apply_review

DEFAULT_CONFIG = Path("config/breakout.config.yaml")
CALENDAR_CACHE = Path(".cache/calendar.json")
OUT_DIR = Path("out")
DB_URL_ENV = "SUPABASE_DB_URL"
AUTO = "auto"
SWEEP = "sweep"
PROMOTE = "promote"
MODES = (AUTO, "weekly_scan", "midweek_checkin", SWEEP, PROMOTE)
GITHUB_RUN_ENV = "GITHUB_RUN_ID"
GITHUB_WORKFLOW_ENV = "GITHUB_WORKFLOW"


def check_config(path: Path, *, tokens_required: bool = True) -> int:
    try:
        config, sha, version = load_config(path)
    except ConfigError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 1
    print(f"config ok: {version} (sha256 {sha})")
    available = 0
    for token in config.tapetide.tokens:
        present = bool(os.environ.get(token.env, "").strip())
        available += present
        print(f"  token {token.label}: {token.env} {'set' if present else 'NOT SET'}")
    if not available and tokens_required:
        print("no Tapetide token available", file=sys.stderr)
        return 1
    if available < config.tapetide.recommended_tokens:
        print(f"warning: {available} of {config.tapetide.recommended_tokens} tokens set")
    return 0


def _connect(*, live: bool):
    """Without live writes the session itself is read-only, so a bug cannot reach the book."""
    url = os.environ.get(DB_URL_ENV, "").strip()
    if not url:
        if live:
            raise SystemExit(f"{DB_URL_ENV} is required for live writes")
        return None
    conn = psycopg.connect(url, prepare_threshold=None)
    conn.read_only = not live
    return conn


def _now(config) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.schedule.timezone))


def _api(config) -> tuple[TapetideApi, TokenPool]:
    pool = TokenPool.from_config(config.tapetide)
    return TapetideApi(TapetideClient(config.tapetide.base_url, config.http, pool)), pool


def run(path: Path, mode: str, dry_run: bool, today: dt.date | None) -> int:
    config, sha, version = load_config(path)
    live = config.shadow.live_writes and not dry_run
    now = _now(config)
    today = today or now.date()
    conn = _connect(live=live)
    tracker = Tracker(conn, live=live)
    needs_api = mode != PROMOTE
    api, pool = _api(config) if needs_api else (None, None)
    # The promotion sweep spends no units, so only cached or known holidays decide its window.
    calendar = (
        load_calendar(api, CALENDAR_CACHE, today, config.data)
        if api
        else _cached_calendar(today, config)
    )
    selected = mode
    if mode == AUTO:
        selected = decide_mode(today, calendar, config)
        if selected is None:
            print(f"{today}: no run scheduled")
            return 0
    ctx = RunContext(
        config=config,
        api=api,
        pool=pool,
        tracker=tracker,
        calendar=calendar,
        today=today,
        now=now,
        version=version,
        sha256=sha,
        trigger_id=os.environ.get(GITHUB_WORKFLOW_ENV),
        session_id=os.environ.get(GITHUB_RUN_ENV),
    )
    runner = {SWEEP: run_late_sweep, PROMOTE: run_promotion, **RUNNERS}[selected]
    rows_path = None if live else OUT_DIR / f"rows-{selected}-{today.isoformat()}.json"
    state = execute(ctx, runner, rows_path)
    print(f"{selected} {today}: {state.status}; {json.dumps(state.counts, sort_keys=True)}")
    for line in [*state.notes, *state.problems]:
        print(f"  {line}")
    if rows_path:
        print(f"  would-be rows: {rows_path}")
    return 0 if state.status == SUCCEEDED else 1


def _cached_calendar(today: dt.date, config) -> TradingCalendar:
    if CALENDAR_CACHE.exists():
        stored = json.loads(CALENDAR_CACHE.read_text("utf-8"))
        holidays = [dt.date.fromisoformat(d) for d in stored["holidays"]]
        return TradingCalendar(holidays, config.data.special_sessions)
    return TradingCalendar([], config.data.special_sessions)


def review(path: Path, signal_id: int, body: str) -> int:
    """Record a why-review. Only judgement fields are accepted; a number is rejected."""
    config, sha, version = load_config(path)
    if not config.shadow.live_writes:
        print("live writes are off: reviews are not recorded in shadow mode", file=sys.stderr)
        return 1
    review_input = Review.model_validate_json(body)
    conn = _connect(live=True)
    tracker = Tracker(conn, live=True)
    now = _now(config)
    api, _ = _api(config)
    calendar = load_calendar(api, CALENDAR_CACHE, now.date(), config.data)
    run_id = tracker.open_run(
        "adhoc", None, None, now.date(), version, sha, config.config_date, {"review": signal_id}
    )
    result = apply_review(tracker, signal_id, review_input, run_id, now.date(), calendar, config)
    tracker.commit()
    tracker.close_run(run_id, SUCCEEDED, f"Review of signal {signal_id}: {result.action}.", {})
    print(f"signal {signal_id}: {result.action} as {result.signal_id}")
    return 0


def sync_ledger(path: Path, confirm: bool) -> int:
    config = load_config(path)[0]
    conn = _connect(live=True)
    diffs = sync_mirror(config.ledger_mirror, Tracker(conn, live=True), confirmed=confirm)
    print(f"synced: {sorted(diffs) or 'nothing to do'}")
    return 0


def compare(path: Path, rows_file: Path, claude_file: Path) -> int:
    config = load_config(path)[0]
    runner_rows = json.loads(rows_file.read_text("utf-8")).get("signals", [])
    claude_rows = json.loads(claude_file.read_text("utf-8"))
    print(render_report(compare_rows(runner_rows, claude_rows, config)))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(prog="breakout-runner")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-config", help="validate the config and list token variables")
    check.add_argument(
        "--no-tokens-required", action="store_true", help="validate only, as CI does"
    )
    runner = commands.add_parser("run", help="run a scan, check-in, sweep or promotion")
    runner.add_argument("--mode", choices=MODES, default=AUTO)
    runner.add_argument("--dry-run", action="store_true", help="evaluate and write nothing")
    runner.add_argument("--today", type=dt.date.fromisoformat, help="override the IST date")
    rev = commands.add_parser("review", help="record a why-review for a provisional ENTRY")
    rev.add_argument("--signal-id", type=int, required=True)
    rev.add_argument("--json", required=True, help='{"why_grade":"B","why_text":"..."}')
    sync = commands.add_parser("sync-ledger", help="push ledger_mirror values to the database")
    sync.add_argument("--confirm", action="store_true", help="this rewrites paper history")
    cmp_ = commands.add_parser("compare", help="compare shadow rows with Claude's rows")
    cmp_.add_argument("--rows", type=Path, required=True)
    cmp_.add_argument("--claude", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.config, args.mode, args.dry_run, args.today)
    if args.command == "review":
        return review(args.config, args.signal_id, args.json)
    if args.command == "sync-ledger":
        return sync_ledger(args.config, args.confirm)
    if args.command == "compare":
        return compare(args.config, args.rows, args.claude)
    return check_config(args.config, tokens_required=not args.no_tokens_required)


if __name__ == "__main__":
    sys.exit(main())

__all__ = ["PARTIAL", "main"]
