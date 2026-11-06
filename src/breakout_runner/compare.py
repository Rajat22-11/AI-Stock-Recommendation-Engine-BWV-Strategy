"""Shadow report: the runner's rows beside Claude's for the same week, with the deciding rule."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .config.models import Config

AGREE = "agree"
DIFFER = "differ"
ONLY_RUNNER = "only_runner"
ONLY_CLAUDE = "only_claude"
# Claude's ENTRY and the runner's Friday ENTRY both mean "buy zone": the review step differs.
ENTRY_FAMILY = {"ENTRY", "ENTRY_REDUCED"}


@dataclass(frozen=True)
class Comparison:
    symbol: str
    status: str
    runner_verdict: str | None
    claude_verdict: str | None
    differences: tuple[str, ...]
    rules: tuple[str, ...]


def _same_verdict(a: str, b: str) -> bool:
    return a == b or (a in ENTRY_FAMILY and b in ENTRY_FAMILY)


def _close(a: Any, b: Any, tolerance_pct: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    return math.isclose(a, b, rel_tol=tolerance_pct / 100, abs_tol=tolerance_pct / 100)


def compare_rows(
    runner: list[dict[str, Any]], claude: list[dict[str, Any]], config: Config
) -> list[Comparison]:
    tolerance = config.shadow.compare_tolerance_pct
    fields = config.shadow.compare_fields
    mine = {r["symbol"]: r for r in runner}
    theirs = {r["symbol"]: r for r in claude}
    report = []
    for symbol in sorted(mine.keys() | theirs.keys()):
        a, b = mine.get(symbol), theirs.get(symbol)
        rules = tuple(((a or {}).get("raw") or {}).get("rules_fired") or ())
        if a is None or b is None:
            status = ONLY_CLAUDE if a is None else ONLY_RUNNER
            report.append(
                Comparison(
                    symbol,
                    status,
                    a and a["verdict"],
                    b and b["verdict"],
                    (),
                    rules,
                )
            )
            continue
        differences = []
        if not _same_verdict(a["verdict"], b["verdict"]):
            differences.append(f"verdict {a['verdict']} vs {b['verdict']}")
        for name in fields:
            if not _close(a.get(name), b.get(name), tolerance):
                differences.append(f"{name} {a.get(name)} vs {b.get(name)}")
        report.append(
            Comparison(
                symbol,
                DIFFER if differences else AGREE,
                a["verdict"],
                b["verdict"],
                tuple(differences),
                rules,
            )
        )
    return report


def render_report(report: list[Comparison]) -> str:
    agree = sum(1 for r in report if r.status == AGREE)
    lines = [f"{agree} of {len(report)} names agree."]
    for r in report:
        if r.status == AGREE:
            continue
        detail = "; ".join(r.differences) or f"{r.runner_verdict} / {r.claude_verdict}"
        rules = ", ".join(r.rules) or "no runner rule"
        lines.append(f"{r.symbol} [{r.status}]: {detail} ({rules})")
    return "\n".join(lines)
