"""Q-01: plan calls before spending, and shed optional work in the configured order."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.models import Quota

# Shed items not named in `shed_order` are kept: open-position bars are never sheddable,
# and a config naming them fails validation (see config.models.SHEDDABLE).


@dataclass(frozen=True)
class ScanEstimate:
    q1_pages: int
    q2_pages: int
    q2_candidates: int
    q1_candidates: int
    open_positions: int
    deep_verify: int
    surveillance: int
    heatmap: int
    holidays: int
    pledge_names: int = 0

    def items(self) -> dict[str, int]:
        return {
            "core_screen": self.q1_pages + self.q1_candidates,
            "alert_screen": self.q2_pages + self.q2_candidates,
            "open_position_bars": self.open_positions,
            "surveillance": self.surveillance,
            "heatmap": self.heatmap,
            "holidays": self.holidays,
            "financials": self.deep_verify,
            "pledge": self.pledge_names,
        }


@dataclass
class Plan:
    items: dict[str, int]
    usable: int | None
    deep_verify: int
    shed: list[str] = field(default_factory=list)
    short: bool = False

    @property
    def total(self) -> int:
        return sum(self.items.values())

    def enabled(self, name: str) -> bool:
        return self.items.get(name, 0) > 0


def usable_quota(combined_remaining: int | None, cfg: Quota) -> int | None:
    if combined_remaining is None:
        return None
    return max(0, combined_remaining - cfg.reserve_calls - cfg.reserve_for_claude)


def plan_scan(estimate: ScanEstimate, usable: int | None, cfg: Quota) -> Plan:
    """Shed until the plan fits. `short` means even the minimum run does not fit."""
    plan = Plan(items=estimate.items(), usable=usable, deep_verify=estimate.deep_verify)
    if usable is None or plan.total <= usable:
        return plan
    for name in cfg.shed_order:
        if plan.total <= usable:
            break
        if name == "deep_verify":
            # Financials already went first; cutting names now drops their bars.
            while plan.deep_verify > 0 and plan.total > usable:
                plan.deep_verify -= 1
                plan.items["core_screen"] = max(0, plan.items["core_screen"] - 1)
            plan.shed.append(f"deep_verify->{plan.deep_verify}")
            continue
        if plan.items.get(name, 0) > 0:
            plan.items[name] = 0
            plan.shed.append(name)
    plan.short = plan.total > usable
    return plan


def plan_midweek(
    tracked_names: int, open_positions: int, new_high_calls: int, cfg: Quota, usable: int | None
) -> Plan:
    """One bar call per tracked name. New-high detection goes first, positions never."""
    limit = cfg.midweek_budget_calls if usable is None else min(cfg.midweek_budget_calls, usable)
    plan = Plan(
        items={
            "open_position_bars": open_positions,
            "tracked_bars": tracked_names,
            "new_high": new_high_calls,
        },
        usable=limit,
        deep_verify=0,
    )
    if plan.total > limit and plan.items["new_high"]:
        plan.items["new_high"] = 0
        plan.shed.append("new_high")
    if plan.total > limit:
        kept = max(0, limit - plan.items["open_position_bars"])
        if kept < plan.items["tracked_bars"]:
            plan.shed.append(f"tracked_bars->{kept}")
            plan.items["tracked_bars"] = kept
    plan.short = plan.total > limit
    return plan
