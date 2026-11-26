# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator

from poe2brain.models.common import AffixSide, Poe2Model, StatRollRange


class ItemRarity(StrEnum):
    NORMAL = "normal"
    MAGIC = "magic"
    RARE = "rare"
    UNIQUE = "unique"
    GEM = "gem"
    CURRENCY = "currency"
    QUEST = "quest"


class ModDomain(StrEnum):
    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    FRACTURED = "fractured"
    DESECRATED = "desecrated"
    RUNE = "rune"
    ENCHANT = "enchant"
    CRAFTED = "crafted"
    CORRUPTED = "corrupted"
    SKILL = "skill"


class ItemRequirements(Poe2Model):
    level: int = Field(default=0, validation_alias=AliasChoices("level", "lvl", "Level"))
    strength: int = Field(
        default=0, validation_alias=AliasChoices("strength", "str", "Str", "Strength")
    )
    dexterity: int = Field(
        default=0, validation_alias=AliasChoices("dexterity", "dex", "Dex", "Dexterity")
    )
    intelligence: int = Field(
        default=0, validation_alias=AliasChoices("intelligence", "int", "Int", "Intelligence")
    )


class ParsedMod(Poe2Model):
    raw_text: str
    domain: ModDomain = ModDomain.EXPLICIT
    affix_side: AffixSide | None = None
    affix_name: str | None = None
    display_tier: int | None = None
    tier_code: str | None = None
    required_level: int | None = None
    values: list[float] = Field(default_factory=list)
    roll_ranges: list[StatRollRange] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    unscalable: bool = False
    bonded: bool = False
    trade_stat_ids: list[str] = Field(default_factory=list)

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, value: Any) -> Any:
        if isinstance(value, str):
            norm = value.strip().lower()
            if norm == "augment":
                return "rune"
            return norm
        return value


class ParsedItem(Poe2Model):
    raw_text: str = ""
    item_class: str | None = None
    rarity: ItemRarity = ItemRarity.RARE
    name: str | None = None
    base_type: str
    item_level: int | None = None
    area_level: int | None = None
    identified: bool = True
    unidentified_tier: int | None = None

    quality: int = 0
    catalyst_type: str | None = None
    spirit: int | None = None
    armour: int | None = None
    evasion: int | None = None
    energy_shield: int | None = None
    ward: int | None = None
    block_chance: float | None = None

    physical_dps: float | None = None
    elemental_dps: float | None = None
    total_dps: float | None = None
    attacks_per_second: float | None = None
    critical_hit_chance: float | None = None
    reload_time: float | None = None

    rune_sockets: int = 0
    socketed_runes: list[str] = Field(default_factory=list)

    gem_level: int | None = None
    gem_sockets: int | None = None
    spirit_reservation: int | None = None

    waystone_tier: int | None = None
    waystone_revives: int | None = None
    waystone_drop_chance_pct: int | None = None

    requirements: ItemRequirements = Field(default_factory=ItemRequirements)

    corrupted: bool = False
    twice_corrupted: bool = False
    sanctified: bool = False
    fractured: bool = False
    mirrored: bool = False
    veiled_desecrated: bool = False

    granted_skills: list[str] = Field(default_factory=list)
    mods: list[ParsedMod] = Field(default_factory=list)
    note: str | None = None

    @field_validator("rarity", mode="before")
    @classmethod
    def _normalize_rarity(cls, value: Any) -> Any:
        if isinstance(value, str):
            norm = value.strip().lower()
            if norm == "uniquefoil":
                return "unique"
            return norm
        return value

    @model_validator(mode="after")
    def _sync_flags(self) -> ParsedItem:
        if self.twice_corrupted:
            self.corrupted = True
        if any(m.domain == ModDomain.FRACTURED for m in self.mods):
            self.fractured = True
        if self.unidentified_tier is not None:
            self.identified = False
        return self
