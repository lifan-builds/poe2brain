from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field

from poe2brain.models.common import Poe2Model


class BuildStats(Poe2Model):
    class_name: str | None = None
    ascendancy_name: str | None = None
    level: int = 1
    main_skill: str | None = None

    full_dps: float = Field(default=0.0, validation_alias=AliasChoices("full_dps", "FullDPS"))
    total_dps: float = Field(default=0.0, validation_alias=AliasChoices("total_dps", "TotalDPS"))
    total_ehp: float = Field(default=0.0, validation_alias=AliasChoices("total_ehp", "TotalEHP"))

    life: float = Field(default=0.0, validation_alias=AliasChoices("life", "Life"))
    energy_shield: float = Field(
        default=0.0, validation_alias=AliasChoices("energy_shield", "ES", "EnergyShield")
    )
    mana: float = Field(default=0.0, validation_alias=AliasChoices("mana", "Mana"))

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

    block_chance: float = Field(
        default=0.0, validation_alias=AliasChoices("block_chance", "BlockChance", "Block")
    )
    deflection_chance: float = Field(
        default=0.0,
        validation_alias=AliasChoices("deflection_chance", "DeflectionChance", "Deflection"),
    )
    evasion: float = Field(default=0.0, validation_alias=AliasChoices("evasion", "Evasion"))
    armour: float = Field(default=0.0, validation_alias=AliasChoices("armour", "Armour"))

    allocated_nodes: int = 0
    active_skills: list[str] = Field(default_factory=list)


class StrippedPobFlag(Poe2Model):
    flag: str
    raw_value: Any = None
    dps_inflation_pct: float = 0.0
    ehp_inflation_pct: float = 0.0
    reason: str = ""


class DewateredAudit(Poe2Model):
    slot_name: str = "main"
    raw_stats: BuildStats
    sustainable_pinnacle_stats: BuildStats
    dps_inflation_pct: float = 0.0
    ehp_inflation_pct: float = 0.0
    stripped_flags: list[StrippedPobFlag] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


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
    resist_deltas: dict[str, float] = Field(default_factory=dict)
    dps_per_exalted: float | None = None


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
