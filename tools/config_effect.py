"""For a config pull request: the keys that changed and what they do to the golden ACMESOLAR week.

Run: python tools/config_effect.py BASE.yaml HEAD.yaml. Prints Markdown. Makes no network call.
"""

from __future__ import annotations

import csv
import datetime as dt
import sys
from pathlib import Path

import yaml

from breakout_runner.config.loader import parse_config
from breakout_runner.data.bars import WeeklyBar
from breakout_runner.indicators.weekly import compute_metrics
from breakout_runner.rules.entry import EntryInputs, evaluate_entry

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "ACMESOLAR_weekly.csv"
GOLDEN_WEEK = dt.date(2026, 9, 14)
DEFAULT_SESSIONS = 5
COLUMNS = ("verdict", "pivot", "vol_mult", "entry_high", "stop", "trim_price", "size_pct")


def _flatten(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, f"{prefix}{key}.")
    else:
        yield prefix[:-1], node


def _bars() -> list[WeeklyBar]:
    bars = []
    with FIXTURE.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            week = dt.date.fromisoformat(row["date"])
            bars.append(
                WeeklyBar(
                    week=week,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    sessions=int(row.get("sessions") or DEFAULT_SESSIONS),
                    last_date=week,
                )
            )
    return bars


def _golden(config) -> dict:
    bars = _bars()
    t = next(i for i, b in enumerate(bars) if b.week == GOLDEN_WEEK)
    metrics = compute_metrics(bars, t, config.indicators, config.data)
    decision = evaluate_entry(
        EntryInputs(
            symbol="ACMESOLAR",
            metrics=metrics,
            signal_date=dt.date(2026, 9, 18),
            bar_complete=True,
            window_start=dt.date(2026, 9, 21),
            window_end=dt.date(2026, 9, 24),
        ),
        config,
    )
    return {c: getattr(decision, c, None) for c in COLUMNS} if decision else {}


def main(argv: list[str]) -> int:
    base_raw, head_raw = (yaml.safe_load(Path(p).read_text("utf-8")) for p in argv[:2])
    before, after = dict(_flatten(base_raw)), dict(_flatten(head_raw))
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    print("## Config change\n")
    if not changed:
        print("No config values changed.")
        return 0
    print("| Key | Before | After |\n|---|---|---|")
    for key in changed:
        print(f"| `{key}` | {before.get(key)} | {after.get(key)} |")
    print("\n## Golden week: ACMESOLAR, 14 Sep 2026\n")
    try:
        base_row, head_row = _golden(parse_config(base_raw)), _golden(parse_config(head_raw))
    except Exception as exc:
        print(f"Could not compare the golden week: {exc}")
        return 0
    print("| Field | Before | After |\n|---|---|---|")
    for column in COLUMNS:
        print(f"| {column} | {base_row.get(column)} | {head_row.get(column)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
