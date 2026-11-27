# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field

from poe2brain.models.common import Poe2Model


class BuildStats(Poe2Model):
    class_name: str | None = None
    ascendancy_name: str | None = None
    level: int = 1
    main_skill: str | None = None

    full_dps: float = Field(default=0.0, validation_alias=AliasChoices("full_dps", "FullDPS"))
    total_dps: float = Field(default=0.0, validation_alias=AliasChoices("total_dps", "TotalDPS"))
    combined_dps: float = Field(
        default=0.0, validation_alias=AliasChoices("combined_dps", "CombinedDPS")
    )
    effective_dps: float = Field(
        default=0.0, validation_alias=AliasChoices("effective_dps", "EffectiveDPS")
    )
    total_ehp: float = Field(default=0.0, validation_alias=AliasChoices("total_ehp", "TotalEHP"))

    life: float = Field(default=0.0, validation_alias=AliasChoices("life", "Life"))
    life_unreserved: float = Field(
        default=0.0, validation_alias=AliasChoices("life_unreserved", "LifeUnreserved")
    )
    life_regen: float = Field(
        default=0.0, validation_alias=AliasChoices("life_regen", "LifeRegenRecovery")
    )
    energy_shield: float = Field(
        default=0.0, validation_alias=AliasChoices("energy_shield", "ES", "EnergyShield")
    )
    es_recharge: float = Field(
        default=0.0, validation_alias=AliasChoices("es_recharge", "EnergyShieldRecharge")
    )
    mana: float = Field(default=0.0, validation_alias=AliasChoices("mana", "Mana"))
    mana_unreserved: float = Field(
        default=0.0, validation_alias=AliasChoices("mana_unreserved", "ManaUnreserved")
    )
    mana_regen: float = Field(
        default=0.0,
        validation_alias=AliasChoices("mana_regen", "ManaRegenRecovery", "ManaRegen"),
    )
    mana_cost: float = Field(default=0.0, validation_alias=AliasChoices("mana_cost", "ManaCost"))
    mana_per_second_cost: float = Field(
        default=0.0,
        validation_alias=AliasChoices("mana_per_second_cost", "ManaPerSecondCost"),
    )
    mana_net_sustain: float = 0.0
    mana_payable: bool = True
    mana_sustainable: bool = True

    spirit_total: int = Field(
        default=0, validation_alias=AliasChoices("spirit_total", "SpiritTotal", "Spirit")
    )
    spirit_reserved: int = Field(
        default=0, validation_alias=AliasChoices("spirit_reserved", "SpiritReserved")
    )
    spirit_unreserved: int = Field(
        default=0, validation_alias=AliasChoices("spirit_unreserved", "SpiritUnreserved")
    )

    fire_resist: float = Field(
        default=0.0, validation_alias=AliasChoices("fire_resist", "FireResist")
    )
    cold_resist: float = Field(
        default=0.0, validation_alias=AliasChoices("cold_resist", "ColdResist")
    )
    lightning_resist: float = Field(
        default=0.0, validation_alias=AliasChoices("lightning_resist", "LightningResist")
    )
    chaos_resist: float = Field(
        default=0.0, validation_alias=AliasChoices("chaos_resist", "ChaosResist")
    )
    chaos_immune: bool = Field(
        default=False, validation_alias=AliasChoices("chaos_immune", "ChaosInoculation")
    )

    phys_max_hit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("phys_max_hit", "PhysicalMaximumHitTaken"),
    )
    fire_max_hit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("fire_max_hit", "FireMaximumHitTaken"),
    )
    cold_max_hit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("cold_max_hit", "ColdMaximumHitTaken"),
    )
    lightning_max_hit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("lightning_max_hit", "LightningMaximumHitTaken"),
    )
    chaos_max_hit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("chaos_max_hit", "ChaosMaximumHitTaken"),
    )

    block_chance: float = Field(
        default=0.0, validation_alias=AliasChoices("block_chance", "BlockChance", "Block")
    )
    effective_block_chance: float = Field(
        default=0.0,
        validation_alias=AliasChoices("effective_block_chance", "EffectiveBlockChance", "Block"),
    )
    block_chance_max: float = Field(
        default=50.0, validation_alias=AliasChoices("block_chance_max", "BlockChanceMax")
    )
    spell_block_chance: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "spell_block_chance", "EffectiveSpellBlockChance", "SpellBlockChance"
        ),
    )
    deflection_chance: float = Field(
        default=0.0,
        validation_alias=AliasChoices("deflection_chance", "DeflectionChance", "Deflection"),
    )
    deflect_chance: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "deflect_chance", "DeflectChance", "DeflectionChance", "Deflection"
        ),
    )
    evade_chance: float = Field(
        default=0.0, validation_alias=AliasChoices("evade_chance", "EvadeChance")
    )
    evasion: float = Field(default=0.0, validation_alias=AliasChoices("evasion", "Evasion"))
    armour: float = Field(default=0.0, validation_alias=AliasChoices("armour", "Armour"))
    physical_damage_reduction: float = Field(
        default=0.0,
        validation_alias=AliasChoices("physical_damage_reduction", "PhysicalDamageReduction"),
    )

    str_total: int = Field(default=0, validation_alias=AliasChoices("str_total", "Str"))
    dex_total: int = Field(default=0, validation_alias=AliasChoices("dex_total", "Dex"))
    int_total: int = Field(default=0, validation_alias=AliasChoices("int_total", "Int"))
    req_str: int = Field(default=0, validation_alias=AliasChoices("req_str", "ReqStr"))
    req_dex: int = Field(default=0, validation_alias=AliasChoices("req_dex", "ReqDex"))
    req_int: int = Field(default=0, validation_alias=AliasChoices("req_int", "ReqInt"))
    attributes_met: bool = True

    movement_speed_mod: float = Field(
        default=1.0,
        validation_alias=AliasChoices(
            "movement_speed_mod", "EffectiveMovementSpeedMod", "MovementSpeedMod"
        ),
    )
    movement_speed_while_using_skill: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "movement_speed_while_using_skill", "MovementSpeedWhileUsingSkill"
        ),
    )
    item_rarity_pct: float = Field(
        default=0.0,
        validation_alias=AliasChoices("item_rarity_pct", "ItemRarityPct"),
    )
    attack_cast_speed: float = Field(
        default=0.0, validation_alias=AliasChoices("attack_cast_speed", "Speed")
    )
    crit_chance: float = Field(
        default=0.0, validation_alias=AliasChoices("crit_chance", "CritChance")
    )
    crit_multiplier: float = Field(
        default=0.0, validation_alias=AliasChoices("crit_multiplier", "CritMultiplier")
    )

    allocated_nodes: int = 0
    active_skills: list[str] = Field(default_factory=list)
    keystones: list[str] = Field(default_factory=list)


class StrippedPobFlag(Poe2Model):
    flag: str
    raw_value: Any = None
    dps_inflation_pct: float = 0.0
    ehp_inflation_pct: float = 0.0
    reason: str = ""


class HcRipRisk(Poe2Model):
    risk_id: str
    severity: Literal["critical", "high", "medium", "info"]
    title: str
    summary: str
    metric_value: float | None = None
    threshold_value: float | None = None
    mitigation: str = ""


class WeaponSwapAudit(Poe2Model):
    has_weapon_swap: bool = False
    active_set: int = 1
    alternate_set: int = 2
    weapon_set1_passives: int = 0
    weapon_set2_passives: int = 0
    alternate_full_dps: float = 0.0
    alternate_total_ehp: float = 0.0
    ehp_drop_pct: float = 0.0
    alternate_spirit_unreserved: int = 0
    alternate_attributes_met: bool = True
    alternate_resists: dict[str, float] = Field(default_factory=dict)
    swap_warnings: list[str] = Field(default_factory=list)


class DewateredAudit(Poe2Model):
    slot_name: str = "main"
    raw_stats: BuildStats
    sustainable_pinnacle_stats: BuildStats
    dps_inflation_pct: float = 0.0
    ehp_inflation_pct: float = 0.0
    stripped_flags: list[StrippedPobFlag] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    hc_rip_risks: list[HcRipRisk] = Field(default_factory=list)
    attribute_margins: dict[str, int] = Field(default_factory=dict)
    weapon_swap_audit: WeaponSwapAudit | None = None
    pinnacle_ready: bool = True


class MegalomaniacCombo(Poe2Model):
    notables: list[str]
    combined_delta: float
    combined_pct: float
    note: str = ""


class MegalomaniacSearchReport(Poe2Model):
    slot_name: str = "main"
    stat: str = "FullDPS"
    league: str = ""
    top_notables: list[dict[str, Any]] = Field(default_factory=list)
    top_2_notable_combos: list[MegalomaniacCombo] = Field(default_factory=list)
    top_3_notable_combos: list[MegalomaniacCombo] = Field(default_factory=list)
    matched_trade2_stat_count: int = 0
    trade2_query: dict[str, Any] = Field(default_factory=dict)
    trade_url: str | None = None
    usage_note: str = ""


class RuneSocketRecommendation(Poe2Model):
    equipment_slot: str
    item_name: str
    socket_category: Literal["armour", "weapon", "shield", "focus", "other"]
    sockets_allocated: int
    augment_name: str
    effect_summary: str
    purpose: str
    unit_cost_exalted: float | None = None
    total_cost_exalted: float | None = None
    can_overwrite_in_socket: bool = True


class RuneOptimizationReport(Poe2Model):
    slot_name: str = "main"
    objective: str = "balanced"
    league: str = ""
    total_available_sockets: int = 0
    armour_sockets: int = 0
    weapon_sockets: int = 0
    resist_deficits_before: dict[str, float] = Field(default_factory=dict)
    recommendations: list[RuneSocketRecommendation] = Field(default_factory=list)
    estimated_total_cost_exalted: float | None = None
    pob_verified_dps_delta: float = 0.0
    pob_verified_ehp_delta: float = 0.0
    pob_verified_resists_after: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class GoldRespecEstimate(Poe2Model):
    character_level: int
    gold_per_passive_point: int
    gold_per_ascendancy_point: int
    refunded_passive_points: int = 0
    refunded_ascendancy_points: int = 0
    allocated_new_points: int = 0
    total_gold_cost: int = 0
    atlas_respec_gold_per_point: int = 5000
    note: str = ""


class DefensivePassiveRespecOption(Poe2Model):
    node_id: int
    name: str
    path_dist: int
    stats: list[str] = Field(default_factory=list)
    ehp_delta: float = 0.0
    ehp_pct: float = 0.0
    per_point_ehp_delta: float = 0.0
    per_point_ehp_pct: float = 0.0
    respec_gold_cost: int = 0


class HcRipAuditReport(Poe2Model):
    slot_name: str = "main"
    dewatered_audit: DewateredAudit
    max_hits: dict[str, float] = Field(default_factory=dict)
    chaos_es_effective_pool: float = 0.0
    bleed_poison_life_pool: float | None = None
    has_chaos_inoculation: bool = False
    equipped_charms: list[str] = Field(default_factory=list)
    rip_risks: list[HcRipRisk] = Field(default_factory=list)
    top_defensive_passives: list[DefensivePassiveRespecOption] = Field(default_factory=list)
    respec_gold_per_point: int = 0
    pinnacle_ready: bool = True


class EquipmentSlotDiff(Poe2Model):
    equipment_slot: str
    before_name: str | None = None
    before_base: str | None = None
    before_rarity: str | None = None
    after_name: str | None = None
    after_base: str | None = None
    after_rarity: str | None = None
    change_type: Literal["added", "removed", "upgraded", "unchanged"]


class SkillGroupDiff(Poe2Model):
    active_skill: str
    change_type: Literal["added", "removed", "modified", "unchanged"]
    added_supports: list[str] = Field(default_factory=list)
    removed_supports: list[str] = Field(default_factory=list)
    level_before: int | None = None
    level_after: int | None = None


class BuildSlotComparison(Poe2Model):
    slot_a: str
    slot_b: str
    dewatered: bool = False
    stats_a: BuildStats
    stats_b: BuildStats
    level_delta: int = 0
    effective_dps_before: float = 0.0
    effective_dps_after: float = 0.0
    effective_dps_delta: float = 0.0
    effective_dps_pct: float = 0.0
    total_ehp_before: float = 0.0
    total_ehp_after: float = 0.0
    total_ehp_delta: float = 0.0
    total_ehp_pct: float = 0.0
    stat_deltas: dict[str, float] = Field(default_factory=dict)
    added_uniques: list[str] = Field(default_factory=list)
    removed_uniques: list[str] = Field(default_factory=list)
    equipment_changes: list[EquipmentSlotDiff] = Field(default_factory=list)
    added_skills: list[str] = Field(default_factory=list)
    removed_skills: list[str] = Field(default_factory=list)
    skill_changes: list[SkillGroupDiff] = Field(default_factory=list)
    added_keystones: list[str] = Field(default_factory=list)
    removed_keystones: list[str] = Field(default_factory=list)
    added_notables: list[str] = Field(default_factory=list)
    removed_notables: list[str] = Field(default_factory=list)
    added_ascendancy_nodes: list[str] = Field(default_factory=list)
    removed_ascendancy_nodes: list[str] = Field(default_factory=list)
    passive_nodes_added_count: int = 0
    passive_nodes_removed_count: int = 0
    respec_estimate: GoldRespecEstimate


class CharacterTimelinePoint(Poe2Model):
    label: str
    available: bool
    http_status: int | None = None
    level: int | None = None
    class_name: str | None = None
    ascendancy_name: str | None = None
    main_skill: str | None = None
    effective_dps: float | None = None
    total_ehp: float | None = None
    unique_items: list[str] = Field(default_factory=list)
    keystones: list[str] = Field(default_factory=list)
    pob_code: str | None = None


class CharacterTimelineReport(Poe2Model):
    account: str
    character: str
    league: str
    overview_slug: str
    available_time_machine_labels: list[str] = Field(default_factory=list)
    probed_points: list[CharacterTimelinePoint] = Field(default_factory=list)
    earliest_available_label: str | None = None
    latest_available_label: str | None = None
    stepping_stone_label: str | None = None
    stepping_stone_note: str | None = None
    notes: list[str] = Field(default_factory=list)


class ItemUpgradeDiff(Poe2Model):
    equipment_slot: str
    candidate_label: str | None = None
    candidate_item_text: str | None = None
    price_exalted: float | None = None

    full_dps_before: float = 0.0
    full_dps_after: float = 0.0
    full_dps_delta: float = 0.0
    full_dps_pct: float = 0.0

    total_dps_before: float = 0.0
    total_dps_after: float = 0.0
    total_dps_pct: float = 0.0

    total_ehp_before: float = 0.0
    total_ehp_after: float = 0.0
    total_ehp_delta: float = 0.0
    total_ehp_pct: float = 0.0

    life_delta: float = 0.0
    es_delta: float = 0.0
    mana_delta: float = 0.0
    spirit_unreserved_delta: int = 0
    phys_max_hit_delta: float = 0.0
    elemental_max_hit_delta: float = 0.0
    chaos_max_hit_delta: float = 0.0
    resist_deltas: dict[str, float] = Field(default_factory=dict)
    dps_per_exalted: float | None = None
    ehp_per_exalted: float | None = None


class StatWeightEntry(Poe2Model):
    stat_id: str
    stat_label: str
    weight: float
    dps_per_unit: float = 0.0
    ehp_per_unit: float = 0.0


class SlotStatWeights(Poe2Model):
    equipment_slot: str
    objective: str = "balanced"
    weights: list[StatWeightEntry] = Field(default_factory=list)
    trade2_weighted_filter: dict[str, Any] = Field(default_factory=dict)


class CalcStatMatch(Poe2Model):
    key: str
    value: Any = None
    lines: list[str] = Field(default_factory=list)
    slots: list[dict[str, Any]] = Field(default_factory=list)
    reservations: list[dict[str, Any]] = Field(default_factory=list)
    damage_types: list[dict[str, Any]] = Field(default_factory=list)


class CalcModProvenanceRow(Poe2Model):
    stat_name: str
    mod_type: str
    value: Any = None
    source_type: str = "Other"
    source_label: str = ""
    flags: str = ""


class CalcSourceTotal(Poe2Model):
    source_type: str
    base_total: float = 0.0
    inc_total: float = 0.0
    more_pct: float = 0.0
    mod_count: int = 0


class CalcBreakdownReport(Poe2Model):
    slot: str = "main"
    query: str
    skill_group: int = 1
    active_skill_name: str = "None"
    matched_stats: list[CalcStatMatch] = Field(default_factory=list)
    modifier_count_total: int = 0
    source_totals: list[CalcSourceTotal] = Field(default_factory=list)
    modifiers: list[CalcModProvenanceRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SupportGemCandidate(Poe2Model):
    gem_id: str
    name: str
    gem_family: str | None = None
    support_tier: int | None = None
    is_lineage: bool = False
    lineage_conflict_skill: str | None = None
    replaces_gem: str | None = None
    already_socketed: bool = False
    dps_before: float = 0.0
    dps_after: float = 0.0
    dps_delta: float = 0.0
    dps_pct: float = 0.0
    ehp_delta: float = 0.0
    mana_cost_before: float = 0.0
    mana_cost_after: float = 0.0
    mana_sustainable_after: bool = True
    spirit_unreserved_before: int = 0
    spirit_unreserved_after: int = 0
    spirit_already_overflowing: bool = False
    spirit_overflow: bool = False
    attributes_met_after: bool = True


class SupportGemRankingReport(Poe2Model):
    slot: str = "main"
    group_index: int = 1
    replace_gem_slot: int | None = None
    active_skill_label: str = ""
    current_supports: list[str] = Field(default_factory=list)
    max_support_sockets: int = 5
    supports_full: bool = False
    hypothetical_extra_socket: bool = False
    best_swap: bool = False
    dewatered: bool = False
    max_tier: int | None = None
    include_lineage: bool = True
    total_valid: int = 0
    candidates: list[SupportGemCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RadiusJewelCandidate(Poe2Model):
    rank: int = 1
    jewel_name: str
    socket_node_id: int
    socket_name: str
    socket_allocated: bool = False
    socket_path_dist: int = 0
    ring_index: int = 1
    ring_label: str = ""
    allocated_notables_in_radius: int = 0
    allocated_smalls_in_radius: int = 0
    ring_notables: list[str] = Field(default_factory=list)
    nearby_unallocated_notables: list[str] = Field(default_factory=list)
    total_points_spent: int = 1
    normal_points_saved: int = 0
    stat: str = "FullDPS"
    before: float = 0.0
    after: float = 0.0
    delta: float = 0.0
    pct: float = 0.0
    per_point_delta: float = 0.0
    per_point_pct: float = 0.0


class RadiusJewelRankingReport(Poe2Model):
    slot: str = "main"
    jewel_name: str
    stat: str = "FullDPS"
    base_value: float = 0.0
    candidates: list[RadiusJewelCandidate] = Field(default_factory=list)


class WhatIfPieceAttribution(Poe2Model):
    label: str
    kind: Literal["equipment_slot", "main_skill_group", "edited_item", "passive_tree"]
    effective_dps_delta: float = 0.0
    effective_dps_pct: float = 0.0
    total_ehp_delta: float = 0.0
    total_ehp_pct: float = 0.0
    spirit_unreserved_after: int = 0
    mana_sustainable_after: bool = True


class WhatIfScenarioPoint(Poe2Model):
    scenario: str
    effective_dps: float = 0.0
    effective_dps_delta_vs_baseline: float = 0.0
    effective_dps_pct_vs_baseline: float = 0.0
    total_ehp: float = 0.0
    total_ehp_delta_vs_baseline: float = 0.0
    mana_sustainable: bool = True
    spirit_unreserved: int = 0


class WhatIfReport(Poe2Model):
    slot: str = "main"
    applied: bool = False
    rolled_back: bool = True
    baseline_stats: BuildStats
    after_stats: BuildStats
    effective_dps_delta: float = 0.0
    effective_dps_pct: float = 0.0
    total_ehp_delta: float = 0.0
    total_ehp_pct: float = 0.0
    edited_equipment_slot: str | None = None
    edited_item_text: str | None = None
    borrowed_from_slot: str | None = None
    piece_attributions: list[WhatIfPieceAttribution] = Field(default_factory=list)
    scenario_matrix: list[WhatIfScenarioPoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
