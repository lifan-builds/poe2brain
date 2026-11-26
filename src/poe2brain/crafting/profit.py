# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from poe2brain.config import DEFAULT_LEAGUE
from poe2brain.crafting.planner import (
    DEFAULT_CRAFTING_PRICES_EX,
    CraftingPlanner,
    PriceBook,
    _percentile,
)
from poe2brain.crafting.pool import (
    AffixSide,
    BaseItemEntry,
    CraftingMod,
    CraftingState,
    ModEntry,
    ModPoolEngine,
    TargetSpec,
    _norm_key,
)
from poe2brain.http import get_cached_json
from poe2brain.models.common import Poe2Model
from poe2brain.models.items import ItemRarity
from poe2brain.trade.economy import EconomyService
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle
from poe2brain.trade.item_parser import resolve_slot_or_category
from poe2brain.trade.stat_matcher import (
    MatchedStat,
    cap_unauth_stat_groups,
    resolve_natural_stat,
)
from poe2brain.trade.trade2_client import DetailedPriceCheckResult, Trade2Client

BudgetTier = Literal["low", "mid", "high"]

CraftStageKind = Literal[
    "magic_opener",
    "planner",
    "essence_regal",
    "regal_step",
    "pad_mods",
    "fracture_lock",
    "crystallisation_swap",
    "desecration_unveil",
    "targeted_slam",
    "chance_unique",
    "runeforge_recipe",
]

MIN_TRUSTED_EXCHANGE_VOLUME_DIV: float = 0.08

_TIER_ORDER_CHAINS: tuple[tuple[str, ...], ...] = (
    (
        "Orb of Transmutation",
        "Greater Orb of Transmutation",
        "Perfect Orb of Transmutation",
    ),
    (
        "Orb of Augmentation",
        "Greater Orb of Augmentation",
        "Perfect Orb of Augmentation",
    ),
    ("Regal Orb", "Greater Regal Orb", "Perfect Regal Orb"),
    ("Exalted Orb", "Greater Exalted Orb", "Perfect Exalted Orb"),
    ("Chaos Orb", "Greater Chaos Orb", "Perfect Chaos Orb"),
)


class MarketQuerySpec(Poe2Model):
    category: str | None = None
    base_type: str | None = None
    unique_name: str | None = None
    rarity: Literal["normal", "magic", "rare", "nonunique", "unique"] = "nonunique"
    min_ilvl: int | None = None
    min_es: int | None = None
    min_spirit: int | None = None
    min_rune_sockets: int | None = None
    stat_filters: list[tuple[str, float | None, float | None]] = Field(default_factory=list)
    match_any_explicit_domain: bool = True
    economy_query: str | None = None
    fallback_price_ex: float = 5.0
    fallback_liquidity_count: int = 10


class CraftStageSpec(Poe2Model):
    kind: CraftStageKind
    description: str
    lever: str | None = None
    secondary_lever: str | None = None
    omens: list[str] = Field(default_factory=list)
    target_mod: str | dict[str, Any] | None = None
    target_mods: list[str | dict[str, Any]] = Field(default_factory=list)
    pad_side: AffixSide | None = None
    pad_count: int = 1
    accept_any_roll: bool = False
    reset_with_omen_of_light: bool = True
    fixed_probability: float | None = None
    runeforge_source_base: str | None = None
    runeforge_variant_prob: float = 1.0


class CraftRecipeSpec(Poe2Model):
    recipe_id: str
    name: str
    budget_tier: BudgetTier
    archetype: str
    base_type: str
    ilvl: int = 81
    starting_rarity: ItemRarity = ItemRarity.NORMAL
    base_market: MarketQuerySpec
    exit_market: MarketQuerySpec
    stages: list[CraftStageSpec] = Field(default_factory=list)
    target_mods: list[str | dict[str, Any]] = Field(default_factory=list)
    live_trade_priority: int = 10
    query_live_base: bool = False
    notes: str = ""


class MarketQuoteSummary(Poe2Model):
    source: Literal["live_trade2_securable", "economy_service", "reference_benchmark"]
    selected_price_ex: float
    securable_min_ex: float | None = None
    securable_p10_ex: float | None = None
    securable_median_ex: float | None = None
    overall_min_ex: float | None = None
    total_active_listings: int = 0
    securable_count_fetched: int = 0
    trade_url: str | None = None
    fixer_warnings: list[str] = Field(default_factory=list)


class RecipeExecutionStep(Poe2Model):
    step_index: int
    stage_kind: str
    action: str
    instruction: str
    step_unit_cost_ex: float
    step_hit_probability: float
    expected_stage_cost_ex: float
    on_miss: str


class CraftProfitEvaluation(Poe2Model):
    rank: int = 0
    recipe_id: str
    name: str
    budget_tier: BudgetTier
    effective_budget_tier: BudgetTier
    archetype: str
    base_type: str
    ilvl: int
    league: str = DEFAULT_LEAGUE
    divine_rate_ex: float
    unit_base_cost_ex: float
    expected_bases_used: float
    base_cost_ex: float
    expected_craft_cost_ex: float
    expected_cost_exalted: float
    expected_cost_divine: float
    p50_ex: float
    p80_ex: float
    p95_ex: float
    p80_drawdown_ex: float
    p80_overrun_ex: float
    p80_profit_ex: float
    exit_price_ex: float
    exit_price_divine: float
    expected_profit_ex: float
    profit_divine: float
    expected_roi_pct: float
    success_probability_per_base: float
    breakeven_hit_rate: float
    risk_adjusted_score_ex: float
    competition_level: Literal["low", "medium", "high"] = "medium"
    niche_score: float = 0.0
    margin_tier: Literal["high_margin", "solid", "thin", "negative"] = "solid"
    base_market_quote: MarketQuoteSummary
    exit_market_quote: MarketQuoteSummary
    planner_naive_cost_ex: float | None = None
    execution_steps: list[RecipeExecutionStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CraftProfitScanReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    divine_rate_ex: float
    chaos_rate_ex: float
    price_source: str
    live_trade_queries_used: int = 0
    total_recipes_evaluated: int = 0
    profitable_count: int = 0
    top_overall: list[CraftProfitEvaluation] = Field(default_factory=list)
    top_by_tier: dict[str, list[CraftProfitEvaluation]] = Field(default_factory=dict)
    evaluations: list[CraftProfitEvaluation] = Field(default_factory=list)
    price_anomalies_sanitized: list[str] = Field(default_factory=list)


def classify_budget_tier(cost_divine: float) -> BudgetTier:
    if cost_divine < 5.0:
        return "low"
    if cost_divine < 40.0:
        return "mid"
    return "high"


def build_sanitized_crafting_prices(
    economy: EconomyService | None,
    league: str = DEFAULT_LEAGUE,
    *,
    raw_overrides: Mapping[str, float] | None = None,
) -> tuple[dict[str, float], float, float, str, list[str]]:
    """Build a liquidity-sanitized `{currency_key: exalted_price}` map.

    Filters out sub-0.08 Div single-unit dust/RMT price distortions on `poe.ninja` /
    `poe2scout` (e.g. 0.09 Ex Boss Omens or 448 Ex Lesser Essences) while never pricing
    missing/unavailable items at 0.0.
    """
    anomalies: list[str] = []
    prices: dict[str, float] = {
        "Orb of Chance": 18.0,
        "Omen of Chance": 1500.0,
        "Liquid Verisium": 35.0,
        **{k: float(v) for k, v in DEFAULT_CRAFTING_PRICES_EX.items() if v > 0},
    }
    div_ex = prices.get("Divine Orb", 450.0)
    chaos_ex = prices.get("Chaos Orb", 35.0)
    source_label = "fallback_reference_0.5.5"

    vol_by_norm: dict[str, float | None] = {}

    if economy is not None:
        rates = economy.get_reference_rates(league)
        if rates.divine_exalted > 0:
            div_ex = float(rates.divine_exalted)
        if rates.chaos_exalted > 0:
            chaos_ex = float(rates.chaos_exalted)
        bulk = economy.get_bulk_crafting_prices(league)
        source_label = f"live_economy:{league}"

        for slug, quote in bulk.items():
            if quote is None or quote.exalted <= 0:
                continue
            live_ex = float(quote.exalted)
            vol = quote.volume_primary
            norm_name = _norm_key(quote.name)
            vol_by_norm[norm_name] = vol

            ref_ex = DEFAULT_CRAFTING_PRICES_EX.get(quote.name)
            is_illiquid = vol is None or vol < MIN_TRUSTED_EXCHANGE_VOLUME_DIV

            # Guard 1: Illiquid dust lowballs (<25% of ref baseline or <5 Ex on Alloys/Perfects)
            if is_illiquid and ref_ex is not None and live_ex < 0.25 * ref_ex:
                anomalies.append(
                    f"Floored illiquid lowball {quote.name}: {live_ex:.2f} Ex "
                    f"(vol={vol} Div) -> {ref_ex:.2f} Ex"
                )
                live_ex = ref_ex
            elif (
                is_illiquid
                and ref_ex is None
                and live_ex < 5.0
                and ("Alloy" in quote.name or quote.name.startswith("Perfect Essence"))
            ):
                cat_floor = 25.0 if "Alloy" in quote.name else 15.0
                anomalies.append(
                    f"Floored illiquid category lowball {quote.name}: {live_ex:.2f} Ex "
                    f"(vol={vol} Div) -> {cat_floor:.2f} Ex"
                )
                live_ex = cat_floor
            # Guard 2: Illiquid RMT spikes on basic/lesser currencies (>8x reference on thin volume)
            elif is_illiquid and ref_ex is not None and live_ex > 8.0 * ref_ex and ref_ex <= 10.0:
                anomalies.append(
                    f"Capped illiquid spike {quote.name}: {live_ex:.2f} Ex "
                    f"(vol={vol} Div) -> {ref_ex:.2f} Ex"
                )
                live_ex = ref_ex

            prices[slug] = live_ex
            prices[quote.name] = live_ex

        prices["Exalted Orb"] = 1.0
        prices["Divine Orb"] = div_ex
        prices["Chaos Orb"] = chaos_ex

        # Guard 3: Enforce tier monotonicity when a lower orb is illiquidly misquoted
        for low_name, mid_name, high_name in _TIER_ORDER_CHAINS:
            p_low = prices.get(low_name)
            p_mid = prices.get(mid_name)
            p_high = prices.get(high_name)
            v_mid = vol_by_norm.get(_norm_key(mid_name))
            if p_low is not None and p_mid is not None and p_mid < p_low:
                if v_mid is None or v_mid < 0.5:
                    fixed = round(
                        max(p_low * 1.15, DEFAULT_CRAFTING_PRICES_EX.get(mid_name, p_low)), 4
                    )
                    anomalies.append(
                        f"Monotonicity fix {mid_name}: {p_mid:.2f} Ex < {low_name} "
                        f"({p_low:.2f} Ex, vol={v_mid}) -> {fixed:.2f} Ex"
                    )
                    prices[mid_name] = fixed
                    prices[_norm_key(mid_name)] = fixed
                    p_mid = fixed
            if p_mid is not None and p_high is not None and p_high < p_mid:
                v_high = vol_by_norm.get(_norm_key(high_name))
                if v_high is None or v_high < 0.5:
                    fixed_h = round(
                        max(p_mid * 1.5, DEFAULT_CRAFTING_PRICES_EX.get(high_name, p_mid)), 4
                    )
                    anomalies.append(
                        f"Monotonicity fix {high_name}: {p_high:.2f} Ex < {mid_name} "
                        f"({p_mid:.2f} Ex, vol={v_high}) -> {fixed_h:.2f} Ex"
                    )
                    prices[high_name] = fixed_h
                    prices[_norm_key(high_name)] = fixed_h

    if raw_overrides:
        for k, v in raw_overrides.items():
            if v is None or float(v) <= 0:
                raise ValueError(f"Invalid non-positive crafting price override for {k!r}: {v!r}")
            prices[k] = float(v)
        if "Divine Orb" in raw_overrides:
            div_ex = float(raw_overrides["Divine Orb"])
        if "Chaos Orb" in raw_overrides:
            chaos_ex = float(raw_overrides["Chaos Orb"])
        if economy is None:
            source_label = "custom_overrides"

    return prices, div_ex, chaos_ex, source_label, anomalies


def build_trade_query_from_market_spec(
    spec: MarketQuerySpec,
    *,
    securable_only: bool = True,
    bundle: EE2Bundle | None = None,
) -> dict[str, Any]:
    """Compile a `MarketQuerySpec` into an official `/api/trade2/search` JSON payload.

    Strips `#` placeholders before stat resolution and optionally expands `explicit`
    affix checks into `count min: 1` groups across `explicit` + `fractured` + `desecrated`
    domains so fractured/desecrated crafted items are included in exit market checks.
    """
    ee2 = bundle or get_default_bundle()
    status_opt = "securable" if securable_only else "available"
    trade_cat = resolve_slot_or_category(spec.category) if spec.category else None

    filters_block: dict[str, Any] = {}

    def _set_filter(group: str, key: str, val: Any) -> None:
        filters_block.setdefault(group, {"filters": {}})["filters"][key] = val

    if trade_cat:
        _set_filter("type_filters", "category", {"option": trade_cat})
    if spec.rarity:
        _set_filter("type_filters", "rarity", {"option": spec.rarity})
    if spec.min_ilvl is not None:
        _set_filter("type_filters", "ilvl", {"min": spec.min_ilvl})
    if spec.min_es is not None:
        _set_filter("equipment_filters", "es", {"min": spec.min_es})
    if spec.min_spirit is not None:
        _set_filter("equipment_filters", "spirit", {"min": spec.min_spirit})
    if spec.min_rune_sockets is not None:
        _set_filter("equipment_filters", "rune_sockets", {"min": spec.min_rune_sockets})

    _set_filter("misc_filters", "corrupted", {"option": "false"})
    _set_filter("misc_filters", "mirrored", {"option": "false"})

    and_filters: list[dict[str, Any]] = []
    extra_groups: list[dict[str, Any]] = []

    for raw_q, min_v, max_v in spec.stat_filters:
        clean_q = raw_q.replace("#%", "").replace("+#", "").replace("#", "").strip()
        cands = resolve_natural_stat(clean_q, category=trade_cat, limit=1, bundle=ee2)
        if not cands:
            raise ValueError(f"Could not resolve PoE2 trade2 stat filter: {raw_q!r}")
        best = cands[0]
        if best.equipment_filter_key:
            vr: dict[str, Any] = {}
            if min_v is not None:
                vr["min"] = min_v
            if max_v is not None:
                vr["max"] = max_v
            _set_filter("equipment_filters", best.equipment_filter_key, vr)
            continue

        trade_ids = list(best.trade_ids)
        if spec.match_any_explicit_domain and best.domain == "explicit":
            st_obj = ee2.stat_by_ref.get(best.ref)
            ids_map = (st_obj.get("trade") or {}).get("ids") if st_obj else None
            if isinstance(ids_map, Mapping):
                expanded: list[str] = []
                for dom_name in ("explicit", "fractured", "desecrated"):
                    for tid in ids_map.get(dom_name) or []:
                        if tid not in expanded:
                            expanded.append(tid)
                if expanded:
                    trade_ids = expanded

        ms = MatchedStat(
            ref=best.ref,
            domain=best.domain,
            trade_ids=trade_ids,
            ambiguous_local_global=len(trade_ids) > 1,
        )
        built = ms.to_trade_stat_group(min_override=min_v, max_override=max_v)
        if built["kind"] == "single":
            and_filters.append(built["filter"])
        else:
            extra_groups.append(built["group"])

    cap_unauth_stat_groups(and_filters, extra_groups)
    stats_list: list[dict[str, Any]] = [{"type": "and", "filters": and_filters}, *extra_groups]
    query: dict[str, Any] = {"status": {"option": status_opt}, "stats": stats_list}
    if spec.unique_name:
        query["name"] = spec.unique_name
    if spec.base_type:
        query["type"] = spec.base_type
    if filters_block:
        query["filters"] = filters_block

    return {"query": query, "sort": {"price": "asc"}}


def default_curated_recipes() -> list[CraftRecipeSpec]:
    """Curated Patch 0.5.5 recipes across Low (<5 Div), Mid (5-40 Div), and High (40+ Div)."""
    return [
        # 1. LOW BUDGET (<5 Div): ilvl 82 Sekhema Sandals 35% MS + Greater Essence ES
        CraftRecipeSpec(
            recipe_id="sekhema_boots_35ms_es",
            name="Sekhema Sandals (ilvl 82): 35% Movement Speed + High Energy Shield",
            budget_tier="low",
            archetype="Tiered Basic Floor Culling + Deterministic Greater Essence",
            base_type="Sekhema Sandals",
            ilvl=82,
            live_trade_priority=1,
            base_market=MarketQuerySpec(
                category="armour.boots",
                base_type="Sekhema Sandals",
                rarity="normal",
                min_ilvl=82,
                fallback_price_ex=25.0,
            ),
            exit_market=MarketQuerySpec(
                category="armour.boots",
                min_es=220,
                stat_filters=[("Movement Speed", 35, None)],
                fallback_price_ex=1800.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll 35% Movement Speed (T1, ilvl 82) via floor=70 orbs",
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Perfect Orb of Augmentation",
                    target_mod="MovementVelocity6",
                ),
                CraftStageSpec(
                    kind="essence_regal",
                    description="Guarantee (68-79)% increased Energy Shield (Magic -> Rare)",
                    lever="Greater Essence of Enhancement",
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Slam open prefix for flat/hybrid ES (floor=35)",
                    lever="Greater Exalted Orb",
                    omens=["Omen of Sinistral Exaltation"],
                    accept_any_roll=True,
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Fill high-tier resistance/attribute suffix (floor=35)",
                    lever="Greater Exalted Orb",
                    accept_any_roll=True,
                ),
            ],
            notes=(
                "Perfect Augmentation (floor=70) culls every Boots_int prefix except 35% MS "
                "(w=1000) and Hybrid ES/Stun (w=1000), yielding a 50.0% 35% MS hit rate!"
            ),
        ),
        # 2. LOW/MID BUDGET: 2-Socket ES Boots Deterministic 30% MS + 60% Augment Effect
        CraftRecipeSpec(
            recipe_id="boots_30ms_60augment_2s",
            name="2-Socket Sekhema Sandals: 30% MS + 60% Socketed Augment Effect",
            budget_tier="low",
            archetype="Dual Corrupted Essence + Sinistral/Dextral Crystallisation",
            base_type="Sekhema Sandals",
            ilvl=80,
            live_trade_priority=2,
            base_market=MarketQuerySpec(
                category="armour.boots",
                rarity="rare",
                min_ilvl=78,
                min_es=180,
                min_rune_sockets=2,
                fallback_price_ex=180.0,
            ),
            exit_market=MarketQuerySpec(
                category="armour.boots",
                min_es=160,
                min_rune_sockets=2,
                stat_filters=[
                    ("Movement Speed", 30, None),
                    ("effect of Socketed Augment Items", 55, None),
                ],
                fallback_price_ex=3600.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Open 2-Socket Sekhema Sandals to Magic with a disposable Prefix",
                    lever="Orb of Transmutation",
                    accept_any_roll=True,
                ),
                CraftStageSpec(
                    kind="essence_regal",
                    description="Guarantee +(31-35)% Lightning Resistance Suffix (Magic -> Rare)",
                    lever="Greater Essence of Grounding",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Ensure 2 disposable Prefixes while Resistance Suffix stays safe",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #1 & force 60% Socketed Augment Effect Suffix",
                    lever="Essence of Horror",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #2 & force 30% Movement Speed Prefix",
                    lever="Essence of Hysteria",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Slam open Prefix for Local % / Flat Energy Shield",
                    lever="Greater Exalted Orb",
                    omens=["Omen of Sinistral Exaltation"],
                    accept_any_roll=True,
                ),
            ],
            notes=(
                "100% deterministic 30% Movement Speed + 60% Socketed Augment Effect + "
                "+(31-35)% Lightning Res on 2-Rune-Socket ES Boots via Sinistral Crystallisation."
            ),
        ),
        # 3. LOW BUDGET (<5 Div): Deterministic +6 All Spell Skills + Cast Speed + Mana Staff
        CraftRecipeSpec(
            recipe_id="staff_plus6_spell_archmage",
            name="Paralysing Staff (ilvl 81): +6 All Spell Skills + (38-43)% Cast Speed + Mana",
            budget_tier="low",
            archetype="Cross-Side Sinistral Crystallisation + Perfect Essence + Celestial Alloy",
            base_type="Paralysing Staff",
            ilvl=81,
            live_trade_priority=3,
            base_market=MarketQuerySpec(
                category="weapon.staff",
                rarity="normal",
                min_ilvl=81,
                fallback_price_ex=15.0,
            ),
            exit_market=MarketQuerySpec(
                category="weapon.staff",
                stat_filters=[
                    ("Level of all Spell Skills", 5, None),
                    ("Cast Speed", 35, None),
                    ("maximum Mana", 135, None),
                    ("Spell Damage per 100 maximum Mana", 4, None),
                ],
                fallback_price_ex=4500.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Open Magic base with any Prefix so Greater Essence lands cleanly",
                    lever="Orb of Transmutation",
                    accept_any_roll=True,
                ),
                CraftStageSpec(
                    kind="essence_regal",
                    description="Guarantee (38-43)% increased Cast Speed Suffix (Magic -> Rare)",
                    lever="Greater Essence of Alacrity",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Ensure 2 disposable Prefixes exist for cross-side Sinistral swaps",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove 1 disposable Prefix & force +5 All Spell Skills Suffix",
                    lever="Perfect Essence of Sorcery",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description=(
                        "Remove remaining disposable Prefix & force +1 All Spell Skills + "
                        "(142-188) Mana Prefix"
                    ),
                    lever="Celestial Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Kurgal (4-5)% Spell Damage per 100 Max Mana Prefix (100%)",
                    lever="Preserved Jawbone",
                    omens=["Omen of the Blackblooded", "Omen of Sinistral Necromancy"],
                    target_mod={
                        "query": "Spell Damage per 100 maximum Mana",
                        "source_pool": "desecrated",
                    },
                ),
            ],
            notes=(
                "Cross-side Omen of Sinistral Crystallisation removes disposable Prefixes while "
                "Perfect Essence of Sorcery (+5 Spell) & Celestial Alloy (+1 Spell + Mana) land "
                "with 100% determinism and 0% risk to Cast Speed!"
            ),
        ),
        # 4. MID BUDGET (5-40 Div): Galvanic Wand +6 Lightning Spell + Cast Speed + Mana + Ele Dmg
        CraftRecipeSpec(
            recipe_id="wand_plus6_lightning_archmage",
            name=(
                "Galvanic Wand (ilvl 81): +5 Lightning & +1 All Spell (+6) + "
                "Cast Speed + Mana + Ele Dmg"
            ),
            budget_tier="mid",
            archetype="Tag-Culled Base + Cross-Side Sinistral Alloys + Amanamu Desecration",
            base_type="Galvanic Wand",
            ilvl=81,
            live_trade_priority=4,
            base_market=MarketQuerySpec(
                category="weapon.wand",
                base_type="Galvanic Wand",
                rarity="normal",
                min_ilvl=81,
                fallback_price_ex=10.0,
            ),
            exit_market=MarketQuerySpec(
                category="weapon.wand",
                stat_filters=[
                    ("Level of all Lightning Spell Skills", 5, None),
                    ("Level of all Spell Skills", 1, None),
                    ("Cast Speed", 25, None),
                    ("Elemental Damage", 70, None),
                ],
                fallback_price_ex=9800.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description=(
                        "Roll +5 Lightning Spell Skills on tag-culled Galvanic Wand "
                        "(abandon missed bases)"
                    ),
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="GlobalLightningSpellGemsLevelWeapon5",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Upgrade Magic -> Rare forcing a disposable Prefix",
                    lever="Regal Orb",
                    omens=["Omen of Sinistral Coronation"],
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Ensure 2 disposable Prefixes (protecting +5 Lightning Suffix)",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description=(
                        "Remove Prefix #1 & force (26-31)% Cast Speed + (7-11)% Extra Cold Suffix"
                    ),
                    lever="Transcendent Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description=(
                        "Remove Prefix #2 & force +1 All Spell Skills + (142-188) Mana Prefix"
                    ),
                    lever="Celestial Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Amanamu (74-89)% Elemental Damage Prefix (100% 3-of-3)",
                    lever="Preserved Jawbone",
                    omens=["Omen of the Liege", "Omen of Sinistral Necromancy"],
                    target_mod={"query": "Elemental Damage", "source_pool": "desecrated"},
                ),
            ],
            notes=(
                "Galvanic Wand's native no_fire/cold/chaos/phys_spell_mods tags shrink the "
                "Perfect Transmutation pool from 10,050 to 8,050 (+25% hit rate), followed by 3 "
                "100%-deterministic Alloy & Boss-Bone steps without needing a Fracturing Orb."
            ),
        ),
        # 5. MID BUDGET (5-40 Div): ilvl 75 Solar Amulet +3 Spell Skills + Global Defences
        CraftRecipeSpec(
            recipe_id="amulet_plus3_spell_defences",
            name=(
                "Solar Amulet (ilvl 75): +3 All Spell Skills + (20-30)% Global Defences + Body ES%"
            ),
            budget_tier="mid",
            archetype="ilvl-75 Pool Cap + Perfect Essence of Enhancement + Kurgal Desecration",
            base_type="Solar Amulet",
            ilvl=75,
            live_trade_priority=5,
            base_market=MarketQuerySpec(
                category="accessory.amulet",
                base_type="Solar Amulet",
                rarity="normal",
                min_ilvl=75,
                fallback_price_ex=12.0,
            ),
            exit_market=MarketQuerySpec(
                category="accessory.amulet",
                stat_filters=[
                    ("Level of all Spell Skills", 3, None),
                    ("Global Armour, Evasion and Energy Shield", 20, None),
                    ("Energy Shield from Equipped Body Armour", 35, None),
                ],
                fallback_price_ex=9500.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description=(
                        "Roll +3 All Spell Skills on ilvl 75 Solar Amulet "
                        "(blocks ilvl 80-82 dilution)"
                    ),
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="GlobalSpellGemsLevel3",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Upgrade Magic -> Rare adding 1 disposable Prefix",
                    lever="Regal Orb",
                    omens=["Omen of Sinistral Coronation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description=(
                        "Replace disposable Prefix with (20-30)% Global Defences "
                        "(0% risk to +3 Spell)"
                    ),
                    lever="Perfect Essence of Enhancement",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description=(
                        "Unveil Kurgal (35-50)% Energy Shield from Equipped Body Armour (100%)"
                    ),
                    lever="Preserved Collarbone",
                    omens=["Omen of the Blackblooded", "Omen of Sinistral Necromancy"],
                    target_mod={
                        "query": "Energy Shield from Equipped Body Armour",
                        "source_pool": "desecrated",
                    },
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Slam high-tier Suffix (Resists/Attr/Crit) via Greater Exalted Orb",
                    lever="Greater Exalted Orb",
                    accept_any_roll=True,
                ),
            ],
            notes=(
                "Capping Solar Amulet at ilvl 75 unlocks +3 Spell Skills (req_lvl=75) while "
                "blocking 8,850 weight of ilvl 80-82 mana/resist/attribute tiers!"
            ),
        ),
        # 6. MID BUDGET (5-40 Div): ilvl 75 Stellar Amulet +3 Projectile Skills + Global Defences
        CraftRecipeSpec(
            recipe_id="amulet_plus3_proj_defences",
            name=(
                "Stellar Amulet (ilvl 75): +3 Projectile Skills + "
                "(20-30)% Global Defences + Body ES%"
            ),
            budget_tier="mid",
            archetype="ilvl-75 Pool Cap + Perfect Essence of Enhancement + Kurgal Desecration",
            base_type="Stellar Amulet",
            ilvl=75,
            live_trade_priority=6,
            base_market=MarketQuerySpec(
                category="accessory.amulet",
                base_type="Stellar Amulet",
                rarity="normal",
                min_ilvl=75,
                fallback_price_ex=15.0,
            ),
            exit_market=MarketQuerySpec(
                category="accessory.amulet",
                stat_filters=[
                    ("Level of all Projectile Skills", 3, None),
                    ("Global Armour, Evasion and Energy Shield", 20, None),
                    ("Energy Shield from Equipped Body Armour", 35, None),
                ],
                fallback_price_ex=9000.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll +3 All Projectile Skills on ilvl 75 Stellar Amulet",
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="GlobalProjectileSkillGemLevel3",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Upgrade Magic -> Rare adding 1 disposable Prefix",
                    lever="Regal Orb",
                    omens=["Omen of Sinistral Coronation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Force (20-30)% Global Defences Prefix (Sinistral Crystallisation)",
                    lever="Perfect Essence of Enhancement",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Kurgal (35-50)% ES from Body Armour Prefix (100%)",
                    lever="Preserved Collarbone",
                    omens=["Omen of the Blackblooded", "Omen of Sinistral Necromancy"],
                    target_mod={
                        "query": "Energy Shield from Equipped Body Armour",
                        "source_pool": "desecrated",
                    },
                ),
            ],
            notes=(
                "Targets Bow/Crossbow/Spear ES/Hybrid builds needing attributes (+5-7 All Attr "
                "implicit) and +3 Projectile levels."
            ),
        ),
        # 7. MID BUDGET (5-40 Div): Feathered Raiment (ilvl 80) High-ES + Spirit + MoM Desecration
        CraftRecipeSpec(
            recipe_id="robe_high_es_spirit",
            name="Feathered Raiment (ilvl 80): +(45-61) Spirit + High Energy Shield + Kurgal MoM",
            budget_tier="mid",
            archetype="Spirit Prefix Opener + Greater Essence ES% + Kurgal MoM Desecration",
            base_type="Feathered Raiment",
            ilvl=80,
            live_trade_priority=7,
            base_market=MarketQuerySpec(
                category="armour.chest",
                base_type="Feathered Raiment",
                rarity="normal",
                min_ilvl=80,
                fallback_price_ex=20.0,
            ),
            exit_market=MarketQuerySpec(
                category="armour.chest",
                min_es=550,
                stat_filters=[("to Spirit", 45, None)],
                fallback_price_ex=6800.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll T1/T2 Spirit (+47-61 Spirit) via Perfect Transmute/Augment",
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="T2 to Spirit",
                ),
                CraftStageSpec(
                    kind="essence_regal",
                    description="Guarantee (68-79)% Local Energy Shield Prefix (Magic -> Rare)",
                    lever="Greater Essence of Enhancement",
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Slam 3rd Prefix for Flat/Hybrid ES (Perfect Exalt + Sinistral)",
                    lever="Perfect Exalted Orb",
                    omens=["Omen of Sinistral Exaltation"],
                    target_mod="T3 maximum Energy Shield",
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Kurgal (10-20)% Damage taken from Mana before Life Suffix",
                    lever="Preserved Rib",
                    omens=["Omen of the Blackblooded", "Omen of Dextral Necromancy"],
                    target_mod={
                        "query": "Damage is taken from Mana before Life",
                        "source_pool": "desecrated",
                    },
                ),
            ],
            notes=(
                "Stacks Feathered Raiment's (5-10)% MoM implicit with Kurgal's (10-20)% MoM "
                "Desecrated suffix, +(47-61) Spirit, and dual/triple Energy Shield prefixes."
            ),
        ),
        # 8. HIGH BUDGET (40-200+ Div): Fractured Gemini Bow (+Additional Arrow) + +2 Attack
        CraftRecipeSpec(
            recipe_id="fractured_gemini_bow_extra_arrow",
            name=(
                "Gemini Bow (ilvl 82): Fractured Additional Arrow + +2 Attack Skills + Quiver Bonus"
            ),
            budget_tier="high",
            archetype="4-Mod 25% Fracture Lock + Perfect Essences + Kurgal Quiver Desecration",
            base_type="Gemini Bow",
            ilvl=82,
            live_trade_priority=8,
            base_market=MarketQuerySpec(
                category="weapon.bow",
                base_type="Gemini Bow",
                rarity="normal",
                min_ilvl=82,
                fallback_price_ex=45.0,
            ),
            exit_market=MarketQuerySpec(
                category="weapon.bow",
                base_type="Gemini Bow",
                stat_filters=[
                    ("Surpassing chance to fire an additional Arrow", 150, None),
                    ("Level of all Attack Skills", 2, None),
                    ("bonuses gained from Equipped Quiver", 28, None),
                ],
                fallback_price_ex=42000.0,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll T1 Surpassing Additional Arrow (175-200%) on Gemini Bow",
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="AdditionalArrowChance4",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Upgrade Magic -> Rare",
                    lever="Regal Orb",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Slam cheap Exalted Orb(s) to reach exact 4-mod Fracture threshold",
                    pad_count=1,
                ),
                CraftStageSpec(
                    kind="fracture_lock",
                    description="Lock Additional Arrow Suffix with Fracturing Orb (1/4 = 25.0%)",
                    target_mod="AdditionalArrowChance4",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Ensure 2 disposable Prefixes exist for cross-side Sinistral swaps",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove disposable Prefix & force +2 Attack Skills Suffix",
                    lever="Perfect Essence of Battle",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove disposable Prefix & force Gain (15-20)% Extra Lightning",
                    lever="Perfect Essence of Electricity",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Kurgal (30-40)% bonuses from Equipped Quiver (100%)",
                    lever="Preserved Jawbone",
                    omens=["Omen of the Blackblooded", "Omen of Sinistral Necromancy"],
                    target_mod={
                        "query": "bonuses gained from Equipped Quiver",
                        "source_pool": "desecrated",
                    },
                ),
            ],
            notes=(
                "Always fracture at 4 mods (25.0%), never 5-6! Once Additional Arrow is fractured, "
                "Perfect Essence of Battle + Perfect Essence of Electricity + Kurgal Quiver Bonus "
                "all land deterministically."
            ),
        ),
        # 9. LOW BUDGET (<5 Div): Prismatic Ring +20% Quality Cap + Chaos Res + Attack Speed
        CraftRecipeSpec(
            recipe_id="ring_breach_quality_mana_chaos",
            name="Prismatic Ring (ilvl 80): +20% Max Quality + Chaos Res + Attack Speed + Spell ES",
            budget_tier="low",
            archetype="Zero-RNG Triple Essence/Alloy + Kurgal Desecration Ring",
            base_type="Prismatic Ring",
            ilvl=80,
            live_trade_priority=11,
            base_market=MarketQuerySpec(
                category="accessory.ring",
                base_type="Prismatic Ring",
                rarity="normal",
                min_ilvl=80,
                fallback_price_ex=5.0,
            ),
            exit_market=MarketQuerySpec(
                category="accessory.ring",
                stat_filters=[
                    ("Maximum Quality", 18, None),
                    ("Chaos Resistance", 15, None),
                ],
                fallback_price_ex=1350.0,
                fallback_liquidity_count=18,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Open Normal Prismatic Ring to Magic",
                    lever="Orb of Transmutation",
                    accept_any_roll=True,
                ),
                CraftStageSpec(
                    kind="essence_regal",
                    description="Guarantee +(16-19)% Chaos Resistance Suffix (Magic -> Rare)",
                    lever="Greater Essence of Ruin",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Pad Prefixes to 2 disposable Prefixes so Suffix stays 100% safe",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #1 & force (7-9)% Attack Speed Suffix (Swift Alloy)",
                    lever="Swift Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #2 & force +20% Max Quality (Essence of the Breach)",
                    lever="Essence of the Breach",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Kurgal (15-25)% Spell Damage on Full ES Prefix (100%)",
                    lever="Preserved Collarbone",
                    omens=["Omen of the Blackblooded", "Omen of Sinistral Necromancy"],
                    target_mod={
                        "query": "Spell Damage while on Full Energy Shield",
                        "source_pool": "desecrated",
                    },
                ),
            ],
            notes=(
                "100% deterministic 4-affix craft using cross-side Sinistral Crystallisation "
                "plus Kurgal 2-pool Prefix Desecration with 0% collateral risk."
            ),
        ),
        # 10. LOW BUDGET: Tasalian Focus +2 All Spell Skills + Transcendent Alloy Cast Speed
        CraftRecipeSpec(
            recipe_id="focus_plus2_spell_transcendent_es",
            name=(
                "Tasalian Focus (ilvl 80): +2 All Spell Skills + "
                "(26-31)% Cast Speed + Extra Cold + ES%"
            ),
            budget_tier="low",
            archetype="Basic-Floor Skill Level Opener + Sovereign & Transcendent Alloys",
            base_type="Tasalian Focus",
            ilvl=80,
            live_trade_priority=12,
            base_market=MarketQuerySpec(
                category="armour.focus",
                base_type="Tasalian Focus",
                rarity="normal",
                min_ilvl=80,
                fallback_price_ex=10.0,
            ),
            exit_market=MarketQuerySpec(
                category="armour.focus",
                min_es=240,
                stat_filters=[
                    ("Level of all Spell Skills", 2, None),
                    ("Cast Speed", 25, None),
                ],
                fallback_price_ex=1950.0,
                fallback_liquidity_count=22,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll +2 All Spell Skills (req_lvl=41 — use basic floor=1 orbs!)",
                    lever="Orb of Transmutation",
                    secondary_lever="Orb of Augmentation",
                    target_mod="GlobalSpellGemsLevel2",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Upgrade Magic -> Rare & pad 2 disposable Prefixes",
                    pad_side=AffixSide.PREFIX,
                    pad_count=2,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #1 & force (26-31)% Cast Speed + Extra Cold Suffix",
                    lever="Transcendent Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #2 & force (24-30)% Runic Ward Prefix",
                    lever="Sovereign Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
            ],
            notes=(
                "Anti-trap: +2 All Spell Skills on Focus is req_lvl=41, so Greater/Perfect "
                "Transmutation (floor 55/70) has 0.0% hit probability! Basic orbs hit cleanly."
            ),
        ),
        # 11. HIGH BUDGET: Fractured Galvanic Wand 6-Mod Mirror-Tier
        CraftRecipeSpec(
            recipe_id="fractured_wand_plus6_mirror_tier",
            name=(
                "Galvanic Wand (ilvl 81): Fractured +5 Lightning + +1 All Spell + "
                "Cast Speed + Augment Effect"
            ),
            budget_tier="high",
            archetype="4-Mod Fracture Lock + Triple Alloy (Transcendent/Sovereign/Celestial)",
            base_type="Galvanic Wand",
            ilvl=81,
            live_trade_priority=13,
            base_market=MarketQuerySpec(
                category="weapon.wand",
                base_type="Galvanic Wand",
                rarity="normal",
                min_ilvl=81,
                fallback_price_ex=10.0,
            ),
            exit_market=MarketQuerySpec(
                category="weapon.wand",
                stat_filters=[
                    ("Level of all Lightning Spell Skills", 5, None),
                    ("Level of all Spell Skills", 1, None),
                    ("effect of Socketed Augment Items", 20, None),
                ],
                fallback_price_ex=38000.0,
                fallback_liquidity_count=8,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description="Roll +5 Lightning Spell Skills on ilvl 81 Galvanic Wand",
                    lever="Perfect Orb of Transmutation",
                    secondary_lever="Greater Orb of Augmentation",
                    target_mod="GlobalLightningSpellGemsLevelWeapon5",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Upgrade Magic -> Rare",
                    lever="Regal Orb",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Slam Exalted Orb(s) to hit exact 1/4 Fracture threshold",
                    pad_count=1,
                ),
                CraftStageSpec(
                    kind="fracture_lock",
                    description="Lock +5 Lightning Spell Skills with Fracturing Orb (25.0%)",
                    target_mod="GlobalLightningSpellGemsLevelWeapon5",
                ),
                CraftStageSpec(
                    kind="pad_mods",
                    description="Ensure 3 disposable Prefixes for triple deterministic Alloys",
                    pad_side=AffixSide.PREFIX,
                    pad_count=3,
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #1 & force (26-31)% Cast Speed + Extra Cold Suffix",
                    lever="Transcendent Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #2 & force (20-30)% Socketed Augment Effect Suffix",
                    lever="Sovereign Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="crystallisation_swap",
                    description="Remove Prefix #3 & force +1 All Spell Skills + (142-188) Mana",
                    lever="Celestial Alloy",
                    omens=["Omen of Sinistral Crystallisation"],
                ),
                CraftStageSpec(
                    kind="desecration_unveil",
                    description="Unveil Amanamu (74-89)% Elemental Damage Prefix (100%)",
                    lever="Preserved Jawbone",
                    omens=["Omen of the Liege", "Omen of Sinistral Necromancy"],
                    target_mod={"query": "Elemental Damage", "source_pool": "desecrated"},
                ),
            ],
            notes=(
                "Fracturing +5 Lightning unlocks all 3 Suffixes (+5 Lt, Transcendent Cast Speed, "
                "Sovereign 30% Augment Effect) plus +1 All Spell & Amanamu Ele Dmg."
            ),
        ),
        # 12. LOW BUDGET: Verisium Anvil Runic Fork -> Runemastered (+2 Spell Projectiles Implicit)
        CraftRecipeSpec(
            recipe_id="verisium_runic_fork_proj_transform",
            name="Verisium Anvil: Runic Fork -> Runemastered Runic Fork (+2 Spell Projectiles)",
            budget_tier="low",
            archetype="Patch 0.5 Verisium Anvil 1-in-3 Base Implicit Transform",
            base_type="Runic Fork",
            ilvl=81,
            live_trade_priority=14,
            base_market=MarketQuerySpec(
                category="weapon.wand",
                base_type="Runic Fork",
                rarity="normal",
                min_ilvl=81,
                fallback_price_ex=15.0,
            ),
            exit_market=MarketQuerySpec(
                category="weapon.wand",
                base_type="Runemastered Runic Fork",
                fallback_price_ex=320.0,
                fallback_liquidity_count=12,
            ),
            stages=[
                CraftStageSpec(
                    kind="runeforge_recipe",
                    description="Transform Runic Fork + Liquid Verisium at Verisium Anvil (1/3)",
                    lever="Liquid Verisium",
                    runeforge_source_base="Runic Fork",
                    runeforge_variant_prob=1.0 / 3.0,
                )
            ],
            notes=(
                "Verisium Anvil rolls uniformly across 3 Runemastered Runic Fork implicits; "
                "1/3 hits (30-50)% chance for Spell Skills to fire 2 additional Projectiles."
            ),
        ),
        # 13. LOW/MID BUDGET: 0.5.5 Expedition Tablet Rolling (Verisium / Logbooks / Rarity)
        CraftRecipeSpec(
            recipe_id="expedition_tablet_verisium_roll",
            name="Expedition Tablet Rolling (Logbooks / Verisium / Rare Monsters)",
            budget_tier="low",
            archetype="0.5.5 Map Device Tablet Flipping & Juicing",
            base_type="Expedition Tablet",
            ilvl=82,
            live_trade_priority=6,
            base_market=MarketQuerySpec(
                category="map.tablet",
                base_type="Expedition Tablet",
                rarity="normal",
                fallback_price_ex=8.0,
                fallback_liquidity_count=200,
            ),
            exit_market=MarketQuerySpec(
                category="map.tablet",
                base_type="Expedition Tablet",
                rarity="magic",
                stat_filters=[
                    ("increased quantity of expedition logbooks", 15.0, None),
                ],
                fallback_price_ex=180.0,
                fallback_liquidity_count=60,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description=(
                        "Transmute + Augment clean Expedition Tablet for Logbooks / Rarity"
                    ),
                    lever="Orb of Transmutation",
                    secondary_lever="Orb of Augmentation",
                    target_mod="Logbooks",
                ),
            ],
            notes=(
                "High-velocity 0.5.5 hideout flip popularized on YouTube/Reddit: mass-roll "
                "Expedition Tablets for Logbook Quantity, Verisium, or Rare Monsters and slot "
                "directly into the Map Device (or sell bulk)."
            ),
        ),
        # 14. MID BUDGET: 5-Mod Energy Shield / Caster Sapphire Jewel Craft
        CraftRecipeSpec(
            recipe_id="sapphire_es_caster_jewel",
            name="ES + Spell / Crit Sapphire Jewel (Mid-Budget Jewel Flip)",
            budget_tier="mid",
            archetype="CI / Stormweaver / Blood Mage Jewel Slot",
            base_type="Sapphire",
            ilvl=82,
            live_trade_priority=7,
            base_market=MarketQuerySpec(
                category="jewel",
                base_type="Sapphire",
                rarity="normal",
                fallback_price_ex=10.0,
                fallback_liquidity_count=150,
            ),
            exit_market=MarketQuerySpec(
                category="jewel",
                base_type="Sapphire",
                rarity="rare",
                stat_filters=[
                    ("increased maximum energy shield", 12.0, None),
                    ("increased spell damage", 10.0, None),
                ],
                fallback_price_ex=450.0,
                fallback_liquidity_count=45,
            ),
            stages=[
                CraftStageSpec(
                    kind="magic_opener",
                    description=(
                        "Transmute + Augment Sapphire targeting +% Maximum Energy Shield "
                        "(Jewel mods require ilvl 1, so use base Transmute/Augment)"
                    ),
                    lever="Orb of Transmutation",
                    secondary_lever="Orb of Augmentation",
                    target_mod="Energy Shield",
                ),
                CraftStageSpec(
                    kind="regal_step",
                    description="Regal Orb to Rare Sapphire",
                    lever="Regal Orb",
                ),
                CraftStageSpec(
                    kind="targeted_slam",
                    description="Exalted Orb slam for Spell Damage / Crit / Mana Recovery",
                    lever="Exalted Orb",
                    target_mod="Spell Damage",
                ),
            ],
            notes=(
                "Sapphire jewels have a compact ES + Spell/Crit pool; hitting T1 ES + Spell Damage "
                "or Crit sells rapidly to CI Stormweaver, Chronomancer, and Blood Mage builds."
            ),
        ),
        # 15. HIGH BUDGET TRAP CHECK: Chancing Heavy Belt -> Headhunter (Proves Negative EV)
        CraftRecipeSpec(
            recipe_id="headhunter_chance_negative_ev_check",
            name="Chance Check: Heavy Belt + Orb of Chance -> Headhunter (Negative-EV Benchmark)",
            budget_tier="high",
            archetype="Chanceable T0 Unique Benchmark (Anti-Trap Verification)",
            base_type="Heavy Belt",
            ilvl=50,
            live_trade_priority=15,
            base_market=MarketQuerySpec(
                category="accessory.belt",
                base_type="Heavy Belt",
                rarity="normal",
                fallback_price_ex=2.0,
            ),
            exit_market=MarketQuerySpec(
                category="accessory.belt",
                unique_name="Headhunter",
                base_type="Heavy Belt",
                rarity="unique",
                economy_query="Headhunter",
                fallback_price_ex=3895.0,
                fallback_liquidity_count=1500,
            ),
            stages=[
                CraftStageSpec(
                    kind="chance_unique",
                    description="Apply Orb of Chance to Normal Heavy Belt (T0 ~1/2000 odds)",
                    lever="Orb of Chance",
                    fixed_probability=0.0005,
                )
            ],
            notes=(
                "Included to prove mathematically why chancing T0 Headhunter (~110 Div expected "
                "cost vs ~8.7 Div market buy) is a -92% ROI trap in Forbidden Rites."
            ),
        ),
    ]


class CraftProfitScanner:
    """Live PoE2 Patch 0.5.5 Crafting Profit, ROI, Tail-Risk & Liquidity Scanner."""

    def __init__(
        self,
        *,
        pool_engine: ModPoolEngine | None = None,
        planner: CraftingPlanner | None = None,
        economy: EconomyService | None = None,
        trade2: Trade2Client | None = None,
        bundle: EE2Bundle | None = None,
        trade_cache_ttl_seconds: float = 1800.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._owns_engine = pool_engine is None and planner is None
        self.engine = pool_engine or (planner.engine if planner is not None else ModPoolEngine())
        self.planner = planner or CraftingPlanner(self.engine)
        self._economy = economy
        self._owns_economy = False
        self._trade2 = trade2
        self._owns_trade2 = False
        self.bundle = bundle or get_default_bundle()
        self.trade_cache_ttl_seconds = trade_cache_ttl_seconds
        self._sleep_fn = sleep_fn
        self._live_trade_calls_made = 0

    def close(self) -> None:
        if self._owns_trade2 and self._trade2 is not None:
            self._trade2.close()
        if self._owns_economy and self._economy is not None:
            self._economy.close()
        if self._owns_engine:
            self.engine.close()

    def __enter__(self) -> CraftProfitScanner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get_economy(self) -> EconomyService:
        if self._economy is None:
            self._economy = EconomyService()
            self._owns_economy = True
        return self._economy

    def _get_trade2(self) -> Trade2Client:
        if self._trade2 is None:
            self._trade2 = Trade2Client(economy=self._economy)
            self._owns_trade2 = True
        return self._trade2

    def default_recipes(self) -> list[CraftRecipeSpec]:
        return default_curated_recipes()

    def _lookup_price_ex(
        self, price_book: PriceBook, item_name: str, raw_map: Mapping[str, float]
    ) -> float:
        direct = (
            raw_map.get(item_name)
            or raw_map.get(_norm_key(item_name))
            or raw_map.get(item_name.lower().replace(" ", "-"))
        )
        if direct is not None and direct > 0:
            return float(direct)
        try:
            lv = self.engine.resolve_lever(item_name)
            p = price_book.price_of(lv, use_category_fallback=True)
            if p is not None and p > 0:
                return float(p)
        except ValueError:
            pass
        raise ValueError(f"Missing positive market quote for crafting input {item_name!r}")

    def _resolve_market_quote(
        self,
        spec: MarketQuerySpec,
        *,
        league: str,
        divine_rate_ex: float,
        allow_live_trade2: bool,
        max_trade_queries: int,
        trade_delay_seconds: float,
        is_exit: bool,
    ) -> MarketQuoteSummary:
        # 1. EconomyService fast path (e.g. Unique Headhunter, Runes, Stackables)
        if spec.economy_query and self._economy is not None:
            uq_hits = []
            if spec.rarity == "unique":
                for ucat in ("accessory", "armour", "weapon", "flask", "jewel"):
                    for uq in self._economy.get_uniques_by_category(ucat, league):
                        if uq.name.lower() == spec.economy_query.lower():
                            uq_hits.append(uq)
                            break
                    if uq_hits:
                        break
            if uq_hits:
                u0 = uq_hits[0]
                return MarketQuoteSummary(
                    source="economy_service",
                    selected_price_ex=round(u0.exalted, 2),
                    securable_min_ex=round(u0.exalted, 2),
                    securable_p10_ex=round(u0.exalted, 2),
                    securable_median_ex=round(u0.exalted, 2),
                    overall_min_ex=round(u0.exalted, 2),
                    total_active_listings=int(u0.quantity or spec.fallback_liquidity_count),
                    securable_count_fetched=1,
                )
            q_obj = self._economy.get_price(spec.economy_query, league=league)
            if q_obj is not None and q_obj.exalted > 0:
                return MarketQuoteSummary(
                    source="economy_service",
                    selected_price_ex=round(q_obj.exalted, 2),
                    securable_min_ex=round(q_obj.exalted, 2),
                    securable_p10_ex=round(q_obj.exalted, 2),
                    securable_median_ex=round(q_obj.exalted, 2),
                    overall_min_ex=round(q_obj.exalted, 2),
                    total_active_listings=spec.fallback_liquidity_count,
                    securable_count_fetched=1,
                )

        # 2. Live Trade2Client Instant Buyout (`securable`) lookup with disk TTL cache & pacing
        if allow_live_trade2 and self._live_trade_calls_made < max_trade_queries:
            payload = build_trade_query_from_market_spec(
                spec, securable_only=True, bundle=self.bundle
            )
            payload_sig = hashlib.sha256(
                json.dumps({"league": league, "payload": payload}, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            cache_key = f"craft_profit_trade2_{league}_{payload_sig}"

            network_hit = False

            def _fetch_live() -> dict[str, Any]:
                nonlocal network_hit
                if self._live_trade_calls_made > 0 and trade_delay_seconds > 0:
                    self._sleep_fn(trade_delay_seconds)
                network_hit = True
                res: DetailedPriceCheckResult = self._get_trade2().price_check(
                    payload,
                    league=league,
                    securable_only=True,
                    max_fetch=20,
                )
                return res.model_dump(mode="json")

            # Bypass disk cache when a non-Trade2Client test double is injected
            if self._trade2 is not None and not isinstance(self._trade2, Trade2Client):
                raw_res = _fetch_live()
            else:
                raw_res = get_cached_json(
                    cache_key,
                    ttl_seconds=self.trade_cache_ttl_seconds,
                    fetcher=_fetch_live,
                    force_refresh=(self.trade_cache_ttl_seconds <= 0),
                    allow_stale_on_error=(self.trade_cache_ttl_seconds > 0),
                )

            if network_hit:
                self._live_trade_calls_made += 1

            dist = raw_res.get("distribution") or {}
            sec_cnt = int(dist.get("count_securable") or 0)
            tot_matches = int(raw_res.get("total_matches") or 0)
            sec_min = raw_res.get("min_exalted") or dist.get("securable_min_exalted")
            sec_p10 = raw_res.get("p10_exalted") or sec_min
            sec_med = raw_res.get("median_exalted") or dist.get("securable_median_exalted")

            if sec_cnt > 0 and sec_min is not None and float(sec_min) > 0:
                if is_exit:
                    chosen = float(sec_p10) if (sec_cnt >= 4 and sec_p10) else float(sec_min)
                else:
                    chosen = float(sec_p10) if (sec_cnt >= 3 and sec_p10) else float(sec_min)
                return MarketQuoteSummary(
                    source="live_trade2_securable",
                    selected_price_ex=round(chosen, 2),
                    securable_min_ex=round(float(sec_min), 2),
                    securable_p10_ex=round(float(sec_p10), 2) if sec_p10 is not None else None,
                    securable_median_ex=round(float(sec_med), 2) if sec_med is not None else None,
                    overall_min_ex=round(float(sec_min), 2),
                    total_active_listings=tot_matches,
                    securable_count_fetched=sec_cnt,
                    trade_url=raw_res.get("trade_url"),
                    fixer_warnings=list(raw_res.get("fixer_warnings") or []),
                )

            return MarketQuoteSummary(
                source="live_trade2_securable",
                selected_price_ex=round(spec.fallback_price_ex, 2),
                total_active_listings=0,
                securable_count_fetched=0,
                trade_url=raw_res.get("trade_url"),
                fixer_warnings=[
                    "0 active Instant Buyout (securable) listings matched on live trade2; "
                    "using conservative fallback benchmark and applying illiquidity penalty."
                ],
            )

        return MarketQuoteSummary(
            source="reference_benchmark",
            selected_price_ex=round(spec.fallback_price_ex, 2),
            securable_min_ex=round(spec.fallback_price_ex, 2),
            securable_p10_ex=round(spec.fallback_price_ex, 2),
            securable_median_ex=round(spec.fallback_price_ex, 2),
            overall_min_ex=round(spec.fallback_price_ex, 2),
            total_active_listings=spec.fallback_liquidity_count,
            securable_count_fetched=0,
        )

    def _simulate_recipe(
        self,
        recipe: CraftRecipeSpec,
        *,
        price_book: PriceBook,
        raw_prices_ex: Mapping[str, float],
        unit_base_cost_ex: float,
        league: str,
        n_sims: int,
        seed: int,
        include_planner_diagnostic: bool,
    ) -> tuple[
        float,  # expected_bases_used
        float,  # expected_craft_cost_ex
        float,  # p50_ex
        float,  # p80_ex
        float,  # p95_ex
        float,  # success_prob_per_base
        list[RecipeExecutionStep],
        float | None,  # planner_naive_cost_ex
        list[str],  # warnings
    ]:
        warnings: list[str] = []
        base_entry: BaseItemEntry = self.engine.resolve_base(recipe.base_type)

        # Caller-supplied shorthand recipe with `target_mods` and no explicit `stages`
        if not recipe.stages and recipe.target_mods:
            plan = self.planner.plan_craft(
                base=base_entry.name,
                ilvl=recipe.ilvl,
                target_mods=list(recipe.target_mods),
                currency_prices_ex=dict(raw_prices_ex),
                base_cost_ex=unit_base_cost_ex,
                league=league,
                n_sims=n_sims,
                seed=seed,
            )
            p_base = max(1e-6, float(plan.success_probability_per_base or 1.0))
            exp_bases = 1.0 / p_base
            tot_exp = float(plan.expected_cost_exalted)
            base_tot = min(tot_exp, unit_base_cost_ex * exp_bases)
            craft_only = max(0.0, tot_exp - base_tot)
            eff_bases = base_tot / unit_base_cost_ex if unit_base_cost_ex > 0 else exp_bases
            steps_out = [
                RecipeExecutionStep(
                    step_index=s.step_index,
                    stage_kind="planner",
                    action=s.recommended_action,
                    instruction=f"When {s.when_state}: apply {s.recommended_action}",
                    step_unit_cost_ex=s.step_cost_exalted,
                    step_hit_probability=s.transition_probability,
                    expected_stage_cost_ex=round(
                        s.step_cost_exalted / max(1e-6, s.transition_probability), 2
                    ),
                    on_miss=s.on_miss,
                )
                for s in plan.policy_steps
            ]
            return (
                eff_bases,
                craft_only,
                float(plan.p50_cost_exalted),
                float(plan.p80_cost_exalted),
                float(plan.p95_cost_exalted),
                p_base,
                steps_out,
                tot_exp,
                list(plan.warnings),
            )

        # Optional diagnostic check showing naive CraftingPlanner cost on the primary normal target
        planner_naive_ex: float | None = None
        if include_planner_diagnostic and recipe.stages:
            s0 = recipe.stages[0]
            if (
                s0.kind == "magic_opener"
                and isinstance(s0.target_mod, str)
                and not s0.accept_any_roll
            ):
                try:
                    naive_plan = self.planner.plan_craft(
                        base=base_entry.name,
                        ilvl=recipe.ilvl,
                        target_mods=[s0.target_mod],
                        currency_prices_ex=dict(raw_prices_ex),
                        base_cost_ex=unit_base_cost_ex,
                        league=league,
                        n_sims=400,
                        seed=seed,
                    )
                    planner_naive_ex = float(naive_plan.expected_cost_exalted)
                except Exception:
                    planner_naive_ex = None

        # Build analytical execution playbook + exact per-stage transition rules
        probe_state = CraftingState(
            base=base_entry, ilvl=recipe.ilvl, rarity=recipe.starting_rarity
        )
        protected_ids: set[str] = set()
        exec_steps: list[RecipeExecutionStep] = []

        # Pre-resolve stage metadata so Monte Carlo runs in microseconds per trial
        compiled_stages: list[dict[str, Any]] = []

        for idx, stg in enumerate(recipe.stages, start=1):
            if stg.kind == "magic_opener":
                assert stg.lever is not None
                lv1 = self.engine.resolve_lever(stg.lever)
                c1 = price_book.action_cost(lv1, ())
                if c1 is None:
                    raise ValueError(f"Unpriced lever {stg.lever!r}")

                if stg.accept_any_roll or stg.target_mod is None:
                    exec_steps.append(
                        RecipeExecutionStep(
                            step_index=idx,
                            stage_kind=stg.kind,
                            action=lv1.name,
                            instruction=stg.description,
                            step_unit_cost_ex=round(c1, 3),
                            step_hit_probability=1.0,
                            expected_stage_cost_ex=round(c1, 2),
                            on_miss="none (deterministic state advance)",
                        )
                    )
                    probe_state.rarity = ItemRarity.MAGIC
                    probe_state.mods.append(
                        CraftingMod(
                            mod_id=f"__opener_{idx}__",
                            mod_type=f"__opener_{idx}__",
                            family_id=f"__opener_{idx}__",
                            families=(f"__opener_{idx}__",),
                            affix_side=AffixSide.PREFIX,
                            display_tier=7,
                            required_level=1,
                            stat_text="Disposable Magic Prefix",
                        )
                    )
                    compiled_stages.append({"kind": "magic_any", "c1": c1})
                    continue

                spec: TargetSpec = self.engine.resolve_target(
                    base_entry.poe2db_slug, stg.target_mod, recipe.ilvl
                )
                t_ids = set(spec.matching_mod_ids)
                ev1 = self.engine.evaluate_action(probe_state, t_ids, lv1.name)
                if ev1.hit_probability <= 0:
                    raise ValueError(
                        f"Stage {idx} ({lv1.name}) has 0.0% hit probability for {spec.label} "
                        f"on {base_entry.name} ilvl {recipe.ilvl} ({ev1.notes})."
                    )
                p1 = ev1.hit_probability

                # Compute exact fraction of step-1 misses that leave the target's affix side open
                elig1 = self.engine.eligible_pool(
                    probe_state,
                    source_pool="normal",
                    target_rarity=ItemRarity.MAGIC,
                    min_mod_level=lv1.min_mod_level_floor or 1,
                )
                tot_w1 = sum(w for _, w in elig1)
                opp_side = (
                    AffixSide.PREFIX if spec.affix_side == AffixSide.SUFFIX else AffixSide.SUFFIX
                )
                opp_miss_w = sum(
                    w for m, w in elig1 if m.affix_side == opp_side and m.mod_id not in t_ids
                )
                p_open_side_after_miss = (opp_miss_w / tot_w1) if tot_w1 > 0 else 0.0

                use_aug = False
                c2 = 0.0
                p2_given_open = 0.0
                lv2_name = ""
                if stg.secondary_lever:
                    lv2 = self.engine.resolve_lever(stg.secondary_lever)
                    c2_raw = price_book.action_cost(lv2, ())
                    if c2_raw is not None:
                        tmp_m = CraftingState(
                            base=base_entry,
                            ilvl=recipe.ilvl,
                            rarity=ItemRarity.MAGIC,
                            mods=[
                                CraftingMod(
                                    "__opp__",
                                    "__opp__",
                                    "__opp__",
                                    ("__opp__",),
                                    opp_side,
                                    8,
                                    1,
                                    "opp",
                                )
                            ],
                        )
                        ev2 = self.engine.evaluate_action(tmp_m, t_ids, lv2.name)
                        if ev2.hit_probability > 0:
                            fresh_eff = (unit_base_cost_ex + c1) / p1
                            aug_eff = c2_raw / ev2.hit_probability
                            if aug_eff < fresh_eff * 1.15:
                                use_aug = True
                                c2 = c2_raw
                                p2_given_open = ev2.hit_probability
                                lv2_name = lv2.name

                p_comb = p1 + p_open_side_after_miss * p2_given_open if use_aug else p1
                mean_orb_per_try = c1 + (p_open_side_after_miss * c2 if use_aug else 0.0)
                act_lbl = f"{lv1.name}" + (f" -> (if open side) {lv2_name}" if use_aug else "")

                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=act_lbl,
                        instruction=stg.description,
                        step_unit_cost_ex=round(mean_orb_per_try, 3),
                        step_hit_probability=round(p_comb, 6),
                        expected_stage_cost_ex=round(
                            (unit_base_cost_ex + mean_orb_per_try) / p_comb, 2
                        ),
                        on_miss="abandon_magic_base_and_restart",
                    )
                )

                top_hit: ModEntry = max(
                    ev1.matching_tiers, key=lambda e: (e.weight, -e.display_tier)
                )
                probe_state.rarity = ItemRarity.MAGIC
                probe_state.mods.append(CraftingMod.from_entry(top_hit))
                protected_ids.add(top_hit.mod_id)

                compiled_stages.append(
                    {
                        "kind": "magic_target",
                        "c1": c1,
                        "p1": p1,
                        "use_aug": use_aug,
                        "c2": c2,
                        "p_open": p_open_side_after_miss,
                        "p2": p2_given_open,
                        "hit_mod": CraftingMod.from_entry(top_hit),
                        "opp_side": opp_side,
                    }
                )

            elif stg.kind == "essence_regal":
                assert stg.lever is not None
                lv = self.engine.resolve_lever(stg.lever)
                cost = price_book.action_cost(lv, ())
                if cost is None:
                    raise ValueError(f"Unpriced essence {stg.lever!r}")
                forced = self.engine._lookup_forced_mod_entry(base_entry, lv)
                if forced is None:
                    raise ValueError(f"{lv.name} does not apply to {base_entry.name}")
                ev = self.engine.evaluate_action(probe_state, {forced.mod_id}, lv.name)
                if ev.hit_probability <= 0:
                    raise ValueError(f"{lv.name} failed on {base_entry.name}: {ev.notes}")

                probe_state.rarity = ItemRarity.RARE
                f_mod = CraftingMod.from_entry(forced)
                probe_state.mods.append(f_mod)
                protected_ids.add(forced.mod_id)

                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=lv.name,
                        instruction=f"{stg.description} [{forced.stat_text}]",
                        step_unit_cost_ex=round(cost, 3),
                        step_hit_probability=1.0,
                        expected_stage_cost_ex=round(cost, 2),
                        on_miss="deterministic (100%)",
                    )
                )
                compiled_stages.append(
                    {"kind": "fixed", "cost": cost, "add_mod": f_mod, "rare": True}
                )

            elif stg.kind == "regal_step":
                assert stg.lever is not None
                lv = self.engine.resolve_lever(stg.lever)
                om_entries = [self.engine.resolve_lever(o) for o in stg.omens]
                cost = price_book.action_cost(lv, om_entries)
                if cost is None:
                    raise ValueError(f"Unpriced regal step {stg.lever!r}")
                side = next(
                    (o.omen_side_constraint for o in om_entries if o.omen_side_constraint),
                    AffixSide.PREFIX
                    if probe_state.prefix_count <= probe_state.suffix_count
                    else AffixSide.SUFFIX,
                )
                probe_state.rarity = ItemRarity.RARE
                d_mod = CraftingMod(
                    mod_id=f"__regal_filler_{idx}__",
                    mod_type=f"__regal_filler_{idx}__",
                    family_id=f"__regal_filler_{idx}__",
                    families=(f"__regal_filler_{idx}__",),
                    affix_side=side,
                    display_tier=8,
                    required_level=1,
                    stat_text=f"Disposable {side.value} modifier",
                )
                probe_state.mods.append(d_mod)
                act_lbl = self.planner._format_action_label(lv.name, [o.name for o in om_entries])
                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=act_lbl,
                        instruction=stg.description,
                        step_unit_cost_ex=round(cost, 3),
                        step_hit_probability=1.0,
                        expected_stage_cost_ex=round(cost, 2),
                        on_miss="deterministic rarity upgrade",
                    )
                )
                compiled_stages.append(
                    {"kind": "fixed", "cost": cost, "add_mod": d_mod, "rare": True}
                )

            elif stg.kind == "pad_mods":
                added_cost = 0.0
                added_mods: list[CraftingMod] = []
                action_parts: list[str] = []

                if probe_state.rarity == ItemRarity.MAGIC:
                    reg_lv = self.engine.resolve_lever("Regal Orb")
                    reg_om = (
                        [self.engine.resolve_lever("Omen of Sinistral Coronation")]
                        if stg.pad_side == AffixSide.PREFIX
                        else (
                            [self.engine.resolve_lever("Omen of Dextral Coronation")]
                            if stg.pad_side == AffixSide.SUFFIX
                            else []
                        )
                    )
                    rc = price_book.action_cost(reg_lv, reg_om) or 3.5
                    added_cost += rc
                    probe_state.rarity = ItemRarity.RARE
                    r_side = stg.pad_side or AffixSide.PREFIX
                    rm = CraftingMod(
                        f"__pad_reg_{idx}__",
                        f"__pad_reg_{idx}__",
                        f"__pad_reg_{idx}__",
                        (f"__pad_reg_{idx}__",),
                        r_side,
                        8,
                        1,
                        "Disposable Regal filler",
                    )
                    probe_state.mods.append(rm)
                    added_mods.append(rm)
                    action_parts.append(
                        self.planner._format_action_label(reg_lv.name, [o.name for o in reg_om])
                    )

                ex_lv = self.engine.resolve_lever("Exalted Orb")
                loop_bound = max(stg.pad_count, 4) if stg.pad_side is None else stg.pad_count
                for k_i in range(loop_bound):
                    if stg.pad_side == AffixSide.PREFIX:
                        cur_disposable = sum(
                            1
                            for m in probe_state.mods
                            if m.affix_side == AffixSide.PREFIX
                            and not m.fractured
                            and m.mod_id not in protected_ids
                        )
                        if cur_disposable >= stg.pad_count or not probe_state.has_open_prefix():
                            break
                        om_e = (
                            [self.engine.resolve_lever("Omen of Sinistral Exaltation")]
                            if probe_state.has_open_suffix()
                            else []
                        )
                        p_side = AffixSide.PREFIX
                    elif stg.pad_side == AffixSide.SUFFIX:
                        cur_disposable = sum(
                            1
                            for m in probe_state.mods
                            if m.affix_side == AffixSide.SUFFIX
                            and not m.fractured
                            and m.mod_id not in protected_ids
                        )
                        if cur_disposable >= stg.pad_count or not probe_state.has_open_suffix():
                            break
                        om_e = (
                            [self.engine.resolve_lever("Omen of Dextral Exaltation")]
                            if probe_state.has_open_prefix()
                            else []
                        )
                        p_side = AffixSide.SUFFIX
                    else:
                        if len(probe_state.mods) >= 4:
                            break
                        om_e = []
                        p_side = (
                            AffixSide.PREFIX if probe_state.has_open_prefix() else AffixSide.SUFFIX
                        )

                    sc = price_book.action_cost(ex_lv, om_e) or 1.0
                    added_cost += sc
                    pm = CraftingMod(
                        f"__pad_{idx}_{k_i}__",
                        f"__pad_{idx}_{k_i}__",
                        f"__pad_{idx}_{k_i}__",
                        (f"__pad_{idx}_{k_i}__",),
                        p_side,
                        8,
                        1,
                        f"Disposable {p_side.value} filler",
                    )
                    probe_state.mods.append(pm)
                    added_mods.append(pm)
                    action_parts.append(
                        self.planner._format_action_label(ex_lv.name, [o.name for o in om_e])
                    )

                if added_cost > 0 or added_mods:
                    exec_steps.append(
                        RecipeExecutionStep(
                            step_index=idx,
                            stage_kind=stg.kind,
                            action=" + ".join(dict.fromkeys(action_parts)) or "Exalted Orb",
                            instruction=stg.description,
                            step_unit_cost_ex=round(added_cost, 3),
                            step_hit_probability=1.0,
                            expected_stage_cost_ex=round(added_cost, 2),
                            on_miss="deterministic slot setup",
                        )
                    )
                    compiled_stages.append({"kind": "pad", "cost": added_cost, "mods": added_mods})

            elif stg.kind == "fracture_lock":
                assert stg.target_mod is not None
                frac_lv = self.engine.resolve_lever("Fracturing Orb")
                f_cost = price_book.action_cost(frac_lv, ())
                if f_cost is None:
                    raise ValueError("Unpriced Fracturing Orb")
                f_spec = self.engine.resolve_target(
                    base_entry.poe2db_slug, stg.target_mod, recipe.ilvl
                )
                f_ids = set(f_spec.matching_mod_ids)
                ev_f = self.engine.evaluate_action(probe_state, f_ids, "Fracturing Orb")
                if ev_f.hit_probability <= 0:
                    raise ValueError(f"Fracturing Orb invalid at stage {idx}: {ev_f.notes}")

                for m_i, mod_item in enumerate(probe_state.mods):
                    if mod_item.mod_id in f_ids:
                        probe_state.mods[m_i] = CraftingMod(
                            mod_id=mod_item.mod_id,
                            mod_type=mod_item.mod_type,
                            family_id=mod_item.family_id,
                            families=mod_item.families,
                            affix_side=mod_item.affix_side,
                            display_tier=mod_item.display_tier,
                            required_level=mod_item.required_level,
                            stat_text=mod_item.stat_text,
                            adds_tags=mod_item.adds_tags,
                            mod_tags=mod_item.mod_tags,
                            fractured=True,
                        )
                        break

                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action="Fracturing Orb",
                        instruction=f"{stg.description} ({ev_f.notes})",
                        step_unit_cost_ex=round(f_cost, 3),
                        step_hit_probability=round(ev_f.hit_probability, 6),
                        expected_stage_cost_ex=round(f_cost / ev_f.hit_probability, 2),
                        on_miss="abandon_missed_fracture_and_restart_from_step_1",
                    )
                )
                compiled_stages.append(
                    {"kind": "fracture", "cost": f_cost, "p": ev_f.hit_probability}
                )

            elif stg.kind == "crystallisation_swap":
                assert stg.lever is not None
                lv = self.engine.resolve_lever(stg.lever)
                om_entries = [self.engine.resolve_lever(o) for o in stg.omens]
                cost = price_book.action_cost(lv, om_entries)
                if cost is None:
                    raise ValueError(f"Unpriced swap lever {stg.lever!r}")
                forced = self.engine._lookup_forced_mod_entry(base_entry, lv)
                if forced is None:
                    raise ValueError(f"{lv.name} not applicable to {base_entry.name}")

                ev_c = self.engine.evaluate_action(
                    probe_state,
                    {forced.mod_id},
                    lv.name,
                    stg.omens,
                    protected_mod_ids=protected_ids,
                )
                if ev_c.hit_probability <= 0:
                    raise ValueError(
                        f"Crystallisation swap {lv.name} failed at stage {idx}: {ev_c.notes}"
                    )
                if ev_c.collateral_remove_probability > 0:
                    warnings.append(
                        f"Stage {idx} ({lv.name}) carries "
                        f"{ev_c.collateral_remove_probability:.1%} collateral removal risk."
                    )

                side_con = next(
                    (o.omen_side_constraint for o in om_entries if o.omen_side_constraint), None
                )
                rem_candidates = [
                    i_m
                    for i_m, m in enumerate(probe_state.mods)
                    if not m.fractured
                    and m.mod_id not in protected_ids
                    and (side_con is None or m.affix_side == side_con)
                ]
                if rem_candidates:
                    probe_state.mods.pop(rem_candidates[0])
                f_mod = CraftingMod.from_entry(forced)
                probe_state.mods.append(f_mod)
                protected_ids.add(forced.mod_id)

                act_lbl = self.planner._format_action_label(lv.name, [o.name for o in om_entries])
                forced_txt = forced.stat_text.replace("\n", " + ")
                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=act_lbl,
                        instruction=f"{stg.description} -> {forced_txt}",
                        step_unit_cost_ex=round(cost, 3),
                        step_hit_probability=round(ev_c.hit_probability, 6),
                        expected_stage_cost_ex=round(cost / ev_c.hit_probability, 2),
                        on_miss="deterministic (100%)"
                        if ev_c.hit_probability >= 0.9999
                        else "restart_if_protected_removed",
                    )
                )
                compiled_stages.append(
                    {
                        "kind": "swap",
                        "cost": cost,
                        "p": ev_c.hit_probability,
                        "collat": ev_c.collateral_remove_probability,
                    }
                )

            elif stg.kind == "desecration_unveil":
                assert stg.lever is not None and stg.target_mod is not None
                lv = self.engine.resolve_lever(stg.lever)
                om_entries = [self.engine.resolve_lever(o) for o in stg.omens]
                cost = price_book.action_cost(lv, om_entries)
                if cost is None:
                    raise ValueError(f"Unpriced bone/omen at stage {idx}")
                d_spec = self.engine.resolve_target(
                    base_entry.poe2db_slug, stg.target_mod, recipe.ilvl
                )
                ev_d = self.engine.evaluate_action(
                    probe_state,
                    set(d_spec.matching_mod_ids),
                    lv.name,
                    stg.omens,
                    protected_mod_ids=protected_ids,
                )
                if ev_d.hit_probability <= 0:
                    raise ValueError(f"Desecration stage {idx} hit_prob=0: {ev_d.notes}")

                reset_cost = 0.0
                if ev_d.hit_probability < 0.9999 and stg.reset_with_omen_of_light:
                    annul_lv = self.engine.resolve_lever("Orb of Annulment")
                    light_om = self.engine.resolve_lever("Omen of Light")
                    rc = price_book.action_cost(annul_lv, (light_om,))
                    if rc is None:
                        raise ValueError("Unpriced Omen of Light + Orb of Annulment")
                    reset_cost = rc

                p_d = ev_d.hit_probability
                exp_d_cost = cost + ((1.0 - p_d) / p_d) * (cost + reset_cost)
                top_d = ev_d.matching_tiers[0]
                probe_state.mods.append(CraftingMod.from_entry(top_d, is_desecrated=True))
                protected_ids.add(top_d.mod_id)

                act_lbl = self.planner._format_action_label(lv.name, [o.name for o in om_entries])
                top_txt = top_d.stat_text.replace("\n", " / ")
                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=act_lbl,
                        instruction=f"{stg.description} [{top_txt}]",
                        step_unit_cost_ex=round(cost, 3),
                        step_hit_probability=round(p_d, 6),
                        expected_stage_cost_ex=round(exp_d_cost, 2),
                        on_miss="deterministic (100% 3-choice unveil)"
                        if p_d >= 0.9999
                        else "Orb of Annulment + Omen of Light (100% safe reset) & repeat",
                    )
                )
                compiled_stages.append(
                    {"kind": "desecrate", "cost": cost, "p": p_d, "reset_cost": reset_cost}
                )

            elif stg.kind == "targeted_slam":
                assert stg.lever is not None
                lv = self.engine.resolve_lever(stg.lever)
                om_entries = [self.engine.resolve_lever(o) for o in stg.omens]
                cost = price_book.action_cost(lv, om_entries)
                if cost is None:
                    raise ValueError(f"Unpriced slam lever {stg.lever!r}")
                act_lbl = self.planner._format_action_label(lv.name, [o.name for o in om_entries])

                if stg.accept_any_roll or stg.target_mod is None:
                    exec_steps.append(
                        RecipeExecutionStep(
                            step_index=idx,
                            stage_kind=stg.kind,
                            action=act_lbl,
                            instruction=stg.description,
                            step_unit_cost_ex=round(cost, 3),
                            step_hit_probability=1.0,
                            expected_stage_cost_ex=round(cost, 2),
                            on_miss="single high-floor value slam",
                        )
                    )
                    compiled_stages.append({"kind": "fixed", "cost": cost})
                else:
                    s_spec = self.engine.resolve_target(
                        base_entry.poe2db_slug, stg.target_mod, recipe.ilvl
                    )
                    ev_s = self.engine.evaluate_action(
                        probe_state,
                        set(s_spec.matching_mod_ids),
                        lv.name,
                        stg.omens,
                        protected_mod_ids=protected_ids,
                    )
                    p_s = max(0.05, ev_s.hit_probability) if ev_s.hit_probability > 0 else 1.0
                    exec_steps.append(
                        RecipeExecutionStep(
                            step_index=idx,
                            stage_kind=stg.kind,
                            action=act_lbl,
                            instruction=f"{stg.description} (T3+ hit={ev_s.hit_probability:.1%})",
                            step_unit_cost_ex=round(cost, 3),
                            step_hit_probability=round(p_s, 6),
                            expected_stage_cost_ex=round(cost, 2),
                            on_miss="keep_rolled_tier_or_whittle",
                        )
                    )
                    if ev_s.matching_tiers:
                        probe_state.mods.append(CraftingMod.from_entry(ev_s.matching_tiers[0]))
                    compiled_stages.append({"kind": "fixed", "cost": cost})

            elif stg.kind == "chance_unique":
                assert stg.lever is not None
                c_cost = self._lookup_price_ex(price_book, stg.lever, raw_prices_ex)
                for om_n in stg.omens:
                    c_cost += self._lookup_price_ex(price_book, om_n, raw_prices_ex)
                p_ch = float(stg.fixed_probability or 0.0005)
                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=self.planner._format_action_label(stg.lever, stg.omens),
                        instruction=stg.description,
                        step_unit_cost_ex=round(c_cost, 3),
                        step_hit_probability=round(p_ch, 6),
                        expected_stage_cost_ex=round((unit_base_cost_ex + c_cost) / p_ch, 2),
                        on_miss="base_destroyed_buy_fresh_normal_base",
                    )
                )
                compiled_stages.append({"kind": "chance", "cost": c_cost, "p": p_ch})

            elif stg.kind == "runeforge_recipe":
                assert stg.lever is not None
                rf_cost = self._lookup_price_ex(price_book, stg.lever, raw_prices_ex)
                p_rf = float(stg.runeforge_variant_prob)
                exec_steps.append(
                    RecipeExecutionStep(
                        step_index=idx,
                        stage_kind=stg.kind,
                        action=f"Verisium Anvil + {stg.lever}",
                        instruction=stg.description,
                        step_unit_cost_ex=round(rf_cost, 3),
                        step_hit_probability=round(p_rf, 6),
                        expected_stage_cost_ex=round((unit_base_cost_ex + rf_cost) / p_rf, 2),
                        on_miss="sell_other_runemastered_variant_or_re_anvil_fresh_base",
                    )
                )
                compiled_stages.append({"kind": "chance", "cost": rf_cost, "p": p_rf})

        for re_idx, step_obj in enumerate(exec_steps, start=1):
            step_obj.step_index = re_idx

        # Exact Monte Carlo over `compiled_stages` (geometric draws for low-p steps)
        rng = random.Random(seed)
        tot_costs: list[float] = []
        craft_spends: list[float] = []
        bases_counts: list[float] = []

        for _ in range(n_sims):
            bases_used = 0.0
            orb_spend = 0.0
            while True:
                bases_used += 1.0
                restarted = False
                for cs in compiled_stages:
                    ck = cs["kind"]
                    if ck == "magic_any":
                        orb_spend += cs["c1"]
                    elif ck == "magic_target":
                        # Sample geometric misses directly when p_comb < 0.08
                        p1 = cs["p1"]
                        use_aug = cs["use_aug"]
                        p_open = cs["p_open"]
                        p2 = cs["p2"]
                        p_comb = p1 + (p_open * p2 if use_aug else 0.0)
                        if p_comb < 0.08:
                            u = max(1e-12, rng.random())
                            extra_misses = int(
                                math.floor(math.log(u) / math.log(max(1e-12, 1.0 - p_comb)))
                            )
                            bases_used += extra_misses
                            mean_miss_orb = cs["c1"] + (
                                (p_open * (1.0 - p2) / max(1e-9, 1.0 - p_comb)) * cs["c2"]
                                if use_aug
                                else 0.0
                            )
                            orb_spend += extra_misses * mean_miss_orb
                            # Winning attempt orb cost
                            hit_via_aug_prob = (p_open * p2) / p_comb if use_aug else 0.0
                            orb_spend += cs["c1"] + (
                                cs["c2"] if rng.random() < hit_via_aug_prob else 0.0
                            )
                        else:
                            orb_spend += cs["c1"]
                            r = rng.random()
                            if r >= p1:
                                if use_aug and r < p1 + p_open:
                                    orb_spend += cs["c2"]
                                    if rng.random() >= p2:
                                        restarted = True
                                        break
                                else:
                                    restarted = True
                                    break
                    elif ck in ("fixed", "pad"):
                        orb_spend += cs["cost"]
                    elif ck == "fracture":
                        orb_spend += cs["cost"]
                        if rng.random() >= cs["p"]:
                            restarted = True
                            break
                    elif ck == "swap":
                        orb_spend += cs["cost"]
                        if cs["collat"] > 0 and rng.random() < cs["collat"]:
                            restarted = True
                            break
                    elif ck == "desecrate":
                        while True:
                            orb_spend += cs["cost"]
                            if rng.random() < cs["p"]:
                                break
                            orb_spend += cs["reset_cost"]
                    elif ck == "chance":
                        p_c = cs["p"]
                        u = max(1e-12, rng.random())
                        extra_m = int(math.floor(math.log(u) / math.log(max(1e-12, 1.0 - p_c))))
                        bases_used += extra_m
                        orb_spend += (extra_m + 1) * cs["cost"]
                if not restarted:
                    break

            tot_c = bases_used * unit_base_cost_ex + orb_spend
            tot_costs.append(tot_c)
            craft_spends.append(orb_spend)
            bases_counts.append(bases_used)

        tot_costs.sort()
        exp_bases_mean = sum(bases_counts) / len(bases_counts)
        exp_craft_mean = sum(craft_spends) / len(craft_spends)
        p50 = _percentile(tot_costs, 0.50)
        p80 = _percentile(tot_costs, 0.80)
        p95 = _percentile(tot_costs, 0.95)
        p_per_base = min(1.0, max(1e-7, 1.0 / exp_bases_mean))

        return (
            exp_bases_mean,
            exp_craft_mean,
            p50,
            p80,
            p95,
            p_per_base,
            exec_steps,
            planner_naive_ex,
            warnings,
        )

    def evaluate_recipe(
        self,
        recipe: CraftRecipeSpec,
        *,
        league: str = DEFAULT_LEAGUE,
        currency_prices_ex: Mapping[str, float] | None = None,
        divine_rate_ex: float | None = None,
        live_trade: bool = False,
        max_trade_queries: int = 8,
        trade_delay_seconds: float = 1.5,
        n_sims: int = 3000,
        seed: int = 42,
        include_planner_diagnostic: bool = False,
    ) -> CraftProfitEvaluation:
        if currency_prices_ex is None:
            prices_map, div_ex, _, _, _ = build_sanitized_crafting_prices(
                self._get_economy() if live_trade or self._economy is not None else None,
                league=league,
            )
        else:
            prices_map = dict(currency_prices_ex)
            div_ex = (
                float(divine_rate_ex)
                if divine_rate_ex and divine_rate_ex > 0
                else float(prices_map.get("Divine Orb") or 450.0)
            )

        price_book = PriceBook.build(prices_map, use_fallback_prices=True)

        base_quote = self._resolve_market_quote(
            recipe.base_market,
            league=league,
            divine_rate_ex=div_ex,
            allow_live_trade2=live_trade and recipe.query_live_base,
            max_trade_queries=max_trade_queries,
            trade_delay_seconds=trade_delay_seconds,
            is_exit=False,
        )
        exit_quote = self._resolve_market_quote(
            recipe.exit_market,
            league=league,
            divine_rate_ex=div_ex,
            allow_live_trade2=live_trade,
            max_trade_queries=max_trade_queries,
            trade_delay_seconds=trade_delay_seconds,
            is_exit=True,
        )

        unit_base_ex = base_quote.selected_price_ex
        (
            exp_bases,
            exp_craft_ex,
            p50_ex,
            p80_ex,
            p95_ex,
            p_base,
            exec_steps,
            naive_ex,
            sim_warnings,
        ) = self._simulate_recipe(
            recipe,
            price_book=price_book,
            raw_prices_ex=prices_map,
            unit_base_cost_ex=unit_base_ex,
            league=league,
            n_sims=n_sims,
            seed=seed,
            include_planner_diagnostic=include_planner_diagnostic,
        )

        base_cost_total_ex = round(unit_base_ex * exp_bases, 2)
        craft_cost_ex = round(exp_craft_ex, 2)
        total_expected_ex = round(base_cost_total_ex + craft_cost_ex, 2)
        total_expected_div = round(total_expected_ex / div_ex, 3)

        exit_ex = round(exit_quote.selected_price_ex, 2)
        exit_div = round(exit_ex / div_ex, 3)

        profit_ex = round(exit_ex - base_cost_total_ex - craft_cost_ex, 2)
        profit_div = round(profit_ex / div_ex, 3)
        roi_pct = (
            round((profit_ex / total_expected_ex) * 100.0, 2) if total_expected_ex > 0 else 0.0
        )

        p80_r = round(p80_ex, 2)
        p80_overrun = round(max(0.0, p80_r - total_expected_ex), 2)
        p80_profit = round(exit_ex - p80_r, 2)

        # Breakeven composite hit rate per base: p* where (attempt_cost / p*) == exit_price_ex
        mean_attempt_cost_ex = total_expected_ex * p_base
        breakeven_rate = round(mean_attempt_cost_ex / max(exit_ex, 1e-6), 6)

        # Risk & liquidity adjusted expected profit score
        active_cnt = exit_quote.total_active_listings
        if exit_quote.source == "live_trade2_securable" and active_cnt == 0:
            liq_mult = 0.10
        else:
            liq_mult = min(1.0, math.log1p(max(1, active_cnt)) / math.log1p(12))

        raw_risk_adj = profit_ex - 0.20 * p80_overrun
        risk_score = round(
            raw_risk_adj * liq_mult if raw_risk_adj > 0 else raw_risk_adj,
            2,
        )

        if active_cnt <= 5:
            competition_level: Literal["low", "medium", "high"] = "low"
            comp_mult = 1.35 if active_cnt >= 1 else 0.35
        elif active_cnt <= 25:
            competition_level = "medium"
            comp_mult = 1.0
        else:
            competition_level = "high"
            comp_mult = 0.7

        if profit_ex <= 0:
            margin_tier: Literal["high_margin", "solid", "thin", "negative"] = "negative"
        elif roi_pct >= 60.0:
            margin_tier = "high_margin"
        elif roi_pct >= 20.0:
            margin_tier = "solid"
        else:
            margin_tier = "thin"

        niche_score = round(
            max(0.0, roi_pct * comp_mult * liq_mult) if profit_ex > 0 else 0.0,
            2,
        )

        all_warnings = [*sim_warnings, *exit_quote.fixer_warnings]
        if recipe.notes:
            all_warnings.append(f"Playbook: {recipe.notes}")

        eff_tier = classify_budget_tier(total_expected_div)

        return CraftProfitEvaluation(
            recipe_id=recipe.recipe_id,
            name=recipe.name,
            budget_tier=recipe.budget_tier,
            effective_budget_tier=eff_tier,
            archetype=recipe.archetype,
            base_type=self.engine.resolve_base(recipe.base_type).name,
            ilvl=recipe.ilvl,
            league=league,
            divine_rate_ex=round(div_ex, 2),
            unit_base_cost_ex=round(unit_base_ex, 2),
            expected_bases_used=round(exp_bases, 3),
            base_cost_ex=base_cost_total_ex,
            expected_craft_cost_ex=craft_cost_ex,
            expected_cost_exalted=total_expected_ex,
            expected_cost_divine=total_expected_div,
            p50_ex=round(p50_ex, 2),
            p80_ex=p80_r,
            p95_ex=round(p95_ex, 2),
            p80_drawdown_ex=p80_r,
            p80_overrun_ex=p80_overrun,
            p80_profit_ex=p80_profit,
            exit_price_ex=exit_ex,
            exit_price_divine=exit_div,
            expected_profit_ex=profit_ex,
            profit_divine=profit_div,
            expected_roi_pct=roi_pct,
            success_probability_per_base=round(p_base, 6),
            breakeven_hit_rate=breakeven_rate,
            risk_adjusted_score_ex=risk_score,
            competition_level=competition_level,
            niche_score=niche_score,
            margin_tier=margin_tier,
            base_market_quote=base_quote,
            exit_market_quote=exit_quote,
            planner_naive_cost_ex=round(naive_ex, 2) if naive_ex is not None else None,
            execution_steps=exec_steps,
            warnings=all_warnings,
        )

    def scan(
        self,
        recipes: Sequence[CraftRecipeSpec] | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        budget_tier: BudgetTier | None = None,
        max_budget_divine: float | None = None,
        live_economy: bool = True,
        live_trade: bool = True,
        max_trade_queries: int = 8,
        trade_delay_seconds: float = 1.5,
        currency_prices_ex: Mapping[str, float] | None = None,
        n_sims: int = 3000,
        seed: int = 42,
        include_planner_diagnostic: bool = False,
    ) -> CraftProfitScanReport:
        self._live_trade_calls_made = 0
        econ = (
            self._get_economy() if (live_economy and currency_prices_ex is None) else self._economy
        )

        prices_map, div_ex, chaos_ex, price_src, anomalies = build_sanitized_crafting_prices(
            econ,
            league=league,
            raw_overrides=currency_prices_ex,
        )

        candidate_recipes = list(recipes) if recipes is not None else self.default_recipes()
        # Prioritize recipes matching requested budget_tier first, then live_trade_priority
        candidate_recipes.sort(
            key=lambda r: (
                0 if (budget_tier is None or r.budget_tier == budget_tier) else 1,
                r.live_trade_priority,
            )
        )

        evals: list[CraftProfitEvaluation] = []
        for idx_r, rec in enumerate(candidate_recipes):
            ev = self.evaluate_recipe(
                rec,
                league=league,
                currency_prices_ex=prices_map,
                divine_rate_ex=div_ex,
                live_trade=live_trade,
                max_trade_queries=max_trade_queries,
                trade_delay_seconds=trade_delay_seconds,
                n_sims=n_sims,
                seed=seed + idx_r,
                include_planner_diagnostic=include_planner_diagnostic,
            )
            if budget_tier is not None and budget_tier not in (
                ev.budget_tier,
                ev.effective_budget_tier,
            ):
                continue
            if max_budget_divine is not None and ev.expected_cost_divine > max_budget_divine:
                continue
            evals.append(ev)

        evals.sort(
            key=lambda e: (
                1 if e.expected_profit_ex > 0 else 0,
                e.risk_adjusted_score_ex,
                e.expected_roi_pct,
            ),
            reverse=True,
        )
        for rank_idx, ev in enumerate(evals, start=1):
            ev.rank = rank_idx

        top_by_tier: dict[str, list[CraftProfitEvaluation]] = {"low": [], "mid": [], "high": []}
        for ev in evals:
            if ev.expected_profit_ex > 0:
                top_by_tier[ev.budget_tier].append(ev)

        return CraftProfitScanReport(
            league=league,
            divine_rate_ex=round(div_ex, 2),
            chaos_rate_ex=round(chaos_ex, 2),
            price_source=price_src,
            live_trade_queries_used=self._live_trade_calls_made,
            total_recipes_evaluated=len(evals),
            profitable_count=sum(1 for e in evals if e.expected_profit_ex > 0),
            top_overall=[e for e in evals if e.expected_profit_ex > 0][:5],
            top_by_tier=top_by_tier,
            evaluations=evals,
            price_anomalies_sanitized=anomalies,
        )
