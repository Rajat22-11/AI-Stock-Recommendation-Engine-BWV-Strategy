"""What the rules produce: one verdict row, in the tracker's own column names."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    ENTRY = "ENTRY"
    ENTRY_REDUCED = "ENTRY_REDUCED"
    WAIT = "WAIT"
    WAIT_ALERT = "WAIT_ALERT"
    NO_GO = "NO_GO"
    TRIM = "TRIM"
    ADD = "ADD"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class SetupKind(StrEnum):
    A_BASE = "A_base"
    B_FLAG = "B_flag"
    HOLDING = "holding"
    OTHER = "other"


STOP_BASIS_CAP = "cap8"


@dataclass
class Decision:
    symbol: str
    signal_date: dt.date
    verdict: Verdict
    provisional: bool
    setup: str
    stage: int
    notes: str
    raw: dict[str, Any]
    signal_close: float | None = None
    pivot: float | None = None
    alert_trigger: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    stop_basis: str | None = None
    trim_price: float | None = None
    size_pct: float | None = None
    risk_pct: float | None = None
    vol_mult: float | None = None
    req_vol_mult: float | None = None
    req_vol_abs: int | None = None
    sma13: float | None = None
    sma13_rising: bool | None = None
    dist_sma_pct: float | None = None
    correction_pct: float | None = None
    base_range_pct: float | None = None
    close_loc: float | None = None
    liquidity_tier: str | None = None
    priority_rank: int | None = None
    window_start: dt.date | None = None
    window_end: dt.date | None = None
    why_grade: str | None = None
    why_text: str | None = None
    rationale: dict[str, Any] = field(default_factory=dict)

    def columns(self) -> dict[str, Any]:
        """Column values for `signals`; the rationale goes to its own table."""
        row = asdict(self)
        row.pop("rationale")
        row["verdict"] = self.verdict.value
        return row
