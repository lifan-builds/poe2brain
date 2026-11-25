# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH, get_cache_dir, get_gamedata_db_path
from poe2brain.filters.grammar import (
    FilterGamedataIndex,
    get_filter_gamedata_index,
    to_filter_class,
)
from poe2brain.filters.themes import BUILTIN_THEME_PRESETS, compose_theme, get_visual_theme
from poe2brain.filters.validator import FilterValidator
from poe2brain.models.filters import (
    BuildFilterContext,
    FilterCompileResult,
    SoundPackManifest,
    StrictnessTier,
    TierVisualStyle,
    VisualThemeSpec,
)
from poe2brain.ninja.xml_inspector import decode_pob2_code_to_xml, inspect_pob2_xml_or_code

if TYPE_CHECKING:
    from poe2brain.pob.service import BuildService
    from poe2brain.trade.economy import EconomyService


@dataclass(frozen=True, slots=True)
class StrictnessPolicy:
    min_currency_ex: float
    min_endgame_waystone_tier: int
    min_gold_stack_endgame: int
    min_uncut_gem_level: int
    min_unid_tier_generic: int
    min_unid_tier_build: int
    hide_endgame_normal_magic_gear: bool
    hide_endgame_untiered_rares: bool
    hide_campaign_clutter: bool


STRICTNESS_POLICIES: dict[StrictnessTier, StrictnessPolicy] = {
    StrictnessTier.SOFT: StrictnessPolicy(
        min_currency_ex=0.0,
        min_endgame_waystone_tier=1,
        min_gold_stack_endgame=1,
        min_uncut_gem_level=1,
        min_unid_tier_generic=2,
        min_unid_tier_build=2,
        hide_endgame_normal_magic_gear=False,
        hide_endgame_untiered_rares=False,
        hide_campaign_clutter=False,
    ),
    StrictnessTier.REGULAR: StrictnessPolicy(
        min_currency_ex=0.15,
        min_endgame_waystone_tier=1,
        min_gold_stack_endgame=50,
        min_uncut_gem_level=5,
        min_unid_tier_generic=2,
        min_unid_tier_build=2,
        hide_endgame_normal_magic_gear=True,
        hide_endgame_untiered_rares=False,
        hide_campaign_clutter=False,
    ),
    StrictnessTier.SEMI_STRICT: StrictnessPolicy(
        min_currency_ex=0.45,
        min_endgame_waystone_tier=5,
        min_gold_stack_endgame=150,
        min_uncut_gem_level=12,
        min_unid_tier_generic=3,
        min_unid_tier_build=2,
        hide_endgame_normal_magic_gear=True,
        hide_endgame_untiered_rares=False,
        hide_campaign_clutter=False,
    ),
    StrictnessTier.STRICT: StrictnessPolicy(
        min_currency_ex=1.25,
        min_endgame_waystone_tier=10,
        min_gold_stack_endgame=400,
        min_uncut_gem_level=16,
        min_unid_tier_generic=3,
        min_unid_tier_build=3,
        hide_endgame_normal_magic_gear=True,
        hide_endgame_untiered_rares=True,
        hide_campaign_clutter=False,
    ),
    StrictnessTier.VERY_STRICT: StrictnessPolicy(
        min_currency_ex=3.5,
        min_endgame_waystone_tier=13,
        min_gold_stack_endgame=800,
        min_uncut_gem_level=18,
        min_unid_tier_generic=4,
        min_unid_tier_build=3,
        hide_endgame_normal_magic_gear=True,
        hide_endgame_untiered_rares=True,
        hide_campaign_clutter=True,
    ),
    StrictnessTier.UBER_STRICT: StrictnessPolicy(
        min_currency_ex=10.0,
        min_endgame_waystone_tier=15,
        min_gold_stack_endgame=1500,
        min_uncut_gem_level=19,
        min_unid_tier_generic=5,
        min_unid_tier_build=4,
        hide_endgame_normal_magic_gear=True,
        hide_endgame_untiered_rares=True,
        hide_campaign_clutter=True,
    ),
}

# Offline Patch 0.5.5 Exalted-equivalent baseline when live EconomyService is not queried
OFFLINE_BASELINE_EXALTED: dict[str, float] = {
    "Mirror of Kalandra": 250_000.0,
    "Hinekora's Lock": 35_000.0,
    "Zarokh's Reliquary Key: Temporalis": 95_000.0,
    "Zarokh's Reliquary Key: Against the Darkness": 1_800.0,
    "Divine Orb": 420.0,
    "Fracturing Orb": 650.0,
    "Perfect Jeweller's Orb": 520.0,
    "Perfect Exalted Orb": 480.0,
    "Perfect Chaos Orb": 320.0,
    "Vaal Cultivation Orb": 310.0,
    "Omen of Whittling": 900.0,
    "Omen of Light": 780.0,
    "Omen of Sinistral Erasure": 620.0,
    "Omen of Dextral Erasure": 620.0,
    "Omen of Sinistral Annulment": 540.0,
    "Omen of Dextral Annulment": 540.0,
    "Omen of Corruption": 140.0,
    "Omen of Sanctification": 160.0,
    "Omen of Sinistral Crystallisation": 85.0,
    "Omen of Dextral Crystallisation": 85.0,
    "Azmeri Reliquary Key": 220.0,
    "Origin Core": 180.0,
    "Origin Cradle": 120.0,
    "Ancient Jawbone": 95.0,
    "Ancient Rib": 95.0,
    "Ancient Collarbone": 95.0,
    "Greater Jeweller's Orb": 45.0,
    "Greater Exalted Orb": 32.0,
    "Greater Chaos Orb": 24.0,
    "Primary Calamity Fragment": 55.0,
    "Secondary Calamity Fragment": 48.0,
    "Tertiary Calamity Fragment": 48.0,
    "Ancient Crisis Fragment": 65.0,
    "Weathered Crisis Fragment": 42.0,
    "Faded Crisis Fragment": 42.0,
    "Orb of Annulment": 28.0,
    "Orb of Chance": 14.0,
    "Chaos Orb": 6.5,
    "Gemcutter's Prism": 8.0,
    "Glassblower's Bauble": 4.5,
    "Exalted Orb": 1.0,
    "Vaal Orb": 1.8,
    "Orb of Alchemy": 1.2,
    "Regal Orb": 0.65,
    "Artificer's Orb": 0.8,
    "Lesser Jeweller's Orb": 0.6,
    "Armourer's Scrap": 0.35,
    "Blacksmith's Whetstone": 0.35,
    "Arcanist's Etcher": 0.45,
    "Orb of Augmentation": 0.08,
    "Orb of Transmutation": 0.05,
    "Scroll of Wisdom": 0.02,
}

CHANCING_BASES_055: tuple[str, ...] = (
    "Stellar Amulet",
    "Heavy Belt",
    "Sapphire Ring",
    "Gold Ring",
    "Emerald Ring",
    "Solar Amulet",
)

WEAPON_FILTER_CLASSES: frozenset[str] = frozenset(
    {
        "Bows",
        "Claws",
        "Crossbows",
        "Daggers",
        "Flails",
        "One Hand Axes",
        "One Hand Maces",
        "One Hand Swords",
        "Quarterstaves",
        "Sceptres",
        "Spears",
        "Staves",
        "Talismans",
        "Two Hand Axes",
        "Two Hand Maces",
        "Two Hand Swords",
        "Wands",
    }
)

OFFHAND_FILTER_CLASSES: frozenset[str] = frozenset({"Bucklers", "Foci", "Quivers", "Shields"})

ARMOUR_FILTER_CLASSES: tuple[str, ...] = ("Body Armours", "Helmets", "Gloves", "Boots")


def _quote_list(items: Sequence[str]) -> str:
    return " ".join(f'"{x}"' for x in sorted(dict.fromkeys(items)))


def _style_lines(
    style: TierVisualStyle,
    *,
    sound_pack: SoundPackManifest | None = None,
    require_custom_sounds: bool = False,
) -> list[str]:
    lines = [
        f"\tSetFontSize {style.font_size}",
        f"\tSetTextColor {style.text_color.to_filter_tokens()}",
        f"\tSetBorderColor {style.border_color.to_filter_tokens()}",
        f"\tSetBackgroundColor {style.background_color.to_filter_tokens()}",
    ]
    clip = sound_pack.clips.get(style.tier.value) if sound_pack else None
    if style.builtin_sound_id is not None and not (clip and require_custom_sounds):
        lines.append(f"\tPlayAlertSound {style.builtin_sound_id} {style.sound_volume}")
    if clip is not None:
        kw = "CustomAlertSound" if require_custom_sounds else "CustomAlertSoundOptional"
        lines.append(f'\t{kw} "{clip.relative_filter_path}" {style.sound_volume}')
    if style.beam is not None:
        lines.append(f"\t{style.beam.to_filter_line()}")
    if style.minimap_icon is not None:
        lines.append(f"\t{style.minimap_icon.to_filter_line()}")
    return lines


def _render_show_block(
    meta_type: str,
    meta_tier: str,
    conditions: Sequence[str],
    style: TierVisualStyle,
    *,
    sound_pack: SoundPackManifest | None = None,
    require_custom_sounds: bool = False,
    section_comment: str | None = None,
) -> str:
    out: list[str] = []
    if section_comment:
        out.append(f"# {section_comment}")
    out.append(f"Show # $type->{meta_type} $tier->{meta_tier}")
    for cond in conditions:
        out.append(f"\t{cond}")
    out.extend(
        _style_lines(style, sound_pack=sound_pack, require_custom_sounds=require_custom_sounds)
    )
    return "\n".join(out)


def _dedup_preserve_order(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(x for x in items if x))


def merge_build_filter_contexts(contexts: Sequence[BuildFilterContext]) -> BuildFilterContext:
    """Merge multiple PoB2 BuildFilterContexts into one shared co-op / multi-build party context."""
    valid_ctxs = [c for c in contexts if c is not None]
    if not valid_ctxs:
        raise ValueError("merge_build_filter_contexts requires at least one BuildFilterContext.")
    if len(valid_ctxs) == 1:
        single = valid_ctxs[0].model_copy(deep=True)
        if not single.party_members:
            ident = single.ascendancy_name or single.class_name
            if ident:
                lbl = f"{ident} ({single.main_skill})" if single.main_skill else ident
                single.party_members = [lbl]
        return single

    classes = _dedup_preserve_order([c.class_name or "" for c in valid_ctxs])
    ascendancies = _dedup_preserve_order(
        [(c.ascendancy_name or c.class_name or "") for c in valid_ctxs]
    )
    skills = _dedup_preserve_order([c.main_skill or "" for c in valid_ctxs])
    weapons = _dedup_preserve_order([w for c in valid_ctxs for w in c.weapon_filter_classes])
    offhands = _dedup_preserve_order([o for c in valid_ctxs for o in c.offhand_filter_classes])
    armour_tags = _dedup_preserve_order([t for c in valid_ctxs for t in c.preferred_armour_tags])
    resists = _dedup_preserve_order([r for c in valid_ctxs for r in c.uncapped_resists])
    equipped = _dedup_preserve_order([b for c in valid_ctxs for b in c.equipped_bases])

    members: list[str] = []
    for c in valid_ctxs:
        if c.party_members:
            for m in c.party_members:
                if m not in members:
                    members.append(m)
        else:
            ident = c.ascendancy_name or c.class_name or "Build"
            lbl = f"{ident} ({c.main_skill})" if c.main_skill else ident
            if lbl not in members:
                members.append(lbl)

    return BuildFilterContext(
        class_name=" + ".join(classes) if classes else None,
        ascendancy_name=" + ".join(ascendancies) if ascendancies else None,
        level=max(c.level for c in valid_ctxs),
        main_skill=" + ".join(skills) if skills else None,
        weapon_filter_classes=weapons,
        offhand_filter_classes=offhands,
        preferred_armour_tags=armour_tags,
        uncapped_resists=resists,
        equipped_bases=equipped,
        party_members=members,
    )


def _infer_class_weapon_and_offhand_defaults(hint: str) -> tuple[list[str], list[str]]:
    low = hint.lower()
    if any(k in low for k in ("ranger", "deadeye", "pathfinder", "arrow", "bow")):
        return (["Bows"], ["Quivers"])
    if any(
        k in low
        for k in (
            "monk",
            "martial artist",
            "invoker",
            "acolyte",
            "chayula",
            "quarterstaff",
            "flurry",
            "bell",
        )
    ):
        return (["Quarterstaves"], [])
    if any(
        k in low
        for k in (
            "mercenary",
            "witchhunter",
            "gemling",
            "tactician",
            "crossbow",
            "grenade",
            "bolt",
        )
    ):
        return (["Crossbows"], [])
    if any(k in low for k in ("warrior", "titan", "warbringer", "smith", "kitava", "slam", "mace")):
        return (["Two Hand Maces", "One Hand Maces"], ["Shields"])
    if any(
        k in low for k in ("huntress", "amazon", "spirit walker", "ritualist", "spear", "companion")
    ):
        return (["Spears"], ["Bucklers"])
    if any(k in low for k in ("druid", "shaman", "oracle", "talisman")):
        return (["Talismans", "Staves"], [])
    return (["Wands", "Sceptres", "Staves"], ["Foci"])


def _infer_class_armour_tags(hint: str) -> list[str]:
    low = hint.lower()
    if any(
        k in low
        for k in (
            "huntress",
            "amazon",
            "spirit walker",
            "ritualist",
            "ranger",
            "deadeye",
            "pathfinder",
        )
    ):
        return ["dex_armour", "dex_int_armour"]
    if any(k in low for k in ("monk", "martial artist", "invoker", "acolyte", "chayula")):
        return ["dex_int_armour", "dex_armour"]
    if any(k in low for k in ("mercenary", "witchhunter", "gemling", "tactician")):
        return ["str_dex_armour", "dex_armour", "str_armour"]
    if any(k in low for k in ("warrior", "titan", "warbringer", "smith", "kitava")):
        return ["str_armour", "str_dex_armour"]
    if any(k in low for k in ("druid", "shaman", "oracle")):
        return ["str_int_armour", "int_armour"]
    if any(
        k in low
        for k in (
            "witch",
            "infernalist",
            "lich",
            "blood mage",
            "sorceress",
            "stormweaver",
            "chronomancer",
            "varashta",
        )
    ):
        return ["int_armour"]
    return ["int_armour", "dex_armour", "str_armour"]


_SAFE_SLOT_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _build_filter_context_from_parts(
    *,
    class_name: str | None,
    ascendancy_name: str | None,
    level: int,
    main_skill: str | None,
    raw_base_names: Sequence[str],
    armour: float,
    evasion: float,
    energy_shield: float,
    fire_resist: float,
    cold_resist: float,
    lightning_resist: float,
    chaos_resist: float,
    idx: FilterGamedataIndex,
) -> BuildFilterContext:
    equipped_bases: list[str] = []
    weapon_classes: list[str] = []
    offhand_classes: list[str] = []

    con = sqlite3.connect(f"file:{get_gamedata_db_path()}?mode=ro", uri=True)
    try:
        base_to_ic = {
            name: ic
            for name, ic in con.execute(
                "SELECT name, item_class FROM base_items WHERE is_live_poe2 = 1"
            )
        }
    finally:
        con.close()

    for raw_bname in raw_base_names:
        bname = str(raw_bname or "").strip()
        if not bname:
            continue
        if bname not in equipped_bases:
            equipped_bases.append(bname)
        fc = to_filter_class(base_to_ic.get(bname, ""), idx)
        if not fc and bname in idx.basetype_to_classes:
            fc = next(iter(idx.basetype_to_classes[bname]), None)
        if fc in WEAPON_FILTER_CLASSES and fc not in weapon_classes:
            weapon_classes.append(fc)
        elif fc in OFFHAND_FILTER_CLASSES and fc not in offhand_classes:
            offhand_classes.append(fc)

    hint = f"{class_name or ''} {ascendancy_name or ''} {main_skill or ''}"

    # Infer archetype weapon/offhand classes when naked starter build or 1H weapon lacks offhand
    if not weapon_classes:
        def_w, def_o = _infer_class_weapon_and_offhand_defaults(hint)
        for wc in def_w:
            if wc not in weapon_classes:
                weapon_classes.append(wc)
        for oc in def_o:
            if oc not in offhand_classes:
                offhand_classes.append(oc)
    elif not offhand_classes:
        if "Spears" in weapon_classes:
            offhand_classes.append("Bucklers")
        elif "Bows" in weapon_classes:
            offhand_classes.append("Quivers")
        elif any(w in weapon_classes for w in ("Wands", "Sceptres")) and not any(
            w in weapon_classes
            for w in ("Spears", "Bows", "Crossbows", "Quarterstaves", "Talismans", "Two Hand Maces")
        ):
            offhand_classes.append("Foci")
        elif any(w in weapon_classes for w in ("One Hand Maces", "Flails")) and (
            "Two Hand Maces" not in weapon_classes
        ):
            offhand_classes.append("Shields")

    # Infer armour defense tags from BuildStats (falling back to class identity on naked slots)
    ar_v = float(armour or 0.0)
    ev_v = float(evasion or 0.0)
    es_v = float(energy_shield or 0.0)
    preferred_tags: list[str] = []
    if es_v >= 400 and ev_v >= 600:
        preferred_tags.extend(["dex_int_armour", "int_armour", "dex_armour"])
    elif es_v >= 400 and ar_v >= 600:
        preferred_tags.extend(["str_int_armour", "int_armour", "str_armour"])
    elif ar_v >= 600 and ev_v >= 600:
        preferred_tags.extend(["str_dex_armour", "str_armour", "dex_armour"])
    elif es_v >= max(ar_v, ev_v, 150.0):
        preferred_tags.append("int_armour")
    elif ev_v >= max(ar_v, 150.0):
        preferred_tags.append("dex_armour")
    elif ar_v >= 150.0:
        preferred_tags.append("str_armour")
    else:
        preferred_tags.extend(_infer_class_armour_tags(hint))

    uncapped: list[str] = []
    if float(fire_resist) < 75.0:
        uncapped.append("fire")
    if float(cold_resist) < 75.0:
        uncapped.append("cold")
    if float(lightning_resist) < 75.0:
        uncapped.append("lightning")
    if float(chaos_resist) < 40.0:
        uncapped.append("chaos")

    ident = ascendancy_name or class_name
    party_lbl = [f"{ident} ({main_skill})" if main_skill else ident] if ident else []

    return BuildFilterContext(
        class_name=class_name,
        ascendancy_name=ascendancy_name,
        level=level,
        main_skill=main_skill,
        weapon_filter_classes=weapon_classes,
        offhand_filter_classes=offhand_classes,
        preferred_armour_tags=preferred_tags,
        uncapped_resists=uncapped,
        equipped_bases=equipped_bases,
        party_members=party_lbl,
    )


def extract_build_filter_context_from_xml(
    code_or_xml: str | Path,
    *,
    index: FilterGamedataIndex | None = None,
) -> BuildFilterContext:
    """Extract BuildFilterContext in <2ms via pure-Python XML inspection (0 MB LuaJIT RSS)."""
    idx = index or get_filter_gamedata_index()
    raw_input = (
        code_or_xml.read_text(encoding="utf-8")
        if isinstance(code_or_xml, Path)
        else str(code_or_xml)
    )
    ins = inspect_pob2_xml_or_code(raw_input)
    xs = ins.xml_stats
    base_names = [
        it.base_type for it in ins.equipped_items if it.base_type and it.base_type != "Unknown"
    ]
    return _build_filter_context_from_parts(
        class_name=ins.class_name if ins.class_name != "Unknown" else None,
        ascendancy_name=ins.ascendancy_name,
        level=ins.level,
        main_skill=ins.main_skill,
        raw_base_names=base_names,
        armour=float(xs.get("Armour", 0.0)),
        evasion=float(xs.get("Evasion", 0.0)),
        energy_shield=float(xs.get("EnergyShield", ins.energy_shield)),
        fire_resist=float(xs.get("FireResist", 0.0)),
        cold_resist=float(xs.get("ColdResist", 0.0)),
        lightning_resist=float(xs.get("LightningResist", 0.0)),
        chaos_resist=float(xs.get("ChaosResist", 0.0)),
        idx=idx,
    )


def extract_build_filter_context(
    build_service: BuildService | None = None,
    *,
    pob_code: str | Path | None = None,
    slot: str = "main",
    index: FilterGamedataIndex | None = None,
) -> BuildFilterContext:
    idx = index or get_filter_gamedata_index()
    bridge_idle = build_service is None or (
        not build_service.bridge.is_running and not build_service.bridge.is_hibernated
    )

    if pob_code is not None:
        raw_pob = (
            pob_code.read_text(encoding="utf-8")
            if isinstance(pob_code, Path)
            else str(pob_code).strip()
        )
        if (
            not isinstance(pob_code, Path)
            and len(raw_pob) < 512
            and "\n" not in raw_pob
            and not raw_pob.startswith(("<", "{", "http://", "https://"))
            and Path(raw_pob).expanduser().is_file()
        ):
            raw_pob = Path(raw_pob).expanduser().read_text(encoding="utf-8")

        if not raw_pob.startswith(("{", "http://", "https://")):
            try:
                xml_text = decode_pob2_code_to_xml(raw_pob)
            except ValueError:
                xml_text = ""
            if xml_text and (build_service is None or (bridge_idle and "<PlayerStat" in xml_text)):
                return extract_build_filter_context_from_xml(xml_text, index=idx)

        if build_service is None:
            raise ValueError("build_service is required to evaluate non-XML or remote PoB2 inputs.")
        build_service.load_build(pob_code, slot=slot)
    elif bridge_idle:
        safe_slot = _SAFE_SLOT_FILENAME_RE.sub("_", slot.strip()) or "main"
        snap_path = get_cache_dir() / "pob_slots" / f"{safe_slot}.xml"
        if snap_path.is_file():
            snap_xml = snap_path.read_text(encoding="utf-8")
            if build_service is None or "<PlayerStat" in snap_xml:
                return extract_build_filter_context_from_xml(snap_xml, index=idx)
        if build_service is None:
            raise ValueError(f"No persisted PoB2 XML snapshot found for slot {slot!r}.")

    assert build_service is not None
    summary = build_service.inspect_build(slot=slot, include_item_raw=False)
    stats = summary["stats"]
    raw_bases = [
        str(entry.get("baseName") or "").strip()
        for entry in (summary.get("items") or [])
        if isinstance(entry, Mapping)
    ]
    return _build_filter_context_from_parts(
        class_name=stats.class_name,
        ascendancy_name=stats.ascendancy_name,
        level=stats.level,
        main_skill=stats.main_skill,
        raw_base_names=raw_bases,
        armour=float(stats.armour or 0.0),
        evasion=float(stats.evasion or 0.0),
        energy_shield=float(stats.energy_shield or 0.0),
        fire_resist=float(stats.fire_resist),
        cold_resist=float(stats.cold_resist),
        lightning_resist=float(stats.lightning_resist),
        chaos_resist=float(stats.chaos_resist),
        idx=idx,
    )


_SKILL_IMPLICIT_WEAPON_CLASSES: frozenset[str] = frozenset({"Sceptres", "Wands", "Staves"})


def _query_top_bases_for_context(
    ctx: BuildFilterContext,
    index: FilterGamedataIndex,
) -> tuple[list[str], list[str]]:
    """Returns balanced `(top_weapon_offhand_bases, top_armour_bases)` from `poe2_0.5.5.sqlite`."""
    ordered_weapon_fcs = _dedup_preserve_order(
        [*ctx.weapon_filter_classes, *ctx.offhand_filter_classes]
    )
    target_weapon_fcs = set(ordered_weapon_fcs)
    ordered_tags = _dedup_preserve_order(ctx.preferred_armour_tags)
    want_tags = set(ordered_tags)

    weapons_by_fc: dict[str, dict[str, int]] = {fc: {} for fc in ordered_weapon_fcs}
    armours_by_slot_tag: dict[tuple[str, str], dict[str, int]] = {}

    con = sqlite3.connect(f"file:{get_gamedata_db_path()}?mode=ro", uri=True)
    try:
        for name, ic_id, drop_lvl, tags_json in con.execute(
            "SELECT name, item_class, drop_level, tags_json FROM base_items WHERE is_live_poe2 = 1"
        ):
            if (
                not name
                or name in index.dormant_or_removed_basetypes
                or name.startswith(("Runeforged ", "Runemastered "))
            ):
                continue
            fc = to_filter_class(ic_id, index)
            if not fc:
                continue
            dl = int(drop_lvl or 1)
            if fc in target_weapon_fcs:
                prev_dl = weapons_by_fc[fc].get(name, -1)
                if dl > prev_dl:
                    weapons_by_fc[fc][name] = dl
            elif fc in ARMOUR_FILTER_CLASSES and dl >= 60:
                tags = set(json.loads(tags_json or "[]"))
                matched_t = [t for t in ordered_tags if t in tags]
                for mt in matched_t:
                    slot_bucket = armours_by_slot_tag.setdefault((fc, mt), {})
                    if dl > slot_bucket.get(name, -1):
                        slot_bucket[name] = dl
    finally:
        con.close()

    # Select balanced top bases per weapon/offhand class (preserving skill-implicit Sceptres/Wands)
    per_fc_lists: list[list[str]] = []
    for fc in ordered_weapon_fcs:
        items_map = weapons_by_fc.get(fc) or {}
        if not items_map:
            continue
        sorted_items = sorted(items_map.items(), key=lambda kv: (-kv[1], kv[0]))
        if fc in _SKILL_IMPLICIT_WEAPON_CLASSES:
            filtered = [nm for nm, _ in sorted_items[:10]]
        else:
            max_dl = sorted_items[0][1]
            dl_floor = max(1, min(58, max_dl - 16))
            filtered = [nm for nm, dl in sorted_items if dl >= dl_floor][:10]
        if filtered:
            per_fc_lists.append(filtered)

    top_weapons: list[str] = []
    for eq_b in ctx.equipped_bases:
        if eq_b in index.live_poe2_basetypes and not eq_b.startswith(
            ("Runeforged ", "Runemastered ")
        ):
            eq_fcs = index.basetype_to_classes.get(eq_b, frozenset())
            if (eq_fcs & target_weapon_fcs) and eq_b not in top_weapons:
                top_weapons.append(eq_b)

    max_w_len = max((len(lst) for lst in per_fc_lists), default=0)
    for idx_i in range(max_w_len):
        for lst in per_fc_lists:
            if idx_i < len(lst) and lst[idx_i] not in top_weapons:
                top_weapons.append(lst[idx_i])
                if len(top_weapons) >= 36:
                    break
        if len(top_weapons) >= 36:
            break

    # Round-robin across (armour_slot, preferred_tag) so co-op builds share equal armour coverage
    armour_lists: list[list[str]] = []
    for mt in ordered_tags:
        for fc in ARMOUR_FILTER_CLASSES:
            bucket = armours_by_slot_tag.get((fc, mt))
            if not bucket:
                continue
            sorted_a = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
            armour_lists.append([nm for nm, _ in sorted_a[:4]])

    top_armours: list[str] = []
    for eq_b in ctx.equipped_bases:
        if eq_b in index.live_poe2_basetypes and not eq_b.startswith(
            ("Runeforged ", "Runemastered ")
        ):
            eq_fcs = index.basetype_to_classes.get(eq_b, frozenset())
            if (eq_fcs & set(ARMOUR_FILTER_CLASSES)) and eq_b not in top_armours and want_tags:
                top_armours.append(eq_b)

    max_a_len = max((len(lst) for lst in armour_lists), default=0)
    for idx_i in range(max_a_len):
        for lst in armour_lists:
            if idx_i < len(lst) and lst[idx_i] not in top_armours:
                top_armours.append(lst[idx_i])
                if len(top_armours) >= 40:
                    break
        if len(top_armours) >= 40:
            break

    return top_weapons, top_armours


class FilterGenerator:
    """Compiles validated PoE2 0.5.5 `.filter` files from economy, PoB2, themes, and soundpacks."""

    def __init__(
        self,
        *,
        economy_service: EconomyService | None = None,
        build_service: BuildService | None = None,
        index: FilterGamedataIndex | None = None,
    ) -> None:
        self.economy_service = economy_service
        self.build_service = build_service
        self.index = index or get_filter_gamedata_index()
        self.validator = FilterValidator(self.index)

    def collect_market_prices(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        use_live_economy: bool = True,
        price_overrides_exalted: Mapping[str, float] | None = None,
    ) -> tuple[dict[str, float], dict[str, tuple[float, float]]]:
        """Returns `(stackable_prices_ex, unique_base_min_max_ex)`."""
        prices: dict[str, float] = dict(OFFLINE_BASELINE_EXALTED)

        unique_base_prices: dict[str, list[float]] = {}
        if use_live_economy and self.economy_service is not None:
            try:
                for cat in (
                    "currency",
                    "fragments",
                    "runes",
                    "essences",
                    "ritual",
                    "ultimatum",
                    "delirium",
                    "breach",
                    "expedition",
                    "verisium",
                    "abyss",
                    "uncutgems",
                    "vaultkeys",
                ):
                    for q in self.economy_service.get_currencies_by_category(cat, league=league):
                        if q.name and q.exalted is not None and q.exalted > 0:
                            prices[q.name] = float(q.exalted)
                for ucat in ("accessory", "armour", "flask", "jewel", "weapon", "sanctum"):
                    for uq in self.economy_service.get_uniques_by_category(ucat, league=league):
                        if uq.base_type and uq.exalted is not None and uq.exalted > 0:
                            unique_base_prices.setdefault(uq.base_type, []).append(
                                float(uq.exalted)
                            )
            except Exception:
                pass

        if price_overrides_exalted:
            for k, v in price_overrides_exalted.items():
                prices[k] = float(v)

        # Curated multi-base chase unique safety floor when offline
        for chase_base, chase_ex in (
            ("Silk Robe", 650_000.0),
            ("Stellar Amulet", 45_000.0),
            ("Heavy Belt", 32_000.0),
            ("Emerald", 2_000.0),
            ("Sapphire", 1_500.0),
            ("Ruby", 1_500.0),
            ("Time-Lost Diamond", 25_000.0),
            ("Diamond", 8_000.0),
            ("Furtive Wraps", 4_500.0),
            ("Utility Belt", 18_000.0),
            ("Armoured Cap", 1_800.0),
        ):
            unique_base_prices.setdefault(chase_base, []).append(chase_ex)
            if len(self.index.uniques_by_basetype.get(chase_base, ())) > 1:
                unique_base_prices[chase_base].append(1.0)

        unique_min_max = {
            bt: (min(vals), max(vals))
            for bt, vals in unique_base_prices.items()
            if vals and bt in self.index.live_poe2_basetypes
        }
        return prices, unique_min_max

    def build_poe2_override_blocks(
        self,
        theme: VisualThemeSpec,
        *,
        strictness: StrictnessTier = StrictnessTier.SEMI_STRICT,
        build_context: BuildFilterContext | None = None,
        sound_pack: SoundPackManifest | None = None,
        require_custom_sounds: bool = False,
        prices_ex: Mapping[str, float] | None = None,
    ) -> list[str]:
        """Generates Patch 0.5.5 economy + PoB2 override blocks for native or NeverSink filters."""
        blocks: list[str] = []
        price_map = prices_ex or OFFLINE_BASELINE_EXALTED
        policy = STRICTNESS_POLICIES[strictness]

        # 1. Patch 0.5.5 Reliquary / Vault Keys & Calamity/Crisis/Origin drops
        key_tiers: dict[str, list[str]] = {"s_plus": [], "s": [], "a": [], "b": []}
        highlight_055_candidates = (
            "Zarokh's Reliquary Key: Temporalis",
            "Zarokh's Reliquary Key: Against the Darkness",
            "Azmeri Reliquary Key",
            "Origin Core",
            "Origin Cradle",
            "Primary Calamity Fragment",
            "Secondary Calamity Fragment",
            "Tertiary Calamity Fragment",
            "Ancient Crisis Fragment",
            "Weathered Crisis Fragment",
            "Faded Crisis Fragment",
            "Preserved Vertebrae",
        )
        for nm in highlight_055_candidates:
            if (
                nm not in self.index.basetype_to_classes
                or nm in self.index.dormant_or_removed_basetypes
            ):
                continue
            val = float(price_map.get(nm, 35.0))
            if val >= 400.0:
                key_tiers["s_plus"].append(nm)
            elif val >= 90.0:
                key_tiers["s"].append(nm)
            elif val >= 20.0:
                key_tiers["a"].append(nm)
            elif val >= policy.min_currency_ex:
                key_tiers["b"].append(nm)

        for tk in ("s_plus", "s", "a", "b"):
            if key_tiers[tk]:
                blocks.append(
                    _render_show_block(
                        "poe2brain_055_drops",
                        tk,
                        [f"BaseType == {_quote_list(key_tiers[tk])}"],
                        theme.tiers[tk],
                        sound_pack=sound_pack,
                        require_custom_sounds=require_custom_sounds,
                        section_comment=f"Patch {POE2_PATCH} Live Economy Priority ({tk.upper()})",
                    )
                )

        # 2. PoB2 Build-Aware Gear, Unidentified Tier, Socket & Charm Highlights
        if build_context is not None:
            top_weapons, top_armours = _query_top_bases_for_context(build_context, self.index)
            b_style = theme.tiers["build_upgrade"]
            w_classes = [
                c
                for c in (
                    build_context.weapon_filter_classes + build_context.offhand_filter_classes
                )
                if c in self.index.valid_filter_classes
            ]

            if top_weapons:
                blocks.append(
                    _render_show_block(
                        "build_upgrade",
                        "pob_top_weapons_ilvl81",
                        [
                            "Rarity Normal Magic Rare",
                            f"BaseType == {_quote_list(top_weapons)}",
                            "ItemLevel >= 81",
                        ],
                        b_style,
                        sound_pack=sound_pack,
                        require_custom_sounds=require_custom_sounds,
                        section_comment=(
                            "PoB2 Build Priority ("
                            f"{build_context.ascendancy_name or build_context.class_name or 'Slot'}"
                            f" — {build_context.main_skill or 'Main Skill'})"
                        ),
                    )
                )
            if top_armours:
                blocks.append(
                    _render_show_block(
                        "build_upgrade",
                        "pob_top_armours_ilvl82",
                        [
                            "Rarity Normal Magic Rare",
                            f"BaseType == {_quote_list(top_armours)}",
                            "ItemLevel >= 82",
                        ],
                        b_style,
                        sound_pack=sound_pack,
                        require_custom_sounds=require_custom_sounds,
                    )
                )
            if w_classes:
                blocks.append(
                    _render_show_block(
                        "build_upgrade",
                        "pob_unid_tiered_weapons",
                        [
                            f"Class == {_quote_list(w_classes)}",
                            "Rarity Rare",
                            f"UnidentifiedItemTier >= {policy.min_unid_tier_build}",
                        ],
                        b_style,
                        sound_pack=sound_pack,
                        require_custom_sounds=require_custom_sounds,
                    )
                )
            if build_context.uncapped_resists:
                res_charm_map = {
                    "fire": "Ruby Charm",
                    "cold": "Sapphire Charm",
                    "lightning": "Topaz Charm",
                    "chaos": "Amethyst Charm",
                }
                needed_charms = [
                    res_charm_map[r]
                    for r in build_context.uncapped_resists
                    if r in res_charm_map and res_charm_map[r] in self.index.basetype_to_classes
                ]
                if needed_charms:
                    blocks.append(
                        _render_show_block(
                            "build_upgrade",
                            "pob_uncapped_resist_charms",
                            [
                                'Class == "Charms"',
                                f"BaseType == {_quote_list(needed_charms)}",
                                "ItemLevel >= 65",
                            ],
                            theme.tiers["b"],
                            sound_pack=sound_pack,
                            require_custom_sounds=require_custom_sounds,
                        )
                    )

            asc_hay = (
                f"{build_context.ascendancy_name or ''} {' '.join(build_context.party_members)}"
            ).lower()
            if any(k in asc_hay for k in ("spirit walker", "shaman")):
                azmeri_idols = sorted(
                    bt
                    for bt in self.index.live_poe2_basetypes
                    if "Idol" in bt
                    and "Augment" in self.index.basetype_to_classes.get(bt, frozenset())
                    and bt not in self.index.dormant_or_removed_basetypes
                )
                if azmeri_idols:
                    blocks.append(
                        _render_show_block(
                            "build_upgrade",
                            "pob_azmeri_idols",
                            [
                                'Class == "Augment"',
                                f"BaseType == {_quote_list(azmeri_idols)}",
                            ],
                            b_style,
                            sound_pack=sound_pack,
                            require_custom_sounds=require_custom_sounds,
                            section_comment="Azmeri Companion & Bonded Idol Priority",
                        )
                    )

        return blocks

    def _resolve_combined_build_context(
        self,
        *,
        build_context: BuildFilterContext | None = None,
        build_contexts: Sequence[BuildFilterContext] | None = None,
        pob_code: str | None = None,
        pob_codes: Sequence[str] | None = None,
        pob_slot: str | None = None,
        pob_slots: Sequence[str] | None = None,
    ) -> BuildFilterContext | None:
        collected: list[BuildFilterContext] = []
        if build_context is not None:
            collected.append(build_context)
        if build_contexts:
            collected.extend(c for c in build_contexts if c is not None)

        if self.build_service is not None:
            if pob_code is not None and not collected:
                collected.append(
                    extract_build_filter_context(
                        self.build_service,
                        pob_code=pob_code,
                        slot=pob_slot or "main",
                        index=self.index,
                    )
                )
            elif pob_slot is not None and not collected and not pob_slots and not pob_codes:
                collected.append(
                    extract_build_filter_context(
                        self.build_service,
                        slot=pob_slot,
                        index=self.index,
                    )
                )
            for idx_c, code_str in enumerate(pob_codes or ()):
                collected.append(
                    extract_build_filter_context(
                        self.build_service,
                        pob_code=code_str,
                        slot=f"_filter_coop_{idx_c}",
                        index=self.index,
                    )
                )
            for slot_name in pob_slots or ():
                collected.append(
                    extract_build_filter_context(
                        self.build_service,
                        slot=slot_name,
                        index=self.index,
                    )
                )

        if not collected:
            return None
        return merge_build_filter_contexts(collected)

    def generate(
        self,
        *,
        filter_name: str = "poe2brain_forbidden_rites",
        strictness: StrictnessTier | str = StrictnessTier.SEMI_STRICT,
        theme: VisualThemeSpec | str = "forbidden_abyss",
        style_prompt: str | None = None,
        theme_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        sound_pack: SoundPackManifest | None = None,
        require_custom_sounds: bool = False,
        league: str = DEFAULT_LEAGUE,
        use_live_economy: bool = True,
        price_overrides_exalted: Mapping[str, float] | None = None,
        pob_code: str | None = None,
        pob_codes: Sequence[str] | None = None,
        pob_slot: str | None = None,
        pob_slots: Sequence[str] | None = None,
        build_context: BuildFilterContext | None = None,
        build_contexts: Sequence[BuildFilterContext] | None = None,
        output_path: Path | None = None,
    ) -> FilterCompileResult:
        strict_tier = StrictnessTier(strictness) if isinstance(strictness, str) else strictness
        policy = STRICTNESS_POLICIES[strict_tier]

        if isinstance(theme, VisualThemeSpec):
            resolved_theme = theme
        elif style_prompt or theme_overrides:
            base_p = theme if theme in BUILTIN_THEME_PRESETS else None
            resolved_theme = compose_theme(
                style_prompt,
                base_preset=base_p,
                overrides=theme_overrides,
            )
        else:
            resolved_theme = get_visual_theme(theme)

        resolved_build_ctx = self._resolve_combined_build_context(
            build_context=build_context,
            build_contexts=build_contexts,
            pob_code=pob_code,
            pob_codes=pob_codes,
            pob_slot=pob_slot,
            pob_slots=pob_slots,
        )

        prices_ex, unique_min_max = self.collect_market_prices(
            league=league,
            use_live_economy=use_live_economy,
            price_overrides_exalted=price_overrides_exalted,
        )

        # Categorize live stackable/fungible items in SQLite + extra basetypes by Exalted value
        fungible_filter_classes = {
            "Stackable Currency",
            "Omen",
            "Augment",
            "Tablet",
            "Pinnacle Keys",
            "Vault Keys",
            "Breachstones",
            "Map Fragments",
            "Trial Coins",
            "Inscribed Ultimatum",
            "Wombgifts",
        }
        tier_buckets: dict[str, list[str]] = {
            "s_plus": [],
            "s": [],
            "a": [],
            "b": [],
            "c": [],
            "d": [],
            "hide": [],
        }
        economy_count = 0

        is_ssf_league = "ssf" in league.lower()
        ssf_core_currencies = {
            "Exalted Orb",
            "Regal Orb",
            "Orb of Alchemy",
            "Vaal Orb",
            "Artificer's Orb",
            "Lesser Jeweller's Orb",
        }

        for bt_name in sorted(self.index.live_poe2_basetypes):
            if bt_name in self.index.dormant_or_removed_basetypes or bt_name == "Gold":
                continue
            fcs = self.index.basetype_to_classes.get(bt_name, frozenset())
            if not (fcs & fungible_filter_classes):
                continue
            ex_val = prices_ex.get(bt_name)
            if ex_val is not None:
                economy_count += 1
            else:
                # Default heuristic by item class when unquoted
                if "Vault Keys" in fcs or "Pinnacle Keys" in fcs:
                    ex_val = 65.0
                elif "Omen" in fcs or "Tablet" in fcs or "Breachstones" in fcs:
                    ex_val = 8.0
                elif "Augment" in fcs:
                    ex_val = 2.5 if "Soul Core" in bt_name or "Greater" in bt_name else 0.8
                else:
                    ex_val = 0.5

            if ex_val >= 350.0:
                tier_buckets["s_plus"].append(bt_name)
            elif ex_val >= 60.0:
                tier_buckets["s"].append(bt_name)
            elif ex_val >= 12.0:
                tier_buckets["a"].append(bt_name)
            elif ex_val >= 2.0:
                tier_buckets["b"].append(bt_name)
            elif ex_val >= max(0.35, policy.min_currency_ex) or (
                is_ssf_league and bt_name in ssf_core_currencies
            ):
                tier_buckets["c"].append(bt_name)
            elif ex_val >= policy.min_currency_ex:
                tier_buckets["d"].append(bt_name)
            else:
                tier_buckets["hide"].append(bt_name)

        banner_bar = "#" + "=" * 95
        sections: list[str] = [
            "\n".join(
                [
                    banner_bar,
                    f"# poe2brain Loot Filter — Path of Exile 2 Patch {POE2_PATCH} ({league})",
                    f"# FILTER:     {filter_name}",
                    f"# STRICTNESS: {strict_tier.value}",
                    f"# THEME:      {resolved_theme.id} ({resolved_theme.name})",
                    f"# SOUNDPACK:  {sound_pack.pack_id if sound_pack else 'builtin_poe2'}",
                    banner_bar,
                ]
            )
        ]

        # 1. Override & Build-Specific Priority Blocks
        override_blocks = self.build_poe2_override_blocks(
            resolved_theme,
            strictness=strict_tier,
            build_context=resolved_build_ctx,
            sound_pack=sound_pack,
            require_custom_sounds=require_custom_sounds,
            prices_ex=prices_ex,
        )
        sections.extend(override_blocks)

        # 2. Economy-Tiered Stackable Currency, Omens, Essences, Augments, Keys & Tablets
        for tk in ("s_plus", "s", "a", "b", "c", "d"):
            items = tier_buckets[tk]
            if not items:
                continue
            sections.append(
                _render_show_block(
                    "currency",
                    tk,
                    [f"BaseType == {_quote_list(items)}"],
                    resolved_theme.tiers[tk],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                    section_comment=f"Economy Fungibles — Tier {tk.upper()} ({len(items)} bases)",
                )
            )

        if tier_buckets["hide"]:
            sections.append(
                "\n".join(
                    [
                        "# Hide below-threshold low-value currency in Endgame (shown in Campaign)",
                        "Hide # $type->currency $tier->exhide",
                        "\tAreaLevel >= 65",
                        f"\tBaseType == {_quote_list(tier_buckets['hide'])}",
                        "\tSetFontSize 18",
                        "\tDisableDropSound True",
                    ]
                )
            )

        # 3. Uniques (TwiceCorrupted + Pure Chase Bases + Multi-Base Jackpot Check + General)
        pure_chase_uniques = [
            bt for bt, (mn, mx) in unique_min_max.items() if mx >= 200.0 and mn >= 50.0
        ]
        multibase_jackpots = [
            bt for bt, (mn, mx) in unique_min_max.items() if mx >= 200.0 and mn < 50.0
        ]

        sections.append(
            _render_show_block(
                "uniques",
                "vaal_or_twice_corrupted",
                ["Rarity Unique", "TwiceCorrupted True"],
                resolved_theme.tiers["s_plus"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Uniques — Twice-Corrupted & Chase Tiering",
            )
        )
        if pure_chase_uniques:
            sections.append(
                _render_show_block(
                    "uniques",
                    "s_plus",
                    ["Rarity Unique", f"BaseType == {_quote_list(pure_chase_uniques)}"],
                    resolved_theme.tiers["s_plus"],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                )
            )
        if multibase_jackpots:
            sections.append(
                _render_show_block(
                    "uniques",
                    "multispecialhigh",
                    ["Rarity Unique", f"BaseType == {_quote_list(multibase_jackpots)}"],
                    resolved_theme.tiers["s"],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                    section_comment="Multi-Base Boss/Chase Unique Check (never hide shared base!)",
                )
            )
        sections.append(
            _render_show_block(
                "uniques",
                "any_unique",
                ["Rarity Unique"],
                resolved_theme.tiers[
                    "a" if strict_tier in (StrictnessTier.SOFT, StrictnessTier.REGULAR) else "b"
                ],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
            )
        )

        # 4. Waystone Endgame Progression
        sections.append(
            _render_show_block(
                "waystones",
                "waystone_t15_t16",
                ['Class == "Waystones"', "WaystoneTier >= 15"],
                resolved_theme.tiers["waystone_high"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Atlas Waystones (T15+ Pinnacle Sustain & Progression)",
            )
        )
        if policy.min_endgame_waystone_tier < 15:
            sections.append(
                _render_show_block(
                    "waystones",
                    "waystone_prog",
                    [
                        'Class == "Waystones"',
                        f"WaystoneTier >= {policy.min_endgame_waystone_tier}",
                        "WaystoneTier <= 14",
                    ],
                    resolved_theme.tiers["waystone_prog"],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                )
            )
        if policy.min_endgame_waystone_tier > 1:
            sections.append(
                "\n".join(
                    [
                        "Hide # $type->waystones $tier->exhide",
                        '\tClass == "Waystones"',
                        "\tAreaLevel >= 78",
                        f"\tWaystoneTier < {policy.min_endgame_waystone_tier}",
                        "\tSetFontSize 18",
                        "\tDisableDropSound True",
                    ]
                )
            )

        # 5. Uncut Skill / Spirit / Support Gems & Level 20+ Gems
        sections.append(
            _render_show_block(
                "gems",
                "level_20_gems",
                [
                    'Class == "Uncut Skill Gems" "Uncut Spirit Gems" "Skill Gems" "Support Gems"',
                    "GemLevel >= 20",
                ],
                resolved_theme.tiers["s"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Uncut & High-Level Gems",
            )
        )
        sections.append(
            _render_show_block(
                "gems",
                "uncut_progression",
                [
                    'Class == "Uncut Skill Gems" "Uncut Spirit Gems" "Uncut Support Gems"',
                    f"ItemLevel >= {policy.min_uncut_gem_level}",
                ],
                resolved_theme.tiers["b"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
            )
        )

        # 6. Jewels, Relics, Expedition Logbooks, Chancing Bases & High Unidentified Tiers
        sections.append(
            _render_show_block(
                "jewels_relics",
                "endgame",
                ['Class == "Jewels" "Relics" "Expedition Logbook"'],
                resolved_theme.tiers["a"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Jewels, Sanctum Relics & Logbooks",
            )
        )
        live_chancing = [b for b in CHANCING_BASES_055 if b in self.index.live_poe2_basetypes]
        if live_chancing and strict_tier != StrictnessTier.UBER_STRICT:
            sections.append(
                _render_show_block(
                    "chancing",
                    "chance_bases",
                    ["Rarity Normal", f"BaseType == {_quote_list(live_chancing)}"],
                    resolved_theme.tiers["chancing"],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                    section_comment="Orb of Chance Jackpot White Bases",
                )
            )

        # Exceptional Fractured / Multi-Socket / Tiered Unidentified Gear
        sections.append(
            _render_show_block(
                "exceptional_gear",
                "fractured",
                ["FracturedItem True"],
                resolved_theme.tiers["a"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Fractured, Multi-Socket & High UnidentifiedItemTier Gear",
            )
        )
        sections.append(
            _render_show_block(
                "exceptional_gear",
                "unid_tier_high",
                ["Rarity Rare", f"UnidentifiedItemTier >= {max(4, policy.min_unid_tier_generic)}"],
                resolved_theme.tiers["a"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
            )
        )
        if policy.min_unid_tier_generic < 4:
            sections.append(
                _render_show_block(
                    "exceptional_gear",
                    "unid_tier_mid",
                    [
                        "Rarity Rare",
                        f"UnidentifiedItemTier >= {policy.min_unid_tier_generic}",
                        "UnidentifiedItemTier <= 3",
                    ],
                    resolved_theme.tiers["b"],
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                )
            )
        sections.append(
            _render_show_block(
                "salvage",
                "multi_sockets",
                ["Sockets >= 2", "Rarity Normal Magic Rare"],
                resolved_theme.tiers["c"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
            )
        )
        sections.append(
            _render_show_block(
                "salvage",
                "high_quality",
                ["Quality >= 15", "Rarity Normal Magic Rare"],
                resolved_theme.tiers["c"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
            )
        )

        # 7. Gold Stacks
        sections.append(
            _render_show_block(
                "gold",
                "gold_high",
                ['BaseType == "Gold"', "StackSize >= 1500"],
                resolved_theme.tiers["gold_high"],
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                section_comment="Gold Piles",
            )
        )
        if policy.min_gold_stack_endgame > 1:
            if policy.min_gold_stack_endgame < 1500:
                sections.append(
                    _render_show_block(
                        "gold",
                        "gold_mid",
                        [
                            'BaseType == "Gold"',
                            f"StackSize >= {policy.min_gold_stack_endgame}",
                            "StackSize < 1500",
                        ],
                        resolved_theme.tiers["gold_low"],
                    )
                )
            sections.append(
                "\n".join(
                    [
                        "Hide # $type->gold $tier->dust",
                        '\tBaseType == "Gold"',
                        "\tAreaLevel >= 68",
                        f"\tStackSize < {policy.min_gold_stack_endgame}",
                        "\tSetFontSize 18",
                        "\tDisableDropSound True",
                    ]
                )
            )
        else:
            sections.append(
                _render_show_block(
                    "gold",
                    "gold_low",
                    ['BaseType == "Gold"', "StackSize < 1500"],
                    resolved_theme.tiers["gold_low"],
                )
            )

        # 8. Quest & AlwaysShow Safety Net + Endgame Clutter Hide Layers
        sections.append(
            _render_show_block(
                "quest",
                "safety",
                ['Class == "Quest Items" "Instance Local Items"'],
                resolved_theme.tiers["a"],
                section_comment="Quest & Instance Items (Never Hide)",
            )
        )

        gear_classes = sorted(
            WEAPON_FILTER_CLASSES | OFFHAND_FILTER_CLASSES | set(ARMOUR_FILTER_CLASSES)
        )
        if policy.hide_endgame_untiered_rares:
            sections.append(
                "\n".join(
                    [
                        "# Endgame Clutter Filter — Hide Untiered Rares & Normal/Magic Bases",
                        "Hide # $type->hidelayer $tier->raresendgame",
                        "\tAreaLevel >= 68",
                        "\tRarity Rare",
                        f"\tClass == {_quote_list(gear_classes)}",
                        "\tSetFontSize 18",
                        "\tDisableDropSound True",
                    ]
                )
            )
        if policy.hide_endgame_normal_magic_gear:
            sections.append(
                "\n".join(
                    [
                        "Hide # $type->hidelayer $tier->normalmagicendgame",
                        "\tAreaLevel >= 65",
                        "\tRarity Normal Magic",
                        f"\tClass == {_quote_list(gear_classes)}",
                        "\tSetFontSize 18",
                        "\tDisableDropSound True",
                    ]
                )
            )

        # Campaign & Safety Catch-All
        sections.append(
            _render_show_block(
                "safety_fallback",
                "anyremaining",
                [],
                resolved_theme.tiers["d"],
                section_comment="Campaign & Unknown New-Item Safety Fallback",
            )
        )

        filter_text = "\n\n".join(sections) + "\n"

        out_dir: Path | None = None
        written_path_str: str | None = None
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(filter_text, encoding="utf-8")
            out_dir = output_path.parent
            if sound_pack is not None and not (out_dir / sound_pack.pack_id).is_dir():
                out_dir = Path(sound_pack.pack_dir).parent
            written_path_str = str(output_path)
        elif sound_pack is not None:
            out_dir = Path(sound_pack.pack_dir).parent

        report = self.validator.validate_text(filter_text, filter_dir=out_dir)
        if not report.valid:
            first_err = report.diagnostics[0]
            raise RuntimeError(
                f"Compiler invariant violated at L{first_err.line} "
                f"({first_err.code}): {first_err.message}"
            )

        return FilterCompileResult(
            filter_name=filter_name,
            patch=POE2_PATCH,
            league=league,
            strictness=strict_tier,
            theme_id=resolved_theme.id,
            sound_pack_id=sound_pack.pack_id if sound_pack else None,
            filter_text=filter_text,
            filter_path=written_path_str,
            validation=report,
            build_context=resolved_build_ctx,
            economy_tiered_items=economy_count,
            injected_override_blocks=len(override_blocks),
        )
