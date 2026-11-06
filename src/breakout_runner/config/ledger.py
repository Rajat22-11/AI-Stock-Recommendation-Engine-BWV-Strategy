"""C-03: the DB `config` table owns ledger values; the YAML only mirrors them."""

from __future__ import annotations

import logging
import math
from typing import Protocol

from .models import LedgerMirror

logger = logging.getLogger(__name__)

# The cost keys are stored under strategy='shared'; the rest under strategy='breakout'.
SHARED_KEYS = {"cost_roundtrip_pct_liquid", "cost_roundtrip_pct_thin"}


class LedgerConfigStore(Protocol):
    def read_ledger_config(self, keys: list[str]) -> dict[str, float]: ...

    def write_ledger_config(self, values: dict[str, float]) -> None: ...


class LedgerMirrorMismatch(Exception):
    def __init__(self, diffs: dict[str, tuple[float | None, float | None]]):
        self.diffs = diffs
        body = ", ".join(f"{k}: yaml={y} db={d}" for k, (y, d) in sorted(diffs.items()))
        super().__init__(f"ledger mirror differs from the database: {body}")


def mirror_diff(
    mirror: LedgerMirror, store: LedgerConfigStore
) -> dict[str, tuple[float | None, float | None]]:
    wanted = mirror.model_dump()
    actual = store.read_ledger_config(sorted(wanted))
    return {
        k: (y, actual.get(k))
        for k, y in wanted.items()
        if k not in actual or not math.isclose(float(y), float(actual[k]), abs_tol=1e-9)
    }


def check_mirror(mirror: LedgerMirror, store: LedgerConfigStore) -> None:
    """Abort before any write when the YAML and the database disagree."""
    diffs = mirror_diff(mirror, store)
    if diffs:
        raise LedgerMirrorMismatch(diffs)


def sync_mirror(mirror: LedgerMirror, store: LedgerConfigStore, *, confirmed: bool) -> dict:
    """Push YAML values to the DB. process_ledger replays history, so this rewrites paper P&L."""
    diffs = mirror_diff(mirror, store)
    if not diffs:
        return {}
    if not confirmed:
        raise PermissionError("ledger sync rewrites paper history; pass an explicit confirmation")
    store.write_ledger_config({k: v[0] for k, v in diffs.items()})
    logger.warning("ledger config synced from YAML: %s", sorted(diffs))
    return diffs
