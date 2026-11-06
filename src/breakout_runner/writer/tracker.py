"""All tracker I/O. A non-live tracker reads through a read-only session and only collects rows."""

from __future__ import annotations

import datetime as dt
import decimal
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..data.bars import DailyBar
from ..data.calendar import week_monday
from ..units import DAYS_PER_WEEK, SECONDS_PER_MINUTE
from .rows import (
    AUTHOR,
    CONTENT_HASH,
    validate_run_status,
    validate_run_type,
    validate_signal,
    validate_tag,
)

BREAKOUT = "breakout"
RUNNER_PREFIX = "runner-"
MODEL = "runner"
BAR_SOURCE = "tapetide"
INSERTED = "inserted"
SUPERSEDED = "superseded"
UNCHANGED = "unchanged"
SHARED_KEYS = ("cost_roundtrip_pct_liquid", "cost_roundtrip_pct_thin")
SIGNAL_COLUMNS = (
    "run_id",
    "symbol",
    "signal_date",
    "source",
    "verdict",
    "provisional",
    "setup",
    "stage",
    "why_grade",
    "why_text",
    "signal_close",
    "pivot",
    "alert_trigger",
    "entry_low",
    "entry_high",
    "stop",
    "stop_basis",
    "trim_price",
    "size_pct",
    "risk_pct",
    "vol_mult",
    "req_vol_mult",
    "req_vol_abs",
    "sma13",
    "sma13_rising",
    "dist_sma_pct",
    "correction_pct",
    "base_range_pct",
    "close_loc",
    "liquidity_tier",
    "priority_rank",
    "window_start",
    "window_end",
    "skill_version",
    "supersedes",
    "notes",
    "raw",
    "strategy",
)


class RunInProgress(Exception):
    """Another runner job holds this run type and data date."""


@dataclass(frozen=True)
class WriteResult:
    action: str
    signal_id: int | None


def _floats(row: dict[str, Any]) -> dict[str, Any]:
    return {k: float(v) if isinstance(v, decimal.Decimal) else v for k, v in row.items()}


class Tracker:
    def __init__(self, conn, *, live: bool):
        self.conn = conn
        self.live = live
        self.outbox: dict[str, list[dict[str, Any]]] = {}
        self._next_fake_id = -1

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        if self.conn is None:
            return []
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return [_floats(r) for r in cur.fetchall()]

    def _write(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchall() if cur.description else []

    def _collect(self, table: str, row: dict[str, Any]) -> int:
        self.outbox.setdefault(table, []).append(row)
        fake = self._next_fake_id
        self._next_fake_id -= 1
        return fake

    def commit(self) -> None:
        if self.conn is not None:
            self.conn.commit()

    def rollback(self) -> None:
        if self.conn is not None:
            self.conn.rollback()

    # LedgerConfigStore
    def read_ledger_config(self, keys: list[str]) -> dict[str, float]:
        rows = self._rows("select key, value from config where key = any(%s)", [list(keys)])
        return {r["key"]: float(r["value"]) for r in rows}

    def write_ledger_config(self, values: dict[str, float]) -> None:
        if not self.live:
            self._collect("config", dict(values))
            return
        for key, value in values.items():
            self._write(
                "update config set value = %s, updated_at = now() where key = %s",
                [Jsonb(value), key],
            )
        self.commit()

    # Reads
    def book_started_on(self) -> dt.date | None:
        rows = self._rows("select value #>> '{}' v from config where key = 'book_started_on'")
        return dt.date.fromisoformat(rows[0]["v"]) if rows else None

    def excluded_signal_ids(self) -> set[int]:
        return {r["signal_id"] for r in self._rows("select signal_id from signal_exclusions")}

    def position_rows(self) -> list[dict[str, Any]]:
        return self._rows("select * from v_positions where status in ('open','pending')")

    def friday_rows(self, today: dt.date, max_age_days: int) -> list[dict[str, Any]]:
        since = today - dt.timedelta(days=max_age_days)
        return self._rows(
            "select s.* from signals s where s.strategy = %s and s.source = 'weekly_scan' "
            "and s.signal_date >= %s and not exists "
            "(select 1 from signals y where y.supersedes = s.id)",
            [BREAKOUT, since],
        )

    def recent_alert_symbols(self, today: dt.date, weeks: int) -> list[str]:
        since = today - dt.timedelta(days=weeks * DAYS_PER_WEEK)
        rows = self._rows(
            "select distinct symbol from signals where strategy = %s "
            "and verdict = 'WAIT_ALERT' and signal_date >= %s order by symbol",
            [BREAKOUT, since],
        )
        return [r["symbol"] for r in rows]

    def latest_stored_bars(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        rows = self._rows(
            "select distinct on (symbol) symbol, bar_date, close from price_bars "
            "where symbol = any(%s) order by symbol, bar_date desc",
            [list(symbols)],
        )
        return {r["symbol"]: r for r in rows}

    def last_run(self, run_type: str) -> dict[str, Any] | None:
        rows = self._rows(
            "select * from runs where strategy = %s and run_type = %s and skill_version like %s "
            "order by started_at desc limit 1",
            [BREAKOUT, run_type, f"{RUNNER_PREFIX}%"],
        )
        return rows[0] if rows else None

    def active_signal(
        self, symbol: str, signal_date: dt.date, source: str, excluded: set[int]
    ) -> dict[str, Any] | None:
        rows = self._rows(
            "select s.* from signals s where s.strategy = %s and s.symbol = %s "
            "and s.signal_date = %s and s.source = %s and not exists "
            "(select 1 from signals y where y.supersedes = s.id) order by s.id desc",
            [BREAKOUT, symbol, signal_date, source],
        )
        return next((r for r in rows if r["id"] not in excluded), None)

    def signal(self, signal_id: int) -> dict[str, Any] | None:
        rows = self._rows("select * from signals where id = %s", [signal_id])
        return rows[0] if rows else None

    def rationale(self, signal_id: int) -> dict[str, Any] | None:
        rows = self._rows("select * from signal_rationale where signal_id = %s", [signal_id])
        return rows[0] if rows else None

    def unpromoted_entries(self) -> list[dict[str, Any]]:
        """Only a runner's own Friday rows: mid-week ENTRY waits for the weekly close."""
        return self._rows(
            "select s.* from signals s where s.strategy = %s and s.verdict = 'ENTRY' "
            "and s.provisional and s.source = 'weekly_scan' and s.skill_version like %s "
            "and not exists (select 1 from signals y where y.supersedes = s.id) "
            "and not exists (select 1 from signal_exclusions e where e.signal_id = s.id) "
            "order by s.id",
            [BREAKOUT, f"{RUNNER_PREFIX}%"],
        )

    def closed_positions_without_review(self) -> list[dict[str, Any]]:
        return self._rows(
            "select r.* from v_trade_review r where r.status = 'closed' and not exists "
            "(select 1 from trade_reviews t where t.signal_id = r.signal_id)"
        )

    def outcome_candidates(self, cutoff: dt.date) -> list[dict[str, Any]]:
        return self._rows(
            "select o.* from v_signal_outcomes o where o.verdict in ('WAIT','WAIT_ALERT','NO_GO') "
            "and o.signal_date <= %s and not exists "
            "(select 1 from trade_reviews t where t.signal_id = o.signal_id) and not exists "
            "(select 1 from signal_exclusions e where e.signal_id = o.signal_id)",
            [cutoff],
        )

    def has_review_tag(self, signal_id: int, tag: str) -> bool:
        return bool(
            self._rows(
                "select 1 from trade_reviews where signal_id = %s and tag = %s", [signal_id, tag]
            )
        )

    def trade_stats(self) -> dict[str, Any]:
        rows = self._rows("select * from v_trade_stats where dimension = 'overall'")
        return rows[0] if rows else {}

    def open_position_stats(self) -> dict[str, Any]:
        rows = self._rows(
            "select count(*) n, coalesce(sum(unrealized_pnl), 0) unrealized from v_open_positions"
        )
        return rows[0] if rows else {}

    def signals_in_run(self, run_id: int) -> int:
        rows = self._rows("select count(*) n from signals where run_id = %s", [run_id])
        return rows[0]["n"] if rows else len(self.outbox.get("signals", []))

    # Run lifecycle
    def guard(self, run_type: str, data_as_of: dt.date, max_minutes: int) -> None:
        """Claude's old `running` rows are not ours to close and do not block a run."""
        rows = self._rows(
            "select id, extract(epoch from now() - started_at) age from runs "
            "where strategy = %s and status = 'running' and run_type = %s "
            "and data_as_of = %s and skill_version like %s",
            [BREAKOUT, run_type, data_as_of, f"{RUNNER_PREFIX}%"],
        )
        for row in rows:
            if row["age"] < max_minutes * SECONDS_PER_MINUTE:
                raise RunInProgress(f"run {row['id']} is still running")
            if self.live:
                self._write(
                    "update runs set status = 'failed', finished_at = now(), "
                    "summary = 'Stale: never closed by its job.' where id = %s",
                    [row["id"]],
                )
        if self.live:
            self.commit()

    def open_run(
        self,
        run_type: str,
        trigger_id: str | None,
        session_id: str | None,
        data_as_of: dt.date,
        version: str,
        sha256: str,
        effective_from: dt.date,
        notes: dict[str, Any],
    ) -> int:
        validate_run_type(run_type)
        row = {
            "run_type": run_type,
            "trigger_id": trigger_id,
            "session_id": session_id,
            "data_as_of": data_as_of,
            "model": MODEL,
            "skill_version": version,
            "notes": json.dumps(notes, sort_keys=True),
        }
        if not self.live:
            return self._collect("runs", row)
        self._write(
            "insert into skill_versions(version, effective_from, sha256, summary, strategy) "
            "values (%s, %s, %s, %s, %s) on conflict (version) do nothing",
            [version, effective_from, sha256, "Deterministic runner config", BREAKOUT],
        )
        created = self._write(
            "insert into runs(run_type, trigger_id, session_id, data_as_of, model, skill_version, "
            "notes, strategy) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
            [*row.values(), BREAKOUT],
        )
        self.commit()
        return created[0]["id"]

    def close_run(
        self,
        run_id: int,
        status: str,
        summary: str,
        data_calls: dict[str, Any],
        notes: dict[str, Any] | None = None,
    ) -> None:
        validate_run_status(status)
        if not self.live:
            self.outbox["runs"][0].update(status=status, summary=summary, data_calls=data_calls)
            if notes:
                self.outbox["runs"][0]["notes"] = json.dumps(notes, sort_keys=True)
            return
        self._write(
            "update runs set status = %s, finished_at = now(), summary = %s, data_calls = %s, "
            "notes = coalesce(%s, notes) where id = %s",
            [
                status,
                summary,
                Jsonb(data_calls),
                json.dumps(notes, sort_keys=True) if notes else None,
                run_id,
            ],
        )
        self.commit()

    # Writes
    def upsert_bars(self, symbol: str, daily: list[DailyBar], tolerance_pct: float) -> bool:
        """New bars from the day after the latest stored one. False flags a transcription gap."""
        if not daily:
            return True
        stored = self.latest_stored_bars([symbol]).get(symbol)
        ok = True
        if stored:
            fresh = next((b for b in daily if b.date == stored["bar_date"]), None)
            if fresh and abs(fresh.close / stored["close"] - 1) * 100 > tolerance_pct:
                ok = False
            start = stored["bar_date"]
            new = [b for b in daily if b.date > start]
        else:
            monday = week_monday(daily[-1].date)
            new = [b for b in daily if b.date >= monday]
        for b in new:
            row = (symbol, b.date, b.open, b.high, b.low, b.close, b.raw_volume, BAR_SOURCE)
            if not self.live:
                self._collect("price_bars", dict(symbol=symbol, bar_date=b.date, close=b.close))
                continue
            self._write(
                "insert into price_bars(symbol, bar_date, open, high, low, close, volume, source) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict (symbol, bar_date) do update "
                "set open = excluded.open, high = excluded.high, low = excluded.low, "
                "close = excluded.close, volume = excluded.volume, source = excluded.source",
                row,
            )
        return ok

    def process_ledger(self, through: dt.date) -> dict[str, Any] | None:
        if not self.live:
            self._collect("ledger_calls", {"through": through})
            return None
        rows = self._write("select process_ledger(%s) result", [through])
        return rows[0]["result"]

    def write_signal(
        self,
        row: dict[str, Any],
        run_id: int,
        excluded: set[int],
        rationale: dict[str, Any] | None = None,
        confidence: int | None = None,
    ) -> WriteResult:
        """Append-only: skip on the same hash, otherwise a new row superseding the old one."""
        validate_signal(row)
        active = self.active_signal(row["symbol"], row["signal_date"], row["source"], excluded)
        new_hash = row["raw"].get(CONTENT_HASH)
        if active and (active.get("raw") or {}).get(CONTENT_HASH) == new_hash:
            return WriteResult(UNCHANGED, active["id"])
        return self.insert_signal(
            row, run_id, active["id"] if active else None, rationale, confidence
        )

    def insert_signal(
        self,
        row: dict[str, Any],
        run_id: int,
        supersedes: int | None,
        rationale: dict[str, Any] | None,
        confidence: int | None,
    ) -> WriteResult:
        validate_signal(row)
        full = {**row, "run_id": run_id, "supersedes": supersedes, "strategy": BREAKOUT}
        action = SUPERSEDED if supersedes else INSERTED
        if not self.live:
            signal_id = self._collect("signals", full)
        else:
            values = [Jsonb(full[c]) if c == "raw" else full.get(c) for c in SIGNAL_COLUMNS]
            names = ", ".join(SIGNAL_COLUMNS)
            marks = ", ".join(["%s"] * len(SIGNAL_COLUMNS))
            created = self._write(
                f"insert into signals({names}) values ({marks}) returning id", values
            )
            signal_id = created[0]["id"]
        if rationale:
            self._insert_rationale(signal_id, run_id, rationale, confidence)
        return WriteResult(action, signal_id)

    def _insert_rationale(
        self, signal_id: int, run_id: int, rationale: dict[str, Any], confidence: int | None
    ) -> None:
        row = {
            "signal_id": signal_id,
            "run_id": run_id,
            "confidence": confidence,
            **{k: rationale.get(k) for k in _RATIONALE_FIELDS},
        }
        if not self.live:
            self._collect("signal_rationale", row)
            return
        names = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        self._write(f"insert into signal_rationale({names}) values ({marks})", row.values())

    def insert_trade_review(
        self, signal_id: int, symbol: str, run_id: int, tag: str, lesson: str
    ) -> bool:
        """Never a second row for one (signal, tag), and never an edit of another author's row."""
        validate_tag(tag)
        if self.has_review_tag(signal_id, tag):
            return False
        row = {
            "signal_id": signal_id,
            "symbol": symbol,
            "run_id": run_id,
            "tag": tag,
            "lesson": lesson,
            "author": AUTHOR,
        }
        if not self.live:
            self._collect("trade_reviews", row)
            return True
        self._write(
            "insert into trade_reviews(signal_id, symbol, run_id, tag, lesson, author) "
            "values (%s, %s, %s, %s, %s, %s)",
            row.values(),
        )
        return True

    def insert_review(
        self, run_id: int, review_date: dt.date, period: str, stats: dict, observations: str
    ) -> None:
        row = {"run_id": run_id, "review_date": review_date, "period": period, "stats": stats}
        if not self.live:
            self._collect("reviews", {**row, "observations": observations})
            return
        self._write(
            "insert into reviews(run_id, review_date, period, stats, observations) "
            "values (%s, %s, %s, %s, %s)",
            [run_id, review_date, period, Jsonb(stats), observations],
        )


_RATIONALE_FIELDS = ("thesis", "catalyst", "risks", "invalidation", "alternatives", "data_gaps")
