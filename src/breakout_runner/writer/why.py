"""The why-review boundary: a size-limited packet out, judgement-only fields in."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ..config.models import Config, WhyGateMode
from ..data.calendar import TradingCalendar
from ..rules.types import Decision, Verdict
from ..units import SECONDS_PER_HOUR
from .rows import REVIEW_PACKET
from .tracker import Tracker, WriteResult

QUESTIONS = (
    "What is the news catalyst, if any?",
    "Any auditor, regulatory or governance event?",
    "Any promoter open-market selling?",
    "Why grade A, B, C or D?",
)
# Lowest priority first: dropped in this order until the packet fits.
DROP_ORDER = ("flags", "data_gaps", "numbers", "notes")
FULL_SIZE_GRADES = {"A", "B"}
# Everything the ledger must not see from a successor's predecessor.
NOT_COPIED = ("id", "created_at", "retro_seeded", "exchange")


class Review(BaseModel):
    """Judgement only. Any other field, such as a volume multiple, is rejected outright."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    why_grade: Literal["A", "B", "C", "D"]
    why_text: str
    catalyst: str | None = None
    events: str | None = None


def review_packet(decision: Decision, max_chars: int) -> dict[str, Any]:
    measures = decision.raw.get("measures", {})
    packet: dict[str, Any] = {
        "symbol": decision.symbol,
        "signal_date": decision.signal_date.isoformat(),
        "questions": list(QUESTIONS),
        "numbers": {
            "close": decision.signal_close,
            "pivot": decision.pivot,
            "vol_mult": decision.vol_mult,
            "dist_sma_pct": decision.dist_sma_pct,
            "correction_pct": decision.correction_pct,
            "base_range_pct": decision.base_range_pct,
            "grade": measures.get("grade"),
        },
        "notes": decision.notes,
        "flags": decision.raw.get("flags", []),
        "data_gaps": decision.raw.get("data_gaps", []),
    }
    for name in DROP_ORDER:
        if len(json.dumps(packet)) <= max_chars:
            break
        packet.pop(name, None)
    return packet


def successor_row(
    row: dict[str, Any],
    verdict: Verdict,
    window_start: dt.date,
    size_multiple: float,
    note: str,
    *,
    grade: str | None = None,
    why_text: str | None = None,
) -> dict[str, Any]:
    """A non-provisional copy; the original row is never modified."""
    new = {k: v for k, v in row.items() if k not in NOT_COPIED}
    new.update(
        verdict=verdict.value,
        provisional=False,
        window_start=window_start,
        why_grade=grade,
        why_text=why_text,
        notes=f"{row.get('notes') or ''} {note}".strip(),
    )
    if new.get("size_pct") is not None:
        new["size_pct"] = new["size_pct"] * size_multiple
    raw = dict(row.get("raw") or {})
    raw.pop(REVIEW_PACKET, None)
    new["raw"] = raw
    return new


def _rationale_of(tracker: Tracker, signal_id: int, catalyst: str | None) -> dict | None:
    existing = tracker.rationale(signal_id)
    if not existing:
        return None
    return {**existing, "catalyst": catalyst or existing.get("catalyst")}


def apply_review(
    tracker: Tracker,
    signal_id: int,
    review: Review,
    run_id: int,
    today: dt.date,
    calendar: TradingCalendar,
    config: Config,
) -> WriteResult:
    """A or B promotes to ENTRY; C or D to ENTRY_REDUCED at the reduced size."""
    row = tracker.signal(signal_id)
    if row is None or row["verdict"] != Verdict.ENTRY.value or not row["provisional"]:
        raise ValueError(f"signal {signal_id} is not a provisional ENTRY")
    full = review.why_grade in FULL_SIZE_GRADES
    verdict = Verdict.ENTRY if full else Verdict.ENTRY_REDUCED
    multiple = 1 if full else config.ledger_mirror.reduced_size_multiple
    note = f"Reviewed {today.isoformat()}: grade {review.why_grade}."
    if review.events:
        note = f"{note} Events: {review.events}"
    new = successor_row(
        row,
        verdict,
        calendar.next_session(today),
        multiple,
        note,
        grade=review.why_grade,
        why_text=review.why_text,
    )
    return tracker.insert_signal(
        new,
        run_id,
        signal_id,
        _rationale_of(tracker, signal_id, review.catalyst),
        (tracker.rationale(signal_id) or {}).get("confidence"),
    )


def promote_due(
    tracker: Tracker,
    run_id: int,
    now: dt.datetime,
    today: dt.date,
    calendar: TradingCalendar,
    config: Config,
) -> tuple[list[str], list[str]]:
    """Returns (promoted, still pending). The window opens on the next session after now,
    so the ledger cannot back-date a fill to before the row existed."""
    gate = config.why_gate
    promoted: list[str] = []
    pending: list[str] = []
    for row in tracker.unpromoted_entries():
        symbol = row["symbol"]
        if gate.mode == WhyGateMode.REVIEW_REQUIRED:
            pending.append(symbol)
            continue
        full = gate.mode == WhyGateMode.AUTO_FULL
        age_hours = (now - row["created_at"]).total_seconds() / SECONDS_PER_HOUR
        if not full and age_hours < gate.grace_hours:
            pending.append(symbol)
            continue
        window_start = calendar.next_session(today)
        if row["window_end"] is not None and window_start > row["window_end"]:
            pending.append(symbol)
            continue
        verdict = Verdict.ENTRY if full else Verdict.ENTRY_REDUCED
        multiple = 1 if full else config.ledger_mirror.reduced_size_multiple
        note = "Promoted without a review after the grace period, at reduced size."
        new = successor_row(row, verdict, window_start, multiple, note)
        tracker.insert_signal(
            new,
            run_id,
            row["id"],
            _rationale_of(tracker, row["id"], None),
            (tracker.rationale(row["id"]) or {}).get("confidence"),
        )
        promoted.append(symbol)
    return promoted, pending
