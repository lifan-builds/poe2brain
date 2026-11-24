from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH, get_gamedata_db_path
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


def extract_build_filter_context(
    build_service: BuildService,
    *,
    pob_code: str | None = None,
    slot: str = "main",
    index: FilterGamedataIndex | None = None,
) -> BuildFilterContext:
    idx = index or get_filter_gamedata_index()
    if pob_code:
        build_service.load_build(pob_code, slot=slot)
    summary = build_service.inspect_build(slot=slot, include_item_raw=False)
    stats = summary["stats"]

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

    for entry in summary.get("items") or []:
        if not isinstance(entry, Mapping):
            continue
        bname = str(entry.get("baseName") or "").strip()
        if not bname:
            continue
        if bname not in equipped_bases:
            equipped_bases.append(bname)
        fc = to_filter_class(base_to_ic.get(bname, ""), idx)
        if fc in WEAPON_FILTER_CLASSES and fc not in weapon_classes:
            weapon_classes.append(fc)
        elif fc in OFFHAND_FILTER_CLASSES and fc not in offhand_classes:
            offhand_classes.append(fc)

    # Infer archetype weapon class from class/ascendancy/main_skill when naked starter build
    if not weapon_classes:
        hint = (
            f"{stats.class_name or ''} {stats.ascendancy_name or ''} {stats.main_skill or ''}"
        ).lower()
        if any(k in hint for k in ("ranger", "deadeye", "pathfinder", "arrow", "bow")):
            weapon_classes.append("Bows")
            offhand_classes.append("Quivers")
        elif any(
            k in hint for k in ("monk", "invoker", "acolyte", "quarterstaff", "flurry", "bell")
        ):
            weapon_classes.append("Quarterstaves")
        elif any(
            k in hint
            for k in ("mercenary", "witchhunter", "gemling", "crossbow", "grenade", "bolt")
        ):
            weapon_classes.append("Crossbows")
        elif any(k in hint for k in ("warrior", "titan", "warbringer", "smith", "slam", "mace")):
            weapon_classes.extend(["Two Hand Maces", "One Hand Maces"])
        elif any(k in hint for k in ("huntress", "amazon", "ritualist", "spear")):
            weapon_classes.append("Spears")
            offhand_classes.append("Bucklers")
        elif any(k in hint for k in ("druid", "shaman", "oracle", "talisman")):
            weapon_classes.extend(["Talismans", "Staves"])
        else:
            weapon_classes.extend(["Wands", "Sceptres", "Staves"])
            offhand_classes.append("Foci")

    # Infer armour defense tags from BuildStats
    ar_v = float(stats.armour or 0.0)
    ev_v = float(stats.evasion or 0.0)
    es_v = float(stats.energy_shield or 0.0)
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
        preferred_tags.extend(["int_armour", "dex_armour", "str_armour"])

    uncapped: list[str] = []
    if float(stats.fire_resist) < 75.0:
        uncapped.append("fire")
    if float(stats.cold_resist) < 75.0:
        uncapped.append("cold")
    if float(stats.lightning_resist) < 75.0:
        uncapped.append("lightning")
    if float(stats.chaos_resist) < 40.0:
        uncapped.append("chaos")

    return BuildFilterContext(
        class_name=stats.class_name,
        ascendancy_name=stats.ascendancy_name,
        level=stats.level,
        main_skill=stats.main_skill,
        weapon_filter_classes=weapon_classes,
        offhand_filter_classes=offhand_classes,
        preferred_armour_tags=preferred_tags,
        uncapped_resists=uncapped,
        equipped_bases=equipped_bases,
    )


def _query_top_bases_for_context(
    ctx: BuildFilterContext,
    index: FilterGamedataIndex,
) -> tuple[list[str], list[str]]:
    """Returns `(top_weapon_offhand_bases, top_armour_bases)` from `poe2_0.5.5.sqlite`."""
    target_weapon_fcs = set(ctx.weapon_filter_classes) | set(ctx.offhand_filter_classes)
    want_tags = set(ctx.preferred_armour_tags)

    weapon_candidates: list[tuple[int, str]] = []
    armour_candidates: list[tuple[int, str]] = []

    con = sqlite3.connect(f"file:{get_gamedata_db_path()}?mode=ro", uri=True)
    try:
        for name, ic_id, drop_lvl, tags_json in con.execute(
            "SELECT name, item_class, drop_level, tags_json FROM base_items WHERE is_live_poe2 = 1"
        ):
            if not name or name in index.dormant_or_removed_basetypes:
                continue
            fc = to_filter_class(ic_id, index)
            if not fc:
                continue
            dl = int(drop_lvl or 1)
            if fc in target_weapon_fcs and dl >= 58:
                weapon_candidates.append((dl, name))
            elif fc in ARMOUR_FILTER_CLASSES and dl >= 62:
                tags = set(json.loads(tags_json or "[]"))
                if tags & want_tags:
                    armour_candidates.append((dl, name))
    finally:
        con.close()

    weapon_candidates.sort(key=lambda t: (-t[0], t[1]))
    armour_candidates.sort(key=lambda t: (-t[0], t[1]))
    top_weapons = [nm for _, nm in weapon_candidates[:28]]
    top_armours = [nm for _, nm in armour_candidates[:32]]
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

        return blocks

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
        pob_slot: str | None = None,
        build_context: BuildFilterContext | None = None,
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

        resolved_build_ctx = build_context
        if (
            resolved_build_ctx is None
            and self.build_service is not None
            and (pob_code is not None or pob_slot is not None)
        ):
            resolved_build_ctx = extract_build_filter_context(
                self.build_service,
                pob_code=pob_code,
                slot=pob_slot or "main",
                index=self.index,
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
            elif ex_val >= max(0.35, policy.min_currency_ex):
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
