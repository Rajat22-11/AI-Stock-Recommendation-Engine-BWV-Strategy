"""E-rules: YoY growth, acceleration and quality flags from the fundamentals route.

The API's own percent changes are quarter-on-quarter, so YoY is computed here by period.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..client.endpoints import FundamentalRow
from ..config.models import Fundamentals, Holdings

SALES = "revenue"
EPS = "eps"
NET_PROFIT = "net_profit"
OTHER_INCOME = "other_income"
BORROWINGS = "borrowings"
SHARE_CAPITAL = "share_capital"
RESERVES = "reserves"
QUARTERLY_METRICS = [SALES, EPS, NET_PROFIT, OTHER_INCOME]
ANNUAL_METRICS = [BORROWINGS, SHARE_CAPITAL, RESERVES]
QUARTER = "quarter"
ANNUAL = "annual"


@dataclass(frozen=True)
class Trend:
    sales_accelerating: bool | None
    earnings_accelerating: bool | None
    earnings_metric: str
    eps_provisional: bool
    other_income_share_pct: float | None
    low_base: bool
    debt_to_equity: float | None

    @property
    def accelerating(self) -> bool | None:
        if self.sales_accelerating is None or self.earnings_accelerating is None:
            return None
        return self.sales_accelerating and self.earnings_accelerating


def _series(rows: list[FundamentalRow], metric: str, period_type: str) -> dict[str, float | None]:
    return {
        r.period_end: r.value for r in rows if r.metric == metric and r.period_type == period_type
    }


def _year_ago(period_end: str) -> str:
    year, rest = period_end.split("-", 1)
    return f"{int(year) - 1}-{rest}"


def yoy_series(values: dict[str, float | None]) -> list[tuple[str, float | None]]:
    """Oldest first. A missing or non-positive base gives None rather than a fake percentage."""
    series = []
    for period in sorted(values):
        base = values.get(_year_ago(period))
        current = values[period]
        if base is None or current is None or base <= 0:
            if base is not None or current is None:
                series.append((period, None))
            continue
        series.append((period, (current / base - 1) * 100))
    return series


def _accelerating(values: dict[str, float | None], quarters: int) -> bool | None:
    """E-03: each of the last N quarters' YoY growth is faster than the one before it."""
    points = [v for _, v in yoy_series(values)][-(quarters + 1) :]
    if len(points) < quarters + 1 or any(p is None for p in points):
        return None
    return all(a < b for a, b in zip(points, points[1:], strict=False))


def analyse(
    rows: list[FundamentalRow], cfg: Fundamentals, *, corporate_action: bool = False
) -> Trend:
    sales = _series(rows, SALES, QUARTER)
    eps = _series(rows, EPS, QUARTER)
    profit = _series(rows, NET_PROFIT, QUARTER)
    latest = max(eps, default=None)
    # E-04: as-reported EPS is not restated for splits, and the newest quarter can be missing.
    eps_provisional = latest is None or eps.get(latest) is None or latest != max(profit, default="")
    use_profit = eps_provisional or corporate_action
    earnings = profit if use_profit else eps

    share = None
    low_base = False
    if profit:
        newest = max(profit)
        income = _series(rows, OTHER_INCOME, QUARTER).get(newest)
        current = profit[newest]
        if income is not None and current and current > 0:
            share = income / current * 100
        base = profit.get(_year_ago(newest))
        if base is not None and current and current > 0:
            low_base = base / current * 100 < cfg.low_base_pct

    borrowings = _series(rows, BORROWINGS, ANNUAL)
    equity_parts = (_series(rows, SHARE_CAPITAL, ANNUAL), _series(rows, RESERVES, ANNUAL))
    debt_to_equity = None
    if borrowings:
        newest = max(borrowings)
        capital, reserves = (part.get(newest) for part in equity_parts)
        if None not in (borrowings[newest], capital, reserves) and capital + reserves > 0:
            debt_to_equity = borrowings[newest] / (capital + reserves)

    return Trend(
        sales_accelerating=_accelerating(sales, cfg.accel_quarters),
        earnings_accelerating=_accelerating(earnings, cfg.accel_quarters),
        earnings_metric=NET_PROFIT if use_profit else EPS,
        eps_provisional=eps_provisional,
        other_income_share_pct=share,
        low_base=low_base,
        debt_to_equity=debt_to_equity,
    )


PROMOTERS = "Promoters"
INSTITUTIONS = ("FIIs", "DIIs")
TRACKED_BUCKETS = (PROMOTERS, *INSTITUTIONS)
RECLASSIFICATION = "possible_reclassification"


def _quarter_key(label: str) -> dt.datetime:
    return dt.datetime.strptime(label, "%b %Y")


def holdings_trend(
    table: dict[str, dict[str, float]], cfg: Holdings
) -> tuple[bool | None, list[str]]:
    """I-15: promoters not selling while institutions add. A jump in any bucket reads as a
    reclassification, which is not a pass and is tagged; missing data is unknown."""
    quarters = sorted(table.get(PROMOTERS) or {}, key=_quarter_key)
    if len(quarters) <= 1:
        return None, []
    latest = quarters.pop()
    before = quarters.pop()
    moves = {}
    for bucket in TRACKED_BUCKETS:
        series = table.get(bucket) or {}
        if series.get(before) is None or series.get(latest) is None:
            return None, []
        moves[bucket] = series[latest] - series[before]
    if any(abs(move) > cfg.reclass_warn_pp for move in moves.values()):
        return None, [RECLASSIFICATION]
    institutions = sum(moves[b] for b in INSTITUTIONS)
    return moves[PROMOTERS] >= 0 and institutions > 0, []
