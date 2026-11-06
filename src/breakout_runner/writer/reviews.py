"""Deterministic review records: trade lessons, old-signal outcomes and the weekly row."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config.models import Reviews
from ..units import DAYS_PER_WEEK
from .tracker import Tracker

MISSED_RUNNER = "missed_runner"
AVOIDED_LOSER = "avoided_loser"
OTHER_TAG = "other"
WEEKLY = "weekly"
# `v_trade_review.auto_tag` can be `loser`, which `trade_reviews.tag` does not allow.
AUTO_TAG_TO_TAG = {"loser": OTHER_TAG}


def trade_tag(auto_tag: str) -> str:
    return AUTO_TAG_TO_TAG.get(auto_tag, auto_tag)


def trade_lesson(row: dict[str, Any]) -> str:
    return (
        f"Filled {row.get('fill_price')} on {row.get('fill_date')}, "
        f"closed {row.get('exit_price')} on {row.get('exit_date')} ({row.get('exit_reason')}). "
        f"Realised {row.get('realized_pnl')}, "
        f"{row.get('r_multiple')}R over {row.get('days_held')} days. "
        f"Tracker tag: {row.get('auto_tag')}."
    )


def outcome_tag(row: dict[str, Any], cfg: Reviews) -> str | None:
    up, down = row.get("max_up_4w_pct"), row.get("max_down_4w_pct")
    if up is not None and up >= cfg.missed_runner_gain_pct:
        return MISSED_RUNNER
    if down is not None and down <= cfg.avoided_loser_drawdown_pct:
        return AVOIDED_LOSER
    return None


def outcome_lesson(row: dict[str, Any]) -> str:
    return (
        f"{row['verdict']} on {row['signal_date']}; "
        f"4-week maximum gain {row.get('max_up_4w_pct')}%, "
        f"maximum drawdown {row.get('max_down_4w_pct')}%."
    )


def write_trade_reviews(tracker: Tracker, run_id: int, today: dt.date, cfg: Reviews) -> int:
    written = 0
    for row in tracker.closed_positions_without_review():
        written += tracker.insert_trade_review(
            row["signal_id"], row["symbol"], run_id, trade_tag(row["auto_tag"]), trade_lesson(row)
        )
    cutoff = today - dt.timedelta(days=cfg.outcome_min_weeks * DAYS_PER_WEEK)
    for row in tracker.outcome_candidates(cutoff):
        tag = outcome_tag(row, cfg)
        if tag:
            written += tracker.insert_trade_review(
                row["signal_id"], row["symbol"], run_id, tag, outcome_lesson(row)
            )
    return written


def weekly_stats(tracker: Tracker, run_id: int, capital: float, decimals: int) -> dict[str, Any]:
    overall = tracker.trade_stats()
    book = tracker.open_position_stats()
    realized = overall.get("realized_pnl") or 0
    unrealized = book.get("unrealized") or 0
    return {
        "equity": round(capital + realized + unrealized, decimals),
        "realized_pnl": realized,
        "closed_trades": overall.get("closed") or 0,
        "open_positions": book.get("n") or 0,
        "win_rate_pct": overall.get("win_rate_pct"),
        "avg_r": overall.get("avg_r"),
        "signals_logged": tracker.signals_in_run(run_id),
    }


def write_weekly_review(
    tracker: Tracker,
    run_id: int,
    today: dt.date,
    capital: float,
    reviews_written: int,
    summary: str,
    decimals: int,
) -> None:
    """Observations only. `proposed_change` stays empty: rule changes are a person's call."""
    stats = weekly_stats(tracker, run_id, capital, decimals)
    observations = (
        f"{summary} {stats['closed_trades']} closed trades, {stats['open_positions']} open, "
        f"realised {stats['realized_pnl']}. {reviews_written} trade reviews written."
    )
    tracker.insert_review(run_id, today, WEEKLY, stats, observations)
