"""Stage 1 (S1-rules): one candidate in, one verdict out. Pure: no I/O, no clock."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from ..config.models import Config
from ..data.bars import OK, RAW_MISSING
from ..indicators.rs import AVAILABLE, RsResult
from ..indicators.weekly import LOOSE, NOT_A_BASE, TIGHT, Metrics
from .types import STOP_BASIS_CAP, Decision, SetupKind, Verdict

NO_BASE = "no_base"
LEVELS = ("low", "medium", "high")
THIN = "thin"
STOP_WARNING = "Stop is a decision level, not a guaranteed exit."


@dataclass(frozen=True)
class EntryInputs:
    symbol: str
    metrics: Metrics
    signal_date: dt.date
    bar_complete: bool
    window_start: dt.date | None
    window_end: dt.date | None
    quality: str = OK
    flags: frozenset[str] = frozenset()
    atr_pct: float | None = None
    atr_from_weekly: bool = False
    liquidity_tier: str | None = None
    circuit_suspect: bool = False
    rs: RsResult = field(default_factory=lambda: RsResult(None, "unavailable"))
    accelerating: bool | None = None
    holdings_rising: bool | None = None
    surveillance: tuple[str, ...] | None = None
    grade: str = "unknown"
    screen: str = ""
    priority_rank: int | None = None
    screen_trusted: bool = True


def classify_setup(m: Metrics, config: Config) -> str:
    """S1-01. A stock without a deep correction is checked as Setup B, never just failed."""
    correction = m.correction.pct if m.correction else None
    if correction is not None and correction >= config.indicators.acceptable_correction_pct:
        return SetupKind.A_BASE
    b = config.setup.setup_b
    if (
        m.stacked is True
        and m.base_range_pct is not None
        and m.base_range_pct <= b.max_range_pct
        and m.below_high_pct is not None
        and m.below_high_pct <= b.max_below_high_pct
    ):
        return SetupKind.B_FLAG
    return NO_BASE


def is_triggered(m: Metrics, bar_complete: bool, config: Config) -> bool:
    """S1-03, the strict gate: a completed week closing above the pivot on real volume."""
    return (
        bar_complete
        and m.breakout_close
        and m.vol_mult is not None
        and m.vol_mult >= config.volume.breakout_min_multiple
    )


def required_multiple(m: Metrics, config: Config) -> float:
    if m.base_class in (LOOSE, NOT_A_BASE):
        return config.volume.loose_base_volume_multiple
    return config.volume.breakout_min_multiple


def required_volume(m: Metrics, multiple: float) -> int | None:
    # The baseline is already a full-week figure after per-session normalisation.
    return math.ceil(multiple * m.baseline) if m.baseline else None


def selection_signals(i: EntryInputs, config: Config) -> dict[str, bool | None]:
    """S1-13: None is unknown and does not count as a pass."""
    m = i.metrics
    rs_ok = i.rs.rs >= config.rs.min_rs if i.rs.status == AVAILABLE else None
    tight_near_high = None
    if m.below_high_pct is not None:
        tight_near_high = (
            m.base_class in (TIGHT, "normal")
            and m.below_high_pct <= config.setup.setup_b.max_below_high_pct
        )
    return {
        "rs": rs_ok,
        "stacked": m.stacked,
        "structure": m.structure_ok,
        "accelerating": i.accelerating,
        "tight_near_high": tight_near_high,
        "holdings": i.holdings_rising,
    }


def conviction_of(i: EntryInputs, setup: str, config: Config) -> dict[str, Any]:
    m = i.metrics
    signals = selection_signals(i, config)
    passes = sum(1 for v in signals.values() if v is True)
    cfg = config.conviction
    level = "high" if passes >= cfg.high_min else ("medium" if passes >= cfg.medium_min else "low")
    position = LEVELS.index(level)
    reasons = []
    volume = config.volume
    vol = m.vol_mult
    if setup == SetupKind.A_BASE and m.first_higher_low is None:
        reasons.append("no higher low yet")
    if vol is not None and vol > volume.edge_shrinks_above:
        reasons.append(
            "volume above the weakest band"
            if vol > volume.weakest_above
            else "volume above the strong band"
        )
    if m.base_class in (LOOSE, NOT_A_BASE) and (
        vol is None or vol < volume.loose_base_volume_multiple
    ):
        reasons.append("loose base on low volume")
    atr = i.atr_pct
    atr_cfg = config.risk.atr_filter_daily_pct
    if atr is not None and atr_cfg.normal_max < atr <= atr_cfg.half_size_max:
        reasons.append("ATR in the half-size band")
    final = max(0, position - len(reasons))
    return {
        "level": LEVELS[final],
        "before_notches": LEVELS[position],
        "passes": passes,
        "signals": signals,
        "notches": reasons,
    }


def size_of(i: EntryInputs, level: str, config: Config) -> tuple[float, list[str]]:
    """S1-06, S1-08: cap times every multiplier that applies."""
    ledger = config.ledger_mirror
    multiple = getattr(config.conviction.size_multiplier, level)
    reasons = []
    atr_cfg = config.risk.atr_filter_daily_pct
    if i.atr_pct is not None and atr_cfg.normal_max < i.atr_pct <= atr_cfg.half_size_max:
        multiple *= ledger.reduced_size_multiple
        reasons.append("ATR half size")
    if i.liquidity_tier == THIN or i.circuit_suspect:
        multiple *= ledger.reduced_size_multiple
        reasons.append("thin or circuit-suspect half size")
    return ledger.position_cap_pct * multiple, reasons


def data_gaps_of(i: EntryInputs, signals: dict[str, bool | None]) -> list[str]:
    """L-06: every unavailable input is named, never treated as a pass or a fail."""
    gaps = [f"{name}: unknown" for name, value in signals.items() if value is None]
    if i.rs.status != AVAILABLE:
        gaps.append("rs: unavailable")
    if i.surveillance is None:
        gaps.append("surveillance: unknown")
    if RAW_MISSING in i.flags:
        gaps.append("raw_close: missing in baseline")
    if i.atr_pct is None:
        gaps.append("atr: unavailable")
    elif i.atr_from_weekly:
        gaps.append("atr: from weekly bars")
    return sorted(set(gaps))


class _Fmt:
    def __init__(self, config: Config):
        self.decimals = config.output.price_decimals

    def r(self, value: float | None) -> float | None:
        return None if value is None else round(value, self.decimals)

    def s(self, value: float | None) -> str:
        return "n/a" if value is None else f"{value:.{self.decimals}f}"


def _measures(i: EntryInputs, fmt: _Fmt) -> dict[str, Any]:
    m = i.metrics
    return {
        "baseline": fmt.r(m.baseline),
        "sessions": m.sessions,
        "smas": {str(k): fmt.r(v) for k, v in m.smas.items()},
        "sma13_slope": fmt.r(m.sma13_slope),
        "stacked": m.stacked,
        "already_broke_out": m.already_broke_out,
        "prior_ath_just_above": m.prior_ath_just_above,
        "correction": {
            "pct": fmt.r(m.correction.pct),
            "peak": m.correction.peak,
            "peak_date": m.correction.peak_date.isoformat(),
            "trough": m.correction.trough,
            "trough_date": m.correction.trough_date.isoformat(),
            "weeks": m.correction.weeks_taken,
        }
        if m.correction
        else None,
        "first_higher_low": {
            "price": m.first_higher_low.price,
            "date": m.first_higher_low.date.isoformat(),
        }
        if m.first_higher_low
        else None,
        "base_class": m.base_class,
        "base_quality": m.base_quality,
        "below_high_pct": fmt.r(m.below_high_pct),
        "atr_pct": fmt.r(i.atr_pct),
        "rs": {"value": fmt.r(i.rs.rs), "status": i.rs.status, "method": i.rs.method},
        "grade": i.grade,
        "screen": i.screen,
        "surveillance": list(i.surveillance) if i.surveillance is not None else None,
    }


def _base_columns(i: EntryInputs, setup: str, fmt: _Fmt) -> dict[str, Any]:
    m = i.metrics
    return {
        "symbol": i.symbol,
        "signal_date": i.signal_date,
        "stage": 1,
        "setup": SetupKind.OTHER.value if setup == NO_BASE else str(setup),
        "signal_close": m.close,
        "pivot": m.pivot,
        "vol_mult": fmt.r(m.vol_mult),
        "sma13": fmt.r(m.sma13),
        "sma13_rising": m.sma13_rising,
        "dist_sma_pct": fmt.r(m.dist_sma_pct),
        "correction_pct": fmt.r(m.correction.pct) if m.correction else None,
        "base_range_pct": fmt.r(m.base_range_pct),
        "close_loc": fmt.r(m.close_loc),
        "liquidity_tier": i.liquidity_tier,
        "priority_rank": i.priority_rank,
    }


def monday_plan(
    entry_low: float, entry_high: float, stop: float, trim: float, stop_pct: float, f: _Fmt
) -> str:
    return (
        f"Plan: limit buy {f.s(entry_low)}-{f.s(entry_high)}; do not chase above {f.s(entry_high)}"
        f" (a gap above stays unfilled unless price returns). "
        f"GTT stop {stop_pct:g}% below your fill: "
        f"at {f.s(entry_high)} that is {f.s(stop)}. Trim {f.s(trim)}. "
        "If unfilled by the window end it reverts to an alert at the new pivot."
    )


def _rationale(i: EntryInputs, alternative: str, gaps: list[str], stop_pct: float, f: _Fmt) -> dict:
    m = i.metrics
    return {
        "thesis": (
            f"{i.symbol} closed {f.s(m.close)} against pivot {f.s(m.pivot)} on "
            f"{f.s(m.vol_mult)}x median volume, {f.s(m.dist_sma_pct)}% above the 13-week SMA."
        ),
        "catalyst": "unverified",
        "risks": f"A breakout can fail with all conditions met; the {stop_pct:g}% stop is the cap.",
        "invalidation": f"A weekly close back below pivot {f.s(m.pivot)} warns of failure.",
        "alternatives": alternative,
        "data_gaps": "; ".join(gaps),
    }


def evaluate_entry(i: EntryInputs, config: Config) -> Decision | None:
    """The ordered table from the spec. First match wins; None means nothing is written."""
    fmt = _Fmt(config)
    m = i.metrics
    setup = classify_setup(m, config)
    triggered = is_triggered(m, i.bar_complete, config)
    conviction = conviction_of(i, setup, config)
    gaps = data_gaps_of(i, conviction["signals"])
    base = _base_columns(i, setup, fmt)
    atr_cfg = config.risk.atr_filter_daily_pct

    def raw(rules: list[str], **extra: Any) -> dict[str, Any]:
        return {
            "rules_fired": rules,
            "conviction": conviction,
            "flags": sorted(i.flags),
            "data_gaps": gaps,
            "measures": _measures(i, fmt),
            **extra,
        }

    def decision(verdict: Verdict, provisional: bool, notes: str, rules: list[str], **cols):
        return Decision(
            verdict=verdict,
            provisional=provisional,
            notes=notes,
            raw=raw(rules),
            **(base | cols),
        )

    raw_blocked = RAW_MISSING in i.flags and not config.data.allow_raw_missing_trigger
    if i.quality != OK or raw_blocked or m.vol_mult is None:
        reason = i.quality if i.quality != OK else ("raw_missing" if raw_blocked else "no volume")
        return decision(Verdict.WAIT, False, f"Data not usable: {reason}.", ["D-09"])

    if i.atr_pct is not None and i.atr_pct > atr_cfg.half_size_max:
        note = (
            f"ATR {fmt.s(i.atr_pct)}% is above {fmt.s(atr_cfg.half_size_max)}%: stop inside noise."
        )
        return decision(Verdict.NO_GO, False, note, ["S1-05"])

    if setup == NO_BASE and not triggered:
        return decision(Verdict.NO_GO, False, "No real base and no trigger.", ["S1-01"])

    if (
        config.setup.block_second_week_breakout
        and m.breakout_close
        and m.already_broke_out
        and setup != NO_BASE
    ):
        note = (
            f"Already above its pivot last week, so {fmt.s(m.close)} is a second-week close: "
            "no new entry."
        )
        return decision(Verdict.WAIT, not i.bar_complete, note, ["S1-14"])

    if triggered and setup != NO_BASE and not i.screen_trusted:
        note = "Screen failed re-verification, so this run is watch-only: no ENTRY from it."
        return decision(Verdict.WAIT, False, note, ["U-04"])

    if triggered and setup != NO_BASE:
        return _entry_decision(i, setup, conviction, gaps, base, raw, fmt, config)

    multiple = required_multiple(m, config)
    if m.breakout_close and not i.bar_complete:
        label = f"{config.data.partial_week_label} ({m.sessions} sessions)"
        note = (
            f"{label}: close {fmt.s(m.close)} above pivot {fmt.s(m.pivot)} on {fmt.s(m.vol_mult)}x "
            "so far. Provisional, confirm on the weekly close."
        )
        return decision(
            Verdict.WAIT,
            True,
            note,
            ["D-05"],
            req_vol_mult=multiple,
            req_vol_abs=required_volume(m, multiple),
        )

    if m.breakout_close and m.vol_mult < config.volume.breakout_min_multiple:
        need = required_volume(m, multiple)
        note = (
            f"Close above pivot {fmt.s(m.pivot)} on only {fmt.s(m.vol_mult)}x: false-start risk. "
            f"Needs {fmt.s(multiple)}x, about {need} shares in a full week."
        )
        result = decision(
            Verdict.WAIT,
            False,
            note,
            ["S1-07"],
            req_vol_mult=multiple,
            req_vol_abs=need,
        )
        return result

    if m.pivot and not m.breakout_close:
        below = (m.pivot - m.close) / m.pivot * 100
        if below <= config.setup.alert_max_below_pivot_pct and setup != NO_BASE:
            return _alert_decision(i, multiple, gaps, base, raw, below, fmt, config)
    return None


def _entry_decision(i, setup, conviction, gaps, base, raw, fmt, config) -> Decision:
    m = i.metrics
    ledger = config.ledger_mirror
    entry_low = m.close
    entry_high = fmt.r(m.close * (1 + config.risk.entry_zone_max_above_close_pct / 100))
    stop = fmt.r(entry_high * (1 - ledger.stop_cap_pct / 100))
    trim = fmt.r(entry_high * ledger.trim_multiple)
    size, size_reasons = size_of(i, conviction["level"], config)
    plan = monday_plan(entry_low, entry_high, stop, trim, ledger.stop_cap_pct, fmt)
    notes = (
        f"Close {fmt.s(m.close)} above pivot {fmt.s(m.pivot)} on {fmt.s(m.vol_mult)}x. "
        f"Conviction {conviction['level']}. {plan}"
    )
    if i.liquidity_tier == THIN or i.circuit_suspect:
        notes = f"{notes} {STOP_WARNING}"
    return Decision(
        verdict=Verdict.ENTRY,
        provisional=True,  # the ledger waits for a non-provisional successor (why-review)
        notes=notes,
        raw=raw(["S1-03", "S1-08"], size_reasons=size_reasons, plan=plan),
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        stop_basis=STOP_BASIS_CAP,
        trim_price=trim,
        size_pct=fmt.r(size),
        risk_pct=fmt.r((entry_high - stop) / entry_high * 100),
        window_start=i.window_start,
        window_end=i.window_end,
        rationale=_rationale(
            i,
            "WAIT rejected: the close and the volume both qualified.",
            gaps,
            ledger.stop_cap_pct,
            fmt,
        ),
        **base,
    )


def _alert_decision(i, multiple, gaps, base, raw, below, fmt, config) -> Decision:
    m = i.metrics
    ledger = config.ledger_mirror
    stop = fmt.r(m.pivot * (1 - ledger.stop_cap_pct / 100))
    need = required_volume(m, multiple)
    return Decision(
        verdict=Verdict.WAIT_ALERT,
        provisional=False,
        notes=(
            f"{fmt.s(below)}% below pivot {fmt.s(m.pivot)}. Alert there; needs {fmt.s(multiple)}x, "
            f"about {need} shares in a full week. At the pivot a stop would be {fmt.s(stop)}."
        ),
        raw=raw(["S1-06-alert"], below_pivot_pct=fmt.r(below)),
        alert_trigger=m.pivot,
        stop=stop,
        stop_basis=STOP_BASIS_CAP,
        trim_price=fmt.r(m.pivot * ledger.trim_multiple),
        risk_pct=fmt.r(ledger.stop_cap_pct),
        req_vol_mult=multiple,
        req_vol_abs=need,
        rationale=_rationale(
            i,
            "ENTRY rejected: no weekly close above the pivot yet.",
            gaps,
            ledger.stop_cap_pct,
            fmt,
        ),
        **base,
    )
