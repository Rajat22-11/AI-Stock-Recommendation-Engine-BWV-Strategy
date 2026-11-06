"""Typed wrappers over the REST routes the runner uses. REST field names only (F-01)."""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass
from typing import Any

from .http import TapetideClient


def _build[T](cls: type[T], row: dict[str, Any]) -> T:
    # KeyError on a missing field is deliberate: a changed contract must fail loudly.
    return cls(**{f.name: row[f.name] for f in dataclasses.fields(cls)})


@dataclass(frozen=True)
class MarketStatus:
    status: str
    date: str
    is_trading_day: bool
    holiday_name: str | None
    session: dict | None


@dataclass(frozen=True)
class Holiday:
    date: str
    name: str | None
    exchange: str
    type: str | None


@dataclass(frozen=True)
class PriceBar:
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    raw_close: float | None
    volume: int | None


@dataclass(frozen=True)
class PricePage:
    bars: list[PriceBar]
    last_stored_date: str | None
    first_stored_date: str | None
    has_more: bool
    next_cursor: str | None
    first_date: str | None


@dataclass(frozen=True)
class ScreenRow:
    rank: int
    symbol: str
    name: str | None
    ltp: float | None
    pe: float | None
    market_cap_cr: float | None
    net_profit_qtr_cr: float | None
    profit_growth_qtr_pct: float | None
    sales_qtr_cr: float | None
    sales_growth_qtr_pct: float | None
    roce_pct: float | None
    roe_pct: float | None
    sort_value: float | None
    ratio_values: dict[str, float | None]


@dataclass(frozen=True)
class ScreenPage:
    rows: list[ScreenRow]
    total_matches: int | None
    next_cursor: str | None


@dataclass(frozen=True)
class HeatmapRow:
    symbol: str
    change_pct_3m: float | None
    change_pct_6m: float | None
    change_pct_1y: float | None
    session_date: str | None


@dataclass(frozen=True)
class SurveillanceRow:
    symbol: str
    framework: str
    measure: str
    stage: int | None
    as_of: str
    first_seen: str


@dataclass(frozen=True)
class FundamentalRow:
    metric: str
    period_type: str
    period_end: str
    basis: str
    value: float | None
    unit: str
    available_from: str
    eps_basis: str | None


@dataclass(frozen=True)
class SignalRow:
    symbol: str
    session_date: str
    signals: list[str]
    ltp: float | None
    volume: float | None
    relative_volume: float | None
    high_52w: float | None
    market_cap_cr: float | None


@dataclass(frozen=True)
class PledgeRow:
    symbol: str
    quarter_end: str
    promoter_pledged_pct: float | None


@dataclass(frozen=True)
class CorporateAction:
    symbol: str | None
    series: str | None
    action_type: str
    ex_date: str
    ratio_numerator: float | None
    ratio_denominator: float | None


@dataclass(frozen=True)
class LiveQuote:
    symbol: str
    ltp: float | None
    high: float | None
    low: float | None
    prev_close: float | None
    volume: float | None
    market_status: str | None


class TapetideApi:
    def __init__(self, client: TapetideClient):
        self.client = client

    def _walk(self, route: str, path: str, params: dict, *, charged: bool = True) -> list[dict]:
        """Follow meta.next_cursor to the end; a page can hold fewer rows than `limit`."""
        rows: list[dict] = []
        query = dict(params)
        while True:
            body = self.client.request(route, path, query, charged=charged)
            rows.extend(body["data"])
            cursor = body.get("meta", {}).get("next_cursor")
            if not cursor:
                return rows
            query["cursor"] = cursor

    def market_status(self) -> MarketStatus:
        body = self.client.request("market_status", "/v1/market/status", charged=False)
        return _build(MarketStatus, body["data"])

    def holidays(self, start: dt.date, end: dt.date) -> list[Holiday]:
        params = {"from": start.isoformat(), "to": end.isoformat()}
        rows = self._walk("holidays", "/v1/market/holidays", params)
        return [_build(Holiday, r) for r in rows]

    def price(self, symbol: str, days: int, end_date: str | None = None) -> PricePage:
        params: dict[str, Any] = {"days": days, "interval": "daily"}
        if end_date:
            params["end_date"] = end_date
        body = self.client.request("price", f"/v1/stocks/{symbol}/price", params)
        meta = body.get("meta", {})
        window = meta.get("window", {})
        coverage = meta.get("coverage", {})
        return PricePage(
            bars=[_build(PriceBar, r) for r in body["data"]],
            last_stored_date=coverage.get("last_stored_date"),
            first_stored_date=coverage.get("first_stored_date"),
            has_more=bool(window.get("has_more")),
            next_cursor=window.get("next_cursor"),
            first_date=window.get("first_date"),
        )

    def screen_field_names(self) -> set[str]:
        rows = self._walk("screen_fields", "/v1/screens/fundamental/fields", {}, charged=False)
        return {r["name"] for r in rows}

    def screen_fundamental(
        self, query: str, sort: str, order: str, limit: int, cursor: str | None = None
    ) -> ScreenPage:
        params: dict[str, Any] = {"query": query, "sort": sort, "order": order, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = self.client.request("screen_fundamental", "/v1/screens/fundamental", params)
        meta = body.get("meta", {})
        return ScreenPage(
            rows=[_build(ScreenRow, r) for r in body["data"]],
            total_matches=meta.get("total_matches"),
            next_cursor=meta.get("next_cursor"),
        )

    def heatmap(self, slug: str, limit: int) -> list[HeatmapRow]:
        rows = self._walk("heatmap", f"/v1/market/heatmap/{slug}", {"limit": limit})
        return [_build(HeatmapRow, r) for r in rows]

    def corporate_actions(self, limit: int) -> list[CorporateAction]:
        rows = self._walk("corporate_actions", "/v1/market/corporate-actions", {"limit": limit})
        return [_build(CorporateAction, r) for r in rows]

    def live_quotes(self, symbols: list[str]) -> list[LiveQuote]:
        params = {"symbols": ",".join(symbols)}
        rows = self.client.request("live_quotes", "/v1/quotes/live", params)["data"]
        return [_build(LiveQuote, r) for r in rows if r.get("found")]

    def surveillance(self, limit: int) -> list[SurveillanceRow]:
        rows = self._walk("surveillance", "/v1/market/surveillance", {"limit": limit})
        return [_build(SurveillanceRow, r) for r in rows]

    def fundamentals(
        self, symbol: str, metrics: list[str], period_type: str, start: dt.date | None = None
    ) -> list[FundamentalRow]:
        params: dict[str, Any] = {"metrics": ",".join(metrics), "period_type": period_type}
        if start:
            params["from"] = start.isoformat()
        rows = self._walk("fundamentals", f"/v1/stocks/{symbol}/fundamentals", params)
        return [_build(FundamentalRow, r) for r in rows]

    def market_signals(self, signal: str, min_market_cap_cr: float, limit: int) -> list[SignalRow]:
        params = {"signal": signal, "min_market_cap_cr": min_market_cap_cr, "limit": limit}
        rows = self._walk("market_signals", "/v1/market/signals", params)
        return [_build(SignalRow, r) for r in rows]

    def promoter_pledge(self, symbol: str) -> list[PledgeRow]:
        rows = self._walk("pledge", f"/v1/stocks/{symbol}/promoter-pledge", {})
        return [_build(PledgeRow, r) for r in rows]

    def shareholding(self, symbol: str) -> dict[str, dict[str, float]]:
        """Bucket -> {"Dec 2024": percent}. The route nests its table inside `data.data[0]`."""
        body = self.client.request("shareholding", f"/v1/stocks/{symbol}/shareholding")
        tables = body["data"].get("data") or []
        return tables[0]["data"] if tables else {}
