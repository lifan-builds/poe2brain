# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import Field

from poe2brain.config import POE2_PATCH
from poe2brain.knowledge.db import GamedataDB, GamedataHealth
from poe2brain.knowledge.meta import (
    ASCENDANCY_SHORTHAND_ALIASES,
    AscendancyInspection,
    AscendancySummary,
    ClassSummary,
    MetaService,
)
from poe2brain.knowledge.zh_aliases import translate_zh_query
from poe2brain.models.common import Poe2Model
from poe2brain.models.knowledge import (
    BaseItemSummary,
    GemSummary,
    KnowledgeRecord,
    MechanicFact,
    UniqueSummary,
)

POSSESSIVE_RE = re.compile(r"['\u2019]s\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
PLUS_LEVEL_RE = re.compile(r"\+\s*(\d+)")
STALE_BASE_PREFIX_RE = re.compile(r"^(expert|advanced)\s+(.+)$", re.IGNORECASE)
ROMAN_SUFFIX_RE = re.compile(r"\s+(I|II|III|IV|V)$")

POB_UNIQUE_META_PREFIXES: tuple[str, ...] = (
    "Has Alt Variant",
    "Selected Variant",
    "Selected Alt Variant",
    "Allow Duplicate Variants",
    "League:",
    "Source:",
    "LevelReq:",
    "Implicits:",
)

FTS_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "how",
        "does",
        "do",
        "what",
        "why",
        "can",
        "poe",
        "poe1",
        "poe2",
        "work",
        "works",
    }
)

SPELLING_SYNONYMS: dict[str, str] = {
    "armor": "armour",
    "armour": "armor",
    "defense": "defence",
    "defence": "defense",
    "defenses": "defences",
    "defences": "defenses",
    "homogenizing": "homogenising",
    "homogenising": "homogenizing",
    "homogenize": "homogenise",
    "homogenise": "homogenize",
    "crystallization": "crystallisation",
    "crystallisation": "crystallization",
    "catalyzing": "catalysing",
    "catalysing": "catalyzing",
    "scepter": "sceptre",
    "sceptre": "scepter",
    "scepters": "sceptres",
    "sceptres": "scepters",
    "jeweler": "jeweller",
    "jeweller": "jeweler",
    "jewelers": "jewellers",
    "jewellers": "jewelers",
    "color": "colour",
    "colour": "color",
}

TOPIC_ABBREVIATIONS: dict[str, str] = {
    "ci": "Chaos Inoculation",
    "eb": "Eldritch Battery",
    "mom": "Mind Over Matter",
    "aof": "Avatar of Fire",
    "es": "Energy Shield",
    "coc": "Cast on Critical meta energy",
    "6l": "uncut gems jewellers sockets",
    "6-link": "uncut gems jewellers sockets",
    "six-link": "uncut gems jewellers sockets",
}

MECHANIC_HINT_TERMS: frozenset[str] = frozenset(
    {
        "cap",
        "formula",
        "convert",
        "conversion",
        "extra",
        "double",
        "dipping",
        "quality",
        "block",
        "deflect",
        "deflection",
        "evasion",
        "armour",
        "armor",
        "chaos",
        "bleed",
        "bleeding",
        "poison",
        "es",
        "grim",
        "feast",
        "support",
        "lineage",
        "spirit",
        "meta",
        "energy",
        "homogenising",
        "homogenizing",
        "recombinator",
        "recombinators",
        "recombination",
        "scour",
        "scouring",
        "alteration",
        "bench",
        "tier",
        "omen",
        "omens",
        "desecrate",
        "desecration",
        "revive",
        "revives",
        "waystone",
        "tablet",
        "tablets",
        "inoculation",
        "eldritch",
        "avatar",
        "glancing",
        "ignite",
        "flammability",
        "stun",
        "cull",
        "culling",
        "leech",
        "charm",
        "charms",
        "flask",
        "flasks",
        "sanctification",
        "whittling",
        "fracture",
        "fracturing",
        "rule",
        "mechanic",
        "mechanics",
        "trap",
        "bypass",
    }
)

CATEGORY_DOC_TYPE_ALIASES: dict[str, str] = {
    "mechanic": "mechanic_fact",
    "mechanics": "mechanic_fact",
    "mechanic_fact": "mechanic_fact",
    "mechanics_facts": "mechanic_fact",
    "rule": "mechanic_fact",
    "rules": "mechanic_fact",
    "keyword": "keyword",
    "keywords": "keyword",
    "gem": "gem",
    "gems": "gem",
    "unique": "unique",
    "uniques": "unique",
    "base": "base_item",
    "bases": "base_item",
    "base_item": "base_item",
    "base_items": "base_item",
    "augment": "augment",
    "augments": "augment",
    "socketable": "augment",
    "crafting_lever": "crafting_lever",
    "crafting_levers": "crafting_lever",
    "lever": "crafting_lever",
    "levers": "crafting_lever",
    "runeforge": "runeforge_recipe",
    "runeforging": "runeforge_recipe",
    "runeforge_recipe": "runeforge_recipe",
    "runeforge_recipes": "runeforge_recipe",
    "ascendancy": "ascendancy",
    "ascendancies": "ascendancy",
    "class": "class",
    "classes": "class",
}

POE1_BASE_TRAP_MAP: dict[str, tuple[str, str, str]] = {
    "vaal regalia": ("Body Armour", "Body_Armours_int", "PoE1 Intelligence ES Body Armour"),
    "widowsilk robe": ("Body Armour", "Body_Armours_int", "PoE1 Intelligence ES Body Armour"),
    "regalia": ("Body Armour", "Body_Armours_int", "PoE1 Intelligence ES Body Armour"),
    "astral plate": ("Body Armour", "Body_Armours_str", "PoE1 Strength Body Armour"),
    "glorious plate": ("Body Armour", "Body_Armours_str", "PoE1 Strength Body Armour"),
    "royal plate": ("Body Armour", "Body_Armours_str", "PoE1 Strength Body Armour"),
    "assassin's garb": ("Body Armour", "Body_Armours_dex", "PoE1 Dexterity Body Armour"),
    "zodiac leather": ("Body Armour", "Body_Armours_dex", "PoE1 Dexterity Body Armour"),
    "sadist garb": ("Body Armour", "Body_Armours_dex_int", "PoE1 Dex/Int Body Armour"),
    "carnal armour": ("Body Armour", "Body_Armours_dex_int", "PoE1 Dex/Int Body Armour"),
    "necrotic armour": ("Body Armour", "Body_Armours_dex_int", "PoE1 Dex/Int Body Armour"),
    "saintly chainmail": ("Body Armour", "Body_Armours_str_int", "PoE1 Str/Int Body Armour"),
    "sacred chainmail": ("Body Armour", "Body_Armours_str_int", "PoE1 Str/Int Body Armour"),
    "triumphant lamellar": ("Body Armour", "Body_Armours_str_dex", "PoE1 Str/Dex Body Armour"),
    "full dragonscale": ("Body Armour", "Body_Armours_str_dex", "PoE1 Str/Dex Body Armour"),
    "sacrificial garb": ("Body Armour", "Body_Armours_str_dex_int", "PoE1 Atziri Body Armour"),
    "hubris circlet": ("Helmet", "Helmets_int", "PoE1 Intelligence ES Helmet"),
    "lich's circlet": ("Helmet", "Helmets_int", "PoE1 Intelligence ES Helmet"),
    "bone helmet": ("Helmet", "Helmets_str_int", "PoE1 Minion Helmet"),
    "royal burgonet": ("Helmet", "Helmets_str", "PoE1 Strength Helmet"),
    "eternal burgonet": ("Helmet", "Helmets_str", "PoE1 Strength Helmet"),
    "lion pelt": ("Helmet", "Helmets_dex", "PoE1 Dexterity Helmet"),
    "deicide mask": ("Helmet", "Helmets_dex_int", "PoE1 Dex/Int Helmet"),
    "nightmare bascinet": ("Helmet", "Helmets_str_dex", "PoE1 Str/Dex Helmet"),
    "sorcerer boots": ("Boots", "Boots_int", "PoE1 Intelligence ES Boots"),
    "warlock boots": ("Boots", "Boots_int", "PoE1 Intelligence ES Boots"),
    "titan greaves": ("Boots", "Boots_str", "PoE1 Strength Boots"),
    "slink boots": ("Boots", "Boots_dex", "PoE1 Dexterity Boots"),
    "two-toned boots": ("Boots", "Boots_str_int", "PoE1 Atlas Hybrid Boots"),
    "fugitive boots": ("Boots", "Boots_dex_int", "PoE1 Atlas Boots"),
    "sorcerer gloves": ("Gloves", "Gloves_int", "PoE1 Intelligence ES Gloves"),
    "fingerless silk gloves": ("Gloves", "Gloves_int", "PoE1 Spell Damage Gloves"),
    "apothecary's gloves": ("Gloves", "Gloves_str_int", "PoE1 Atlas Gloves"),
    "spiked gloves": ("Gloves", "Gloves_str", "PoE1 Melee Damage Gloves"),
    "titan gauntlets": ("Gloves", "Gloves_str", "PoE1 Strength Gloves"),
    "gripped gloves": ("Gloves", "Gloves_dex", "PoE1 Projectile Gloves"),
    "slink gloves": ("Gloves", "Gloves_dex", "PoE1 Dexterity Gloves"),
    "titanium spirit shield": (
        "Focus",
        "Foci",
        "PoE1 Caster ES Offhand (Foci replace Spirit Shields in PoE2)",
    ),
    "harmonic spirit shield": (
        "Focus",
        "Foci",
        "PoE1 Caster ES Offhand (Foci replace Spirit Shields in PoE2)",
    ),
    "archon kite shield": ("Shield", "Shields_str_int", "PoE1 Hybrid Shield"),
    "colossal tower shield": ("Shield", "Shields_str", "PoE1 Tower Shield"),
    "stygian vise": ("Belt", "Belts", "PoE1 Abyss Socket Belt (Belts roll Charm slots in PoE2)"),
    "crystal belt": ("Belt", "Belts", "PoE1 ES Belt"),
    "leather belt": ("Belt", "Belts", "PoE1 Life Belt (see Rawhide Belt / Fine Belt in PoE2)"),
    "vanguard belt": ("Belt", "Belts", "PoE1 Armour/Evasion Belt"),
    "imbued wand": ("Wand", "Wands", "PoE1 Attack/Caster Wand (PoE2 Wands are pure caster bases)"),
    "prophecy wand": ("Wand", "Wands", "PoE1 Caster Wand"),
    "opal wand": ("Wand", "Wands", "PoE1 Caster Wand"),
    "profane wand": ("Wand", "Wands", "PoE1 Cast Speed Wand"),
    "convoking wand": (
        "Wand",
        "Wands",
        "PoE1 Minion Wand (Minion skills live on Sceptres in PoE2)",
    ),
    "tornado wand": ("Wand", "Wands", "PoE1 Wand"),
    "spine bow": ("Bow", "Bows", "PoE1 Bow"),
    "thicket bow": ("Bow", "Bows", "PoE1 Attack Speed Bow"),
    "imperial bow": ("Bow", "Bows", "PoE1 Elemental Bow"),
    "harbinger bow": ("Bow", "Bows", "PoE1 Crit Bow"),
    "opal ring": ("Ring", "Rings", "PoE1 Elemental Ring"),
    "vermilion ring": ("Ring", "Rings", "PoE1 Life Ring"),
    "cerulean ring": ("Ring", "Rings", "PoE1 Mana Ring"),
    "iolite ring": ("Ring", "Rings", "PoE1 Chaos Damage Ring"),
    "bone ring": ("Ring", "Rings", "PoE1 Minion Ring"),
    "marble amulet": ("Amulet", "Amulets", "PoE1 Life Regen Amulet"),
    "blue pearl amulet": ("Amulet", "Amulets", "PoE1 Mana Regen Amulet"),
    "onyx amulet": ("Amulet", "Amulets", "PoE1 All-Attributes Amulet (Stellar Amulet in PoE2)"),
    "turquoise amulet": ("Amulet", "Amulets", "PoE1 Dex/Int Amulet"),
    "citrine amulet": ("Amulet", "Amulets", "PoE1 Str/Dex Amulet"),
    "agate amulet": ("Amulet", "Amulets", "PoE1 Str/Int Amulet"),
    "broadhead arrow quiver": ("Quiver", "Quivers", "PoE1 Quiver"),
    "feathered arrow quiver": ("Quiver", "Quivers", "PoE1 Quiver"),
    "vile arrow quiver": ("Quiver", "Quivers", "PoE1 Quiver"),
    "gemini claw": ("Claw", "Claws", "PoE1 Claw"),
    "imperial claw": ("Claw", "Claws", "PoE1 Claw"),
    "jewelled foil": ("One Hand Sword", "One_Hand_Swords", "PoE1 Thrusting Sword"),
    "vaalin axe": ("Two Hand Axe", "Two_Hand_Axes", "PoE1 Axe"),
    "vaal axe": ("Two Hand Axe", "Two_Hand_Axes", "PoE1 Two Hand Axe"),
    "despot axe": ("Two Hand Axe", "Two_Hand_Axes", "PoE1 Two Hand Axe"),
}

BASE_NOUN_SLUG_HINTS: tuple[tuple[str, str, str], ...] = (
    ("quarterstaff", "Warstaff", "Quarterstaves"),
    ("warstaff", "Warstaff", "Quarterstaves"),
    ("crossbow", "Crossbow", "Crossbows"),
    ("greathammer", "Two Hand Mace", "Two_Hand_Maces"),
    ("greatclub", "Two Hand Mace", "Two_Hand_Maces"),
    ("maul", "Two Hand Mace", "Two_Hand_Maces"),
    ("sledge", "Two Hand Mace", "Two_Hand_Maces"),
    ("hammer", "One Hand Mace", "One_Hand_Maces"),
    ("mace", "One Hand Mace", "One_Hand_Maces"),
    ("club", "One Hand Mace", "One_Hand_Maces"),
    ("sceptre", "Sceptre", "Sceptres"),
    ("scepter", "Sceptre", "Sceptres"),
    ("talisman", "Talisman", "Talismans"),
    ("spear", "Spear", "Spears"),
    ("flail", "Flail", "Flails"),
    ("wand", "Wand", "Wands"),
    ("staff", "Staff", "Staves"),
    ("bow", "Bow", "Bows"),
    ("quiver", "Quiver", "Quivers"),
    ("focus", "Focus", "Foci"),
    ("buckler", "Buckler", "Bucklers"),
    ("targe", "Shield", "Shields_str_dex"),
    ("crest shield", "Shield", "Shields_str_int"),
    ("tower shield", "Shield", "Shields_str"),
    ("shield", "Shield", "Shields_str"),
    ("raiment", "Body Armour", "Body_Armours_int"),
    ("robe", "Body Armour", "Body_Armours_int"),
    ("regalia", "Body Armour", "Body_Armours_int"),
    ("cuirass", "Body Armour", "Body_Armours_str"),
    ("plate", "Body Armour", "Body_Armours_str"),
    ("coat", "Body Armour", "Body_Armours_dex"),
    ("jacket", "Body Armour", "Body_Armours_dex"),
    ("vest", "Body Armour", "Body_Armours_dex"),
    ("garb", "Body Armour", "Body_Armours_dex_int"),
    ("mantle", "Body Armour", "Body_Armours_str_int"),
    ("chainmail", "Body Armour", "Body_Armours_str_int"),
    ("mail", "Body Armour", "Body_Armours_str_dex"),
    ("tiara", "Helmet", "Helmets_int"),
    ("circlet", "Helmet", "Helmets_int"),
    ("crown", "Helmet", "Helmets_str_int"),
    ("mask", "Helmet", "Helmets_dex_int"),
    ("cap", "Helmet", "Helmets_dex"),
    ("helm", "Helmet", "Helmets_str"),
    ("helmet", "Helmet", "Helmets_str"),
    ("sandals", "Boots", "Boots_int"),
    ("leggings", "Boots", "Boots_str_int"),
    ("shoes", "Boots", "Boots_dex_int"),
    ("sabatons", "Boots", "Boots_str_dex"),
    ("greaves", "Boots", "Boots_str"),
    ("boots", "Boots", "Boots_dex"),
    ("cuffs", "Gloves", "Gloves_str_int"),
    ("wraps", "Gloves", "Gloves_dex_int"),
    ("bracers", "Gloves", "Gloves_dex"),
    ("gauntlets", "Gloves", "Gloves_str"),
    ("mitts", "Gloves", "Gloves_str"),
    ("gloves", "Gloves", "Gloves_int"),
    ("amulet", "Amulet", "Amulets"),
    ("ring", "Ring", "Rings"),
    ("signet", "Ring", "Rings"),
    ("belt", "Belt", "Belts"),
    ("vise", "Belt", "Belts"),
    ("jewel", "Jewel", "Ruby"),
    ("tablet", "TowerAugmentation", "Breach_Tablet"),
)

GEM_GENERIC_TAGS: frozenset[str] = frozenset(
    {
        "grants_active_skill",
        "support",
        "lineage",
        "strength",
        "dexterity",
        "intelligence",
        "buff",
        "persistent",
        "conditional",
    }
)


class KeywordEntry(Poe2Model):
    id: str
    term: str
    category: str
    definition: str


class MechanicExplanation(Poe2Model):
    topic: str
    patch: str = POE2_PATCH
    facts: list[MechanicFact] = Field(default_factory=list)
    keywords: list[KeywordEntry] = Field(default_factory=list)
    trap_warnings: list[str] = Field(default_factory=list)


class GemInspection(GemSummary):
    color: str | None = None
    req_weights: dict[str, int] = Field(default_factory=dict)
    tier_variants: list[str] = Field(default_factory=list)
    copy_limit_rule: str = ""
    spirit_energy_rule: str | None = None
    recommended_supports: list[str] = Field(default_factory=list)
    compatible_gems: list[str] = Field(default_factory=list)
    resolved_from: str | None = None
    warning: str | None = None


class TopAffixSummary(Poe2Model):
    mod_id: str
    mod_type: str
    family_id: str
    affix_side: str
    affix_name: str
    display_tier: int
    required_level: int
    weight: int
    stat_text: str


class BaseItemInspection(BaseItemSummary):
    found: bool = True
    ilvl: int = 82
    is_poe1_base_trap: bool = False
    is_stale_prefix_trap: bool = False
    warning: str | None = None
    suggested_bases: list[str] = Field(default_factory=list)
    matching_bases: list[str] = Field(default_factory=list)
    top_prefixes: list[TopAffixSummary] = Field(default_factory=list)
    top_suffixes: list[TopAffixSummary] = Field(default_factory=list)


class UniqueInspection(UniqueSummary):
    slot_category: str = ""
    req_level: int | None = None
    is_live_poe2: bool = True
    has_variants: bool = False


class AugmentSummary(Poe2Model):
    id: str
    name: str
    augment_type: str
    required_level: int = 1
    limit_count: int | None = None
    is_socket_bound: bool = False
    can_overwrite_in_socket: bool = True
    slot_effects: dict[str, list[str]] = Field(default_factory=dict)
    shaman_bonded_effects: dict[str, list[str]] = Field(default_factory=dict)
    description: str = ""
    is_live_poe2: bool = True


class RuneforgeRecipeSummary(Poe2Model):
    recipe_id: str
    category: str
    output_item: str
    source_base: str | None = None
    rune_sequence_ids: list[int] = Field(default_factory=list)
    result_implicits: list[str] = Field(default_factory=list)
    result_properties: dict[str, Any] = Field(default_factory=dict)


def extract_query_tokens(query: str) -> list[str]:
    normalized = query.replace("\u2019", "'").replace("\u2018", "'")
    depossessed = POSSESSIVE_RE.sub("", normalized)
    raw_tokens = TOKEN_RE.findall(depossessed)
    expanded: list[str] = []
    for tok in raw_tokens:
        low = tok.lower()
        if low in TOPIC_ABBREVIATIONS and len(raw_tokens) <= 3:
            expanded.extend(TOKEN_RE.findall(TOPIC_ABBREVIATIONS[low]))
        else:
            expanded.append(tok)
    return expanded


def sanitize_fts5_query(query: str, *, operator: str = "AND") -> str:
    """Convert arbitrary natural-language text into a safe SQLite FTS5 MATCH expression."""
    op = "OR" if operator.upper() == "OR" else "AND"
    tokens = extract_query_tokens(query)
    if not tokens:
        return ""

    filtered = [t for t in tokens if t.lower() not in FTS_STOPWORDS]
    active_tokens = filtered if filtered else tokens

    clauses: list[str] = []
    seen: set[str] = set()
    for tok in active_tokens:
        low = tok.lower()
        if low in seen:
            continue
        seen.add(low)
        alt = SPELLING_SYNONYMS.get(low)
        if alt and alt != low:
            seen.add(alt)
            clauses.append(f'("{low}" OR "{alt}")')
        else:
            clauses.append(f'"{low}"')

    return f" {op} ".join(clauses)


def _clean_unique_lines(lines: list[str]) -> tuple[list[str], bool]:
    cleaned: list[str] = []
    has_variants = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if any(s.startswith(prefix) for prefix in POB_UNIQUE_META_PREFIXES):
            has_variants = True
            continue
        cleaned.append(s)
    return cleaned, has_variants


def _truncate_text(text: str, max_chars: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


class KnowledgeService:
    """PoE2 Patch 0.5.5 anti-hallucination game knowledge engine."""

    def __init__(
        self,
        db: GamedataDB | Path | str | None = None,
        *,
        meta_service: MetaService | None = None,
    ) -> None:
        if isinstance(db, GamedataDB):
            self.db = db
        else:
            self.db = GamedataDB(db)
        self._meta_service: MetaService | None = meta_service
        self._meta_unavailable: bool = False

    def _get_meta_service(self) -> MetaService | None:
        if self._meta_service is not None:
            return self._meta_service
        if self._meta_unavailable:
            return None
        try:
            svc = MetaService()
            svc._ensure_tree_loaded()
            self._meta_service = svc
            return svc
        except (FileNotFoundError, OSError, ValueError):
            self._meta_unavailable = True
            return None

    def list_classes(self) -> list[ClassSummary]:
        svc = self._get_meta_service()
        if svc is None:
            return []
        return svc.list_classes()

    def list_ascendancies(self, class_name: str | None = None) -> list[AscendancySummary]:
        svc = self._get_meta_service()
        if svc is None:
            return []
        return svc.list_ascendancies(class_name=class_name)

    def inspect_ascendancy(self, name_or_class: str) -> AscendancyInspection:
        svc = self._get_meta_service()
        if svc is None:
            svc = MetaService()
        return svc.inspect_ascendancy(name_or_class)

    def health_check(self) -> GamedataHealth:
        return self.db.health_check()

    def _resolve_category_filter(self, category: str | None) -> tuple[str | None, str | None]:
        if not category:
            return None, None
        norm = category.strip().lower()
        if not norm:
            return None, None
        if norm in CATEGORY_DOC_TYPE_ALIASES:
            return CATEGORY_DOC_TYPE_ALIASES[norm], None
        if norm == "defense":
            norm = "defence"
        return None, norm

    def _touches_mechanics(self, query: str, tokens: list[str]) -> bool:
        if "%" in query or "=" in query:
            return True
        return any(t.lower() in MECHANIC_HINT_TERMS for t in tokens)

    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 12,
    ) -> list[KnowledgeRecord]:
        if limit <= 0:
            return []
        query = translate_zh_query(query)
        q_raw = query.strip()
        if not q_raw:
            return []

        tokens = extract_query_tokens(q_raw)
        and_expr = sanitize_fts5_query(q_raw, operator="AND")
        or_expr = sanitize_fts5_query(q_raw, operator="OR")
        if not and_expr and not or_expr:
            return []

        doc_type_filter, subcat_filter = self._resolve_category_filter(category)
        mech_query = self._touches_mechanics(q_raw, tokens)
        q_norm = POSSESSIVE_RE.sub("", q_raw.replace("\u2019", "'")).strip().lower()

        scored: dict[tuple[str, str], tuple[float, str, str]] = {}

        def _record_hit(doc_type: str, doc_id: str, title: str, cat: str, pts: float) -> None:
            if doc_type_filter and doc_type != doc_type_filter:
                return
            if subcat_filter and cat.lower() != subcat_filter and doc_type != subcat_filter:
                return
            key = (doc_type, doc_id)
            prev = scored.get(key)
            if prev is None or pts > prev[0]:
                scored[key] = (pts, title, cat)

        if doc_type_filter not in ("runeforge_recipe", "ascendancy", "class"):
            where_parts = ["knowledge_fts MATCH ?"]
            base_params: list[Any] = []
            if doc_type_filter:
                where_parts.append("doc_type = ?")
                base_params.append(doc_type_filter)
            if subcat_filter:
                where_parts.append("lower(category) = ?")
                base_params.append(subcat_filter)
            where_sql = " AND ".join(where_parts)
            fetch_cap = max(limit * 4, 32)

            if and_expr:
                rows_and = self.db.query_all(
                    f"""
                    SELECT doc_type, doc_id, title, category,
                           bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.5, 1.0) AS rank_score
                    FROM knowledge_fts
                    WHERE {where_sql}
                    ORDER BY rank_score ASC
                    LIMIT ?
                    """,
                    [and_expr, *base_params, fetch_cap],
                )
                for row in rows_and:
                    pts = max(0.5, -float(row["rank_score"])) + 12.0
                    _record_hit(
                        str(row["doc_type"]),
                        str(row["doc_id"]),
                        str(row["title"]),
                        str(row["category"]),
                        pts,
                    )

            if or_expr and (len(scored) < limit or len(tokens) >= 2):
                rows_or = self.db.query_all(
                    f"""
                    SELECT doc_type, doc_id, title, category,
                           bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.5, 1.0) AS rank_score
                    FROM knowledge_fts
                    WHERE {where_sql}
                    ORDER BY rank_score ASC
                    LIMIT ?
                    """,
                    [or_expr, *base_params, fetch_cap],
                )
                for row in rows_or:
                    pts = max(0.2, -float(row["rank_score"]) * 0.65)
                    _record_hit(
                        str(row["doc_type"]),
                        str(row["doc_id"]),
                        str(row["title"]),
                        str(row["category"]),
                        pts,
                    )

        # Direct exact/prefix name boost across tables + runeforge_recipes & non-FTS legacy levers
        if len(q_norm) >= 2:
            like_prefix = f"{q_norm}%"
            like_sub = f"%{q_norm}%"
            exact_queries: list[tuple[str, str, tuple[Any, ...]]] = [
                (
                    "mechanic_fact",
                    "SELECT slug AS id, title, category FROM mechanics_facts "
                    "WHERE lower(slug) = ? OR lower(title) LIKE ?",
                    (q_norm.replace(" ", "_"), like_sub),
                ),
                (
                    "keyword",
                    "SELECT id, term AS title, category FROM keywords "
                    "WHERE lower(term) = ? OR lower(term) LIKE ? LIMIT 12",
                    (q_norm, like_prefix),
                ),
                (
                    "gem",
                    "SELECT id, name AS title, gem_kind AS category FROM gems "
                    "WHERE is_live_poe2 = 1 AND (lower(name) = ? OR lower(name) LIKE ?) LIMIT 12",
                    (q_norm, like_prefix),
                ),
                (
                    "unique",
                    "SELECT id, name AS title, slot_category AS category FROM uniques "
                    "WHERE lower(name) = ? OR lower(name) LIKE ? LIMIT 12",
                    (q_norm, like_prefix),
                ),
                (
                    "base_item",
                    "SELECT id, name AS title, item_class AS category FROM base_items "
                    "WHERE is_live_poe2 = 1 AND (lower(name) = ? OR lower(name) LIKE ?) LIMIT 12",
                    (q_norm, like_prefix),
                ),
                (
                    "augment",
                    "SELECT id, name AS title, augment_type AS category FROM augments "
                    "WHERE lower(name) = ? OR lower(name) LIKE ? LIMIT 12",
                    (q_norm, like_prefix),
                ),
                (
                    "crafting_lever",
                    "SELECT lever_id AS id, name AS title, category FROM crafting_levers "
                    "WHERE lower(name) = ? OR lower(name) LIKE ? LIMIT 12",
                    (q_norm, like_sub),
                ),
            ]
            for dtype, sql, params in exact_queries:
                if doc_type_filter and doc_type_filter != dtype:
                    continue
                for r in self.db.query_all(sql, params):
                    t_str = str(r["title"])
                    t_low = POSSESSIVE_RE.sub("", t_str).lower()
                    if t_low == q_norm:
                        bonus = 45.0
                    elif t_low.startswith(q_norm):
                        bonus = 26.0
                    else:
                        bonus = 15.0
                    key = (dtype, str(r["id"]))
                    existing = scored.get(key)
                    base_pts = existing[0] if existing else 8.0
                    _record_hit(dtype, str(r["id"]), t_str, str(r["category"]), base_pts + bonus)

        # Include runeforge_recipes (882 Verisium Anvil & Kalguuran Remnant recipes)
        if doc_type_filter in (None, "runeforge_recipe"):
            rf_noise = {"alloy", "remnant", "recipe"}
            rf_terms = [
                t.lower()
                for t in tokens
                if t.lower() not in FTS_STOPWORDS and t.lower() not in rf_noise
            ]
            if (
                doc_type_filter == "runeforge_recipe"
                or any(
                    w in q_norm
                    for w in ("runeforge", "runemaster", "verisium", "remnant", "kalguur")
                )
                or rf_terms
            ):
                probe = rf_terms[0] if rf_terms else q_norm
                if len(probe) >= 3:
                    rf_rows = self.db.query_all(
                        """
                        SELECT recipe_id, output_item, category, inputs_json
                        FROM runeforge_recipes
                        WHERE lower(output_item) LIKE ? OR lower(inputs_json) LIKE ?
                        LIMIT 16
                        """,
                        (f"%{probe}%", f"%{probe}%"),
                    )
                    for r in rf_rows:
                        hay = f"{r['output_item']} {r['inputs_json']}".lower()
                        if rf_terms and not all(t in hay for t in rf_terms[:3]):
                            continue
                        out_low = str(r["output_item"]).lower()
                        pts = 32.0 if out_low == q_norm else (20.0 if q_norm in out_low else 11.0)
                        _record_hit(
                            "runeforge_recipe",
                            str(r["recipe_id"]),
                            str(r["output_item"]),
                            str(r["category"]),
                            pts,
                        )

        # Include explicit +N level modifier tier lookups when query contains "+<N>"
        plus_match = PLUS_LEVEL_RE.search(q_raw)
        if plus_match and doc_type_filter is None and subcat_filter in (None, "crafting"):
            plus_num = plus_match.group(1)
            extra_terms = [
                t.lower() for t in tokens if t != plus_num and t.lower() not in FTS_STOPWORDS
            ]
            mt_rows = self.db.query_all(
                """
                SELECT row_id, base_group_slug, mod_type, affix_side, display_tier,
                       required_level, weight, stat_text
                FROM mod_tiers
                WHERE source_pool = 'normal' AND display_tier = 1 AND stat_text LIKE ?
                ORDER BY required_level DESC, weight ASC
                LIMIT 40
                """,
                (f"%+{plus_num}%",),
            )
            seen_stat: set[str] = set()
            for r in mt_rows:
                st = str(r["stat_text"])
                hay = f"{st} {r['base_group_slug']} {r['mod_type']}".lower()
                if extra_terms and not all(t in hay for t in extra_terms):
                    continue
                dedup_key = f"{r['base_group_slug']}:{st}"
                if dedup_key in seen_stat:
                    continue
                seen_stat.add(dedup_key)
                _record_hit(
                    "mod_tier",
                    str(r["row_id"]),
                    f"{st} ({r['base_group_slug']})",
                    "crafting",
                    28.0,
                )
                if len(seen_stat) >= 4:
                    break

        # Include PoE2 0.5.5 8 Classes & 23 Ascendancies (+ notable names) from MetaService
        if doc_type_filter in (None, "ascendancy", "class"):
            meta_svc = self._get_meta_service()
            if meta_svc is not None:
                search_terms = [
                    t.lower() for t in tokens if t.lower() not in FTS_STOPWORDS and len(t) >= 2
                ]
                compact_q = re.sub(r"[^a-z0-9]+", "", q_norm)
                alias_asc = ASCENDANCY_SHORTHAND_ALIASES.get(compact_q)

                if doc_type_filter in (None, "class"):
                    for cls_obj in meta_svc.list_classes():
                        c_low = cls_obj.name.lower()
                        if c_low == q_norm:
                            _record_hit("class", cls_obj.name, cls_obj.name, "class", 55.0)
                        elif len(q_norm) >= 3 and c_low.startswith(q_norm):
                            _record_hit("class", cls_obj.name, cls_obj.name, "class", 28.0)

                if doc_type_filter in (None, "ascendancy"):
                    for asc_name, detail in meta_svc._asc_details_by_name.items():
                        a_low = POSSESSIVE_RE.sub("", asc_name).lower()
                        cls_low = detail.class_name.lower()
                        pts_asc = 0.0
                        if a_low == q_norm or asc_name == alias_asc:
                            pts_asc = 62.0
                        elif len(q_norm) >= 3 and a_low.startswith(q_norm):
                            pts_asc = 38.0
                        elif cls_low == q_norm:
                            pts_asc = 26.0
                        elif search_terms:
                            notable_lows = [
                                POSSESSIVE_RE.sub("", n.name).lower() for n in detail.notables
                            ]
                            if any(nl == q_norm for nl in notable_lows):
                                pts_asc = 46.0
                            else:
                                hay = (
                                    f"{a_low} {cls_low} {' '.join(notable_lows)} "
                                    f"{detail.archetype_summary.lower()}"
                                )
                                if all(st in hay for st in search_terms):
                                    has_name_hit = any(st in a_low for st in search_terms)
                                    pts_asc = 30.0 if has_name_hit else 19.0
                        if pts_asc > 0.0:
                            _record_hit(
                                "ascendancy",
                                detail.name,
                                detail.name,
                                detail.class_name,
                                pts_asc,
                            )

        # Apply exact/prefix adjustments and mechanics_facts anti-hallucination boost
        final_candidates: list[tuple[float, str, str]] = []
        seen_titles_by_type: set[tuple[str, str]] = set()
        for (dtype, did), (pts, title, _cat) in scored.items():
            t_clean = POSSESSIVE_RE.sub("", title.replace("\u2019", "'")).lower()
            if t_clean == q_norm:
                pts += 30.0
            elif q_norm and t_clean.startswith(q_norm):
                pts += 12.0

            if dtype == "mechanic_fact":
                pts += 16.0 if mech_query else 7.0

            final_candidates.append((pts, dtype, did))

        final_candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

        records: list[KnowledgeRecord] = []
        for pts, dtype, did in final_candidates:
            rec = self._hydrate_record(dtype, did, round(pts, 3))
            if rec is None:
                continue
            dedup = (rec.kind, rec.title.lower())
            if rec.kind in ("gem", "base_item") and dedup in seen_titles_by_type:
                continue
            seen_titles_by_type.add(dedup)
            records.append(rec)
            if len(records) >= limit:
                break

        return records

    def _hydrate_record(
        self,
        doc_type: str,
        doc_id: str,
        score: float,
    ) -> KnowledgeRecord | None:
        if doc_type == "mechanic_fact":
            row = self.db.query_one("SELECT * FROM mechanics_facts WHERE slug = ?", (doc_id,))
            if row is None:
                return None
            formula = f" | Formula: {row['formula_expr']}" if row["formula_expr"] else ""
            summary = _truncate_text(
                f"LIVE 0.5.5: {row['poe2_live_rule']}{formula} | "
                f"AVOID TRAP: {row['poe1_or_stale_trap']}",
                380,
            )
            return KnowledgeRecord(
                id=str(row["slug"]),
                kind="mechanic_fact",
                title=str(row["title"]),
                summary=summary,
                category=str(row["category"]),
                patch=str(row["patch_verified"]),
                score=score,
                source_ref=str(row["source_ref"]),
                metadata={
                    "poe1_trap": str(row["poe1_or_stale_trap"]),
                    "formula_expr": row["formula_expr"],
                },
            )

        if doc_type == "keyword":
            row = self.db.query_one("SELECT * FROM keywords WHERE id = ?", (doc_id,))
            if row is None:
                return None
            return KnowledgeRecord(
                id=str(row["id"]),
                kind="keyword",
                title=str(row["term"]),
                summary=_truncate_text(str(row["definition_clean"]), 280),
                category=str(row["category"]),
                score=score,
                source_ref=f"GGG KeywordPopups[{row['id']}]",
            )

        if doc_type == "gem":
            row = self.db.query_one("SELECT * FROM gems WHERE id = ?", (doc_id,))
            if row is None:
                return None
            siblings = self.db.query_all(
                "SELECT * FROM gems WHERE name = ? AND is_live_poe2 = 1",
                (row["name"],),
            )
            merged = self._merge_gem_rows(siblings or [row])
            copy_note = (
                "max 1/char (Lineage)"
                if merged.max_copies_per_char == 1
                else "unlimited copies across skills (0.3+)"
                if merged.gem_kind == "support"
                else merged.gem_kind
            )
            tier_note = f", Tier {merged.support_tier}" if merged.support_tier else ""
            tags_str = ", ".join(merged.tags[:6])
            return KnowledgeRecord(
                id=merged.id,
                kind="gem",
                title=merged.name,
                summary=f"{merged.gem_kind}{tier_note} ({copy_note}) | tags: {tags_str or 'none'}",
                category=merged.gem_kind,
                score=score,
                metadata={
                    "gem_kind": merged.gem_kind,
                    "support_tier": merged.support_tier,
                    "max_copies_per_char": merged.max_copies_per_char,
                },
            )

        if doc_type == "unique":
            row = self.db.query_one("SELECT * FROM uniques WHERE id = ?", (doc_id,))
            if row is None:
                return None
            exps, _ = _clean_unique_lines(json.loads(row["explicits_json"]))
            imps = json.loads(row["implicits_json"])
            summary = _truncate_text(
                f"{row['base_type']} ({row['slot_category']}): " + "; ".join([*imps, *exps[:4]]),
                280,
            )
            return KnowledgeRecord(
                id=str(row["id"]),
                kind="unique",
                title=str(row["name"]),
                summary=summary,
                category=str(row["slot_category"]),
                score=score,
                metadata={
                    "base_type": str(row["base_type"]),
                    "is_live_poe2": bool(row["is_live_poe2"]),
                },
            )

        if doc_type == "base_item":
            row = self.db.query_one(
                "SELECT * FROM base_items WHERE id = ? AND is_live_poe2 = 1",
                (doc_id,),
            )
            if row is None:
                return None
            imps = json.loads(row["implicits_json"])
            imp_str = f" | implicit: {'; '.join(imps)}" if imps else ""
            return KnowledgeRecord(
                id=str(row["id"]),
                kind="base_item",
                title=str(row["name"]),
                summary=(
                    f"{row['item_class']} (drop lvl {row['drop_level']}, "
                    f"slug={row['poe2db_slug']}){imp_str}"
                ),
                category=str(row["item_class"]),
                score=score,
                metadata={
                    "poe2db_slug": row["poe2db_slug"],
                    "drop_level": int(row["drop_level"]),
                },
            )

        if doc_type == "augment":
            row = self.db.query_one("SELECT * FROM augments WHERE id = ?", (doc_id,))
            if row is None:
                return None
            slots: dict[str, list[str]] = json.loads(row["slot_effects_json"])
            bonded: dict[str, list[str]] = json.loads(row["bonded_effects_json"])
            slot_preview = "; ".join(f"{k}: {', '.join(v)}" for k, v in list(slots.items())[:3])
            bonded_note = " [+Shaman Bonded]" if bonded else ""
            bound_note = " [Socket-bound]" if row["is_socket_bound"] else ""
            return KnowledgeRecord(
                id=str(row["id"]),
                kind="augment",
                title=str(row["name"]),
                summary=_truncate_text(
                    f"{row['augment_type']} (lvl {row['required_level']})"
                    f"{bound_note}{bonded_note}: {slot_preview}",
                    280,
                ),
                category=str(row["augment_type"]),
                score=score,
                metadata={
                    "is_socket_bound": bool(row["is_socket_bound"]),
                    "limit_count": row["limit_count"],
                },
            )

        if doc_type == "crafting_lever":
            row = self.db.query_one(
                "SELECT * FROM crafting_levers WHERE lever_id = ?",
                (doc_id,),
            )
            if row is None:
                return None
            active = bool(row["is_active_drop_in_patch"])
            badge = "ACTIVE 0.5.5" if active else "REMOVED/LEGACY IN 0.5.5"
            floor_str = (
                f", min_mod_lvl={row['min_mod_level_floor']}"
                if row["min_mod_level_floor"] is not None
                else ""
            )
            max_str = (
                f", max_ilvl={row['max_item_level']}" if row["max_item_level"] is not None else ""
            )
            return KnowledgeRecord(
                id=str(row["lever_id"]),
                kind="crafting_lever",
                title=str(row["name"]),
                summary=_truncate_text(
                    f"[{badge}] {row['category']} ({row['applies_to_rarities']}"
                    f"{floor_str}{max_str}): {row['description']}",
                    280,
                ),
                category=str(row["category"]),
                score=score,
                metadata={
                    "is_active_drop_in_patch": active,
                    "min_mod_level_floor": row["min_mod_level_floor"],
                    "max_item_level": row["max_item_level"],
                },
            )

        if doc_type == "runeforge_recipe":
            row = self.db.query_one(
                "SELECT * FROM runeforge_recipes WHERE recipe_id = ?",
                (doc_id,),
            )
            if row is None:
                return None
            rf = self._row_to_runeforge(row)
            if rf.source_base:
                imp_part = (
                    f" -> implicits: {'; '.join(rf.result_implicits)}"
                    if rf.result_implicits
                    else ""
                )
                summary = f"Verisium Anvil: {rf.source_base} -> {rf.output_item}{imp_part}"
            else:
                summary = (
                    f"Kalguuran Remnant ({rf.output_item}): rune sequence {rf.rune_sequence_ids}"
                )
            return KnowledgeRecord(
                id=rf.recipe_id,
                kind="runeforge_recipe",
                title=rf.output_item,
                summary=_truncate_text(summary, 260),
                category=rf.category,
                score=score,
                metadata={"source_base": rf.source_base},
            )

        if doc_type == "mod_tier":
            row = self.db.query_one("SELECT * FROM mod_tiers WHERE row_id = ?", (doc_id,))
            if row is None:
                return None
            return KnowledgeRecord(
                id=str(row["row_id"]),
                kind="mod_tier",
                title=f"{row['stat_text']} ({row['base_group_slug']})",
                summary=(
                    f"Tier {row['display_tier']} {row['affix_side']} on {row['base_group_slug']} "
                    f"(req ilvl {row['required_level']}, weight {row['weight']}, "
                    f"family={row['family_id']})"
                ),
                category="crafting",
                score=score,
                metadata={
                    "base_group_slug": str(row["base_group_slug"]),
                    "display_tier": int(row["display_tier"]),
                    "required_level": int(row["required_level"]),
                    "weight": int(row["weight"]),
                },
            )

        if doc_type == "ascendancy":
            meta_svc = self._get_meta_service()
            if meta_svc is None or doc_id not in meta_svc._asc_details_by_name:
                return None
            detail = meta_svc._asc_details_by_name[doc_id]
            notable_preview = ", ".join(detail.notable_names[:6])
            summary = _truncate_text(
                f"{detail.class_name} Ascendancy ({detail.notable_count} notables): "
                f"{detail.archetype_summary} | Notables: {notable_preview}",
                320,
            )
            return KnowledgeRecord(
                id=detail.internal_id or detail.name,
                kind="ascendancy",
                title=detail.name,
                summary=summary,
                category=detail.class_name,
                score=score,
                source_ref="PoB2 TreeData/0_5",
                metadata={
                    "class_name": detail.class_name,
                    "notable_count": detail.notable_count,
                    "notable_names": detail.notable_names,
                    "granted_skills": detail.granted_skills,
                    "replaces_ascendancy": detail.replaces_ascendancy,
                },
            )

        if doc_type == "class":
            meta_svc = self._get_meta_service()
            if meta_svc is None or doc_id not in meta_svc._classes_by_name:
                return None
            cls_obj = meta_svc._classes_by_name[doc_id]
            asc_names = [a.name for a in cls_obj.ascendancies]
            summary = (
                f"Playable PoE2 0.5.5 Class "
                f"(Str {cls_obj.base_str} / Dex {cls_obj.base_dex} / Int {cls_obj.base_int}) — "
                f"{cls_obj.ascendancy_count} Ascendancies: {', '.join(asc_names)}"
            )
            return KnowledgeRecord(
                id=f"class:{cls_obj.name.lower()}",
                kind="class",
                title=cls_obj.name,
                summary=_truncate_text(summary, 280),
                category="class",
                score=score,
                source_ref="PoB2 TreeData/0_5",
                metadata={
                    "class_id": cls_obj.class_id,
                    "ascendancies": asc_names,
                },
            )

        return None

    def explain_mechanic(self, topic: str, limit: int = 6) -> MechanicExplanation:
        q_raw = translate_zh_query(topic.strip())
        if not q_raw:
            return MechanicExplanation(topic=topic)

        low_raw = q_raw.lower()
        expanded_topic = TOPIC_ABBREVIATIONS.get(low_raw, q_raw)
        and_expr = sanitize_fts5_query(expanded_topic, operator="AND")
        or_expr = sanitize_fts5_query(expanded_topic, operator="OR")

        fact_scores: dict[str, float] = {}

        # Exact slug/category/tag substring checks across all 49 mechanics_facts
        all_facts = self.db.query_all("SELECT * FROM mechanics_facts")
        tokens = [
            t.lower()
            for t in extract_query_tokens(expanded_topic)
            if t.lower() not in FTS_STOPWORDS
        ]
        if not tokens:
            tokens = [t.lower() for t in extract_query_tokens(expanded_topic)]

        expanded_tokens: list[set[str]] = []
        for tok in tokens:
            alts = {tok}
            if tok in SPELLING_SYNONYMS:
                alts.add(SPELLING_SYNONYMS[tok])
            # Simple stem prefix for plurals/verb endings (e.g. "converts" -> "convert")
            if len(tok) >= 5:
                alts.add(tok[:5])
            expanded_tokens.append(alts)

        for row in all_facts:
            slug = str(row["slug"])
            title = str(row["title"]).lower()
            tags = [str(x).lower() for x in json.loads(row["tags_json"])]
            hay = (
                f"{slug} {title} {' '.join(tags)} "
                f"{row['poe2_live_rule']} {row['poe1_or_stale_trap']}"
            ).lower()
            pts = 0.0
            if slug == low_raw.replace(" ", "_"):
                pts += 60.0
            if low_raw in title or any(low_raw == tg for tg in tags):
                pts += 35.0
            matched_groups = sum(1 for grp in expanded_tokens if any(a in hay for a in grp))
            if expanded_tokens and matched_groups == len(expanded_tokens):
                pts += 22.0 + matched_groups * 4.0
            elif matched_groups > 0:
                pts += matched_groups * 5.0
            if pts > 0:
                fact_scores[slug] = pts

        for expr, weight in ((and_expr, 25.0), (or_expr, 8.0)):
            if not expr:
                continue
            fts_rows = self.db.query_all(
                """
                SELECT doc_id, bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.5, 1.5) AS rscore
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ? AND doc_type = 'mechanic_fact'
                ORDER BY rscore ASC
                LIMIT ?
                """,
                (expr, max(limit * 2, 10)),
            )
            for r in fts_rows:
                slug = str(r["doc_id"])
                add_pts = max(1.0, -float(r["rscore"])) + weight
                fact_scores[slug] = fact_scores.get(slug, 0.0) + add_pts

        ranked_slugs = sorted(fact_scores.keys(), key=lambda s: -fact_scores[s])[:limit]
        fact_by_slug = {str(r["slug"]): r for r in all_facts}

        facts: list[MechanicFact] = []
        trap_warnings: list[str] = []
        for slug in ranked_slugs:
            r = fact_by_slug[slug]
            mf = MechanicFact(
                slug=slug,
                title=str(r["title"]),
                category=str(r["category"]),
                poe1_trap=str(r["poe1_or_stale_trap"]),
                poe2_live_rule=str(r["poe2_live_rule"]),
                formula_expr=r["formula_expr"],
                source_ref=str(r["source_ref"]),
                patch_verified=str(r["patch_verified"]),
            )
            facts.append(mf)
            trap_warnings.append(f"[{mf.slug}] AVOID TRAP: {mf.poe1_trap}")

        # Related official GGG KeywordPopups
        kw_scores: dict[str, tuple[float, sqlite3.Row]] = {}
        exact_kw = self.db.query_all(
            "SELECT * FROM keywords WHERE lower(term) = ? OR lower(id) = ? LIMIT 4",
            (expanded_topic.lower(), expanded_topic.lower()),
        )
        for r in exact_kw:
            kw_scores[str(r["id"])] = (50.0, r)

        for expr, bonus in ((and_expr, 15.0), (or_expr, 4.0)):
            if not expr or len(kw_scores) >= limit:
                continue
            kw_fts = self.db.query_all(
                """
                SELECT doc_id, bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.0, 1.0) AS rscore
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ? AND doc_type = 'keyword'
                ORDER BY rscore ASC
                LIMIT ?
                """,
                (expr, limit),
            )
            for hit in kw_fts:
                kid = str(hit["doc_id"])
                if kid in kw_scores:
                    continue
                krow = self.db.query_one("SELECT * FROM keywords WHERE id = ?", (kid,))
                if krow is not None:
                    kw_scores[kid] = (max(0.5, -float(hit["rscore"])) + bonus, krow)

        sorted_kws = sorted(kw_scores.values(), key=lambda x: -x[0])[:limit]
        keywords = [
            KeywordEntry(
                id=str(kr["id"]),
                term=str(kr["term"]),
                category=str(kr["category"]),
                definition=_truncate_text(str(kr["definition_clean"]), 360),
            )
            for _sc, kr in sorted_kws
        ]

        return MechanicExplanation(
            topic=q_raw,
            facts=facts,
            keywords=keywords,
            trap_warnings=trap_warnings,
        )

    def _merge_gem_rows(
        self,
        rows: list[sqlite3.Row],
        *,
        resolved_from: str | None = None,
    ) -> GemInspection:
        non_unique = [
            r
            for r in rows
            if "Unique" not in str(r["id"]) and not str(r["id"]).endswith("SandPlayer")
        ]
        candidates = non_unique if non_unique else rows

        kind_priority = {
            "lineage_support": 5,
            "support": 4,
            "meta": 3,
            "spirit": 2,
            "active": 1,
        }

        def _row_rank(r: sqlite3.Row) -> tuple[int, int, int]:
            has_tags = 1 if str(r["tags_json"]) != "[]" else 0
            is_pob_id = 1 if "/Gems/" in str(r["id"]) else 0
            return (has_tags, kind_priority.get(str(r["gem_kind"]), 0), is_pob_id)

        primary = max(candidates, key=_row_rank)
        name = str(primary["name"])
        gem_kind = str(primary["gem_kind"])

        tags: list[str] = []
        req_weights: dict[str, int] = {}
        rec_support_ids: list[str] = []
        color: str | None = None
        support_tier: int | None = primary["support_tier"]
        max_copies: int | None = primary["max_copies_per_char"]

        for r in candidates:
            for tg in json.loads(r["tags_json"]):
                if tg not in tags:
                    tags.append(str(tg))
            rw = json.loads(r["req_weights_json"])
            if rw and not req_weights:
                req_weights = {str(k): int(v) for k, v in rw.items()}
            for sid in json.loads(r["recommended_supports_json"]):
                if sid not in rec_support_ids and "Unknown" not in str(sid):
                    rec_support_ids.append(str(sid))
            if color is None and r["color"]:
                color = str(r["color"])
            if support_tier is None and r["support_tier"] is not None:
                support_tier = int(r["support_tier"])
            if max_copies is None and r["max_copies_per_char"] is not None:
                max_copies = int(r["max_copies_per_char"])

        if gem_kind == "lineage_support":
            max_copies = 1

        # Discover tiered I/II/III family variants if applicable
        base_stem = ROMAN_SUFFIX_RE.sub("", name)
        variant_rows = self.db.query_all(
            """
            SELECT DISTINCT name, support_tier FROM gems
            WHERE is_live_poe2 = 1 AND (name = ? OR name LIKE ?)
            ORDER BY COALESCE(support_tier, 0) ASC, name ASC
            """,
            (base_stem, f"{base_stem} %"),
        )
        tier_variants = [
            str(vr["name"])
            for vr in variant_rows
            if ROMAN_SUFFIX_RE.sub("", str(vr["name"])) == base_stem
        ]

        # Resolve recommended_supports metadata IDs -> live gem names
        recommended_names: list[str] = []
        for sid in rec_support_ids:
            alt_sid = (
                sid.replace("/Gems/", "/Gem/")
                if "/Gems/" in sid
                else sid.replace("/Gem/", "/Gems/")
            )
            srow = self.db.query_one(
                "SELECT name FROM gems WHERE id = ? OR id = ? LIMIT 1",
                (sid, alt_sid),
            )
            if srow is not None:
                sname = str(srow["name"])
                if sname not in recommended_names:
                    recommended_names.append(sname)

        # Tag-compatible gems (supports for active/spirit/meta; skills for supports)
        gameplay_tags = [t for t in tags if t not in GEM_GENERIC_TAGS]
        compatible_gems: list[str] = []
        if gem_kind in ("support", "lineage_support"):
            # First check active/spirit/meta skills that explicitly recommend this gem ID
            gem_ids = [str(r["id"]) for r in candidates]
            for gid in gem_ids[:2]:
                short_tail = gid.split("/")[-1]
                rec_by = self.db.query_all(
                    """
                    SELECT DISTINCT name FROM gems
                    WHERE is_live_poe2 = 1
                      AND gem_kind IN ('active', 'spirit', 'meta')
                      AND recommended_supports_json LIKE ?
                    ORDER BY name ASC LIMIT 10
                    """,
                    (f"%{short_tail}%",),
                )
                for rb in rec_by:
                    gname = str(rb["name"])
                    if gname not in compatible_gems:
                        compatible_gems.append(gname)

            if len(compatible_gems) < 8 and gameplay_tags:
                tag_clauses = " OR ".join("tags_json LIKE ?" for _ in gameplay_tags[:4])
                tag_params = [f'%"{t}"%' for t in gameplay_tags[:4]]
                tag_hits = self.db.query_all(
                    f"""
                    SELECT DISTINCT name, tags_json FROM gems
                    WHERE is_live_poe2 = 1
                      AND gem_kind IN ('active', 'spirit', 'meta')
                      AND ({tag_clauses})
                    LIMIT 30
                    """,
                    tag_params,
                )
                scored_compat: list[tuple[int, str]] = []
                for th in tag_hits:
                    tset = set(json.loads(th["tags_json"]))
                    overlap = len(tset.intersection(gameplay_tags))
                    scored_compat.append((overlap, str(th["name"])))
                scored_compat.sort(key=lambda x: (-x[0], x[1]))
                for _ov, gname in scored_compat:
                    if gname not in compatible_gems:
                        compatible_gems.append(gname)
                    if len(compatible_gems) >= 10:
                        break
        elif gameplay_tags:
            tag_clauses = " OR ".join("tags_json LIKE ?" for _ in gameplay_tags[:4])
            tag_params = [f'%"{t}"%' for t in gameplay_tags[:4]]
            sup_hits = self.db.query_all(
                f"""
                SELECT DISTINCT name, gem_kind, support_tier, tags_json FROM gems
                WHERE is_live_poe2 = 1
                  AND gem_kind IN ('support', 'lineage_support')
                  AND ({tag_clauses})
                LIMIT 40
                """,
                tag_params,
            )
            scored_sups: list[tuple[int, int, str]] = []
            for sh in sup_hits:
                tset = set(json.loads(sh["tags_json"]))
                overlap = len(tset.intersection(gameplay_tags))
                tier_val = int(sh["support_tier"] or 1)
                scored_sups.append((overlap, tier_val, str(sh["name"])))
            scored_sups.sort(key=lambda x: (-x[0], -x[1], x[2]))
            for _ov, _tv, sname in scored_sups:
                if sname not in recommended_names and sname not in compatible_gems:
                    compatible_gems.append(sname)
                if len(compatible_gems) >= 10:
                    break

        spirit_energy_rule: str | None = None
        warning: str | None = None
        if gem_kind == "lineage_support":
            copy_rule = (
                "Lineage Support (drop-only endgame): strict max 1 copy per character "
                "(max_copies_per_char=1)."
            )
        elif gem_kind == "support":
            copy_rule = (
                "Standard Tiered Support (Patch 0.3+): unlimited copies across different skills; "
                "max 1 support from the same SupportGemCategory within a single skill."
            )
        elif gem_kind == "spirit":
            copy_rule = "Persistent Spirit Buff: max 1 active instance per character."
            spirit_energy_rule = (
                "Reserves Spirit (NOT Mana); Spirit reservation & passive tree links can be "
                "toggled independently per Weapon Set I / II (instant 0ms swap)."
            )
        elif gem_kind == "meta":
            copy_rule = "Meta-Gem: reserves Spirit and sockets triggerable active spells."
            spirit_energy_rule = (
                "Builds Energy from specific trigger conditions up to max Energy dependent on "
                "socketed spells' cast times. Triggered skills CANNOT generate Energy and "
                "cannot bypass skill cooldowns."
            )
        else:
            copy_rule = (
                "Active Skill Gem: holds up to 5 support sockets directly on the gem "
                "(Lesser -> 3, Greater -> 4, Perfect Jeweller's Orb -> 5 sockets)."
            )

        if name.lower() == "grim feast":
            warning = (
                "STALE 0.1 TRAP: Grim Feast is NO LONGER an Energy Shield doubling/overflow aura! "
                "In Patch 0.5.5 it is a Spirit-reserving Minion Revival remnant skill."
            )
        elif resolved_from and resolved_from.lower() != name.lower():
            warning = f"Resolved query '{resolved_from}' to live Patch {POE2_PATCH} gem '{name}'."

        stat_lines = [copy_rule]
        if spirit_energy_rule:
            stat_lines.append(spirit_energy_rule)
        if warning:
            stat_lines.append(warning)

        return GemInspection(
            id=str(primary["id"]),
            name=name,
            gem_kind=gem_kind,
            support_tier=support_tier,
            max_copies_per_char=max_copies,
            spirit_cost=None,
            tags=tags,
            stat_lines=stat_lines,
            color=color,
            req_weights=req_weights,
            tier_variants=tier_variants,
            copy_limit_rule=copy_rule,
            spirit_energy_rule=spirit_energy_rule,
            recommended_supports=recommended_names[:12],
            compatible_gems=compatible_gems[:10],
            resolved_from=resolved_from,
            warning=warning,
        )

    def inspect_gem(self, name_or_id: str) -> GemInspection:
        q = translate_zh_query(name_or_id.strip())
        if not q:
            raise LookupError("Gem query cannot be empty.")

        q_norm = q.replace("\u2019", "'")

        # 1. Exact ID match
        by_id = self.db.query_all(
            "SELECT * FROM gems WHERE lower(id) = lower(?) AND is_live_poe2 = 1",
            (q_norm,),
        )
        if by_id:
            siblings = self.db.query_all(
                "SELECT * FROM gems WHERE name = ? AND is_live_poe2 = 1",
                (by_id[0]["name"],),
            )
            return self._merge_gem_rows(siblings or by_id)

        # 2. Exact name match (strip trailing " Support" if caller included PoE1 suffix)
        stripped_sup = re.sub(r"\s+support$", "", q_norm, flags=re.IGNORECASE).strip()
        for candidate in (q_norm, stripped_sup):
            by_name = self.db.query_all(
                "SELECT * FROM gems WHERE lower(name) = lower(?) AND is_live_poe2 = 1",
                (candidate,),
            )
            if by_name:
                return self._merge_gem_rows(
                    by_name,
                    resolved_from=q if candidate.lower() != q_norm.lower() else None,
                )

        # 3. Tiered support base name without Roman numeral (e.g. "Pierce" -> "Pierce I")
        for roman in ("I", "II", "III"):
            tiered_name = f"{stripped_sup} {roman}"
            by_tier = self.db.query_all(
                "SELECT * FROM gems WHERE lower(name) = lower(?) AND is_live_poe2 = 1",
                (tiered_name,),
            )
            if by_tier:
                return self._merge_gem_rows(by_tier, resolved_from=q)

        # 4. Internal metadata ID match for renamed 0.1 gems (e.g. "Persistence")
        condensed = re.sub(r"[^A-Za-z0-9]", "", stripped_sup).lower()
        if condensed:
            id_matches = self.db.query_all(
                """
                SELECT * FROM gems
                WHERE is_live_poe2 = 1 AND (
                    lower(id) LIKE ? OR lower(id) LIKE ? OR lower(id) LIKE ?
                )
                ORDER BY COALESCE(support_tier, 99) ASC, length(id) ASC
                LIMIT 6
                """,
                (
                    f"%/supportgem{condensed}",
                    f"%/skillgem{condensed}",
                    f"%/skillgem{condensed}support",
                ),
            )
            if id_matches:
                target_name = str(id_matches[0]["name"])
                siblings = self.db.query_all(
                    "SELECT * FROM gems WHERE name = ? AND is_live_poe2 = 1",
                    (target_name,),
                )
                return self._merge_gem_rows(siblings or id_matches, resolved_from=q)

        # 5. Prefix / substring fallback
        fuzzy = self.db.query_all(
            """
            SELECT * FROM gems
            WHERE is_live_poe2 = 1 AND lower(name) LIKE lower(?)
            ORDER BY length(name) ASC, name ASC
            LIMIT 6
            """,
            (f"%{stripped_sup}%",),
        )
        if fuzzy:
            target_name = str(fuzzy[0]["name"])
            siblings = self.db.query_all(
                "SELECT * FROM gems WHERE name = ? AND is_live_poe2 = 1",
                (target_name,),
            )
            return self._merge_gem_rows(siblings or fuzzy, resolved_from=q)

        raise LookupError(f"No live PoE2 {POE2_PATCH} gem found matching '{name_or_id}'.")

    def _top_affixes_for_slug(
        self,
        slug: str | None,
        ilvl: int,
        side: str,
        limit: int = 10,
    ) -> list[TopAffixSummary]:
        if not slug:
            return []
        rows = self.db.query_all(
            """
            SELECT mod_id, mod_type, family_id, affix_side, affix_name,
                   display_tier, required_level, weight, stat_text
            FROM mod_tiers
            WHERE base_group_slug = ?
              AND source_pool = 'normal'
              AND affix_side = ?
              AND display_tier = 1
              AND required_level <= ?
            ORDER BY required_level DESC, weight ASC, mod_type ASC
            """,
            (slug, side, ilvl),
        )
        if not rows:
            # Fallback for low-ilvl queries where T1 requires a higher ilvl
            all_eligible = self.db.query_all(
                """
                SELECT mod_id, mod_type, family_id, affix_side, affix_name,
                       display_tier, required_level, weight, stat_text
                FROM mod_tiers
                WHERE base_group_slug = ?
                  AND source_pool = 'normal'
                  AND affix_side = ?
                  AND required_level <= ?
                ORDER BY display_tier ASC, required_level DESC, weight ASC
                """,
                (slug, side, ilvl),
            )
            seen_types: set[str] = set()
            deduped: list[sqlite3.Row] = []
            for r in all_eligible:
                mt = str(r["mod_type"])
                if mt not in seen_types:
                    seen_types.add(mt)
                    deduped.append(r)
            rows = deduped

        return [
            TopAffixSummary(
                mod_id=str(r["mod_id"]),
                mod_type=str(r["mod_type"]),
                family_id=str(r["family_id"]),
                affix_side=str(r["affix_side"]),
                affix_name=str(r["affix_name"]),
                display_tier=int(r["display_tier"]),
                required_level=int(r["required_level"]),
                weight=int(r["weight"]),
                stat_text=str(r["stat_text"]),
            )
            for r in rows[:limit]
        ]

    def _top_live_bases_for_slug(self, slug: str, limit: int = 6) -> list[str]:
        rows = self.db.query_all(
            """
            SELECT DISTINCT name, drop_level FROM base_items
            WHERE poe2db_slug = ?
              AND is_live_poe2 = 1
              AND name NOT LIKE 'Runeforged %'
              AND name NOT LIKE 'Runemastered %'
            ORDER BY drop_level DESC, name ASC
            LIMIT ?
            """,
            (slug, limit),
        )
        return [str(r["name"]) for r in rows]

    def _infer_slug_from_name(self, name: str) -> tuple[str, str | None]:
        low = name.lower().strip()
        if low in POE1_BASE_TRAP_MAP:
            ic, slug, _note = POE1_BASE_TRAP_MAP[low]
            return ic, slug
        for noun, ic, slug in BASE_NOUN_SLUG_HINTS:
            if re.search(rf"\b{re.escape(noun)}\b", low):
                return ic, slug
        return "Unknown", None

    def _row_to_base_inspection(
        self,
        row: sqlite3.Row,
        *,
        ilvl: int,
        matching_bases: list[str] | None = None,
        warning: str | None = None,
    ) -> BaseItemInspection:
        slug = str(row["poe2db_slug"]) if row["poe2db_slug"] else None
        return BaseItemInspection(
            id=str(row["id"]),
            name=str(row["name"]),
            item_class=str(row["item_class"]),
            poe2db_slug=slug,
            drop_level=int(row["drop_level"]),
            max_prefixes=int(row["max_prefixes"]),
            max_suffixes=int(row["max_suffixes"]),
            tags=json.loads(row["tags_json"]),
            implicits=json.loads(row["implicits_json"]),
            properties=json.loads(row["properties_json"]),
            is_live_poe2=bool(row["is_live_poe2"]),
            found=True,
            ilvl=ilvl,
            warning=warning,
            matching_bases=matching_bases or [],
            top_prefixes=self._top_affixes_for_slug(slug, ilvl, "prefix"),
            top_suffixes=self._top_affixes_for_slug(slug, ilvl, "suffix"),
        )

    def inspect_base_item(self, name_or_slug: str, ilvl: int = 82) -> BaseItemInspection:
        q = name_or_slug.strip()
        if not q:
            raise LookupError("Base item name or poe2db_slug cannot be empty.")

        # 1. Check stale Patch 0.1 "Expert " / "Advanced " prefix trap FIRST!
        stale_match = STALE_BASE_PREFIX_RE.match(q)
        if stale_match:
            stale_prefix = stale_match.group(1)
            stripped = stale_match.group(2).strip()
            base_row = self.db.query_one(
                """
                SELECT * FROM base_items
                WHERE lower(name) = lower(?)
                ORDER BY is_live_poe2 DESC, drop_level DESC
                LIMIT 1
                """,
                (stripped,),
            )
            if base_row is not None:
                ic = str(base_row["item_class"])
                slug = str(base_row["poe2db_slug"]) if base_row["poe2db_slug"] else None
            else:
                ic, slug = self._infer_slug_from_name(stripped)

            suggestions: list[str] = []
            if stripped.lower() == "dualstring bow":
                suggestions.append("Gemini Bow")
            if slug:
                for cand in self._top_live_bases_for_slug(slug, limit=6):
                    if cand not in suggestions:
                        suggestions.append(cand)
            if base_row is not None and bool(base_row["is_live_poe2"]):
                bname = str(base_row["name"])
                if bname not in suggestions:
                    suggestions.append(bname)

            sug_text = ", ".join(suggestions[:5]) if suggestions else "query live base_items"
            warn = (
                f"STALE 0.1 BASE TRAP: PoE2 removed '{stale_prefix.title()} ' prefixes from "
                f"endgame bases after Patch 0.1 ('{q}' does not exist in Patch {POE2_PATCH}). "
                f"Suggested live PoE2 {POE2_PATCH} {ic} bases: {sug_text}."
            )
            return BaseItemInspection(
                id=f"stale_0_1_prefix:{re.sub(r'[^a-z0-9]+', '_', q.lower()).strip('_')}",
                name=q,
                item_class=ic,
                poe2db_slug=slug,
                drop_level=int(base_row["drop_level"]) if base_row is not None else 1,
                is_live_poe2=False,
                found=False,
                ilvl=ilvl,
                is_stale_prefix_trap=True,
                warning=warn,
                suggested_bases=suggestions[:6],
                top_prefixes=self._top_affixes_for_slug(slug, ilvl, "prefix"),
                top_suffixes=self._top_affixes_for_slug(slug, ilvl, "suffix"),
            )

        # 2. Exact base item name or metadata ID
        exact_row = self.db.query_one(
            """
            SELECT * FROM base_items
            WHERE lower(name) = lower(?) OR lower(id) = lower(?)
            ORDER BY is_live_poe2 DESC, drop_level DESC
            LIMIT 1
            """,
            (q, q),
        )
        if exact_row is not None:
            removed_lever = self.db.query_one(
                "SELECT is_active_drop_in_patch FROM crafting_levers "
                "WHERE lower(name) = lower(?) AND is_active_drop_in_patch = 0",
                (q,),
            )
            warn = None
            is_live_override = bool(exact_row["is_live_poe2"]) and removed_lever is None
            if removed_lever is not None:
                warn = (
                    f"REMOVED/LEGACY LEVER: '{exact_row['name']}' was removed from active "
                    f"drop pools in PoE2 {POE2_PATCH} (legacy Standard copies only)."
                )
            elif not is_live_override:
                warn = (
                    f"'{exact_row['name']}' exists in client files but is NOT live on "
                    f"PoE2 {POE2_PATCH} trade."
                )
            res = self._row_to_base_inspection(exact_row, ilvl=ilvl, warning=warn)
            res.is_live_poe2 = is_live_override
            return res

        # 3. Exact poe2db_slug or item_class lookup (e.g. "Wands", "Body_Armours_int", "Wand")
        slug_rows = self.db.query_all(
            """
            SELECT * FROM base_items
            WHERE (lower(poe2db_slug) = lower(?) OR lower(item_class) = lower(?))
              AND is_live_poe2 = 1
            ORDER BY
              (name LIKE 'Runeforged %' OR name LIKE 'Runemastered %') ASC,
              drop_level DESC,
              name ASC
            LIMIT 10
            """,
            (q.replace(" ", "_"), q),
        )
        if slug_rows:
            matching = list(dict.fromkeys(str(r["name"]) for r in slug_rows))
            return self._row_to_base_inspection(
                slug_rows[0],
                ilvl=ilvl,
                matching_bases=matching[:8],
            )

        # 4. Explicit PoE1 non-existent base item trap check!
        q_low = q.lower()
        if q_low in POE1_BASE_TRAP_MAP:
            ic, slug, poe1_note = POE1_BASE_TRAP_MAP[q_low]
            suggestions = self._top_live_bases_for_slug(slug, limit=6)
            sug_text = ", ".join(suggestions[:5])
            return BaseItemInspection(
                id=f"poe1_invalid:{re.sub(r'[^a-z0-9]+', '_', q_low).strip('_')}",
                name=q,
                item_class=ic,
                poe2db_slug=slug,
                drop_level=1,
                is_live_poe2=False,
                found=False,
                ilvl=ilvl,
                is_poe1_base_trap=True,
                warning=(
                    f"POE1 BASE TRAP: '{q}' ({poe1_note}) does NOT exist in Path of Exile 2 "
                    f"Patch {POE2_PATCH}. Real live PoE2 {ic} endgame bases: {sug_text}."
                ),
                suggested_bases=suggestions,
                top_prefixes=self._top_affixes_for_slug(slug, ilvl, "prefix"),
                top_suffixes=self._top_affixes_for_slug(slug, ilvl, "suffix"),
            )

        # 5. Substring match on live PoE2 bases before falling back to noun-inferred suggestion
        sub_row = self.db.query_one(
            """
            SELECT * FROM base_items
            WHERE is_live_poe2 = 1 AND lower(name) LIKE lower(?)
            ORDER BY
              (name LIKE 'Runeforged %' OR name LIKE 'Runemastered %') ASC,
              drop_level DESC,
              length(name) ASC
            LIMIT 1
            """,
            (f"%{q}%",),
        )
        if sub_row is not None:
            return self._row_to_base_inspection(sub_row, ilvl=ilvl)

        # 6. Unknown / PoE1 / non-existent base with inferred item class
        ic, slug = self._infer_slug_from_name(q)
        suggestions = self._top_live_bases_for_slug(slug, limit=6) if slug else []
        sug_clause = (
            f" Suggested live PoE2 {POE2_PATCH} {ic} bases: {', '.join(suggestions[:5])}."
            if suggestions
            else ""
        )
        return BaseItemInspection(
            id=f"unknown_base:{re.sub(r'[^a-z0-9]+', '_', q_low).strip('_')}",
            name=q,
            item_class=ic,
            poe2db_slug=slug,
            drop_level=1,
            is_live_poe2=False,
            found=False,
            ilvl=ilvl,
            is_poe1_base_trap=slug is not None,
            warning=(
                f"'{q}' is not a valid Path of Exile 2 Patch {POE2_PATCH} base item.{sug_clause}"
            ),
            suggested_bases=suggestions,
            top_prefixes=self._top_affixes_for_slug(slug, ilvl, "prefix"),
            top_suffixes=self._top_affixes_for_slug(slug, ilvl, "suffix"),
        )

    def inspect_unique(self, name: str) -> UniqueInspection:
        q = translate_zh_query(name.strip())
        if not q:
            raise LookupError("Unique item name cannot be empty.")

        q_norm = q.replace("\u2019", "'")
        row = self.db.query_one(
            """
            SELECT * FROM uniques
            WHERE lower(name) = lower(?) OR lower(id) = lower(?)
            ORDER BY is_live_poe2 DESC
            LIMIT 1
            """,
            (q_norm, q_norm),
        )
        if row is None:
            row = self.db.query_one(
                """
                SELECT * FROM uniques
                WHERE lower(name) LIKE lower(?)
                ORDER BY is_live_poe2 DESC, length(name) ASC, name ASC
                LIMIT 1
                """,
                (f"%{q_norm}%",),
            )
        if row is None:
            fts_expr = sanitize_fts5_query(q_norm, operator="AND")
            if fts_expr:
                hit = self.db.query_one(
                    """
                    SELECT doc_id FROM knowledge_fts
                    WHERE knowledge_fts MATCH ? AND doc_type = 'unique'
                    ORDER BY bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.0, 1.0) ASC
                    LIMIT 1
                    """,
                    (fts_expr,),
                )
                if hit is not None:
                    row = self.db.query_one(
                        "SELECT * FROM uniques WHERE id = ?",
                        (hit["doc_id"],),
                    )

        if row is None:
            raise LookupError(f"No PoE2 {POE2_PATCH} unique item found matching '{name}'.")

        base_type = str(row["base_type"])
        base_row = self.db.query_one(
            "SELECT item_class, is_live_poe2 FROM base_items WHERE lower(name) = lower(?) LIMIT 1",
            (base_type,),
        )
        item_class = (
            str(base_row["item_class"]) if base_row is not None else str(row["slot_category"])
        )
        explicits, has_variants = _clean_unique_lines(json.loads(row["explicits_json"]))
        implicits = json.loads(row["implicits_json"])

        return UniqueInspection(
            id=str(row["id"]),
            name=str(row["name"]),
            base_type=base_type,
            item_class=item_class,
            slot_category=str(row["slot_category"]),
            req_level=int(row["req_level"]) if row["req_level"] is not None else None,
            implicits=implicits,
            explicits=explicits,
            is_live_poe2=bool(row["is_live_poe2"]),
            has_variants=has_variants,
        )

    def _row_to_augment(self, row: sqlite3.Row) -> AugmentSummary:
        bound = bool(row["is_socket_bound"])
        return AugmentSummary(
            id=str(row["id"]),
            name=str(row["name"]),
            augment_type=str(row["augment_type"]),
            required_level=int(row["required_level"]),
            limit_count=int(row["limit_count"]) if row["limit_count"] is not None else None,
            is_socket_bound=bound,
            can_overwrite_in_socket=not bound,
            slot_effects=json.loads(row["slot_effects_json"]),
            shaman_bonded_effects=json.loads(row["bonded_effects_json"]),
            description=str(row["description"]),
            is_live_poe2=bool(row["is_live_poe2"]),
        )

    def inspect_augment(self, query: str, limit: int = 8) -> list[AugmentSummary]:
        q = translate_zh_query(query.strip())
        if not q or limit <= 0:
            return []

        q_norm = q.replace("\u2019", "'")
        q_low = q_norm.lower()

        # Direct exact/substring name or Shaman Bonded / type query
        if q_low in ("shaman", "bonded", "shaman bonded"):
            rows = self.db.query_all(
                """
                SELECT * FROM augments
                WHERE bonded_effects_json != '{}' AND is_live_poe2 = 1
                ORDER BY augment_type ASC, required_level DESC, name ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_augment(r) for r in rows]

        if q_low in ("socket-bound", "socket bound", "socket_bound"):
            rows = self.db.query_all(
                """
                SELECT * FROM augments
                WHERE is_socket_bound = 1
                ORDER BY required_level DESC, name ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_augment(r) for r in rows]

        scored_ids: dict[str, float] = {}
        name_rows = self.db.query_all(
            """
            SELECT id, name, augment_type FROM augments
            WHERE lower(name) = lower(?)
               OR lower(name) LIKE lower(?)
               OR lower(augment_type) = lower(?)
            ORDER BY is_live_poe2 DESC, length(name) ASC
            LIMIT ?
            """,
            (q_norm, f"%{q_norm}%", q_norm.rstrip("s"), max(limit * 2, 16)),
        )
        for r in name_rows:
            aid = str(r["id"])
            n_low = str(r["name"]).lower()
            pts = 50.0 if n_low == q_low else (25.0 if n_low.startswith(q_low) else 15.0)
            scored_ids[aid] = pts

        for op, bonus in (("AND", 12.0), ("OR", 4.0)):
            expr = sanitize_fts5_query(q_norm, operator=op)
            if not expr or (op == "OR" and len(scored_ids) >= limit):
                continue
            hits = self.db.query_all(
                """
                SELECT doc_id, bm25(knowledge_fts, 0.0, 0.0, 10.0, 2.0, 1.0) AS rscore
                FROM knowledge_fts
                WHERE knowledge_fts MATCH ? AND doc_type = 'augment'
                ORDER BY rscore ASC
                LIMIT ?
                """,
                (expr, max(limit * 2, 16)),
            )
            for h in hits:
                aid = str(h["doc_id"])
                pts = max(0.5, -float(h["rscore"])) + bonus
                if pts > scored_ids.get(aid, 0.0):
                    scored_ids[aid] = pts

        ordered_ids = sorted(scored_ids.keys(), key=lambda k: -scored_ids[k])[:limit]
        results: list[AugmentSummary] = []
        for aid in ordered_ids:
            row = self.db.query_one("SELECT * FROM augments WHERE id = ?", (aid,))
            if row is not None:
                results.append(self._row_to_augment(row))
        return results

    def _row_to_runeforge(self, row: sqlite3.Row) -> RuneforgeRecipeSummary:
        inp = json.loads(row["inputs_json"])
        source_base = inp.get("source_base")
        rune_seq = [int(x) for x in (inp.get("rune_sequence_ids") or [])]
        target_mid = inp.get("target_metadata_id")

        implicits: list[str] = []
        props: dict[str, Any] = {}
        if target_mid:
            brow = self.db.query_one(
                "SELECT implicits_json, properties_json FROM base_items WHERE id = ?",
                (target_mid,),
            )
            if brow is not None:
                implicits = json.loads(brow["implicits_json"])
                props = json.loads(brow["properties_json"])

        return RuneforgeRecipeSummary(
            recipe_id=str(row["recipe_id"]),
            category=str(row["category"]),
            output_item=str(row["output_item"]),
            source_base=source_base,
            rune_sequence_ids=rune_seq,
            result_implicits=implicits,
            result_properties=props,
        )

    def search_runeforge(self, query: str, limit: int = 12) -> list[RuneforgeRecipeSummary]:
        q = query.strip()
        if not q or limit <= 0:
            return []

        q_low = q.lower()
        if q_low in ("remnant", "remnants", "kalguuran", "kalguuran remnant"):
            rows = self.db.query_all(
                """
                SELECT * FROM runeforge_recipes
                WHERE category = 'kalguuran_remnant_recipe'
                ORDER BY output_item ASC, recipe_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            return [self._row_to_runeforge(r) for r in rows]

        if q_low in ("verisium", "anvil", "verisium anvil", "runeforged", "runemastered"):
            like_pat = f"{q_low.title()}%" if q_low in ("runeforged", "runemastered") else "%"
            rows = self.db.query_all(
                """
                SELECT * FROM runeforge_recipes
                WHERE category = 'verisium_base_transform' AND output_item LIKE ?
                ORDER BY output_item ASC, recipe_id ASC
                LIMIT ?
                """,
                (like_pat, limit),
            )
            return [self._row_to_runeforge(r) for r in rows]

        cleaned_tokens = [
            t.lower()
            for t in extract_query_tokens(q)
            if t.lower() not in {"alloy", "remnant", "verisium", "anvil", "recipe", "recipes"}
            and t.lower() not in FTS_STOPWORDS
        ]
        probe_terms = cleaned_tokens or [t.lower() for t in extract_query_tokens(q)]
        if not probe_terms:
            return []

        clauses = " AND ".join(
            "(lower(output_item) LIKE ? OR lower(inputs_json) LIKE ? OR lower(category) LIKE ?)"
            for _ in probe_terms[:4]
        )
        params: list[Any] = []
        for term in probe_terms[:4]:
            pat = f"%{term}%"
            params.extend([pat, pat, pat])

        rows = self.db.query_all(
            f"""
            SELECT * FROM runeforge_recipes
            WHERE {clauses}
            ORDER BY
              CASE WHEN lower(output_item) = ? THEN 0
                   WHEN lower(output_item) LIKE ? THEN 1
                   ELSE 2 END,
              output_item ASC,
              recipe_id ASC
            LIMIT ?
            """,
            [*params, probe_terms[0], f"{probe_terms[0]}%", limit],
        )
        return [self._row_to_runeforge(r) for r in rows]
