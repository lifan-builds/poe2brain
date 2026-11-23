from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from poe2brain.config import DEFAULT_LEAGUE
from poe2brain.models.common import AffixSide, Poe2Model


class ModTierSpec(Poe2Model):
    mod_id: str
    family_id: str
    base_group_slug: str | None = None
    source_pool: str = "normal"
    affix_side: AffixSide = AffixSide.PREFIX
    affix_name: str | None = None
    internal_rank_asc: int
    display_tier: int
    required_level: int
    weight: int
    weight_confidence: str = "verified_community"
    boss_tag: str | None = None
    tags: list[str] = Field(default_factory=list)
    stat_text: str = ""
    trade_stat_ids: list[str] = Field(default_factory=list)

    @field_validator("internal_rank_asc", "display_tier")
    @classmethod
    def _positive_tier(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Tier ranks must be >= 1 (display_tier 1 = best tier)")
        return value


class CraftActionEval(Poe2Model):
    lever_id: str
    action_label: str
    step_cost_exalted: float
    hit_probability: float
    expected_attempts_to_hit: float | None = None
    expected_cost_to_hit_exalted: float | None = None
    eligible_pool_weight: int = 0
    target_weight: int = 0
    notes: str | None = None


class CraftPolicyStep(Poe2Model):
    step_index: int
    when_state: str
    recommended_action: str
    lever_ids: list[str] = Field(default_factory=list)
    step_cost_exalted: float = 0.0
    transition_probability: float = 1.0
    on_miss: str = "restart_base"


class TradeVsCraftComparison(Poe2Model):
    recommendation: Literal["buy", "craft", "tie", "craft_only"] = "craft_only"
    market_price_exalted: float | None = None
    expected_craft_exalted: float
    savings_exalted: float | None = None
    trade_url: str | None = None
    rationale: str = ""


class CraftPlanResult(Poe2Model):
    base_type: str
    item_level: int
    league: str = DEFAULT_LEAGUE
    target_descriptions: list[str] = Field(default_factory=list)
    base_cost_exalted: float = 0.0
    expected_cost_exalted: float
    expected_cost_divine: float | None = None
    p50_cost_exalted: float
    p80_cost_exalted: float
    p95_cost_exalted: float
    success_probability_per_base: float | None = None
    policy_steps: list[CraftPolicyStep] = Field(default_factory=list)
    candidate_actions: list[CraftActionEval] = Field(default_factory=list)
    trade_vs_craft: TradeVsCraftComparison | None = None
    warnings: list[str] = Field(default_factory=list)
