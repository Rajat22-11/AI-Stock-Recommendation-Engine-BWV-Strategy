"""Typed schema for breakout.config.yaml. Ranges live beside each field."""

from __future__ import annotations

import datetime as dt
import math
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Pct = Annotated[float, Field(ge=0, le=100)]
PosInt = Annotated[int, Field(ge=1)]
NonNegInt = Annotated[int, Field(ge=0)]
Multiple = Annotated[float, Field(gt=0, le=100)]

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
SHEDDABLE = {"financials", "pledge", "surveillance", "alert_screen", "deep_verify"}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenSource(Model):
    label: str = Field(min_length=1)
    env: str

    @field_validator("env")
    @classmethod
    def _env_is_a_name(cls, v: str) -> str:
        # The message deliberately omits the value: it may be a pasted secret.
        if not ENV_NAME.match(v):
            raise ValueError("must be an environment-variable name, not a value")
        return v


class Tapetide(Model):
    base_url: str
    tokens: list[TokenSource] = Field(min_length=1)
    recommended_tokens: PosInt

    @model_validator(mode="after")
    def _unique(self) -> Tapetide:
        if len({t.label for t in self.tokens}) != len(self.tokens):
            raise ValueError("token labels must be unique")
        if len({t.env for t in self.tokens}) != len(self.tokens):
            raise ValueError("token env names must be unique")
        return self


class Http(Model):
    retries: NonNegInt
    backoff_seconds: list[Annotated[float, Field(ge=0)]] = Field(min_length=1)
    timeout_seconds: Annotated[float, Field(gt=0)]
    burst_per_minute: PosInt
    max_rate_limit_waits: PosInt


class Quota(Model):
    reserve_calls: NonNegInt
    reserve_for_claude: NonNegInt
    price_calls_max: NonNegInt
    midweek_budget_calls: PosInt
    shed_order: list[str]

    @field_validator("shed_order")
    @classmethod
    def _known_items(cls, v: list[str]) -> list[str]:
        unknown = set(v) - SHEDDABLE
        if unknown:
            raise ValueError(f"not sheddable: {sorted(unknown)}")
        return v


class LedgerMirror(Model):
    paper_capital: Annotated[float, Field(gt=0)]
    position_cap_pct: Pct
    risk_per_trade_pct: Pct
    stop_cap_pct: Pct
    trim_multiple: Annotated[float, Field(gt=1)]
    trim_fraction: Annotated[float, Field(gt=0, le=1)]
    reduced_size_multiple: Annotated[float, Field(gt=0, le=1)]
    max_open_positions: PosInt
    cost_roundtrip_pct_liquid: Pct
    cost_roundtrip_pct_thin: Pct


class Data(Model):
    lookback_calendar_days: Annotated[int, Field(ge=1, le=2000)]  # API max per call: 2000
    session_base: PosInt
    corp_action_tolerance_pct: Pct
    max_null_bars: NonNegInt
    allow_raw_missing_trigger: bool
    calendar_cache_days: PosInt
    partial_week_label: str
    special_sessions: list[dt.date]


class BaseBands(Model):
    tight_max: Pct
    normal_max: Pct
    loose_max: Pct

    @model_validator(mode="after")
    def _ordered(self) -> BaseBands:
        if not self.tight_max < self.normal_max < self.loose_max:
            raise ValueError("base bands must increase")
        return self


class Indicators(Model):
    sma_weeks: list[PosInt] = Field(min_length=2)
    slope_lookback_weeks: PosInt
    pivot_lookback_weeks: PosInt
    pivot_low_side_bars: PosInt
    volume_baseline: Literal["median"]
    volume_baseline_weeks: PosInt
    atr_period: PosInt
    atr_weekly_to_daily_ratio: Multiple
    correction_lookback_weeks: PosInt
    acceptable_correction_pct: Pct
    base_window_weeks: PosInt
    base_bands: BaseBands
    ath_proximity_pct: Pct
    overext_peaks_to_measure: PosInt
    overext_min_peaks: PosInt
    overext_lookback_weeks: PosInt
    peak_window_weeks: PosInt
    min_peak_dist_pct: Pct
    thin_turnover_cr: Annotated[float, Field(ge=0)]
    turnover_sessions: PosInt
    circuit_window_sessions: PosInt
    circuit_sessions_flag: PosInt
    circuit_pct_min: Pct
    min_pivot_weeks: PosInt
    structure_lookback_weeks: PosInt
    hh_hl_pivots: PosInt

    @field_validator("sma_weeks")
    @classmethod
    def _increasing(cls, v: list[int]) -> list[int]:
        if v != sorted(set(v)):
            raise ValueError("sma_weeks must be strictly increasing")
        return v


class Universe(Model):
    min_price: Annotated[float, Field(ge=0)]
    min_median_turnover_cr: Annotated[float, Field(ge=0)]
    exclude_symbols: list[str]


class LocalRatio(Model):
    kind: Literal["last_close", "sma_close", "below_high_pct", "avg_volume"]
    sessions: PosInt


class Predicate(Model):
    ratio: str
    op: Literal[">", "<", ">=", "<="]
    value: float | None = None
    ref: str | None = None

    @model_validator(mode="after")
    def _exactly_one_side(self) -> Predicate:
        if (self.value is None) == (self.ref is None):
            raise ValueError("a predicate needs exactly one of value or ref")
        return self


class PostFilters(Model):
    max_close_to_sma13_trigger: Multiple
    max_close_to_sma13_alert: Multiple
    max_daily_atr_pct_trigger: Pct | None
    max_daily_atr_pct_alert: Pct
    discard_if_below_52w_high_pct: Pct


class Screens(Model):
    common: list[Predicate]
    trigger_q1: list[Predicate] = Field(min_length=1)
    alert_q2: list[Predicate] = Field(min_length=1)
    sort: str
    order: Literal["asc", "desc"]
    page_limit: Annotated[int, Field(ge=1, le=50)]
    max_rows: PosInt
    screen_drift_max_pct: Pct
    local_ratios: dict[str, LocalRatio]
    post_filters: PostFilters
    max_deep_verify: PosInt


class GradeA(Model):
    roce_min: float
    profit_growth_min: float
    sales_growth_min: float


class GradeB(Model):
    roce_min: float
    growth_min: float


class GradeDrop(Model):
    qtr_profit_lt: float
    profit_growth_lt: float


class FundamentalGrade(Model):
    A: GradeA
    B: GradeB
    drop_if: GradeDrop


class SetupB(Model):
    max_range_pct: Pct
    max_below_high_pct: Pct


class Setup(Model):
    textbook_correction_pct: Pct
    block_second_week_breakout: bool
    setup_b: SetupB
    alert_max_below_pivot_pct: Pct


class Volume(Model):
    breakout_min_multiple: Multiple
    marginal_below: Multiple
    sweet_spot: tuple[Multiple, Multiple]
    strong: tuple[Multiple, Multiple]
    edge_shrinks_above: Multiple
    weakest_above: Multiple
    loose_base_volume_multiple: Multiple

    @model_validator(mode="after")
    def _ordered(self) -> Volume:
        if not (
            self.marginal_below
            <= self.breakout_min_multiple
            <= self.edge_shrinks_above
            < self.weakest_above
        ):
            raise ValueError("volume bands must increase")
        return self


class RsWindows(Model):
    m3: PosInt
    m6: PosInt
    m12: PosInt


class RsWeights(Model):
    m3: Annotated[float, Field(ge=0, le=1)]
    m6: Annotated[float, Field(ge=0, le=1)]
    m12: Annotated[float, Field(ge=0, le=1)]


class Rs(Model):
    universe_index: str
    page_limit: Annotated[int, Field(ge=1, le=500)]
    window_weeks: RsWindows
    weights: RsWeights
    min_rs: Pct
    ideal_rs: Pct

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Rs:
        w = self.weights
        if not math.isclose(w.m3 + w.m6 + w.m12, 1.0, abs_tol=1e-9):
            raise ValueError("rs.weights must sum to 1.0")
        return self


class AtrFilter(Model):
    normal_max: Pct
    half_size_max: Pct

    @model_validator(mode="after")
    def _ordered(self) -> AtrFilter:
        if self.normal_max >= self.half_size_max:
            raise ValueError("normal_max must be below half_size_max")
        return self


class Risk(Model):
    entry_zone_max_above_close_pct: Pct
    atr_filter_daily_pct: AtrFilter


class SizeMultiplier(Model):
    high: Annotated[float, Field(gt=0, le=1)]
    medium: Annotated[float, Field(gt=0, le=1)]
    low: Annotated[float, Field(gt=0, le=1)]


class Conviction(Model):
    high_min: PosInt
    medium_min: PosInt
    size_multiplier: SizeMultiplier


class Flag(Model):
    min_weeks: PosInt
    max_weeks: PosInt
    max_range_pct: Pct
    max_below_high_pct: Pct
    breakout_vs_flag_avg: Multiple
    min_vol_multiple: Multiple


class SmaRefill(Model):
    low_within_sma_pct: Pct


class Add(Model):
    flag: Flag
    sma_refill: SmaRefill


class Exit(Model):
    signal1_min_volume_multiple: Multiple
    fallback_extension_pct: Pct
    signal2_min_volume_multiple: Multiple
    failure_lookback_weeks: PosInt
    doji_body_to_range: Annotated[float, Field(gt=0, le=1)]
    hammer_wick_to_body: Multiple
    hammer_close_location_max: Annotated[float, Field(gt=0, le=1)]
    signal3_window_weeks: PosInt
    signal3_min_wide_weeks: PosInt
    signal3_wide_range_multiple: Multiple
    signal3_range_avg_weeks: PosInt
    chop_net_flat_pct: Pct
    signal4_min_volume_multiple: Multiple
    absorption_close_location_min: Annotated[float, Field(gt=0, le=1)]
    absorption_min_volume_multiple: Multiple


class KillSwitch(Model):
    pledge_jump_pp: Pct
    gap_below_pivot_min_volume_multiple: Multiple
    alert_on_surveillance: bool


class Surveillance(Model):
    check: bool
    block_entry: bool
    frameworks: list[Literal["asm", "esm", "gsm"]]
    max_age_days: PosInt
    page_limit: Annotated[int, Field(ge=1, le=1000)]


class CorporateActions(Model):
    check: bool
    page_limit: Annotated[int, Field(ge=1, le=500)]
    warn_types: list[str]
    non_equity_series: list[str]


class Fundamentals(Model):
    fetch_financials: bool
    quarters_to_fetch: PosInt
    accel_quarters: PosInt
    other_income_flag_pct: Pct
    low_base_pct: Pct
    fetch_pledge_for_screen_names: bool


class Holdings(Model):
    fetch_shareholding: bool
    reclass_warn_pp: Pct


class WhyGateMode(StrEnum):
    REVIEW_REQUIRED = "review_required"
    AUTO_REDUCED = "auto_reduced"
    AUTO_FULL = "auto_full"


class WhyGate(Model):
    mode: WhyGateMode
    grace_hours: NonNegInt
    packet_max_chars: PosInt


class Distribution(Model):
    heavy_day_volume_multiple: Multiple
    heavy_down_days_flag: PosInt
    week_close_location_max: Annotated[float, Field(gt=0, le=1)]
    daily_baseline_sessions: PosInt


class Midweek(Model):
    quote_batch_size: Annotated[int, Field(ge=1, le=50)]
    near_trigger_pct: Pct
    state_max_age_days: PosInt
    new_high_signal: str
    new_high_min_market_cap_cr: Annotated[float, Field(ge=0)]
    new_high_limit: Annotated[int, Field(ge=1, le=500)]
    distribution: Distribution


class Confidence(Model):
    low: Annotated[int, Field(ge=1, le=5)]
    medium: Annotated[int, Field(ge=1, le=5)]
    high: Annotated[int, Field(ge=1, le=5)]


class Reviews(Model):
    confidence: Confidence
    outcome_min_weeks: PosInt
    missed_runner_gain_pct: Pct
    avoided_loser_drawdown_pct: Annotated[float, Field(ge=-100, le=0)]


class LateSweep(Model):
    enabled: bool
    cron_utc: str


class PromotionSweep(Model):
    enabled: bool
    cron_utc: str


class Schedule(Model):
    promotion_sweep: PromotionSweep
    timezone: str
    run_after_close: str
    cron_utc: str
    midweek_days: list[Literal["MON", "TUE", "WED", "THU", "FRI"]]
    late_sweep: LateSweep


class Output(Model):
    price_decimals: NonNegInt
    price_tolerance_pct: Pct
    run_max_minutes: PosInt
    alert_recent_weeks: PosInt


class Shadow(Model):
    live_writes: bool
    required_weeks: PosInt
    compare_tolerance_pct: Pct
    compare_fields: list[str]


class Config(Model):
    version: PosInt
    config_date: dt.date
    strategy: Literal["breakout"]
    tapetide: Tapetide
    http: Http
    quota: Quota
    ledger_mirror: LedgerMirror
    data: Data
    indicators: Indicators
    universe: Universe
    screens: Screens
    fundamental_grade: FundamentalGrade
    setup: Setup
    volume: Volume
    rs: Rs
    risk: Risk
    conviction: Conviction
    add: Add
    exit: Exit
    kill_switch: KillSwitch
    surveillance: Surveillance
    corporate_actions: CorporateActions
    fundamentals: Fundamentals
    holdings: Holdings
    why_gate: WhyGate
    midweek: Midweek
    reviews: Reviews
    schedule: Schedule
    output: Output
    shadow: Shadow
