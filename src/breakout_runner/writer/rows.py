"""Validate rows against the tracker's CHECK constraints before any insert, and hash them."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from ..rules.types import Decision
from ..units import STAGE_EXIT

VERDICTS = {
    "ENTRY",
    "ENTRY_REDUCED",
    "WAIT",
    "WAIT_ALERT",
    "NO_GO",
    "TRIM",
    "ADD",
    "HOLD",
    "REDUCE",
    "EXIT",
}
SOURCES = {"weekly_scan", "midweek_checkin", "adhoc", "backfill"}
RUN_TYPES = {"weekly_scan", "midweek_checkin", "adhoc", "backfill", "processing"}
RUN_STATUSES = {"running", "succeeded", "partial", "failed"}
SETUPS = {"A_base", "B_flag", "holding", "other"}
STOP_BASES = {"structural", "cap8", "higher_low", "sma", "other"}
WHY_GRADES = {"A", "B", "C", "D"}
LIQUIDITY_TIERS = {"liquid", "thin"}
STAGE_MIN = 1
STAGE_MAX = STAGE_EXIT
TRADE_TAGS = {
    "good_call",
    "winner",
    "valid_stop",
    "whipsaw_tight_stop",
    "gap_through_stop",
    "missed_runner",
    "avoided_loser",
    "bad_entry",
    "rule_violation",
    "data_gap",
    "removed_fresh_start",
    "other",
}
# The live CHECK only allows these two; runner-written rows use the first, as Claude's did.
AUTHOR = "agent"
DEFAULT_LIQUIDITY = "liquid"
CONTENT_HASH = "content_hash"
REVIEW_PACKET = "review_packet"


class InvalidRow(ValueError):
    """A row would violate a table constraint."""


def _check(value: Any, allowed: set[str], name: str, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if value not in allowed:
        raise InvalidRow(f"{name} {value!r} is not one of {sorted(allowed)}")


def validate_signal(row: dict[str, Any]) -> None:
    _check(row.get("verdict"), VERDICTS, "verdict", nullable=False)
    _check(row.get("source"), SOURCES, "source", nullable=False)
    _check(row.get("setup"), SETUPS, "setup")
    _check(row.get("stop_basis"), STOP_BASES, "stop_basis")
    _check(row.get("why_grade"), WHY_GRADES, "why_grade")
    _check(row.get("liquidity_tier"), LIQUIDITY_TIERS, "liquidity_tier", nullable=False)
    stage = row.get("stage")
    if stage is not None and not STAGE_MIN <= stage <= STAGE_MAX:
        raise InvalidRow(f"stage {stage} is outside {STAGE_MIN}-{STAGE_MAX}")
    if not row.get("symbol") or row.get("signal_date") is None:
        raise InvalidRow("symbol and signal_date are required")


def validate_run_type(run_type: str) -> None:
    _check(run_type, RUN_TYPES, "run_type", nullable=False)


def validate_run_status(status: str) -> None:
    _check(status, RUN_STATUSES, "status", nullable=False)


def validate_tag(tag: str) -> None:
    _check(tag, TRADE_TAGS, "tag", nullable=False)


def _plain(value: Any) -> Any:
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return str(value)


def content_hash(columns: dict[str, Any]) -> str:
    """Identical inputs and config hash the same, so a rerun inserts nothing."""
    body = {k: v for k, v in columns.items() if k not in ("id", "run_id", "skill_version")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=_plain)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def signal_row(decision: Decision, source: str, skill_version: str) -> dict[str, Any]:
    """The insert row for `signals`, with the hash in `raw`."""
    row = decision.columns()
    row["source"] = source
    row["liquidity_tier"] = row.get("liquidity_tier") or DEFAULT_LIQUIDITY
    row["raw"] = {**row["raw"], CONTENT_HASH: content_hash(row)}
    row["skill_version"] = skill_version
    validate_signal(row)
    return row
