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


class DewateredAudit(Poe2Model):
    slot_name: str = "main"
    raw_stats: BuildStats
    sustainable_pinnacle_stats: BuildStats
    dps_inflation_pct: float = 0.0
    ehp_inflation_pct: float = 0.0
    stripped_flags: list[StrippedPobFlag] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    hc_rip_risks: list[HcRipRisk] = Field(default_factory=list)
    pinnacle_ready: bool = True


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
