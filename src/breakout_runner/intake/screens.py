"""U-rules: build Q1/Q2 from config, page them, re-verify rows, grade, filter and rank."""

from __future__ import annotations

import datetime as dt
import math
import operator
from collections.abc import Collection
from dataclasses import dataclass, field

from ..client.endpoints import CorporateAction, ScreenRow, SurveillanceRow, TapetideApi
from ..config.models import (
    Config,
    CorporateActions,
    FundamentalGrade,
    LocalRatio,
    Predicate,
    Screens,
    Surveillance,
)
from ..data.bars import DailyBar
from ..indicators.weekly import BASE_QUALITY_ORDER, UNKNOWN_BASE, Metrics

Q1 = "Q1"
Q2 = "Q2"
GRADE_ORDER = ("A", "B", "C", "unknown")
CURRENT_PRICE = "Current price"
OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}


class UnknownRatio(Exception):
    """A configured ratio name is not in the catalogue."""


def _render(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def predicate_text(p: Predicate) -> str:
    right = p.ref if p.ref is not None else _render(p.value)
    return f"{p.ratio} {p.op} {right}"


def build_query(specific: list[Predicate], common: list[Predicate]) -> str:
    """The grammar takes `{ratio} {op} {number | ratio}`: no multipliers, one clause a line."""
    return " AND\n".join(predicate_text(p) for p in [*specific, *common])


def ratio_names(cfg: Screens) -> set[str]:
    names = {cfg.sort}
    for p in [*cfg.common, *cfg.trigger_q1, *cfg.alert_q2]:
        names.add(p.ratio)
        if p.ref is not None:
            names.add(p.ref)
    return names


def validate_ratio_names(cfg: Screens, catalogue: set[str]) -> None:
    missing = sorted(ratio_names(cfg) - catalogue)
    if missing:
        raise UnknownRatio(f"not in the screen catalogue: {missing}")


def run_screen(api: TapetideApi, query: str, cfg: Screens) -> list[ScreenRow]:
    """U-03: a page can hold fewer rows than `limit`, so only the cursor says when to stop."""
    rows: list[ScreenRow] = []
    cursor = None
    while True:
        page = api.screen_fundamental(query, cfg.sort, cfg.order, cfg.page_limit, cursor)
        rows.extend(page.rows)
        cursor = page.next_cursor
        if not cursor or len(rows) >= cfg.max_rows:
            return rows[: cfg.max_rows]


@dataclass(frozen=True)
class Verification:
    checked: int
    verified: int
    mismatched: tuple[str, ...]
    unknown: int
    drift_pct: float
    trusted: bool


def local_ratios(daily: list[DailyBar], cfg: dict[str, LocalRatio]) -> dict[str, float]:
    """U-04: the screen's own ratio values are mostly null, so recompute them from bars.
    A ratio with too little history is left out and stays unknown."""
    values: dict[str, float] = {}
    for name, spec in cfg.items():
        recent = daily[-spec.sessions :]
        if len(recent) < spec.sessions:
            continue
        if spec.kind == "last_close":
            values[name] = recent[-1].close
        elif spec.kind == "sma_close":
            values[name] = sum(b.close for b in recent) / spec.sessions
        elif spec.kind == "avg_volume":
            values[name] = sum(b.volume for b in recent) / spec.sessions
        else:
            high = max(b.high for b in recent)
            values[name] = (high - recent[-1].close) / high * 100
    return values


def _ratio_value(row: ScreenRow, name: str, local: dict[str, float]) -> float | None:
    if name in local:
        return local[name]
    value = row.ratio_values.get(name)
    return row.ltp if value is None and name == CURRENT_PRICE else value


def _row_holds(row: ScreenRow, p: Predicate, local: dict[str, float]) -> bool | None:
    left = _ratio_value(row, p.ratio, local)
    right = _ratio_value(row, p.ref, local) if p.ref is not None else p.value
    if left is None or right is None:
        return None
    return OPS[p.op](left, right)


def verify_rows(
    rows: list[ScreenRow],
    predicates: list[Predicate],
    cfg: Screens,
    local: dict[str, dict[str, float]] | None = None,
) -> Verification:
    """U-04: a null ratio is unknown, not a mismatch. Too many mismatches mean watch-only, and
    so does having checked nothing at all: rows that resolve no clause prove nothing."""
    local = local or {}
    mismatched = []
    unknown = verified = 0
    for row in rows:
        results = [_row_holds(row, p, local.get(row.symbol, {})) for p in predicates]
        if any(r is not None for r in results):
            verified += 1
        if any(r is False for r in results):
            mismatched.append(row.symbol)
        elif any(r is None for r in results):
            unknown += 1
    drift = 100 * len(mismatched) / verified if verified else 0
    return Verification(
        checked=len(rows),
        verified=verified,
        mismatched=tuple(mismatched),
        unknown=unknown,
        drift_pct=drift,
        trusted=(verified > 0 or not rows) and drift <= cfg.screen_drift_max_pct,
    )


def grade_row(row: ScreenRow, cfg: FundamentalGrade) -> str | None:
    """U-05. None means drop. Missing inputs are `unknown`, never A or B."""
    profit, growth = row.net_profit_qtr_cr, row.profit_growth_qtr_pct
    if (profit is not None and profit < cfg.drop_if.qtr_profit_lt) or (
        growth is not None and growth < cfg.drop_if.profit_growth_lt
    ):
        return None
    roce, sales = row.roce_pct, row.sales_growth_qtr_pct
    if None in (roce, growth, sales):
        return "unknown"
    a, b = cfg.A, cfg.B
    if roce >= a.roce_min and growth >= a.profit_growth_min and sales >= a.sales_growth_min:
        return "A"
    if roce >= b.roce_min and growth > b.growth_min and sales > b.growth_min:
        return "B"
    return "C"


@dataclass
class Candidate:
    symbol: str
    screen: str
    grade: str
    row: ScreenRow
    metrics: Metrics | None = None
    atr_pct: float | None = None
    turnover_cr: float | None = None
    dropped: str | None = None
    rank: int | None = None
    flags: set[str] = field(default_factory=set)


def merge_candidates(
    q1: list[ScreenRow],
    q2: list[ScreenRow],
    cfg: FundamentalGrade,
    exclude: Collection[str] = (),
) -> tuple[list[Candidate], list[str]]:
    """A name in both screens is a trigger candidate. Returns (candidates, dropped symbols)."""
    seen: dict[str, Candidate] = {}
    dropped: list[str] = []
    for screen, rows in ((Q1, q1), (Q2, q2)):
        for row in rows:
            if row.symbol in seen or row.symbol in dropped:
                continue
            grade = None if row.symbol in exclude else grade_row(row, cfg)
            if grade is None:
                dropped.append(row.symbol)
            else:
                seen[row.symbol] = Candidate(row.symbol, screen, grade, row)
    return list(seen.values()), dropped


def post_filter_reason(c: Candidate, config: Config) -> str | None:
    """U-06 on weekly data. The volume band never appears here: it only lowers conviction."""
    m = c.metrics
    if m is None:
        return None
    pf = config.screens.post_filters
    uni = config.universe
    trigger = c.screen == Q1
    if m.close < uni.min_price:
        return "below minimum price"
    if c.turnover_cr is not None and c.turnover_cr < uni.min_median_turnover_cr:
        return "below minimum turnover"
    cap = pf.max_close_to_sma13_trigger if trigger else pf.max_close_to_sma13_alert
    if m.sma13 and m.close > cap * m.sma13:
        return "too far above the 13-week SMA"
    atr_cap = pf.max_daily_atr_pct_trigger if trigger else pf.max_daily_atr_pct_alert
    if atr_cap is not None and c.atr_pct is not None and c.atr_pct > atr_cap:
        return "ATR above the screen cap"
    if m.below_high_pct is not None and m.below_high_pct > pf.discard_if_below_52w_high_pct:
        return "pullback, not a breakout"
    return None


def rank_key(c: Candidate) -> tuple:
    """Trigger state, base quality, distance above the 13-week SMA, grade, symbol."""
    quality = c.metrics.base_quality if c.metrics else UNKNOWN_BASE
    dist = c.metrics.dist_sma_pct if c.metrics else None
    return (
        0 if c.screen == Q1 else 1,
        BASE_QUALITY_ORDER.index(quality),
        math.inf if dist is None else dist,
        GRADE_ORDER.index(c.grade),
        c.symbol,
    )


def rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    ranked = sorted(candidates, key=rank_key)
    for position, candidate in enumerate(ranked, start=1):
        candidate.rank = position
    return ranked


@dataclass(frozen=True)
class SurveillanceState:
    """`known` is False when the list is empty or stale: unknown is not the same as clear."""

    known: bool
    members: dict[str, tuple[str, ...]]
    as_of: str | None = None

    def flags_for(self, symbol: str) -> tuple[str, ...] | None:
        return self.members.get(symbol, ()) if self.known else None


def surveillance_state(
    rows: list[SurveillanceRow], today: dt.date, cfg: Surveillance
) -> SurveillanceState:
    wanted = [r for r in rows if r.framework.lower() in cfg.frameworks]
    if not wanted:
        return SurveillanceState(False, {})
    as_of = max(r.as_of for r in wanted)
    age = (today - dt.date.fromisoformat(as_of[: len("YYYY-MM-DD")])).days
    if age > cfg.max_age_days:
        return SurveillanceState(False, {}, as_of)
    members: dict[str, list[str]] = {}
    for r in wanted:
        members.setdefault(r.symbol, []).append(f"{r.framework}:{r.measure}:{r.stage}")
    return SurveillanceState(True, {s: tuple(sorted(v)) for s, v in members.items()}, as_of)


def corporate_action_warnings(
    actions: list[CorporateAction], symbols: Collection[str], cfg: CorporateActions
) -> dict[str, list[str]]:
    """Upcoming splits and bonuses for names we hold: bars get back-adjusted on the ex-date."""
    warnings: dict[str, list[str]] = {}
    for a in actions:
        if a.symbol in symbols and a.action_type in cfg.warn_types:
            warnings.setdefault(a.symbol, []).append(
                f"{a.action_type} {a.ratio_numerator}:{a.ratio_denominator} ex {a.ex_date}"
            )
    return warnings


def non_equity_symbols(actions: list[CorporateAction], cfg: CorporateActions) -> set[str]:
    """InvITs and REITs seen in the calendar; candidates for `universe.exclude_symbols`."""
    return {a.symbol for a in actions if a.symbol and a.series in cfg.non_equity_series}
