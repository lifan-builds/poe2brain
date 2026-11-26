# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from poe2brain.config import get_gamedata_db_path
from poe2brain.models.common import AffixSide
from poe2brain.models.crafting import ModTierSpec
from poe2brain.models.items import ItemRarity, ModDomain, ParsedItem

MARK_OF_ABYSS_MOD_ID = "MarkOfTheAbyssalLord"

POB_CLASS_TO_SLUG_PREFIX: dict[str, str] = {
    "Helmet": "Helmets",
    "Body Armour": "Body_Armours",
    "Gloves": "Gloves",
    "Boots": "Boots",
    "Shield": "Shields",
    "Buckler": "Bucklers",
    "Focus": "Foci",
    "Belt": "Belts",
    "Ring": "Rings",
    "Amulet": "Amulets",
    "Wand": "Wands",
    "Sceptre": "Sceptres",
    "Staff": "Staves",
    "Quarterstaff": "Quarterstaves",
    "Warstaff": "Quarterstaves",
    "Bow": "Bows",
    "Crossbow": "Crossbows",
    "Quiver": "Quivers",
    "One Hand Mace": "One_Hand_Maces",
    "Two Hand Mace": "Two_Hand_Maces",
    "One Hand Sword": "One_Hand_Swords",
    "Two Hand Sword": "Two_Hand_Swords",
    "One Hand Axe": "One_Hand_Axes",
    "Two Hand Axe": "Two_Hand_Axes",
    "Spear": "Spears",
    "Flail": "Flails",
    "Dagger": "Daggers",
    "Claw": "Claws",
    "Talisman": "Talismans",
    "Trap": "Traps",
}


class RemovedLeverError(ValueError):
    """Raised when a caller requests a crafting currency/omen removed in PoE2 0.4/0.5."""


@dataclass(frozen=True, slots=True)
class BaseItemEntry:
    id: str
    name: str
    item_class: str
    poe2db_slug: str
    drop_level: int
    max_prefixes: int
    max_suffixes: int
    tags: tuple[str, ...]
    implicits: tuple[str, ...]
    is_live_poe2: bool


@dataclass(frozen=True, slots=True)
class ModEntry:
    mod_id: str
    mod_type: str
    family_id: str
    families: tuple[str, ...]
    base_group_slug: str
    source_pool: str
    affix_side: AffixSide
    affix_name: str
    internal_rank_asc: int
    display_tier: int
    required_level: int
    weight: int
    weight_confidence: str
    boss_tag: str | None
    currency_name: str | None
    is_alloy: bool
    removes_mod: bool
    adds_tags: tuple[str, ...]
    spawn_weights: tuple[tuple[str, int], ...]
    mod_tags: tuple[str, ...]
    stat_text: str

    def to_spec(self) -> ModTierSpec:
        return ModTierSpec(
            mod_id=self.mod_id,
            family_id=self.family_id,
            base_group_slug=self.base_group_slug,
            source_pool=self.source_pool,
            affix_side=self.affix_side,
            affix_name=self.affix_name or None,
            internal_rank_asc=max(1, self.internal_rank_asc),
            display_tier=max(1, self.display_tier),
            required_level=self.required_level,
            weight=self.weight,
            weight_confidence=self.weight_confidence,
            boss_tag=self.boss_tag,
            tags=list(self.mod_tags),
            stat_text=self.stat_text,
        )


@dataclass(frozen=True, slots=True)
class LeverEntry:
    lever_id: str
    name: str
    category: str
    is_active_drop_in_patch: bool
    applies_to_rarities: str
    target_classes: tuple[str, ...]
    max_item_level: int | None
    min_mod_level_floor: int | None
    omen_side_constraint: AffixSide | None
    omen_boss_constraint: str | None
    forced_mods: dict[str, str]
    description: str


@dataclass(frozen=True, slots=True)
class CraftingMod:
    mod_id: str
    mod_type: str
    family_id: str
    families: tuple[str, ...]
    affix_side: AffixSide
    display_tier: int
    required_level: int
    stat_text: str
    adds_tags: tuple[str, ...] = ()
    mod_tags: tuple[str, ...] = ()
    fractured: bool = False
    is_desecrated: bool = False
    is_abyss_mark: bool = False

    @classmethod
    def from_entry(
        cls,
        entry: ModEntry,
        *,
        fractured: bool = False,
        is_desecrated: bool | None = None,
    ) -> CraftingMod:
        desec = is_desecrated if is_desecrated is not None else (entry.source_pool == "desecrated")
        return cls(
            mod_id=entry.mod_id,
            mod_type=entry.mod_type,
            family_id=entry.family_id,
            families=entry.families,
            affix_side=entry.affix_side,
            display_tier=entry.display_tier,
            required_level=entry.required_level,
            stat_text=entry.stat_text,
            adds_tags=entry.adds_tags,
            mod_tags=entry.mod_tags,
            fractured=fractured,
            is_desecrated=desec,
            is_abyss_mark=(
                "Mark of the Abyssal Lord" in entry.stat_text
                or entry.mod_id == MARK_OF_ABYSS_MOD_ID
            ),
        )


@dataclass(slots=True)
class CraftingState:
    base: BaseItemEntry
    ilvl: int
    rarity: ItemRarity = ItemRarity.NORMAL
    mods: list[CraftingMod] = field(default_factory=list)
    quality: int = 0
    catalyst_tag: str | None = None

    def clone(self) -> CraftingState:
        return CraftingState(
            base=self.base,
            ilvl=self.ilvl,
            rarity=self.rarity,
            mods=list(self.mods),
            quality=self.quality,
            catalyst_tag=self.catalyst_tag,
        )

    @property
    def prefix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_side == AffixSide.PREFIX)

    @property
    def suffix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_side == AffixSide.SUFFIX)

    def max_slots_for_rarity(self, rarity: ItemRarity | None = None) -> tuple[int, int]:
        rar = rarity or self.rarity
        if rar == ItemRarity.NORMAL:
            return 0, 0
        if rar == ItemRarity.MAGIC:
            return 1, 1
        return self.base.max_prefixes, self.base.max_suffixes

    def has_open_prefix(self, rarity: ItemRarity | None = None) -> bool:
        max_p, _ = self.max_slots_for_rarity(rarity)
        return self.prefix_count < max_p

    def has_open_suffix(self, rarity: ItemRarity | None = None) -> bool:
        _, max_s = self.max_slots_for_rarity(rarity)
        return self.suffix_count < max_s

    @property
    def has_fractured(self) -> bool:
        return any(m.fractured for m in self.mods)

    @property
    def has_desecrated(self) -> bool:
        return any(m.is_desecrated for m in self.mods)

    @property
    def has_abyss_mark(self) -> bool:
        return any(m.is_abyss_mark for m in self.mods)

    def blocked_families(self) -> set[str]:
        return {fam for m in self.mods for fam in m.families}

    def effective_tags(self) -> set[str]:
        tags = set(self.base.tags)
        for m in self.mods:
            tags.update(m.adds_tags)
        tags.add("default")
        return tags


@dataclass(frozen=True, slots=True)
class TargetSpec:
    label: str
    affix_side: AffixSide
    mod_type: str
    family_id: str
    matching_mod_ids: frozenset[str]
    matching_entries: tuple[ModEntry, ...]
    max_display_tier: int | None = 1

    def is_satisfied_by(self, state: CraftingState) -> bool:
        return any(m.mod_id in self.matching_mod_ids for m in state.mods)

    def is_family_blocked_by_junk(self, state: CraftingState) -> bool:
        if self.is_satisfied_by(state):
            return False
        return any(self.family_id in m.families for m in state.mods)


@dataclass(frozen=True, slots=True)
class PoolEvalResult:
    lever: LeverEntry
    omens: tuple[LeverEntry, ...]
    hit_probability: float
    collateral_remove_probability: float
    eligible_pool_weight: int
    target_weight: int
    matching_tiers: tuple[ModEntry, ...]
    notes: str = ""


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _extract_primary_roll_bounds(stat_text: str) -> tuple[float, float] | None:
    first_line = stat_text.splitlines()[0] if stat_text else ""
    range_m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\s*-\s*([+-]?\d+(?:\.\d+)?)\)", first_line)
    if range_m:
        a, b = abs(float(range_m.group(1))), abs(float(range_m.group(2)))
        return min(a, b), max(a, b)
    num_m = re.search(r"[+-]?(\d+(?:\.\d+)?)", first_line)
    if num_m:
        v = abs(float(num_m.group(1)))
        return v, v
    return None


_RANGE_OR_NUM_TOKEN_RE = re.compile(
    r"\((?P<lo>[+-]?\d+(?:\.\d+)?)\s*-\s*(?P<hi>[+-]?\d+(?:\.\d+)?)\)"
    r"|(?P<num>[+-]?\d+(?:\.\d+)?)"
)


def _extract_all_roll_bounds(stat_text: str) -> list[tuple[float, float]]:
    bounds: list[tuple[float, float]] = []
    for m in _RANGE_OR_NUM_TOKEN_RE.finditer(stat_text):
        if m.group("lo") is not None and m.group("hi") is not None:
            a, b = abs(float(m.group("lo"))), abs(float(m.group("hi")))
            bounds.append((min(a, b), max(a, b)))
        elif m.group("num") is not None:
            v = abs(float(m.group("num")))
            bounds.append((v, v))
    return bounds


def _strip_numbers_to_pattern(text: str) -> str:
    s = re.sub(r"[+-]?\([^)]*\)", "#", text)
    s = re.sub(r"[+-]?\d+(?:\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def exact_three_choice_unveil_prob(
    eligible: list[tuple[ModEntry, int]],
    target_mod_ids: set[str],
) -> float:
    """Exact probability that >=1 of 3 without-replacement weighted draws hits `target_mod_ids`."""
    total_w = sum(w for _, w in eligible if w > 0)
    if total_w <= 0:
        return 0.0
    hit_w = sum(w for m, w in eligible if w > 0 and m.mod_id in target_mod_ids)
    if hit_w <= 0:
        return 0.0
    misses = [w for m, w in eligible if w > 0 and m.mod_id not in target_mod_ids]
    if len(misses) < 3:
        return 1.0

    miss_w_sum = total_w - hit_w
    p_all_three_miss = 0.0
    n_miss = len(misses)
    for i in range(n_miss):
        wi = misses[i]
        rem1 = total_w - wi
        if rem1 <= 0:
            continue
        p1 = wi / total_w
        inner = 0.0
        for j in range(n_miss):
            if i == j:
                continue
            wj = misses[j]
            rem2 = rem1 - wj
            if rem2 <= 0:
                continue
            rem_miss_w = miss_w_sum - wi - wj
            if rem_miss_w > 0:
                inner += (wj / rem1) * (rem_miss_w / rem2)
        p_all_three_miss += p1 * inner

    return max(0.0, min(1.0, 1.0 - p_all_three_miss))


class ModPoolEngine:
    """Exact PoE2 0.5.5 modifier pool & crafting probability engine backed by SQLite."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_gamedata_db_path()
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"PoE2 gamedata SQLite not found at {self.db_path}. Run scripts/sync_gamedata.py."
            )
        self._conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._levers_by_key: dict[str, LeverEntry] = {}
        self._all_levers: list[LeverEntry] = []
        self._mods_by_slug: dict[str, list[ModEntry]] = {}
        self._bases_cache: dict[str, BaseItemEntry] = {}
        self._load_levers()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ModPoolEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _load_levers(self) -> None:
        rows = self._conn.execute("SELECT * FROM crafting_levers ORDER BY name").fetchall()
        for r in rows:
            side_raw = r["omen_side_constraint"]
            side = AffixSide(side_raw) if side_raw in ("prefix", "suffix") else None
            entry = LeverEntry(
                lever_id=r["lever_id"],
                name=r["name"],
                category=r["category"],
                is_active_drop_in_patch=bool(r["is_active_drop_in_patch"]),
                applies_to_rarities=r["applies_to_rarities"],
                target_classes=tuple(json.loads(r["target_classes_json"])),
                max_item_level=r["max_item_level"],
                min_mod_level_floor=r["min_mod_level_floor"],
                omen_side_constraint=side,
                omen_boss_constraint=r["omen_boss_constraint"],
                forced_mods=json.loads(r["forced_mods_json"]),
                description=r["description"],
            )
            self._all_levers.append(entry)
            self._levers_by_key[entry.lever_id] = entry
            self._levers_by_key[_norm_key(entry.name)] = entry

    def list_levers(
        self, category: str | None = None, *, active_only: bool = True
    ) -> list[LeverEntry]:
        out = self._all_levers
        if category is not None:
            out = [lv for lv in out if lv.category == category]
        if active_only:
            out = [lv for lv in out if lv.is_active_drop_in_patch]
        return list(out)

    def resolve_lever(self, name_or_id: str, *, allow_legacy: bool = False) -> LeverEntry:
        key = _norm_key(name_or_id)
        if "recombinat" in key:
            if not allow_legacy:
                raise RemovedLeverError(
                    "Recombinators (and Omen of Recombination) were phased out in PoE2 Patch 0.5 "
                    "(Return of the Ancients). Use Essences, Perfect Essences, Verisium Alloys, "
                    "Desecration Bones, or Omens instead."
                )
        if "homogenising" in key or "homogenizing" in key:
            key = key.replace("homogenizing", "homogenising")

        aliases = {
            "transmute": "orb_of_transmutation",
            "transmutation": "orb_of_transmutation",
            "greater_transmute": "greater_orb_of_transmutation",
            "perfect_transmute": "perfect_orb_of_transmutation",
            "augment": "orb_of_augmentation",
            "augmentation": "orb_of_augmentation",
            "greater_augment": "greater_orb_of_augmentation",
            "perfect_augment": "perfect_orb_of_augmentation",
            "regal": "regal_orb",
            "greater_regal": "greater_regal_orb",
            "perfect_regal": "perfect_regal_orb",
            "exalt": "exalted_orb",
            "exalted": "exalted_orb",
            "greater_exalt": "greater_exalted_orb",
            "greater_exalted": "greater_exalted_orb",
            "perfect_exalt": "perfect_exalted_orb",
            "perfect_exalted": "perfect_exalted_orb",
            "chaos": "chaos_orb",
            "greater_chaos": "greater_chaos_orb",
            "perfect_chaos": "perfect_chaos_orb",
            "annul": "orb_of_annulment",
            "annulment": "orb_of_annulment",
            "alchemy": "orb_of_alchemy",
            "fracture": "fracturing_orb",
            "fracturing": "fracturing_orb",
            "whittling": "omen_of_whittling",
        }
        key = aliases.get(key, key)
        entry = self._levers_by_key.get(key)
        if entry is None:
            for cand_key, cand in self._levers_by_key.items():
                if key in cand_key:
                    entry = cand
                    break
        if entry is None:
            raise ValueError(f"Unknown PoE2 crafting lever: {name_or_id!r}")
        if not entry.is_active_drop_in_patch and not allow_legacy:
            raise RemovedLeverError(
                f"{entry.name!r} is NOT active in PoE2 Patch 0.5.5: Omen of Homogenising "
                "Exaltation/Coronation were removed from drop pools in Patch 0.4, and "
                "Recombinators were phased out in Patch 0.5."
            )
        return entry

    def resolve_base(self, base_name_or_slug: str) -> BaseItemEntry:
        from poe2brain.knowledge.zh_aliases import translate_zh_query

        clean = translate_zh_query(base_name_or_slug.strip())
        for prefix in ("Superior ", "Exceptional ", "Runeforged ", "Runemastered "):
            if clean.startswith(prefix):
                clean = clean[len(prefix) :].strip()
        # PoE2 0.5.5 shortened "<Type> Precursor Tablet" -> "<Type> Tablet"
        if clean.lower() == "precursor tablet":
            clean = "Irradiated Tablet"
        elif "precursor tablet" in clean.lower():
            clean = re.sub(r"\bPrecursor\s+Tablet\b", "Tablet", clean, flags=re.I).strip()

        cache_key = clean.lower()
        if cache_key in self._bases_cache:
            return self._bases_cache[cache_key]

        # 1. Exact base item name match (preferring live PoE2 items)
        row = self._conn.execute(
            """
            SELECT * FROM base_items
            WHERE lower(name) = lower(?) AND poe2db_slug IS NOT NULL
            ORDER BY is_live_poe2 DESC, drop_level DESC
            LIMIT 1
            """,
            (clean,),
        ).fetchone()

        # 2. Match by poe2db_slug directly (prefer neutral base with fewest 'no_*' tags)
        if row is None:
            slug_candidates = self._conn.execute(
                """
                SELECT * FROM base_items
                WHERE lower(poe2db_slug) = lower(?)
                ORDER BY is_live_poe2 DESC, drop_level DESC
                """,
                (clean.replace(" ", "_"),),
            ).fetchall()
            if slug_candidates:
                slug_candidates.sort(
                    key=lambda r: (
                        sum(1 for t in json.loads(r["tags_json"]) if t.startswith("no_")),
                        -int(r["is_live_poe2"]),
                        -int(r["drop_level"]),
                    )
                )
                row = slug_candidates[0]

        # 3. Strip legacy 0.1 'Expert '/'Advanced ' prefix or fuzzy substring
        if row is None:
            stripped = re.sub(r"^(Expert|Advanced)\s+", "", clean, flags=re.I)
            row = self._conn.execute(
                """
                SELECT * FROM base_items
                WHERE lower(name) LIKE '%' || lower(?) || '%' AND poe2db_slug IS NOT NULL
                ORDER BY is_live_poe2 DESC, drop_level DESC
                LIMIT 1
                """,
                (stripped,),
            ).fetchone()

        if row is None:
            # 4. Synthetic fallback if slug exists in mod_tiers directly (e.g. Breach_Tablet / Ruby)
            slug_probe = clean.replace(" ", "_")
            has_slug = self._conn.execute(
                "SELECT 1 FROM mod_tiers WHERE lower(base_group_slug) = lower(?) LIMIT 1",
                (slug_probe,),
            ).fetchone()
            if has_slug:
                canon_slug = self._conn.execute(
                    "SELECT base_group_slug FROM mod_tiers "
                    "WHERE lower(base_group_slug) = lower(?) LIMIT 1",
                    (slug_probe,),
                ).fetchone()[0]
                is_small = any(
                    k in canon_slug for k in ("Tablet", "Ruby", "Emerald", "Sapphire", "Diamond")
                )
                max_ps = (
                    2 if is_small else (1 if "Flask" in canon_slug or canon_slug == "Charms" else 3)
                )
                entry = BaseItemEntry(
                    id=f"Slug/{canon_slug}",
                    name=canon_slug.replace("_", " "),
                    item_class=canon_slug,
                    poe2db_slug=canon_slug,
                    drop_level=1,
                    max_prefixes=max_ps,
                    max_suffixes=max_ps,
                    tags=("default",),
                    implicits=(),
                    is_live_poe2=True,
                )
                self._bases_cache[cache_key] = entry
                return entry
            raise ValueError(
                f"Could not resolve PoE2 base item or class slug: {base_name_or_slug!r}"
            )

        entry = BaseItemEntry(
            id=row["id"],
            name=row["name"],
            item_class=row["item_class"],
            poe2db_slug=row["poe2db_slug"],
            drop_level=int(row["drop_level"]),
            max_prefixes=int(row["max_prefixes"]),
            max_suffixes=int(row["max_suffixes"]),
            tags=tuple(json.loads(row["tags_json"])),
            implicits=tuple(json.loads(row["implicits_json"])),
            is_live_poe2=bool(row["is_live_poe2"]),
        )
        self._bases_cache[cache_key] = entry
        return entry

    def get_mod_tiers(self, base_slug: str, source_pool: str | None = None) -> list[ModEntry]:
        if base_slug not in self._mods_by_slug:
            rows = self._conn.execute(
                "SELECT * FROM mod_tiers WHERE base_group_slug = ? "
                "ORDER BY affix_side, mod_type, display_tier",
                (base_slug,),
            ).fetchall()
            parsed: list[ModEntry] = []
            for r in rows:
                side_str = r["affix_side"]
                side = (
                    AffixSide(side_str)
                    if side_str in AffixSide._value2member_map_
                    else AffixSide.NONE
                )
                sw_raw = json.loads(r["spawn_weights_json"])
                sw_tuples = tuple(
                    (str(x["tag"]), int(x["weight"])) for x in sw_raw if isinstance(x, dict)
                )
                parsed.append(
                    ModEntry(
                        mod_id=r["mod_id"],
                        mod_type=r["mod_type"],
                        family_id=r["family_id"],
                        families=tuple(json.loads(r["families_json"])),
                        base_group_slug=r["base_group_slug"],
                        source_pool=r["source_pool"],
                        affix_side=side,
                        affix_name=r["affix_name"],
                        internal_rank_asc=int(r["internal_rank_asc"]),
                        display_tier=int(r["display_tier"]),
                        required_level=int(r["required_level"]),
                        weight=int(r["weight"]),
                        weight_confidence=r["weight_confidence"],
                        boss_tag=r["boss_tag"],
                        currency_name=r["currency_name"],
                        is_alloy=bool(r["is_alloy"]),
                        removes_mod=bool(r["removes_mod"]),
                        adds_tags=tuple(json.loads(r["adds_tags_json"])),
                        spawn_weights=sw_tuples,
                        mod_tags=tuple(json.loads(r["mod_tags_json"])),
                        stat_text=r["stat_text"],
                    )
                )
            seen_ids = {m.mod_id for m in parsed}
            for lv in self._all_levers:
                if not lv.is_active_drop_in_patch or not lv.forced_mods:
                    continue
                forced_mid = lv.forced_mods.get(base_slug)
                if forced_mid is None:
                    for k_cls, v_mid in lv.forced_mods.items():
                        mapped_pref = POB_CLASS_TO_SLUG_PREFIX.get(k_cls)
                        if mapped_pref and base_slug.startswith(mapped_pref):
                            forced_mid = v_mid
                            break
                if forced_mid and forced_mid not in seen_ids:
                    frow = self._conn.execute(
                        "SELECT * FROM mod_tiers WHERE mod_id = ? LIMIT 1", (forced_mid,)
                    ).fetchone()
                    if frow is not None and frow["affix_side"] in ("prefix", "suffix"):
                        seen_ids.add(forced_mid)
                        parsed.append(
                            ModEntry(
                                mod_id=frow["mod_id"],
                                mod_type=frow["mod_type"],
                                family_id=frow["family_id"],
                                families=tuple(json.loads(frow["families_json"])),
                                base_group_slug=base_slug,
                                source_pool=frow["source_pool"]
                                if frow["source_pool"] != "normal"
                                else "essence",
                                affix_side=AffixSide(frow["affix_side"]),
                                affix_name=frow["affix_name"],
                                internal_rank_asc=int(frow["internal_rank_asc"]),
                                display_tier=int(frow["display_tier"]),
                                required_level=int(frow["required_level"]),
                                weight=0,
                                weight_confidence="deterministic",
                                boss_tag=None,
                                currency_name=lv.name,
                                is_alloy=lv.category == "alloy",
                                removes_mod=lv.category
                                in ("perfect_or_corrupted_essence", "alloy"),
                                adds_tags=tuple(json.loads(frow["adds_tags_json"])),
                                spawn_weights=(),
                                mod_tags=tuple(json.loads(frow["mod_tags_json"])),
                                stat_text=frow["stat_text"],
                            )
                        )
            self._mods_by_slug[base_slug] = parsed

        all_mods = self._mods_by_slug[base_slug]
        if source_pool is None:
            return list(all_mods)
        return [m for m in all_mods if m.source_pool == source_pool]

    def resolve_target(
        self,
        base_slug: str,
        target: str | dict[str, Any] | TargetSpec,
        ilvl: int = 82,
    ) -> TargetSpec:
        if isinstance(target, TargetSpec):
            return target

        max_tier: int | None = 1
        explicit_tier_given = False
        pool_filter: str | None = None
        if isinstance(target, dict):
            query = str(
                target.get("query") or target.get("mod") or target.get("mod_id") or ""
            ).strip()
            explicit_tier_given = "max_display_tier" in target or "tier" in target
            max_tier = target.get("max_display_tier", target.get("tier", 1))
            pool_filter = target.get("source_pool")
        else:
            query = target.strip()
            # Parse inline tier prefix like "T2 Spell Damage" or "T1+ Cast Speed"
            tm = re.match(r"^[Tt](?:ier\s*)?(\d+)\+?\s+(.+)$", query)
            if tm:
                explicit_tier_given = True
                max_tier = int(tm.group(1))
                query = tm.group(2).strip()

        requested_val: float | None = None
        if not explicit_tier_given:
            vm = re.search(r"(?<![a-zA-Z])[+-]?(\d+(?:\.\d+)?)", query)
            if vm:
                requested_val = float(vm.group(1))

        candidates = [
            m
            for m in self.get_mod_tiers(base_slug)
            if m.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)
            and (pool_filter is None or m.source_pool == pool_filter)
        ]
        if not candidates:
            raise ValueError(f"No prefix/suffix modifiers found for base_slug={base_slug!r}")

        q_low = query.lower()
        # 1. Exact mod_id match (tolerating GGG trailing '_')
        exact_id = [
            m
            for m in candidates
            if m.mod_id.lower() == q_low or m.mod_id.rstrip("_").lower() == q_low.rstrip("_")
        ]
        if exact_id:
            hit = exact_id[0]
            same_ladder = [
                m
                for m in candidates
                if m.source_pool == hit.source_pool
                and m.mod_type == hit.mod_type
                and m.display_tier <= hit.display_tier
                and m.required_level <= ilvl
            ]
            chosen = same_ladder or [hit]
            return TargetSpec(
                label=f"{hit.stat_text} (T{hit.display_tier}+)",
                affix_side=hit.affix_side,
                mod_type=hit.mod_type,
                family_id=hit.family_id,
                matching_mod_ids=frozenset(m.mod_id for m in chosen),
                matching_entries=tuple(chosen),
                max_display_tier=hit.display_tier,
            )

        # 2. Exact mod_type or family_id match (prefer normal pool if available)
        by_type = [
            m for m in candidates if m.mod_type.lower() == q_low or m.family_id.lower() == q_low
        ]
        if not by_type:
            # 3. Substring / token match on stat_text or affix_name
            raw_tokens = [
                tok.lstrip("+")
                for tok in re.split(r"\s+", re.sub(r"[^a-z0-9+%]+", " ", q_low))
                if tok.lstrip("+")
            ]
            word_tokens = [t for t in raw_tokens if not re.fullmatch(r"\d+%?", t)]
            q_tokens = raw_tokens
            scored: list[tuple[int, str, str]] = []
            grouped_ladders: dict[tuple[str, str], list[ModEntry]] = {}
            for m in candidates:
                grouped_ladders.setdefault((m.source_pool, m.mod_type), []).append(m)

            for (spool, mtype), mlist in grouped_ladders.items():
                sample_text = " ".join(
                    f"{x.stat_text} {x.affix_name} {x.mod_type} {x.boss_tag or ''}" for x in mlist
                ).lower()
                tok_ok = q_tokens and all(t in sample_text for t in q_tokens)
                word_ok = (
                    not tok_ok
                    and requested_val is not None
                    and len(word_tokens) >= 2
                    and all(t in sample_text for t in word_tokens)
                    and any(
                        (b := _extract_primary_roll_bounds(x.stat_text)) is not None
                        and b[0] <= requested_val <= b[1]
                        for x in mlist
                    )
                )
                if q_low in sample_text or tok_ok or word_ok:
                    pool_bonus = 100 if spool == "normal" else (60 if spool == "desecrated" else 40)
                    exact_sub = 50 if any(q_low in x.stat_text.lower() for x in mlist) else 0
                    shortest = min(len(x.stat_text) for x in mlist)
                    scored.append((pool_bonus + exact_sub - shortest, spool, mtype))

            if not scored:
                raise ValueError(f"Could not match modifier target {query!r} on {base_slug!r}")
            scored.sort(reverse=True)
            best_pool, best_type = scored[0][1], scored[0][2]
            by_type = [
                m for m in candidates if m.source_pool == best_pool and m.mod_type == best_type
            ]
        else:
            normal_only = [m for m in by_type if m.source_pool == "normal"]
            if normal_only:
                first_type = normal_only[0].mod_type
                by_type = [m for m in normal_only if m.mod_type == first_type]

        ilvl_ok = [m for m in by_type if m.required_level <= ilvl]
        if not ilvl_ok:
            raise ValueError(
                f"Target {query!r} on {base_slug!r} requires higher item level than ilvl={ilvl}"
            )
        ilvl_ok.sort(key=lambda m: m.display_tier)
        best_available_tier = ilvl_ok[0].display_tier
        effective_max_tier = (
            max(best_available_tier, max_tier) if max_tier is not None else ilvl_ok[-1].display_tier
        )
        chosen = [m for m in ilvl_ok if m.display_tier <= effective_max_tier]
        if requested_val is not None and not explicit_tier_given:
            val_hits = [
                m
                for m in ilvl_ok
                if (b := _extract_primary_roll_bounds(m.stat_text)) is not None
                and b[1] >= requested_val
            ]
            if val_hits:
                chosen = val_hits
                effective_max_tier = max(m.display_tier for m in chosen)

        top = chosen[0]

        # Include equivalent cross-pool Essence / Perfect Essence / Alloy / Desecrated mods
        # in the same family + affix side when their roll magnitude satisfies the target floor.
        if pool_filter is None:
            chosen_bounds = [
                b[0] for m in chosen if (b := _extract_primary_roll_bounds(m.stat_text)) is not None
            ]
            min_roll_floor = (
                requested_val
                if requested_val is not None
                else (min(chosen_bounds) if chosen_bounds else None)
            )
            chosen_ids = {m.mod_id for m in chosen}
            for cand in candidates:
                if cand.mod_id in chosen_ids or cand.required_level > ilvl:
                    continue
                if cand.source_pool == top.source_pool and cand.mod_type == top.mod_type:
                    continue
                if cand.affix_side != top.affix_side or cand.family_id != top.family_id:
                    continue
                if cand.source_pool not in ("essence", "perfect_essence", "alloy", "desecrated"):
                    continue
                c_bounds = _extract_primary_roll_bounds(cand.stat_text)
                if min_roll_floor is not None and c_bounds is not None:
                    if c_bounds[1] >= min_roll_floor:
                        chosen.append(cand)
                        chosen_ids.add(cand.mod_id)

        return TargetSpec(
            label=f"{top.stat_text} (<=T{effective_max_tier})",
            affix_side=top.affix_side,
            mod_type=top.mod_type,
            family_id=top.family_id,
            matching_mod_ids=frozenset(m.mod_id for m in chosen),
            matching_entries=tuple(chosen),
            max_display_tier=effective_max_tier,
        )

    def parse_state_from_item(self, item: ParsedItem) -> CraftingState:
        base = self.resolve_base(item.base_type)
        ilvl = item.item_level or max(base.drop_level, item.requirements.level, 82)
        all_tiers = self.get_mod_tiers(base.poe2db_slug)

        pattern_map: dict[str, list[ModEntry]] = {}
        for entry in all_tiers:
            if entry.affix_side in (
                AffixSide.PREFIX,
                AffixSide.SUFFIX,
            ) and entry.source_pool not in (
                "corrupted",
                "corruption_upgrade",
            ):
                pat = _strip_numbers_to_pattern(entry.stat_text)
                pattern_map.setdefault(pat, []).append(entry)

        catalyst_scalar = (
            1.0 + (item.quality / 100.0) if (item.catalyst_type and item.quality > 0) else 1.0
        )

        def _pick_best_entry(
            candidates: list[ModEntry],
            pmod_side: AffixSide | None,
            pmod_name: str | None,
            pmod_tier: int | None,
            pmod_req_lvl: int | None,
            pmod_domain: ModDomain,
            pmod_vals: list[float],
            pmod_ranges: list[tuple[float, float]],
        ) -> ModEntry | None:
            filtered = [
                m
                for m in candidates
                if pmod_side not in (AffixSide.PREFIX, AffixSide.SUFFIX)
                or m.affix_side == pmod_side
            ] or list(candidates)
            if not filtered:
                return None

            best_entry: ModEntry | None = None
            best_key: tuple[float, float, int] | None = None
            abs_vals = [abs(v) for v in pmod_vals]

            for m in filtered:
                score = 0.0
                # 1. Source pool preference
                if pmod_domain == ModDomain.DESECRATED:
                    score += 120.0 if m.source_pool == "desecrated" else 40.0
                else:
                    if m.source_pool == "normal":
                        score += 100.0
                    elif m.source_pool in ("perfect_essence", "alloy", "essence"):
                        score += 35.0
                    elif m.source_pool == "desecrated":
                        score += 10.0

                # 2. Item level feasibility (soft bonus so sanctified/drop anomalies still resolve)
                if m.required_level <= ilvl:
                    score += 30.0
                else:
                    score -= 200.0

                # 3. Ctrl+Alt+C affix name & required_level & display_tier
                if pmod_name and m.affix_name and m.affix_name.lower() == pmod_name.lower():
                    score += 500.0
                if pmod_req_lvl is not None and m.required_level == pmod_req_lvl:
                    score += 450.0
                if pmod_tier is not None:
                    if m.display_tier == pmod_tier:
                        score += 400.0 if m.source_pool == "normal" else 220.0
                    else:
                        score -= 150.0 * abs(m.display_tier - pmod_tier)

                # 4. Ctrl+Alt+C inline roll ranges `(min-max)` vs SQLite bounds
                entry_bounds = _extract_all_roll_bounds(m.stat_text)
                if pmod_ranges and entry_bounds and len(pmod_ranges) == len(entry_bounds):
                    exact_raw = all(
                        abs(r[0] - b[0]) <= 0.05 and abs(r[1] - b[1]) <= 0.05
                        for r, b in zip(pmod_ranges, entry_bounds, strict=False)
                    )
                    exact_cat = catalyst_scalar > 1.0 and all(
                        abs(r[0] - math.floor(b[0] * catalyst_scalar)) <= 1.0
                        and abs(r[1] - math.floor(b[1] * catalyst_scalar)) <= 1.0
                        for r, b in zip(pmod_ranges, entry_bounds, strict=False)
                    )
                    if exact_raw or exact_cat:
                        score += 1000.0

                # 5. Numeric value fit (resolves exact tier & required_level on plain Ctrl+C!)
                dist_penalty = 0.0
                if abs_vals and entry_bounds:
                    n_cmp = min(len(abs_vals), len(entry_bounds))
                    all_in = True
                    for idx_v in range(n_cmp):
                        v = abs_vals[idx_v]
                        lo, hi = entry_bounds[idx_v]
                        d_raw = 0.0 if lo <= v <= hi else min(abs(v - lo), abs(v - hi))
                        if catalyst_scalar > 1.0:
                            v_unscaled = v / catalyst_scalar
                            d_cat = (
                                0.0
                                if lo <= v_unscaled <= hi
                                else min(abs(v_unscaled - lo), abs(v_unscaled - hi))
                            )
                            d_best = min(d_raw, d_cat)
                        else:
                            d_best = d_raw
                        if d_best > 0.0:
                            all_in = False
                            dist_penalty += d_best
                    if all_in:
                        score += 320.0
                    else:
                        score -= min(600.0, dist_penalty * 8.0)

                cand_key = (score, -dist_penalty, -m.display_tier)
                if best_key is None or cand_key > best_key:
                    best_key = cand_key
                    best_entry = m

            return best_entry

        craft_mods: list[CraftingMod] = []
        eff_pmods = [
            pm
            for pm in item.mods
            if pm.domain in (ModDomain.EXPLICIT, ModDomain.FRACTURED, ModDomain.DESECRATED)
        ]
        idx_pm = 0
        while idx_pm < len(eff_pmods):
            pmod = eff_pmods[idx_pm]
            # Check if two consecutive plain Ctrl+C lines form a single 2-line hybrid affix
            if idx_pm + 1 < len(eff_pmods):
                nxt_pm = eff_pmods[idx_pm + 1]
                if (
                    pmod.domain == nxt_pm.domain
                    and pmod.affix_name is None
                    and nxt_pm.affix_name is None
                ):
                    combo_text = f"{pmod.raw_text}\n{nxt_pm.raw_text}"
                    combo_pat = _strip_numbers_to_pattern(combo_text)
                    combo_matches = pattern_map.get(combo_pat)
                    if combo_matches:
                        combo_ranges = [
                            (abs(r.min), abs(r.max))
                            for r in (*pmod.roll_ranges, *nxt_pm.roll_ranges)
                        ]
                        combo_vals = [*pmod.values, *nxt_pm.values]
                        chosen_combo = _pick_best_entry(
                            combo_matches,
                            pmod.affix_side,
                            pmod.affix_name,
                            pmod.display_tier,
                            pmod.required_level,
                            pmod.domain,
                            combo_vals,
                            combo_ranges,
                        )
                        if chosen_combo is not None:
                            cb_bounds = _extract_all_roll_bounds(chosen_combo.stat_text)
                            fits_hybrid_bounds = len(combo_vals) == len(cb_bounds) and all(
                                (lo <= abs(v) <= hi)
                                or (
                                    catalyst_scalar > 1.0 and lo <= (abs(v) / catalyst_scalar) <= hi
                                )
                                for v, (lo, hi) in zip(combo_vals, cb_bounds, strict=False)
                            )
                            if fits_hybrid_bounds:
                                craft_mods.append(
                                    CraftingMod.from_entry(
                                        chosen_combo,
                                        fractured=(pmod.domain == ModDomain.FRACTURED),
                                        is_desecrated=(pmod.domain == ModDomain.DESECRATED),
                                    )
                                )
                                idx_pm += 2
                                continue

            pat = _strip_numbers_to_pattern(pmod.raw_text)
            matches = pattern_map.get(pat) or []
            pm_ranges = [(abs(r.min), abs(r.max)) for r in pmod.roll_ranges]
            chosen = _pick_best_entry(
                matches,
                pmod.affix_side,
                pmod.affix_name,
                pmod.display_tier,
                pmod.required_level,
                pmod.domain,
                pmod.values,
                pm_ranges,
            )

            if chosen is not None:
                craft_mods.append(
                    CraftingMod.from_entry(
                        chosen,
                        fractured=(pmod.domain == ModDomain.FRACTURED),
                        is_desecrated=(pmod.domain == ModDomain.DESECRATED),
                    )
                )
            else:
                side = (
                    pmod.affix_side
                    if pmod.affix_side in (AffixSide.PREFIX, AffixSide.SUFFIX)
                    else AffixSide.PREFIX
                )
                syn_fam = f"Unknown_{len(craft_mods)}"
                craft_mods.append(
                    CraftingMod(
                        mod_id=syn_fam,
                        mod_type=syn_fam,
                        family_id=syn_fam,
                        families=(syn_fam,),
                        affix_side=side,
                        display_tier=pmod.display_tier or 1,
                        required_level=pmod.required_level or 1,
                        stat_text=pmod.raw_text,
                        fractured=(pmod.domain == ModDomain.FRACTURED),
                        is_desecrated=(pmod.domain == ModDomain.DESECRATED),
                    )
                )
            idx_pm += 1

        cat_tag = item.catalyst_type.split()[0].lower() if item.catalyst_type else None
        return CraftingState(
            base=base,
            ilvl=ilvl,
            rarity=item.rarity,
            mods=craft_mods,
            quality=item.quality,
            catalyst_tag=cat_tag,
        )

    def _is_tag_eligible(self, mod: ModEntry, effective_tags: set[str]) -> bool:
        if not mod.spawn_weights:
            if (
                "fire" in mod.mod_tags
                and "caster" in mod.mod_tags
                and "no_fire_spell_mods" in effective_tags
            ):
                return False
            if (
                "cold" in mod.mod_tags
                and "caster" in mod.mod_tags
                and "no_cold_spell_mods" in effective_tags
            ):
                return False
            if (
                "lightning" in mod.mod_tags
                and "caster" in mod.mod_tags
                and "no_lightning_spell_mods" in effective_tags
            ):
                return False
            if (
                "chaos" in mod.mod_tags
                and "caster" in mod.mod_tags
                and "no_chaos_spell_mods" in effective_tags
            ):
                return False
            if (
                "physical" in mod.mod_tags
                and "caster" in mod.mod_tags
                and "no_physical_spell_mods" in effective_tags
            ):
                return False
            return True

        # First matching tag in GGG's ordered spawn_weights table wins!
        # Critically, check negative/blocking tags (`no_*`) on the item/base first if present,
        # and follow exact spawn_weights order when specific base tags match.
        for tag, w in mod.spawn_weights:
            if tag in effective_tags:
                if tag == "default":
                    return w > 0 or mod.weight > 0
                return w > 0
        return True

    def eligible_pool(
        self,
        state: CraftingState,
        *,
        source_pool: str = "normal",
        target_rarity: ItemRarity | None = None,
        side_constraint: AffixSide | None = None,
        min_mod_level: int = 1,
        boss_tag: str | None = None,
        use_catalyst: bool = False,
    ) -> list[tuple[ModEntry, int]]:
        eff_rarity = target_rarity or state.rarity
        open_p = state.has_open_prefix(eff_rarity)
        open_s = state.has_open_suffix(eff_rarity)
        if side_constraint == AffixSide.PREFIX:
            open_s = False
        elif side_constraint == AffixSide.SUFFIX:
            open_p = False
        if not open_p and not open_s:
            return []

        blocked_fams = state.blocked_families()
        eff_tags = state.effective_tags()

        pools_to_scan = (
            [source_pool] if source_pool != "desecrated_combined" else ["normal", "desecrated"]
        )
        out: list[tuple[ModEntry, int]] = []

        for pname in pools_to_scan:
            for mod in self.get_mod_tiers(state.base.poe2db_slug, pname):
                if mod.affix_side == AffixSide.PREFIX and not open_p:
                    continue
                if mod.affix_side == AffixSide.SUFFIX and not open_s:
                    continue
                if mod.affix_side not in (AffixSide.PREFIX, AffixSide.SUFFIX):
                    continue
                if mod.required_level > state.ilvl or mod.required_level < min_mod_level:
                    continue
                if boss_tag is not None and mod.boss_tag != boss_tag:
                    continue
                if any(f in blocked_fams for f in mod.families):
                    continue
                if not self._is_tag_eligible(mod, eff_tags):
                    continue

                w = mod.weight
                if boss_tag is not None and pname == "desecrated":
                    w = 1000
                if w <= 0:
                    continue
                if use_catalyst and state.catalyst_tag and state.quality > 0:
                    if state.catalyst_tag in mod.mod_tags:
                        w = int(round(w * (1.0 + state.quality / 20.0)))
                out.append((mod, w))

        return out

    def _lookup_forced_mod_entry(self, base: BaseItemEntry, lever: LeverEntry) -> ModEntry | None:
        if lever.name == "Essence of the Abyss":
            return ModEntry(
                mod_id=MARK_OF_ABYSS_MOD_ID,
                mod_type=MARK_OF_ABYSS_MOD_ID,
                family_id=MARK_OF_ABYSS_MOD_ID,
                families=(MARK_OF_ABYSS_MOD_ID,),
                base_group_slug=base.poe2db_slug,
                source_pool="perfect_essence",
                affix_side=AffixSide.SUFFIX,
                affix_name="of the Abyss",
                internal_rank_asc=1,
                display_tier=1,
                required_level=1,
                weight=0,
                weight_confidence="deterministic",
                boss_tag=None,
                currency_name=lever.name,
                is_alloy=False,
                removes_mod=True,
                adds_tags=(),
                spawn_weights=(),
                mod_tags=(),
                stat_text="Bears the Mark of the Abyssal Lord",
            )

        mod_id = lever.forced_mods.get(base.poe2db_slug) or lever.forced_mods.get(base.item_class)
        if mod_id is None:
            for k, v in lever.forced_mods.items():
                mapped_pref = POB_CLASS_TO_SLUG_PREFIX.get(k)
                if mapped_pref and base.poe2db_slug.startswith(mapped_pref):
                    mod_id = v
                    break
        if mod_id is None:
            return None

        for entry in self.get_mod_tiers(base.poe2db_slug):
            if entry.mod_id == mod_id:
                return entry

        row = self._conn.execute(
            "SELECT * FROM mod_tiers WHERE mod_id = ? LIMIT 1", (mod_id,)
        ).fetchone()
        if row is not None:
            side_str = row["affix_side"]
            side = AffixSide(side_str) if side_str in ("prefix", "suffix") else AffixSide.PREFIX
            return ModEntry(
                mod_id=row["mod_id"],
                mod_type=row["mod_type"],
                family_id=row["family_id"],
                families=tuple(json.loads(row["families_json"])),
                base_group_slug=base.poe2db_slug,
                source_pool=row["source_pool"],
                affix_side=side,
                affix_name=row["affix_name"],
                internal_rank_asc=int(row["internal_rank_asc"]),
                display_tier=int(row["display_tier"]),
                required_level=int(row["required_level"]),
                weight=0,
                weight_confidence="deterministic",
                boss_tag=None,
                currency_name=lever.name,
                is_alloy=lever.category == "alloy",
                removes_mod=lever.category in ("perfect_or_corrupted_essence", "alloy"),
                adds_tags=tuple(json.loads(row["adds_tags_json"])),
                spawn_weights=(),
                mod_tags=tuple(json.loads(row["mod_tags_json"])),
                stat_text=row["stat_text"],
            )
        return None

    def _removable_indices(
        self,
        state: CraftingState,
        *,
        side_constraint: AffixSide | None = None,
        only_desecrated: bool = False,
        whittling: bool = False,
    ) -> list[int]:
        idxs = [
            i
            for i, m in enumerate(state.mods)
            if not m.fractured
            and (side_constraint is None or m.affix_side == side_constraint)
            and (not only_desecrated or m.is_desecrated)
        ]
        if whittling and idxs:
            min_lvl = min(state.mods[i].required_level for i in idxs)
            idxs = [i for i in idxs if state.mods[i].required_level == min_lvl]
        return idxs

    def evaluate_action(
        self,
        state: CraftingState,
        target_mod_ids: set[str],
        lever_name: str,
        omens: list[str] | None = None,
        *,
        protected_mod_ids: set[str] | None = None,
    ) -> PoolEvalResult:
        lever = self.resolve_lever(lever_name)
        omen_entries = tuple(self.resolve_lever(om) for om in (omens or []))
        protected = protected_mod_ids or set()

        side_con: AffixSide | None = None
        boss_con: str | None = None
        has_whittling = False
        has_light = False
        has_echoes = False
        has_catalysing = False
        extra_mods = False

        for om in omen_entries:
            if om.omen_side_constraint is not None:
                side_con = om.omen_side_constraint
            if om.omen_boss_constraint is not None:
                boss_con = om.omen_boss_constraint
            if om.lever_id == "omen_of_whittling":
                has_whittling = True
            elif om.lever_id == "omen_of_light":
                has_light = True
            elif om.lever_id == "omen_of_abyssal_echoes":
                has_echoes = True
            elif om.lever_id == "omen_of_catalysing_exaltation":
                has_catalysing = True
            elif om.lever_id == "omen_of_greater_exaltation":
                extra_mods = True

        if lever.max_item_level is not None and state.ilvl > lever.max_item_level:
            raise ValueError(
                f"{lever.name} has max item level {lever.max_item_level}, got ilvl={state.ilvl}"
            )

        floor = lever.min_mod_level_floor or 1

        # Case 1: Pure Add-1 Orbs (Transmute, Augment, Regal, Exalt)
        if lever.name in (
            "Orb of Transmutation",
            "Greater Orb of Transmutation",
            "Perfect Orb of Transmutation",
            "Orb of Augmentation",
            "Greater Orb of Augmentation",
            "Perfect Orb of Augmentation",
            "Regal Orb",
            "Greater Regal Orb",
            "Perfect Regal Orb",
            "Exalted Orb",
            "Greater Exalted Orb",
            "Perfect Exalted Orb",
        ):
            if "Transmutation" in lever.name:
                if state.rarity != ItemRarity.NORMAL:
                    return PoolEvalResult(
                        lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Normal item"
                    )
                next_rar = ItemRarity.MAGIC
            elif "Augmentation" in lever.name:
                if state.rarity != ItemRarity.MAGIC:
                    return PoolEvalResult(
                        lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Magic item"
                    )
                next_rar = ItemRarity.MAGIC
            elif "Regal" in lever.name:
                if state.rarity != ItemRarity.MAGIC:
                    return PoolEvalResult(
                        lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Magic item"
                    )
                next_rar = ItemRarity.RARE
            else:
                if state.rarity != ItemRarity.RARE:
                    return PoolEvalResult(
                        lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Rare item"
                    )
                next_rar = ItemRarity.RARE

            elig = self.eligible_pool(
                state,
                source_pool="normal",
                target_rarity=next_rar,
                side_constraint=side_con,
                min_mod_level=floor,
                use_catalyst=has_catalysing,
            )
            tot_w = sum(w for _, w in elig)
            hits = tuple(m for m, _ in elig if m.mod_id in target_mod_ids)
            hit_w = sum(w for m, w in elig if m.mod_id in target_mod_ids)
            p1 = (hit_w / tot_w) if tot_w > 0 else 0.0
            if extra_mods and p1 < 1.0 and tot_w > 0:
                # Approximate 2-draw union probability for Greater Exaltation
                p1 = 1.0 - (1.0 - p1) ** 2
            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=p1,
                collateral_remove_probability=0.0,
                eligible_pool_weight=tot_w,
                target_weight=hit_w,
                matching_tiers=hits,
                notes=f"floor={floor}" + (f", side={side_con.value}" if side_con else ""),
            )

        # Case 2: Orb of Annulment
        if lever.name == "Orb of Annulment":
            if state.rarity not in (ItemRarity.MAGIC, ItemRarity.RARE):
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Magic/Rare"
                )
            rem = self._removable_indices(
                state, side_constraint=side_con, only_desecrated=has_light
            )
            if not rem:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "No removable mods")
            good_rem = sum(1 for idx in rem if state.mods[idx].mod_id not in protected)
            collat = sum(1 for idx in rem if state.mods[idx].mod_id in protected) / len(rem)
            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=good_rem / len(rem),
                collateral_remove_probability=collat,
                eligible_pool_weight=len(rem),
                target_weight=good_rem,
                matching_tiers=(),
                notes="Removes 1 non-protected mod" + (" (Desecrated only)" if has_light else ""),
            )

        # Case 3: Chaos Orb / Greater / Perfect (+ Erasure / Whittling)
        if lever.name in ("Chaos Orb", "Greater Chaos Orb", "Perfect Chaos Orb"):
            if state.rarity != ItemRarity.RARE:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Rare item")
            rem = self._removable_indices(state, side_constraint=side_con, whittling=has_whittling)
            if not rem:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "No removable mods")

            total_hit_prob = 0.0
            collat_prob = 0.0
            last_tot_w = 0
            last_hit_w = 0
            matched_map: dict[str, ModEntry] = {}

            for idx in rem:
                p_rem = 1.0 / len(rem)
                removed_mod = state.mods[idx]
                if removed_mod.mod_id in protected:
                    collat_prob += p_rem
                    continue
                sub_state = state.clone()
                sub_state.mods.pop(idx)
                elig = self.eligible_pool(
                    sub_state,
                    source_pool="normal",
                    target_rarity=ItemRarity.RARE,
                    min_mod_level=floor,
                )
                tot_w = sum(w for _, w in elig)
                hit_w = sum(w for m, w in elig if m.mod_id in target_mod_ids)
                for m, _ in elig:
                    if m.mod_id in target_mod_ids:
                        matched_map[m.mod_id] = m
                last_tot_w = tot_w
                last_hit_w = hit_w
                if tot_w > 0:
                    total_hit_prob += p_rem * (hit_w / tot_w)

            note_parts = [f"floor={floor}"]
            if has_whittling:
                removed_lvls = sorted({state.mods[i].required_level for i in rem})
                sniped_desc = ", ".join(
                    f"{state.mods[i].stat_text.splitlines()[0]} "
                    f"(T{state.mods[i].display_tier}, req_lvl={state.mods[i].required_level})"
                    for i in rem
                )
                note_parts.append(
                    f"Whittling removes min_ilvl={removed_lvls[0]} ({len(rem)} tied: {sniped_desc})"
                )
            if side_con:
                note_parts.append(f"Erasure side={side_con.value}")

            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=total_hit_prob,
                collateral_remove_probability=collat_prob,
                eligible_pool_weight=last_tot_w,
                target_weight=last_hit_w,
                matching_tiers=tuple(matched_map.values()),
                notes=", ".join(note_parts),
            )

        # Case 4: Regular Essences (Lesser / Normal / Greater: Magic -> Rare)
        if lever.category == "essence":
            if state.rarity != ItemRarity.MAGIC:
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Magic item"
                )
            forced = self._lookup_forced_mod_entry(state.base, lever)
            if forced is None:
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Not applicable to base"
                )
            if any(f in state.blocked_families() for f in forced.families):
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Mod family already present"
                )
            is_hit = 1.0 if forced.mod_id in target_mod_ids else 0.0
            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=is_hit,
                collateral_remove_probability=0.0,
                eligible_pool_weight=1,
                target_weight=int(is_hit),
                matching_tiers=(forced,) if is_hit else (),
                notes=f"Deterministic Magic->Rare: {forced.stat_text}",
            )

        # Case 5: Perfect / Corrupted Essences & Verisium Alloys (Rare remove-1 + forced-add-1)
        if lever.category in ("perfect_or_corrupted_essence", "alloy"):
            if state.rarity != ItemRarity.RARE:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Rare item")
            forced = self._lookup_forced_mod_entry(state.base, lever)
            if forced is None:
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Not applicable to base"
                )

            rem = self._removable_indices(state, side_constraint=side_con)
            if not rem:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "No removable mods")

            valid_hits = 0
            collat = 0
            for idx in rem:
                removed = state.mods[idx]
                if removed.mod_id in protected:
                    collat += 1
                    continue
                sub = state.clone()
                sub.mods.pop(idx)
                if forced.affix_side == AffixSide.PREFIX and not sub.has_open_prefix(
                    ItemRarity.RARE
                ):
                    continue
                if forced.affix_side == AffixSide.SUFFIX and not sub.has_open_suffix(
                    ItemRarity.RARE
                ):
                    continue
                if any(f in sub.blocked_families() for f in forced.families):
                    continue
                if forced.mod_id in target_mod_ids or forced.mod_id == MARK_OF_ABYSS_MOD_ID:
                    valid_hits += 1

            p_hit = valid_hits / len(rem)
            side_label = side_con.value if side_con else "any"
            notes_str = f"Removes 1 ({side_label}) + forces {forced.stat_text}"
            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=p_hit
                if (forced.mod_id in target_mod_ids or forced.mod_id == MARK_OF_ABYSS_MOD_ID)
                else 0.0,
                collateral_remove_probability=collat / len(rem),
                eligible_pool_weight=len(rem),
                target_weight=valid_hits,
                matching_tiers=(forced,) if valid_hits > 0 else (),
                notes=notes_str,
            )

        # Case 6: Fracturing Orb (Rare >= 4 mods, no existing fracture)
        if lever.name == "Fracturing Orb":
            if state.rarity != ItemRarity.RARE or len(state.mods) < 4 or state.has_fractured:
                return PoolEvalResult(
                    lever,
                    omen_entries,
                    0.0,
                    0.0,
                    0,
                    0,
                    (),
                    "Requires unfractured Rare item with >= 4 modifiers",
                )
            k = len(state.mods)
            hits_on_item = sum(1 for m in state.mods if m.mod_id in target_mod_ids)
            return PoolEvalResult(
                lever=lever,
                omens=omen_entries,
                hit_probability=hits_on_item / k,
                collateral_remove_probability=0.0,
                eligible_pool_weight=k,
                target_weight=hits_on_item,
                matching_tiers=(),
                notes=f"1/{k} fracture lock odds",
            )

        # Case 7: Abyssal Desecration Bones (Jawbone / Rib / Collarbone)
        if lever.category == "bone":
            if state.rarity != ItemRarity.RARE:
                return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Rare item")
            if state.has_desecrated:
                return PoolEvalResult(
                    lever,
                    omen_entries,
                    0.0,
                    0.0,
                    0,
                    0,
                    (),
                    "Item already has a Desecrated modifier (use Annul + Omen of Light first)",
                )

            pool_mode = "desecrated" if boss_con else "desecrated_combined"

            def _eval_unveil_on(
                sub_st: CraftingState,
            ) -> tuple[float, int, int, tuple[ModEntry, ...]]:
                elig = self.eligible_pool(
                    sub_st,
                    source_pool=pool_mode,
                    target_rarity=ItemRarity.RARE,
                    side_constraint=side_con,
                    min_mod_level=floor,
                    boss_tag=boss_con,
                )
                p3 = exact_three_choice_unveil_prob(elig, target_mod_ids)
                if has_echoes:
                    p3 = 1.0 - (1.0 - p3) ** 2
                tw = sum(w for _, w in elig)
                hw = sum(w for m, w in elig if m.mod_id in target_mod_ids)
                mt = tuple(m for m, _ in elig if m.mod_id in target_mod_ids)
                return p3, tw, hw, mt

            # Essence of the Abyss deterministic Mark replacement!
            if state.has_abyss_mark:
                sub = state.clone()
                sub.mods = [m for m in sub.mods if not m.is_abyss_mark]
                p_hit, tw, hw, mt = _eval_unveil_on(sub)
                return PoolEvalResult(
                    lever,
                    omen_entries,
                    p_hit,
                    0.0,
                    tw,
                    hw,
                    mt,
                    "Replaces Mark of the Abyssal Lord + 3-choice Well of Souls unveil"
                    + (" + Echoes reroll" if has_echoes else ""),
                )

            # Full 6-mod item (or full target side): removes 1 random mod first
            needs_remove = len(state.mods) >= (state.base.max_prefixes + state.base.max_suffixes)
            if side_con == AffixSide.PREFIX and not state.has_open_prefix(ItemRarity.RARE):
                needs_remove = True
            elif side_con == AffixSide.SUFFIX and not state.has_open_suffix(ItemRarity.RARE):
                needs_remove = True

            if needs_remove:
                rem = self._removable_indices(state, side_constraint=side_con)
                if not rem:
                    return PoolEvalResult(
                        lever, omen_entries, 0.0, 0.0, 0, 0, (), "No removable mods"
                    )
                p_acc = 0.0
                collat = 0.0
                tw_last, hw_last = 0, 0
                mt_last: tuple[ModEntry, ...] = ()
                for idx in rem:
                    if state.mods[idx].mod_id in protected:
                        collat += 1.0 / len(rem)
                        continue
                    sub = state.clone()
                    sub.mods.pop(idx)
                    p3, tw_last, hw_last, mt_last = _eval_unveil_on(sub)
                    p_acc += (1.0 / len(rem)) * p3
                return PoolEvalResult(
                    lever,
                    omen_entries,
                    p_acc,
                    collat,
                    tw_last,
                    hw_last,
                    mt_last,
                    f"Full-affix overwrite + 3-choice unveil (floor={floor})",
                )

            p_hit, tw, hw, mt = _eval_unveil_on(state)
            return PoolEvalResult(
                lever,
                omen_entries,
                p_hit,
                0.0,
                tw,
                hw,
                mt,
                f"3-choice Well of Souls unveil (floor={floor}"
                + (f", boss={boss_con}" if boss_con else "")
                + (", Echoes=2x" if has_echoes else "")
                + ")",
            )

        # Case 8: Orb of Alchemy (Normal -> Rare 4 mods)
        if lever.name == "Orb of Alchemy":
            if state.rarity != ItemRarity.NORMAL:
                return PoolEvalResult(
                    lever, omen_entries, 0.0, 0.0, 0, 0, (), "Requires Normal item"
                )
            elig = self.eligible_pool(
                state,
                source_pool="normal",
                target_rarity=ItemRarity.RARE,
                side_constraint=side_con,
            )
            tw = sum(w for _, w in elig)
            hw = sum(w for m, w in elig if m.mod_id in target_mod_ids)
            p_single = (hw / tw) if tw > 0 else 0.0
            p_approx_4 = 1.0 - (1.0 - p_single) ** (3 if side_con else 4)
            mt = tuple(m for m, _ in elig if m.mod_id in target_mod_ids)
            return PoolEvalResult(
                lever, omen_entries, p_approx_4, 0.0, tw, hw, mt, "4-mod Rare opener"
            )

        return PoolEvalResult(lever, omen_entries, 0.0, 0.0, 0, 0, (), "Unsupported action state")

    def sample_transition(
        self,
        state: CraftingState,
        lever_name: str,
        omens: list[str] | None,
        desired_mod_ids: set[str],
        rng: random.Random,
    ) -> CraftingState:
        """Apply a concrete stochastic currency transition to `state` using exact pool weights."""
        lever = self.resolve_lever(lever_name)
        omen_entries = [self.resolve_lever(om) for om in (omens or [])]
        nxt = state.clone()

        side_con: AffixSide | None = None
        boss_con: str | None = None
        has_whittling = False
        has_light = False
        has_echoes = False
        has_catalysing = False

        for om in omen_entries:
            if om.omen_side_constraint is not None:
                side_con = om.omen_side_constraint
            if om.omen_boss_constraint is not None:
                boss_con = om.omen_boss_constraint
            if om.lever_id == "omen_of_whittling":
                has_whittling = True
            elif om.lever_id == "omen_of_light":
                has_light = True
            elif om.lever_id == "omen_of_abyssal_echoes":
                has_echoes = True
            elif om.lever_id == "omen_of_catalysing_exaltation":
                has_catalysing = True

        floor = lever.min_mod_level_floor or 1

        def _weighted_pick(pool: list[tuple[ModEntry, int]]) -> ModEntry | None:
            tot = sum(w for _, w in pool)
            if tot <= 0:
                return None
            r = rng.uniform(0, tot)
            acc = 0.0
            for m, w in pool:
                acc += w
                if r <= acc:
                    return m
            return pool[-1][0]

        if "Transmutation" in lever.name and nxt.rarity == ItemRarity.NORMAL:
            nxt.rarity = ItemRarity.MAGIC
            pick = _weighted_pick(self.eligible_pool(nxt, min_mod_level=floor))
            if pick:
                nxt.mods.append(CraftingMod.from_entry(pick))
            return nxt

        if "Augmentation" in lever.name and nxt.rarity == ItemRarity.MAGIC:
            pick = _weighted_pick(self.eligible_pool(nxt, min_mod_level=floor))
            if pick:
                nxt.mods.append(CraftingMod.from_entry(pick))
            return nxt

        if "Regal" in lever.name and nxt.rarity == ItemRarity.MAGIC:
            nxt.rarity = ItemRarity.RARE
            pick = _weighted_pick(
                self.eligible_pool(nxt, side_constraint=side_con, min_mod_level=floor)
            )
            if pick:
                nxt.mods.append(CraftingMod.from_entry(pick))
            return nxt

        if "Exalted" in lever.name and nxt.rarity == ItemRarity.RARE:
            pick = _weighted_pick(
                self.eligible_pool(
                    nxt,
                    side_constraint=side_con,
                    min_mod_level=floor,
                    use_catalyst=has_catalysing,
                )
            )
            if has_catalysing:
                nxt.quality = 0
            if pick:
                nxt.mods.append(CraftingMod.from_entry(pick))
            return nxt

        if lever.name == "Orb of Annulment":
            rem = self._removable_indices(nxt, side_constraint=side_con, only_desecrated=has_light)
            if rem:
                nxt.mods.pop(rng.choice(rem))
            return nxt

        if "Chaos" in lever.name and nxt.rarity == ItemRarity.RARE:
            rem = self._removable_indices(nxt, side_constraint=side_con, whittling=has_whittling)
            if rem:
                nxt.mods.pop(rng.choice(rem))
                pick = _weighted_pick(self.eligible_pool(nxt, min_mod_level=floor))
                if pick:
                    nxt.mods.append(CraftingMod.from_entry(pick))
            return nxt

        if lever.category == "essence" and nxt.rarity == ItemRarity.MAGIC:
            nxt.rarity = ItemRarity.RARE
            forced = self._lookup_forced_mod_entry(nxt.base, lever)
            if forced and not any(f in nxt.blocked_families() for f in forced.families):
                nxt.mods.append(CraftingMod.from_entry(forced))
            return nxt

        if (
            lever.category in ("perfect_or_corrupted_essence", "alloy")
            and nxt.rarity == ItemRarity.RARE
        ):
            forced = self._lookup_forced_mod_entry(nxt.base, lever)
            rem = self._removable_indices(nxt, side_constraint=side_con)
            if forced and rem:
                nxt.mods.pop(rng.choice(rem))
                if not any(f in nxt.blocked_families() for f in forced.families):
                    nxt.mods.append(CraftingMod.from_entry(forced))
            return nxt

        if lever.name == "Fracturing Orb" and nxt.rarity == ItemRarity.RARE and len(nxt.mods) >= 4:
            if not nxt.has_fractured:
                idx = rng.randrange(len(nxt.mods))
                old = nxt.mods[idx]
                nxt.mods[idx] = CraftingMod(
                    mod_id=old.mod_id,
                    mod_type=old.mod_type,
                    family_id=old.family_id,
                    families=old.families,
                    affix_side=old.affix_side,
                    display_tier=old.display_tier,
                    required_level=old.required_level,
                    stat_text=old.stat_text,
                    adds_tags=old.adds_tags,
                    mod_tags=old.mod_tags,
                    fractured=True,
                    is_desecrated=old.is_desecrated,
                    is_abyss_mark=old.is_abyss_mark,
                )
            return nxt

        if lever.category == "bone" and nxt.rarity == ItemRarity.RARE and not nxt.has_desecrated:
            if nxt.has_abyss_mark:
                nxt.mods = [m for m in nxt.mods if not m.is_abyss_mark]
            elif len(nxt.mods) >= (nxt.base.max_prefixes + nxt.base.max_suffixes):
                rem = self._removable_indices(nxt, side_constraint=side_con)
                if rem:
                    nxt.mods.pop(rng.choice(rem))

            pool_mode = "desecrated" if boss_con else "desecrated_combined"
            elig = self.eligible_pool(
                nxt,
                source_pool=pool_mode,
                side_constraint=side_con,
                min_mod_level=floor,
                boss_tag=boss_con,
            )

            def _draw_three(pool_items: list[tuple[ModEntry, int]]) -> list[ModEntry]:
                local_pool = list(pool_items)
                picks: list[ModEntry] = []
                for _ in range(min(3, len(local_pool))):
                    chosen = _weighted_pick(local_pool)
                    if chosen is None:
                        break
                    picks.append(chosen)
                    local_pool = [(m, w) for m, w in local_pool if m.mod_id != chosen.mod_id]
                return picks

            choices = _draw_three(elig)
            hit_choice = next((c for c in choices if c.mod_id in desired_mod_ids), None)
            if hit_choice is None and has_echoes:
                choices = _draw_three(elig)
                hit_choice = next((c for c in choices if c.mod_id in desired_mod_ids), None)

            selected = hit_choice or (choices[0] if choices else None)
            if selected is not None:
                nxt.mods.append(CraftingMod.from_entry(selected, is_desecrated=True))
            return nxt

        return nxt
