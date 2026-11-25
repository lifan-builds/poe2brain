# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from typing import Literal

from pydantic import Field

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH
from poe2brain.models.builds import BuildStats, DewateredAudit
from poe2brain.models.common import Poe2Model


class NinjaClassStatistic(Poe2Model):
    class_name: str
    ascendancy_name: str | None = None
    share_pct: float
    character_count: int = 0
    trend: int = 0


class NinjaSnapshotInfo(Poe2Model):
    league_name: str
    league_url: str
    mode: Literal["sc_trade", "hc_trade", "ssf", "hc_ssf", "custom"] = "sc_trade"
    hardcore: bool = False
    ssf: bool = False
    total_characters: int = 0
    version: str | None = None
    snapshot_name: str | None = None
    overview_type: int = 0
    passive_tree: str | None = None
    time_machine_labels: list[str] = Field(default_factory=list)
    class_statistics: list[NinjaClassStatistic] = Field(default_factory=list)


class NinjaSnapshotStateReport(Poe2Model):
    patch: str = POE2_PATCH
    requested_league: str = DEFAULT_LEAGUE
    primary: NinjaSnapshotInfo
    modes: dict[str, NinjaSnapshotInfo] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class NinjaFacetCount(Poe2Model):
    name: str
    count: int
    share_pct: float


class NinjaStatSummary(Poe2Model):
    min: float = 0.0
    median: float = 0.0
    p90: float = 0.0
    max: float = 0.0


class NinjaEquippedItemSummary(Poe2Model):
    slot: str
    name: str
    base_type: str
    item_class: str | None = None
    rarity: str = "RARE"
    item_level: int | None = None
    runes: list[str] = Field(default_factory=list)
    anoint: str | None = None


class NinjaSkillGroupSummary(Poe2Model):
    index: int
    is_main_group: bool = False
    slot: str | None = None
    active_skill: str | None = None
    active_gems: list[str] = Field(default_factory=list)
    support_gems: list[str] = Field(default_factory=list)


class NinjaBuildEntry(Poe2Model):
    account: str
    character: str
    league: str = DEFAULT_LEAGUE
    rank_within_ascendancy: int | None = None
    class_name: str
    ascendancy_name: str | None = None
    level: int = 1
    main_skill: str | None = None
    active_skills: list[str] = Field(default_factory=list)
    support_gems: list[str] = Field(default_factory=list)
    unique_items: list[str] = Field(default_factory=list)
    weapon_types: list[str] = Field(default_factory=list)
    keystones: list[str] = Field(default_factory=list)
    ascendancy_notables: list[str] = Field(default_factory=list)
    anoints: list[str] = Field(default_factory=list)
    total_dps: float = 0.0
    combined_dps: float = 0.0
    full_dps: float = 0.0
    effective_dps: float = 0.0
    total_ehp: float = 0.0
    life: float = 0.0
    energy_shield: float = 0.0
    mana: float = 0.0
    spirit: int = 0
    dewatered_dps: float | None = None
    dps_inflation_pct: float | None = None
    dewatered_ehp: float | None = None
    phys_max_hit: float | None = None
    chaos_max_hit: float | None = None
    chaos_immune: bool = False
    movement_speed_mod: float | None = None
    movement_speed_while_using_skill: float | None = None
    ascendancy_share_pct: float | None = None
    corpus_skill_count: int | None = None
    mobility_tags: list[str] = Field(default_factory=list)
    t0_chase_uniques: list[str] = Field(default_factory=list)
    sleeper_score: float | None = None
    ninja_url: str
    source: str = "poeninja_corpus"
    live_refreshed: bool = False
    pob_code: str | None = None


class NinjaBuildSearchReport(Poe2Model):
    patch: str = POE2_PATCH
    league: str = DEFAULT_LEAGUE
    snapshot: NinjaSnapshotInfo | None = None
    corpus_total_builds: int = 0
    matched_builds: int = 0
    returned_builds: int = 0
    preset: str | None = None
    sort_by: Literal["dps", "ehp", "level", "es", "life", "sleeper", "mobility"] = "dps"
    applied_filters: dict[str, str | float] = Field(default_factory=dict)
    top_ascendancies: list[NinjaFacetCount] = Field(default_factory=list)
    top_skills: list[NinjaFacetCount] = Field(default_factory=list)
    top_supports: list[NinjaFacetCount] = Field(default_factory=list)
    top_uniques: list[NinjaFacetCount] = Field(default_factory=list)
    top_keystones: list[NinjaFacetCount] = Field(default_factory=list)
    top_weapons: list[NinjaFacetCount] = Field(default_factory=list)
    dps_distribution: NinjaStatSummary = Field(default_factory=NinjaStatSummary)
    ehp_distribution: NinjaStatSummary = Field(default_factory=NinjaStatSummary)
    life_distribution: NinjaStatSummary = Field(default_factory=NinjaStatSummary)
    es_distribution: NinjaStatSummary = Field(default_factory=NinjaStatSummary)
    builds: list[NinjaBuildEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class NinjaCharacterImport(Poe2Model):
    source_kind: Literal[
        "poeninja_live_pob",
        "poeninja_share_pob",
        "poe2db_pob",
        "maxroll_pob",
        "pobbin_pob",
        "poeninja_archived_corpus",
    ]
    requested_source: str
    resolved_url: str
    account: str | None = None
    character: str | None = None
    league: str | None = None
    timemachine: str | None = None
    ninja_url: str | None = None
    tree_version: str | None = None
    class_name: str
    ascendancy_name: str | None = None
    level: int = 1
    main_skill: str | None = None
    active_skills: list[str] = Field(default_factory=list)
    support_gems: list[str] = Field(default_factory=list)
    unique_items: list[str] = Field(default_factory=list)
    weapon_types: list[str] = Field(default_factory=list)
    keystones: list[str] = Field(default_factory=list)
    ascendancy_notables: list[str] = Field(default_factory=list)
    anoints: list[str] = Field(default_factory=list)
    equipped_items: list[NinjaEquippedItemSummary] = Field(default_factory=list)
    skill_groups: list[NinjaSkillGroupSummary] = Field(default_factory=list)
    xml_stats: dict[str, float] = Field(default_factory=dict)
    pob_code: str
    loaded_into_pob: bool = False
    pob_slot: str | None = None
    pob_stats: BuildStats | None = None
    dewatered_audit: DewateredAudit | None = None
    notes: list[str] = Field(default_factory=list)


class ProtobufDictionaryRef(Poe2Model):
    id: str
    hash: str
    properties_hash: str | None = None


class ProtobufDimensionCount(Poe2Model):
    key: int
    label: str
    count: int


class ProtobufSearchResult(Poe2Model):
    total: int = 0
    dictionaries: list[ProtobufDictionaryRef] = Field(default_factory=list)
    dimensions: dict[str, list[ProtobufDimensionCount]] = Field(default_factory=dict)
    columns: dict[str, list[str | int | float | None]] = Field(default_factory=dict)
    roster: list[dict[str, str | int | float | None]] = Field(default_factory=list)
