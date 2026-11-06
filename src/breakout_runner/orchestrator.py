"""One job per run, in a fixed order: bars, ledger, evaluate, signals, ledger, reviews."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import AllTokensExhausted, ApiError, ServiceUnavailable, TapetideApi, TokenPool
from .client.planner import ScanEstimate, plan_midweek, plan_scan, usable_quota
from .config.ledger import check_mirror
from .config.models import Config
from .data.bars import OK, SymbolHistory, build_history, fetch_daily_bars
from .data.calendar import TradingCalendar
from .indicators import rs
from .indicators.weekly import (
    atr_pct_of,
    circuit_suspect,
    compute_metrics,
    liquidity_tier,
    median_turnover_cr,
)
from .intake.screens import (
    Q1,
    Q2,
    Candidate,
    SurveillanceState,
    Verification,
    corporate_action_warnings,
    local_ratios,
    merge_candidates,
    non_equity_symbols,
    post_filter_reason,
    rank_candidates,
    run_screen,
    surveillance_state,
    validate_ratio_names,
    verify_rows,
)
from .intake.screens import build_query as query_of
from .rules.entry import EntryInputs, evaluate_entry
from .rules.fundamentals import analyse, holdings_trend
from .rules.holdings import HoldingInputs, Position, evaluate_holding, scope_positions
from .rules.midweek import FridayItem, evaluate_midweek, new_high_names, read_friday_state
from .rules.types import Decision, Verdict
from .writer.reviews import write_trade_reviews, write_weekly_review
from .writer.rows import REVIEW_PACKET, signal_row
from .writer.tracker import SUPERSEDED, UNCHANGED, Tracker
from .writer.why import promote_due, review_packet

logger = logging.getLogger(__name__)

WEEKLY_SCAN = "weekly_scan"
MIDWEEK = "midweek_checkin"
ADHOC = "adhoc"
SUCCEEDED = "succeeded"
PARTIAL = "partial"
FAILED = "failed"
RATIONALE_VERDICTS = {Verdict.ENTRY, Verdict.ENTRY_REDUCED, Verdict.WAIT_ALERT}
NOT_FINAL = "not_final"
CONFIG_SHA = "config_sha256"
LIVE_WRITES = "live_writes"
EARNINGS_METRICS = ("revenue", "eps", "net_profit", "other_income")
QUARTER = "quarter"
MIDWEEK_DAY_NAMES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


@dataclass
class RunState:
    status: str = SUCCEEDED
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def partial(self, problem: str) -> None:
        self.status = PARTIAL
        self.problems.append(problem)

    def count(self, action: str) -> None:
        self.counts[action] = self.counts.get(action, 0) + 1


@dataclass
class RunContext:
    config: Config
    api: TapetideApi | None
    pool: TokenPool | None
    tracker: Tracker
    calendar: TradingCalendar
    today: dt.date
    now: dt.datetime
    version: str
    sha256: str
    trigger_id: str | None = None
    session_id: str | None = None
    state: RunState = field(default_factory=RunState)
    run_id: int = 0
    histories: dict[str, SymbolHistory] = field(default_factory=dict)
    excluded: set[int] = field(default_factory=set)

    def notes(self, **extra: Any) -> dict[str, Any]:
        live = self.tracker.live
        return {CONFIG_SHA: self.sha256, LIVE_WRITES: live, **extra}


def decide_mode(today: dt.date, calendar: TradingCalendar, config: Config) -> str | None:
    """Cron fires every weekday; the calendar decides what, if anything, today is."""
    if calendar.is_full_scan_day(today):
        return WEEKLY_SCAN
    if not calendar.is_session(today):
        return None
    return MIDWEEK if MIDWEEK_DAY_NAMES[today.weekday()] in config.schedule.midweek_days else None


def _history(ctx: RunContext, symbol: str) -> SymbolHistory | None:
    """A failed symbol costs that symbol, not the run. Quota exhaustion still ends it."""
    if symbol in ctx.histories:
        return ctx.histories[symbol]
    cfg = ctx.config
    try:
        bars = fetch_daily_bars(ctx.api, symbol, cfg.data.lookback_calendar_days, ctx.today)
    except (ApiError, ServiceUnavailable) as exc:
        ctx.state.partial(f"{symbol}: {exc}")
        return None
    history = build_history(symbol, bars, ctx.calendar, ctx.today, cfg.data, cfg.indicators)
    ctx.histories[symbol] = history
    return history


def _upsert(ctx: RunContext, symbols: list[str]) -> None:
    for symbol in symbols:
        history = ctx.histories.get(symbol)
        if history is None:
            continue
        tolerance = ctx.config.output.price_tolerance_pct
        if not ctx.tracker.upsert_bars(symbol, history.daily, tolerance):
            ctx.state.partial(f"{symbol}: stored close differs from the fresh bar")


def _positions(ctx: RunContext) -> list[Position]:
    book = ctx.tracker.book_started_on()
    rows = ctx.tracker.position_rows()
    if book is None:
        return []
    return scope_positions(rows, book, ctx.excluded)


def _write(ctx: RunContext, decision: Decision, source: str) -> None:
    cfg = ctx.config
    if decision.verdict == Verdict.ENTRY and decision.provisional and source == WEEKLY_SCAN:
        decision.raw[REVIEW_PACKET] = review_packet(decision, cfg.why_gate.packet_max_chars)
    rationale = decision.rationale if decision.verdict in RATIONALE_VERDICTS else None
    level = (decision.raw.get("conviction") or {}).get("level")
    confidence = getattr(cfg.reviews.confidence, level) if level else None
    row = signal_row(decision, source, ctx.version)
    result = ctx.tracker.write_signal(row, ctx.run_id, ctx.excluded, rationale, confidence)
    ctx.state.count(result.action)


def _holding_decision(
    ctx: RunContext, p: Position, full_scan: bool, surveillance: SurveillanceState | None
) -> Decision | None:
    cfg = ctx.config
    history = ctx.histories.get(p.symbol)
    if history is None or not history.weekly:
        return None
    t = len(history.weekly) - 1
    metrics = compute_metrics(history.weekly, t, cfg.indicators, cfg.data)
    turnover = median_turnover_cr(history.daily, cfg.indicators.turnover_sessions)
    jump = _pledge_jump(ctx, p.symbol) if full_scan else None
    return evaluate_holding(
        HoldingInputs(
            position=p,
            bars=history.weekly,
            metrics=metrics,
            signal_date=ctx.today,
            full_scan=full_scan,
            liquidity_tier=liquidity_tier(turnover, cfg.indicators),
            surveillance=surveillance.flags_for(p.symbol) if surveillance else None,
            pledge_jump_pp=jump,
        ),
        cfg,
    )


def _pledge_jump(ctx: RunContext, symbol: str) -> float | None:
    if not ctx.config.surveillance.check:
        return None
    try:
        rows = ctx.api.promoter_pledge(symbol)
    except (ApiError, ServiceUnavailable):
        return None
    values = [r.promoter_pledged_pct for r in sorted(rows, key=lambda r: r.quarter_end)]
    known = [v for v in values if v is not None]
    return known[-1] - known[:-1][-1] if len(known) > 1 else None


def _attach_measures(c: Candidate, history: SymbolHistory, config: Config) -> None:
    ind = config.indicators
    c.metrics = compute_metrics(history.weekly, len(history.weekly) - 1, ind, config.data)
    c.atr_pct = atr_pct_of(history.daily, history.weekly, ind)[0]
    c.turnover_cr = median_turnover_cr(history.daily, ind.turnover_sessions)


def _deep_verify(ctx: RunContext, c: Candidate) -> tuple[bool | None, bool | None, list[str]]:
    """Paid checks for the top-ranked names. Unavailable is unknown, never a failure."""
    cfg = ctx.config
    accelerating = holdings = None
    gaps: list[str] = []
    try:
        if cfg.fundamentals.fetch_financials:
            rows = ctx.api.fundamentals(c.symbol, list(EARNINGS_METRICS), QUARTER)
            trend = analyse(rows, cfg.fundamentals)
            both = (trend.sales_accelerating, trend.earnings_accelerating)
            accelerating = None if None in both else all(both)
        if cfg.holdings.fetch_shareholding:
            holdings, gaps = holdings_trend(ctx.api.shareholding(c.symbol), cfg.holdings)
    except (ApiError, ServiceUnavailable) as exc:
        gaps.append(f"deep verify: {exc.__class__.__name__}")
    return accelerating, holdings, gaps


def _entry_inputs(
    ctx: RunContext,
    c: Candidate,
    history: SymbolHistory,
    scores: list[float] | None,
    surveillance: SurveillanceState | None,
    trusted: bool,
    deep: tuple[bool | None, bool | None, list[str]],
) -> EntryInputs:
    cfg = ctx.config
    t = len(history.weekly) - 1
    atr, from_weekly = atr_pct_of(history.daily, history.weekly, cfg.indicators)
    accelerating, holdings, gaps = deep
    return EntryInputs(
        symbol=c.symbol,
        metrics=c.metrics,
        signal_date=ctx.today,
        bar_complete=history.weekly[t].complete,
        window_start=ctx.calendar.next_session(ctx.today),
        window_end=ctx.calendar.window_end(ctx.today),
        quality=history.quality,
        flags=frozenset(history.flags | set(gaps)),
        atr_pct=atr,
        atr_from_weekly=from_weekly,
        liquidity_tier=liquidity_tier(c.turnover_cr, cfg.indicators),
        circuit_suspect=circuit_suspect(history.daily, cfg.indicators),
        rs=rs.percentile(rs.own_score(history.weekly, t, cfg.rs), scores),
        accelerating=accelerating,
        holdings_rising=holdings,
        surveillance=surveillance.flags_for(c.symbol) if surveillance else None,
        grade=c.grade,
        screen=c.screen,
        priority_rank=c.rank,
        screen_trusted=trusted,
    )


def _universe_scores(ctx: RunContext) -> list[float] | None:
    cfg = ctx.config.rs
    try:
        return rs.universe_scores(ctx.api.heatmap(cfg.universe_index, cfg.page_limit), cfg)
    except (ApiError, ServiceUnavailable) as exc:
        ctx.state.notes.append(f"RS unavailable: {exc}")
        return None


def _surveillance(ctx: RunContext, enabled: bool) -> SurveillanceState | None:
    cfg = ctx.config.surveillance
    if not enabled or not cfg.check:
        return None
    try:
        return surveillance_state(ctx.api.surveillance(cfg.page_limit), ctx.today, cfg)
    except (ApiError, ServiceUnavailable) as exc:
        ctx.state.notes.append(f"Surveillance unavailable: {exc}")
        return None


def _corporate_actions(ctx: RunContext, held: list[str]) -> None:
    cfg = ctx.config.corporate_actions
    if not cfg.check:
        return
    try:
        actions = ctx.api.corporate_actions(cfg.page_limit)
    except (ApiError, ServiceUnavailable) as exc:
        ctx.state.notes.append(f"Corporate actions unavailable: {exc}")
        return
    for symbol, lines in sorted(corporate_action_warnings(actions, held, cfg).items()):
        ctx.state.notes.append(f"{symbol}: {'; '.join(lines)}")
    unknown = non_equity_symbols(actions, cfg) - set(ctx.config.universe.exclude_symbols)
    if unknown:
        ctx.state.notes.append(f"Add to universe.exclude_symbols: {', '.join(sorted(unknown))}")


def _verification(
    ctx: RunContext, candidates: list[Candidate], predicates: list, screen: str
) -> Verification:
    cfg = ctx.config.screens
    rows = [c.row for c in candidates if c.screen == screen and c.row is not None]
    local = {
        c.symbol: local_ratios(ctx.histories[c.symbol].daily, cfg.local_ratios)
        for c in candidates
        if c.symbol in ctx.histories
    }
    return verify_rows(rows, [*predicates, *cfg.common], cfg, local)


def _evaluate_candidates(
    ctx: RunContext,
    candidates: list[Candidate],
    scores: list[float] | None,
    surveillance: SurveillanceState | None,
    trusted: dict[str, bool],
    deep_limit: int,
    source: str,
) -> list[str]:
    """Rank, deep-verify the top, evaluate and write. Returns the names now at ENTRY."""
    cfg = ctx.config
    kept = []
    for c in candidates:
        history = _history(ctx, c.symbol)
        if history is None or not history.weekly:
            continue
        _attach_measures(c, history, cfg)
        c.dropped = post_filter_reason(c, cfg) if history.quality == OK else None
        if c.dropped is None:
            kept.append(c)
    entries = []
    for position, c in enumerate(rank_candidates(kept)):
        history = ctx.histories[c.symbol]
        deep = (None, None, [])
        triggered = c.metrics.breakout_close and history.weekly[-1].complete
        if position < deep_limit and triggered:
            deep = _deep_verify(ctx, c)
        inputs = _entry_inputs(
            ctx, c, history, scores, surveillance, trusted.get(c.screen, True), deep
        )
        decision = evaluate_entry(inputs, cfg)
        if decision is None:
            continue
        if decision.verdict == Verdict.ENTRY:
            entries.append(c.symbol)
        _write(ctx, decision, source)
    return entries


def _not_final(ctx: RunContext, candidates: list[Candidate], positions: list[Position]) -> list:
    held = [{"symbol": p.symbol} for p in positions if _unfinished(ctx, p.symbol)]
    waiting = [
        {"symbol": c.symbol, "screen": c.screen, "grade": c.grade}
        for c in candidates
        if _unfinished(ctx, c.symbol)
    ]
    return held + waiting


def _unfinished(ctx: RunContext, symbol: str) -> bool:
    history = ctx.histories.get(symbol)
    return history is not None and not history.final


def _capture_alert_bars(ctx: RunContext, already: set[str]) -> list[str]:
    """Counterfactual bars for recent alert names, within the tracker's own call cap."""
    cfg = ctx.config
    wanted = ctx.tracker.recent_alert_symbols(ctx.today, cfg.output.alert_recent_weeks)
    room = cfg.quota.price_calls_max - len(already)
    captured = [s for s in wanted if s in ctx.histories and s not in already]
    for symbol in wanted:
        usable = usable_quota(ctx.pool.combined_remaining(), cfg.quota)
        if room <= len(captured) or usable == 0:
            break
        if symbol in ctx.histories or symbol in already:
            continue
        if _history(ctx, symbol):
            captured.append(symbol)
    return captured


def _begin(ctx: RunContext, run_type: str, data_as_of: dt.date, **notes: Any) -> None:
    cfg = ctx.config
    if ctx.tracker.conn is not None:
        check_mirror(cfg.ledger_mirror, ctx.tracker)
    ctx.excluded = ctx.tracker.excluded_signal_ids()
    ctx.tracker.guard(run_type, data_as_of, cfg.output.run_max_minutes)
    ctx.run_id = ctx.tracker.open_run(
        run_type,
        ctx.trigger_id,
        ctx.session_id,
        data_as_of,
        ctx.version,
        ctx.sha256,
        cfg.config_date,
        ctx.notes(**notes),
    )


def _summary(ctx: RunContext, lead: str) -> str:
    state = ctx.state
    parts = [lead, f"Signals: {json.dumps(state.counts, sort_keys=True)}."]
    if state.notes:
        parts.append("Notes: " + " | ".join(state.notes) + ".")
    if state.problems:
        parts.append("Problems: " + " | ".join(state.problems) + ".")
    return " ".join(parts)


def _calls(ctx: RunContext) -> dict[str, Any]:
    return ctx.pool.usage_report() if ctx.pool else {}


def _promote(ctx: RunContext) -> None:
    promoted, pending = promote_due(
        ctx.tracker, ctx.run_id, ctx.now, ctx.today, ctx.calendar, ctx.config
    )
    if promoted:
        ctx.state.notes.append(f"Promoted: {', '.join(promoted)}")
    if pending:
        ctx.state.notes.append(f"Awaiting review or grace: {', '.join(pending)}")
    for _ in promoted:
        ctx.state.count(SUPERSEDED)


def run_full_scan(ctx: RunContext) -> RunState:
    cfg, api, state = ctx.config, ctx.api, ctx.state
    _begin(ctx, WEEKLY_SCAN, ctx.today)
    q1_rows: list = []
    q2_rows: list = []
    candidates: list[Candidate] = []
    positions: list[Position] = []
    extra: dict[str, Any] = {}
    try:
        ctx.api.client.refresh_usage()
        validate_ratio_names(cfg.screens, api.screen_field_names())
        q1_rows = run_screen(api, query_of(cfg.screens.trigger_q1, cfg.screens.common), cfg.screens)
        q2_rows = run_screen(api, query_of(cfg.screens.alert_q2, cfg.screens.common), cfg.screens)
        positions = _positions(ctx)
        held = {p.symbol for p in positions}
        merged, dropped = merge_candidates(
            q1_rows, q2_rows, cfg.fundamental_grade, set(cfg.universe.exclude_symbols)
        )
        candidates = [c for c in merged if c.symbol not in held]
        estimate = ScanEstimate(
            q1_pages=0,
            q2_pages=0,
            q2_candidates=sum(1 for c in candidates if c.screen == Q2),
            q1_candidates=sum(1 for c in candidates if c.screen == Q1),
            open_positions=len(held),
            deep_verify=cfg.screens.max_deep_verify,
            surveillance=1 + cfg.corporate_actions.check,
            heatmap=1,
            holidays=0,
            pledge_names=len(held),
        )
        plan = plan_scan(
            estimate, usable_quota(ctx.pool.combined_remaining(), cfg.quota), cfg.quota
        )
        ctx.pool.ensure_capacity(plan.total)
        if plan.short:
            state.partial(f"Quota short: plan {plan.total}, usable {plan.usable}")
        if plan.shed:
            state.notes.append(f"Shed: {', '.join(plan.shed)}")
        if not plan.enabled("alert_screen"):
            candidates = [c for c in candidates if c.screen != Q2]
        state.notes.append(
            f"Screens: Q1 {len(q1_rows)}, Q2 {len(q2_rows)}; dropped before bars {len(dropped)}"
        )

        for p in positions:
            _history(ctx, p.symbol)
        _upsert(ctx, sorted(held))
        ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
        ctx.tracker.commit()
        positions = _positions(ctx)

        for c in candidates:
            _history(ctx, c.symbol)
        results = {
            Q1: _verification(ctx, candidates, cfg.screens.trigger_q1, Q1),
            Q2: _verification(ctx, candidates, cfg.screens.alert_q2, Q2),
        }
        for name, result in results.items():
            state.notes.append(
                f"{name} drift {result.drift_pct:.1f}% over {result.verified} of {result.checked}"
            )
            if not result.trusted:
                state.partial(f"{name} failed re-verification: watch-only")
        trusted = {name: result.trusted for name, result in results.items()}
        scores = _universe_scores(ctx)
        surveillance = _surveillance(ctx, plan.enabled("surveillance"))
        if plan.enabled("surveillance"):
            _corporate_actions(ctx, sorted(held))

        for p in positions:
            decision = _holding_decision(ctx, p, True, surveillance)
            if decision is not None:
                _write(ctx, decision, WEEKLY_SCAN)
        deep_limit = plan.deep_verify if plan.enabled("financials") else 0
        entries = _evaluate_candidates(
            ctx, candidates, scores, surveillance, trusted, deep_limit, WEEKLY_SCAN
        )
        bars_for = sorted({*held, *entries})
        _upsert(ctx, [s for s in bars_for if s not in held])
        _upsert(ctx, _capture_alert_bars(ctx, set(bars_for)))
        _promote(ctx)
        ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
        ctx.tracker.commit()

        written = write_trade_reviews(ctx.tracker, ctx.run_id, ctx.today, cfg.reviews)
        write_weekly_review(
            ctx.tracker,
            ctx.run_id,
            ctx.today,
            cfg.ledger_mirror.paper_capital,
            written,
            f"Full scan {ctx.today}.",
            cfg.output.price_decimals,
        )
        ctx.tracker.commit()
    except AllTokensExhausted as exc:
        ctx.tracker.rollback()
        state.partial(f"All Tapetide tokens exhausted; reset {exc.reset_at}")
    extra[NOT_FINAL] = _not_final(ctx, candidates, positions)
    ctx.tracker.close_run(
        ctx.run_id,
        state.status,
        _summary(ctx, f"Full scan {ctx.today}."),
        _calls(ctx),
        ctx.notes(**extra),
    )
    return state


def _midweek_quotes(ctx: RunContext, items: list[FridayItem]) -> None:
    """Names the bar budget shed still get a price check, at one unit per batch."""
    cfg = ctx.config.midweek
    by_symbol = {i.symbol: i for i in items}
    symbols = sorted(by_symbol)
    for start in range(0, len(symbols), cfg.quote_batch_size):
        batch = symbols[start : start + cfg.quote_batch_size]
        try:
            quotes = ctx.api.live_quotes(batch)
        except (ApiError, ServiceUnavailable) as exc:
            ctx.state.notes.append(f"Quotes unavailable: {exc}")
            return
        for q in quotes:
            trigger = by_symbol[q.symbol].trigger
            if q.ltp is None or not trigger:
                continue
            below = (trigger - q.ltp) / trigger * 100
            if below <= cfg.near_trigger_pct:
                ctx.state.notes.append(f"{q.symbol} {q.ltp} vs trigger {trigger} (quote only)")


def run_midweek(ctx: RunContext) -> RunState:
    cfg, state = ctx.config, ctx.state
    _begin(ctx, MIDWEEK, ctx.today)
    try:
        ctx.api.client.refresh_usage()
        rows = ctx.tracker.friday_rows(ctx.today, cfg.midweek.state_max_age_days)
        items, stale = read_friday_state(
            rows, ctx.today, ctx.excluded, cfg.midweek.state_max_age_days
        )
        if stale:
            state.notes.append("No full-scan signals in range: open positions only")
        positions = _positions(ctx)
        held = {p.symbol for p in positions}
        tracked = [i for i in items if i.symbol not in held]
        usable = usable_quota(ctx.pool.combined_remaining(), cfg.quota)
        plan = plan_midweek(len(tracked), len(held), 1, cfg.quota, usable)
        ctx.pool.ensure_capacity(plan.total)
        if plan.shed:
            state.notes.append(f"Shed: {', '.join(plan.shed)}")
        if plan.short:
            state.partial(f"Quota short: plan {plan.total}, usable {plan.usable}")

        for p in positions:
            _history(ctx, p.symbol)
        _upsert(ctx, sorted(held))
        ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
        ctx.tracker.commit()
        positions = _positions(ctx)

        for p in positions:
            decision = _holding_decision(ctx, p, False, None)
            if decision is not None:
                _write(ctx, decision, MIDWEEK)
        keep = plan.items["tracked_bars"]
        for item in tracked[:keep]:
            history = _history(ctx, item.symbol)
            if history is None or not history.weekly:
                continue
            t = len(history.weekly) - 1
            metrics = compute_metrics(history.weekly, t, cfg.indicators, cfg.data)
            turnover = median_turnover_cr(history.daily, cfg.indicators.turnover_sessions)
            decision = evaluate_midweek(
                item,
                history.weekly[t],
                metrics,
                history.daily,
                ctx.today,
                cfg,
                liquidity_tier(turnover, cfg.indicators),
            )
            _write(ctx, decision, MIDWEEK)
        _midweek_quotes(ctx, tracked[keep:])
        if plan.items["new_high"]:
            highs = ctx.api.market_signals(
                cfg.midweek.new_high_signal,
                cfg.midweek.new_high_min_market_cap_cr,
                cfg.midweek.new_high_limit,
            )
            known = {i.symbol for i in items} | held
            names = new_high_names(highs, known)
            if names:
                state.notes.append(f"New highs, not on Friday's list: {', '.join(names)}")
        _promote(ctx)
        ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
        ctx.tracker.commit()
    except AllTokensExhausted as exc:
        ctx.tracker.rollback()
        state.partial(f"All Tapetide tokens exhausted; reset {exc.reset_at}")
    ctx.tracker.close_run(
        ctx.run_id, state.status, _summary(ctx, f"Check-in {ctx.today}."), _calls(ctx)
    )
    return state


def run_late_sweep(ctx: RunContext) -> RunState:
    """Only the symbols whose bar was not final at the full scan, through the idempotent path."""
    state = ctx.state
    last = ctx.tracker.last_run(WEEKLY_SCAN)
    pending = json.loads(last["notes"]).get(NOT_FINAL, []) if last else []
    if not pending:
        state.notes.append("Nothing to sweep")
        return state
    ctx.today = last["data_as_of"]
    _begin(ctx, ADHOC, ctx.today, swept=[p["symbol"] for p in pending])
    try:
        ctx.api.client.refresh_usage()
        positions = {p.symbol: p for p in _positions(ctx)}
        trusted: dict[str, bool] = {}
        scores = _universe_scores(ctx)
        surveillance = _surveillance(ctx, True)
        candidates = []
        for item in pending:
            symbol = item["symbol"]
            if symbol in positions:
                _history(ctx, symbol)
                decision = _holding_decision(ctx, positions[symbol], True, surveillance)
                if decision is not None:
                    _write(ctx, decision, WEEKLY_SCAN)
            elif "screen" in item:
                candidates.append(Candidate(symbol, item["screen"], item["grade"], None))
        _evaluate_candidates(ctx, candidates, scores, surveillance, trusted, 0, WEEKLY_SCAN)
        ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
        ctx.tracker.commit()
    except AllTokensExhausted as exc:
        ctx.tracker.rollback()
        state.partial(f"All Tapetide tokens exhausted; reset {exc.reset_at}")
    ctx.tracker.close_run(
        ctx.run_id, state.status, _summary(ctx, f"Late sweep {ctx.today}."), _calls(ctx)
    )
    return state


def run_promotion(ctx: RunContext) -> RunState:
    """No API calls. Runs before Monday's open so promoted entries can fill on Monday."""
    _begin(ctx, ADHOC, ctx.today, promotion=True)
    _promote(ctx)
    ctx.tracker.process_ledger(ctx.calendar.last_session_on_or_before(ctx.today))
    ctx.tracker.commit()
    ctx.tracker.close_run(
        ctx.run_id, ctx.state.status, _summary(ctx, f"Promotion {ctx.today}."), _calls(ctx)
    )
    return ctx.state


RUNNERS = {
    WEEKLY_SCAN: run_full_scan,
    MIDWEEK: run_midweek,
}


def execute(ctx: RunContext, runner, rows_path: Path | None = None) -> RunState:
    """Always closes the run: a crash rolls back the open work and records why."""
    try:
        state = runner(ctx)
    except Exception as exc:
        logger.exception("run failed")
        ctx.tracker.rollback()
        if ctx.run_id:
            detail = ctx.pool.redact(str(exc)) if ctx.pool else str(exc)
            ctx.tracker.close_run(ctx.run_id, FAILED, f"Failed: {detail}", _calls(ctx))
        ctx.state.status = FAILED
        ctx.state.problems.append(str(exc.__class__.__name__))
        state = ctx.state
    if rows_path is not None:
        rows_path.parent.mkdir(parents=True, exist_ok=True)
        rows_path.write_text(
            json.dumps(ctx.tracker.outbox, indent=1, sort_keys=True, default=str), "utf-8"
        )
    return state


__all__ = [
    "ADHOC",
    "FAILED",
    "MIDWEEK",
    "PARTIAL",
    "RUNNERS",
    "SUCCEEDED",
    "UNCHANGED",
    "WEEKLY_SCAN",
    "RunContext",
    "RunState",
    "decide_mode",
    "execute",
    "run_full_scan",
    "run_late_sweep",
    "run_midweek",
    "run_promotion",
]
