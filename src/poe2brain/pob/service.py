# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import atexit
import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote

from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
    GameRealm,
    get_game_realm,
    require_international,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.models.builds import (
    BuildSlotComparison,
    BuildStats,
    CalcBreakdownReport,
    CalcModProvenanceRow,
    CalcSourceTotal,
    CalcStatMatch,
    DefensivePassiveRespecOption,
    DewateredAudit,
    EquipmentSlotDiff,
    GoldRespecEstimate,
    HcRipAuditReport,
    HcRipRisk,
    ItemUpgradeDiff,
    MegalomaniacCombo,
    MegalomaniacSearchReport,
    RadiusJewelCandidate,
    RadiusJewelRankingReport,
    RuneOptimizationReport,
    RuneSocketRecommendation,
    SkillGroupDiff,
    SlotStatWeights,
    StatWeightEntry,
    StrippedPobFlag,
    SupportGemCandidate,
    SupportGemRankingReport,
    WeaponSwapAudit,
    WhatIfPieceAttribution,
    WhatIfReport,
    WhatIfScenarioPoint,
)
from poe2brain.models.common import Poe2Model, SaleType
from poe2brain.models.trade import TradeListing
from poe2brain.pob.bridge import (
    Pob2Bridge,
    Pob2BridgeError,
    close_default_pob2_bridge,
    get_default_pob2_bridge,
)

_POBBIN_URL_RE = re.compile(
    r"^https?://(?:www\.)?pobb\.in/(?:u/[^/]+/)?([A-Za-z0-9_-]+)(?:/raw)?/?$"
)

# Exact PoE2 Patch 0.5.5 GoldRespecPrices.dat (Levels 1..100) from PoB2 v0.23.1 Data/Misc.lua.
GOLD_RESPEC_PRICES_0_5_5: tuple[int, ...] = (
    15,
    19,
    25,
    31,
    39,
    46,
    60,
    73,
    85,
    98,
    113,
    128,
    145,
    163,
    182,
    211,
    225,
    241,
    257,
    273,
    290,
    308,
    326,
    344,
    364,
    384,
    404,
    425,
    447,
    470,
    493,
    517,
    542,
    567,
    593,
    620,
    648,
    676,
    706,
    736,
    767,
    799,
    832,
    866,
    900,
    936,
    973,
    1010,
    1049,
    1089,
    1130,
    1172,
    1215,
    1259,
    1304,
    1351,
    1399,
    1448,
    1498,
    1550,
    1603,
    1657,
    1713,
    1770,
    1829,
    1889,
    1950,
    2014,
    2078,
    2145,
    2213,
    2282,
    2354,
    2427,
    2502,
    2578,
    2657,
    2737,
    2820,
    2904,
    3089,
    3281,
    3480,
    3686,
    3899,
    4120,
    4349,
    4585,
    4829,
    5081,
    5509,
    5952,
    6412,
    6889,
    7383,
    7895,
    8425,
    8974,
    9542,
    10129,
)

ASCENDANCY_RESPEC_MULTIPLIER_0_5_5: int = 5
ATLAS_PASSIVE_RESPEC_GOLD_0_5_5: int = 5000

AnointSsfTier = Literal["farmable_delirium", "concentrated_high_tier", "endgame_simulacrum"]

LIQUID_EMOTION_BY_RECIPE_TOKEN: dict[str, tuple[str, AnointSsfTier]] = {
    "Ire": ("Diluted Liquid Ire", "farmable_delirium"),
    "Guilt": ("Diluted Liquid Guilt", "farmable_delirium"),
    "Greed": ("Diluted Liquid Greed", "farmable_delirium"),
    "Paranoia": ("Liquid Paranoia", "farmable_delirium"),
    "Envy": ("Liquid Envy", "farmable_delirium"),
    "Disgust": ("Liquid Disgust", "farmable_delirium"),
    "Despair": ("Liquid Despair", "farmable_delirium"),
    "Fear": ("Concentrated Liquid Fear", "concentrated_high_tier"),
    "Suffering": ("Concentrated Liquid Suffering", "concentrated_high_tier"),
    "Isolation": ("Concentrated Liquid Isolation", "concentrated_high_tier"),
    "Melancholy": ("Potent Liquid Melancholy", "endgame_simulacrum"),
    "Ferocity": ("Potent Liquid Ferocity", "endgame_simulacrum"),
    "Contempt": ("Potent Liquid Contempt", "endgame_simulacrum"),
}


def estimate_poe2_gold_respec(
    level: int,
    *,
    refunded_passives: int = 0,
    refunded_ascendancy: int = 0,
    allocated_new: int = 0,
) -> GoldRespecEstimate:
    """Compute exact PoE2 Patch 0.5.5 Gold respec cost (Orbs of Regret do not exist in PoE2)."""
    clamped_lvl = max(1, min(100, int(level)))
    per_passive = GOLD_RESPEC_PRICES_0_5_5[clamped_lvl - 1]
    per_asc = per_passive * ASCENDANCY_RESPEC_MULTIPLIER_0_5_5
    ref_p = max(0, int(refunded_passives))
    ref_a = max(0, int(refunded_ascendancy))
    alloc_n = max(0, int(allocated_new))
    total_gold = ref_p * per_passive + ref_a * per_asc
    note = (
        f"Level {clamped_lvl}: {per_passive:,} Gold/passive refund, "
        f"{per_asc:,} Gold/Ascendancy refund (5x); allocating unspent points costs 0 Gold "
        "(Orbs of Regret do not exist in PoE2 0.5.5)."
    )
    return GoldRespecEstimate(
        character_level=clamped_lvl,
        gold_per_passive_point=per_passive,
        gold_per_ascendancy_point=per_asc,
        refunded_passive_points=ref_p,
        refunded_ascendancy_points=ref_a,
        allocated_new_points=alloc_n,
        total_gold_cost=total_gold,
        atlas_respec_gold_per_point=ATLAS_PASSIVE_RESPEC_GOLD_0_5_5,
        note=note,
    )


def _resolve_anoint_emotions(
    raw_recipe: Sequence[Any] | None,
) -> tuple[list[str], AnointSsfTier | None]:
    if not raw_recipe:
        return [], None
    emotions: list[str] = []
    tier_rank = 0
    rank_to_tier: dict[int, AnointSsfTier] = {
        1: "farmable_delirium",
        2: "concentrated_high_tier",
        3: "endgame_simulacrum",
    }
    tier_to_rank: dict[AnointSsfTier, int] = {
        "farmable_delirium": 1,
        "concentrated_high_tier": 2,
        "endgame_simulacrum": 3,
    }
    for tok in raw_recipe:
        key = str(tok).strip()
        if not key:
            continue
        mapped = LIQUID_EMOTION_BY_RECIPE_TOKEN.get(key)
        if mapped is not None:
            emotions.append(mapped[0])
            tier_rank = max(tier_rank, tier_to_rank[mapped[1]])
        else:
            emotions.append(key if "Liquid" in key else f"Liquid {key}")
            tier_rank = max(tier_rank, 1)
    return emotions, rank_to_tier.get(tier_rank)


class PassiveNodeUpgrade(Poe2Model):
    node_id: int
    string_id: str | None = None
    name: str
    node_type: str = "Notable"
    stats: list[str] = Field(default_factory=list)
    path_dist: int | None = None
    path_node_ids: list[int] = Field(default_factory=list)
    single_node_delta: float = 0.0
    single_node_pct: float = 0.0
    full_path_delta: float = 0.0
    full_path_pct: float = 0.0
    per_point_delta: float = 0.0
    per_point_pct: float = 0.0
    anoint_emotions: list[str] = Field(default_factory=list)
    ssf_anoint_tier: AnointSsfTier | None = None
    respec_gold_if_swapped: int | None = None


class LiveUpgradeCandidate(Poe2Model):
    rank: int
    listing: TradeListing
    upgrade_diff: ItemUpgradeDiff
    raw_full_dps_delta: float | None = None
    raw_total_ehp_delta: float | None = None
    top_off_applied: list[str] = Field(default_factory=list)
    top_off_cost_exalted: float = 0.0
    effective_total_cost_exalted: float | None = None
    dps_gain_per_exalted: float | None = None
    ehp_gain_per_exalted: float | None = None
    warnings: list[str] = Field(default_factory=list)


class LiveSlotUpgradeReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    build_slot: str = "main"
    equipment_slot: str
    metric: str = "FullDPS"
    max_price_exalted: float | None = None
    securable_only: bool = True
    normalize_craft_topoff: bool = True
    query_id: str | None = None
    trade_url: str | None = None
    total_market_matches: int = 0
    slot_weights: SlotStatWeights
    candidates: list[LiveUpgradeCandidate] = Field(default_factory=list)


_CONDITIONAL_PADDING_SUPPORTS: tuple[tuple[str, str], ...] = (
    (
        "rageforged",
        "Rageforged (怒火锤炼) consumes Rage per hit for conditional More damage — "
        "verify Rage generation sustains 100% uptime in Pinnacle fights.",
    ),
    (
        "minion pact",
        "Minion Pact (召唤契约) sacrifices Minion life for conditional More damage — "
        "verify disposable minions stay alive every cast/attack.",
    ),
    (
        "ambush",
        "Ambush only applies on Full-Life / opening hits and severely inflates sustained Boss DPS.",
    ),
    (
        "execute",
        "Execute only applies against Low-Life enemies — "
        "check true sustained DPS rather than execute-window tooltip.",
    ),
    (
        "hourglass",
        "Hourglass adds a long cooldown for burst More damage — check sustained rotation uptime.",
    ),
    (
        "exploit weakness",
        "Exploit Weakness requires consuming Broken Armour — "
        "verify reliable Armour Break application on bosses.",
    ),
    (
        "close combat",
        "Close Combat requires point-blank range for full More damage — "
        "account for boss movement downtime.",
    ),
)


def _detect_conditional_support_padding(
    skills_block: Mapping[str, Any] | None,
    sust_stats: BuildStats,
) -> list[str]:
    """Flag conditional burst/bait support gems and unsustainable attack/cast speed padding."""
    flags: list[str] = []
    if isinstance(skills_block, Mapping):
        groups = [
            g
            for g in (skills_block.get("socketGroups") or [])
            if isinstance(g, Mapping) and g.get("enabled", True)
        ]
        target_groups = [g for g in groups if g.get("isMainSkill")] or groups[:1]
        seen_keys: set[str] = set()
        for grp in target_groups:
            for gem in grp.get("gems") or []:
                if not isinstance(gem, Mapping) or not gem.get("enabled", True):
                    continue
                if not gem.get("isSupport"):
                    continue
                raw_name = str(gem.get("name") or "").strip()
                low = raw_name.lower()
                for prefix, note in _CONDITIONAL_PADDING_SUPPORTS:
                    if low.startswith(prefix) and prefix not in seen_keys:
                        seen_keys.add(prefix)
                        flags.append(f"Conditional support padding ({raw_name}): {note}")
                        break

    if (
        sust_stats.attack_cast_speed >= 8.0
        and sust_stats.mana_per_second_cost > 0
        and not sust_stats.mana_sustainable
    ):
        flags.append(
            f"Inflated attack/cast speed ({sust_stats.attack_cast_speed:.2f}/s) exceeds "
            f"sustainable mana recovery ({sust_stats.mana_regen:.0f}/s vs "
            f"{sust_stats.mana_per_second_cost:.0f}/s cost) — "
            "sheet DPS may be padded by unplayable APS/CPS."
        )
    return flags


def _active_skill_names(skills_block: Mapping[str, Any] | None) -> tuple[str | None, list[str]]:
    if not isinstance(skills_block, Mapping):
        return None, []
    main_skill: str | None = None
    names: list[str] = []
    for grp in skills_block.get("socketGroups") or []:
        if not isinstance(grp, Mapping) or not grp.get("enabled", True):
            continue
        act_name = grp.get("mainActiveSkillName")
        if isinstance(act_name, str) and act_name:
            if act_name not in names:
                names.append(act_name)
            if grp.get("isMainSkill"):
                main_skill = act_name
        else:
            for gem in grp.get("gems") or []:
                if (
                    isinstance(gem, Mapping)
                    and gem.get("enabled", True)
                    and not gem.get("isSupport")
                    and isinstance(gem.get("name"), str)
                ):
                    gname = gem["name"]
                    if gname not in names:
                        names.append(gname)
                    if grp.get("isMainSkill") and main_skill is None:
                        main_skill = gname
    if main_skill is None and names:
        main_skill = names[0]
    return main_skill, names


def _parse_int_stat(val: Any, default: int = 0) -> int:
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return int(round(float(val)))
    if isinstance(val, str):
        cleaned = re.sub(r"\^x[0-9A-Fa-f]{6}|\^\d", "", val)
        m = re.search(r"-?\d+", cleaned)
        if m:
            return int(m.group(0))
    return default


def _to_build_stats(
    stats_dict: Mapping[str, Any],
    *,
    character: Mapping[str, Any] | None = None,
    skills: Mapping[str, Any] | None = None,
    tree: Mapping[str, Any] | None = None,
) -> BuildStats:
    main_skill, active_skills = _active_skill_names(skills)
    alloc_count = 0
    if isinstance(tree, Mapping) and isinstance(tree.get("allocatedCount"), int):
        alloc_count = int(tree["allocatedCount"])
    elif isinstance(character, Mapping) and isinstance(character.get("passivePointsUsed"), int):
        alloc_count = int(character["passivePointsUsed"])

    keystones: list[str] = []
    if isinstance(tree, Mapping):
        for ks in tree.get("keystones") or []:
            if isinstance(ks, Mapping) and ks.get("name"):
                ks_name = str(ks["name"])
                if ks_name not in keystones:
                    keystones.append(ks_name)
    if stats_dict.get("ChaosInoculation") is True and "Chaos Inoculation" not in keystones:
        keystones.append("Chaos Inoculation")

    deflect_val = float(
        stats_dict.get(
            "deflect_chance",
            stats_dict.get(
                "deflection_chance",
                stats_dict.get("DeflectChance", stats_dict.get("Deflection", 0.0)),
            ),
        )
        or 0.0
    )
    eff_block = float(
        stats_dict.get(
            "effective_block_chance",
            stats_dict.get("EffectiveBlockChance", stats_dict.get("BlockChance", 0.0)),
        )
        or 0.0
    )
    block_max = float(
        stats_dict.get("block_chance_max", stats_dict.get("BlockChanceMax", 50.0)) or 50.0
    )

    full_dps = float(stats_dict.get("FullDPS", stats_dict.get("full_dps", 0.0)) or 0.0)
    total_dps = float(stats_dict.get("TotalDPS", stats_dict.get("total_dps", 0.0)) or 0.0)
    comb_dps = float(stats_dict.get("CombinedDPS", stats_dict.get("combined_dps", 0.0)) or 0.0)
    eff_dps = float(
        stats_dict.get("EffectiveDPS", stats_dict.get("effective_dps", 0.0))
        or (full_dps if full_dps > 0 else (comb_dps if comb_dps > 0 else total_dps))
    )

    life_val = float(stats_dict.get("Life", stats_dict.get("life", 0.0)) or 0.0)
    life_unres = float(
        stats_dict.get("LifeUnreserved", stats_dict.get("life_unreserved", life_val)) or life_val
    )
    es_val = float(stats_dict.get("EnergyShield", stats_dict.get("energy_shield", 0.0)) or 0.0)
    mana_val = float(stats_dict.get("Mana", stats_dict.get("mana", 0.0)) or 0.0)
    mana_unres = float(
        stats_dict.get("ManaUnreserved", stats_dict.get("mana_unreserved", mana_val)) or mana_val
    )
    mana_regen = float(
        stats_dict.get("ManaRegenRecovery", stats_dict.get("mana_regen", 0.0)) or 0.0
    )
    mana_cost = float(stats_dict.get("ManaCost", stats_dict.get("mana_cost", 0.0)) or 0.0)
    mana_ps_cost = float(
        stats_dict.get("ManaPerSecondCost", stats_dict.get("mana_per_second_cost", 0.0)) or 0.0
    )
    es_protects_mana = bool(stats_dict.get("EnergyShieldProtectsMana"))
    payable_pool = mana_unres + (es_val if es_protects_mana else 0.0)
    mana_warn = bool(stats_dict.get("ManaCostWarning"))
    mana_payable = (mana_cost <= 0.0) or ((payable_pool >= mana_cost) and not mana_warn)
    mana_sustainable = mana_payable and (mana_ps_cost <= 0.0 or mana_regen >= mana_ps_cost)

    str_tot = _parse_int_stat(stats_dict.get("Str", stats_dict.get("str_total", 0)))
    dex_tot = _parse_int_stat(stats_dict.get("Dex", stats_dict.get("dex_total", 0)))
    int_tot = _parse_int_stat(stats_dict.get("Int", stats_dict.get("int_total", 0)))
    req_str = _parse_int_stat(
        stats_dict.get("ReqStr", stats_dict.get("ReqStrString", stats_dict.get("req_str", 0)))
    )
    req_dex = _parse_int_stat(
        stats_dict.get("ReqDex", stats_dict.get("ReqDexString", stats_dict.get("req_dex", 0)))
    )
    req_int = _parse_int_stat(
        stats_dict.get("ReqInt", stats_dict.get("ReqIntString", stats_dict.get("req_int", 0)))
    )
    attrs_met = (str_tot >= req_str) and (dex_tot >= req_dex) and (int_tot >= req_int)

    chaos_immune = (stats_dict.get("ChaosInoculation") is True) or (
        "Chaos Inoculation" in keystones
    )
    ms_mod = float(
        stats_dict.get(
            "movement_speed_mod",
            stats_dict.get(
                "EffectiveMovementSpeedMod",
                stats_dict.get("MovementSpeedMod", 1.0),
            ),
        )
        or 1.0
    )
    ms_skill = float(
        stats_dict.get(
            "movement_speed_while_using_skill",
            stats_dict.get("MovementSpeedWhileUsingSkill", 0.0),
        )
        or 0.0
    )
    atk_cast_spd = float(stats_dict.get("attack_cast_speed", stats_dict.get("Speed", 0.0)) or 0.0)
    crit_ch = float(stats_dict.get("crit_chance", stats_dict.get("CritChance", 0.0)) or 0.0)
    crit_mult = float(
        stats_dict.get("crit_multiplier", stats_dict.get("CritMultiplier", 0.0)) or 0.0
    )
    raw_iir_mod = stats_dict.get("EffectiveLootRarityMod")
    if "item_rarity_pct" in stats_dict and stats_dict["item_rarity_pct"] is not None:
        iir_pct = float(stats_dict["item_rarity_pct"])
    elif raw_iir_mod is not None and float(raw_iir_mod) != 0.0:
        iir_pct = (float(raw_iir_mod) - 1.0) * 100.0
    else:
        iir_pct = 0.0

    payload: dict[str, Any] = {
        **dict(stats_dict),
        "chaos_immune": chaos_immune,
        "movement_speed_mod": round(ms_mod, 4),
        "movement_speed_while_using_skill": round(ms_skill, 4),
        "item_rarity_pct": round(iir_pct, 1),
        "attack_cast_speed": round(atk_cast_spd, 4),
        "crit_chance": round(crit_ch, 2),
        "crit_multiplier": round(crit_mult, 4),
        "class_name": character.get("className") if isinstance(character, Mapping) else None,
        "ascendancy_name": (
            character.get("ascendancyName") if isinstance(character, Mapping) else None
        ),
        "level": int(character.get("level", 1)) if isinstance(character, Mapping) else 1,
        "main_skill": main_skill,
        "combined_dps": comb_dps,
        "effective_dps": eff_dps,
        "life_unreserved": life_unres,
        "mana_unreserved": mana_unres,
        "mana_regen": mana_regen,
        "mana_cost": mana_cost,
        "mana_per_second_cost": mana_ps_cost,
        "mana_net_sustain": round(mana_regen - mana_ps_cost, 2),
        "mana_payable": mana_payable,
        "mana_sustainable": mana_sustainable,
        "spirit_total": int(round(float(stats_dict.get("Spirit", 0) or 0))),
        "spirit_reserved": int(round(float(stats_dict.get("SpiritReserved", 0) or 0))),
        "spirit_unreserved": int(round(float(stats_dict.get("SpiritUnreserved", 0) or 0))),
        "block_chance": eff_block,
        "effective_block_chance": eff_block,
        "block_chance_max": block_max,
        "deflection_chance": deflect_val,
        "deflect_chance": deflect_val,
        "str_total": str_tot,
        "dex_total": dex_tot,
        "int_total": int_tot,
        "req_str": req_str,
        "req_dex": req_dex,
        "req_int": req_int,
        "attributes_met": attrs_met,
        "allocated_nodes": alloc_count,
        "active_skills": active_skills,
        "keystones": keystones,
    }
    return BuildStats.model_validate(payload)


def _format_gem_paste_line(raw_gem: str) -> str:
    trimmed = raw_gem.strip()
    if not trimmed:
        return ""
    # Already in PoB socket-group format ("Spark 20/20  1" or "Slot: Amulet")
    if trimmed.lower().startswith("slot:") or re.search(r"\d+/\d+\s+\d+$", trimmed):
        return trimmed
    # Match optional trailing "20/20" or "Lv20"
    m = re.match(r"^(.*?)\s+(\d+)/(\d+)$", trimmed)
    if m:
        return f"{m.group(1).strip()} {m.group(2)}/{m.group(3)}  1"
    m_lvl = re.match(r"^(.*?)\s+(?:lv|lvl|level)\s*(\d+)$", trimmed, flags=re.IGNORECASE)
    if m_lvl:
        return f"{m_lvl.group(1).strip()} {m_lvl.group(2)}/0  1"
    return f"{trimmed} 20/0  1"


def _to_item_upgrade_diff(
    raw: Mapping[str, Any],
    *,
    item_text: str | None = None,
    price_exalted: float | None = None,
) -> ItemUpgradeDiff:
    before = raw.get("before") or {}
    after = raw.get("after") or {}
    delta = raw.get("delta") or {}
    pct = raw.get("pctChange") or {}

    full_before = float(before.get("FullDPS") or before.get("EffectiveDPS") or 0.0)
    full_after = float(after.get("FullDPS") or after.get("EffectiveDPS") or 0.0)
    full_delta = full_after - full_before
    full_pct = float(pct.get("FullDPS") or pct.get("EffectiveDPS") or 0.0)

    ehp_delta = float(delta.get("TotalEHP") or 0.0)
    dps_per_ex: float | None = None
    ehp_per_ex: float | None = None
    if price_exalted is not None and price_exalted > 0:
        dps_per_ex = round(full_delta / price_exalted, 4)
        ehp_per_ex = round(ehp_delta / price_exalted, 4)

    cand_info = raw.get("candidate") or {}
    label = raw.get("label") or cand_info.get("name") or raw.get("id")

    return ItemUpgradeDiff(
        equipment_slot=str(raw.get("targetSlot") or "Unknown"),
        candidate_label=str(label) if label else None,
        candidate_item_text=item_text,
        price_exalted=price_exalted,
        full_dps_before=full_before,
        full_dps_after=full_after,
        full_dps_delta=full_delta,
        full_dps_pct=full_pct,
        total_dps_before=float(before.get("TotalDPS") or 0.0),
        total_dps_after=float(after.get("TotalDPS") or 0.0),
        total_dps_pct=float(pct.get("TotalDPS") or 0.0),
        total_ehp_before=float(before.get("TotalEHP") or 0.0),
        total_ehp_after=float(after.get("TotalEHP") or 0.0),
        total_ehp_delta=ehp_delta,
        total_ehp_pct=float(pct.get("TotalEHP") or 0.0),
        life_delta=float(delta.get("Life") or 0.0),
        es_delta=float(delta.get("EnergyShield") or 0.0),
        mana_delta=float(delta.get("Mana") or 0.0),
        spirit_unreserved_delta=int(round(float(delta.get("SpiritUnreserved") or 0.0))),
        phys_max_hit_delta=float(delta.get("PhysicalMaximumHitTaken") or 0.0),
        elemental_max_hit_delta=float(delta.get("ElementalMaximumHitTaken") or 0.0),
        chaos_max_hit_delta=float(delta.get("ChaosMaximumHitTaken") or 0.0),
        resist_deltas={
            "fire": float(delta.get("FireResist") or 0.0),
            "cold": float(delta.get("ColdResist") or 0.0),
            "lightning": float(delta.get("LightningResist") or 0.0),
            "chaos": float(delta.get("ChaosResist") or 0.0),
        },
        dps_per_exalted=dps_per_ex,
        ehp_per_exalted=ehp_per_ex,
    )


def _classify_topoff_slot_profile(
    equipment_slot: str,
    base_type: str,
) -> tuple[int, str | None, str | None]:
    """Return `(max_artificer_sockets, quality_currency, greater_iron_rune_mod_line)`."""
    low_slot = equipment_slot.strip().lower()
    low_base = base_type.strip().lower()

    if any(
        tok in low_slot or tok in low_base
        for tok in ("ring", "amulet", "belt", "quiver", "flask", "charm", "jewel", "sceptre")
    ):
        return 0, None, None

    if "body" in low_slot:
        return 2, "Armourer's Scrap", "18% increased Armour, Evasion and Energy Shield"

    if any(tok in low_slot for tok in ("helmet", "gloves", "boots")) or any(
        tok in low_base for tok in ("shield", "buckler", "focus", "targe", "crest")
    ):
        return 1, "Armourer's Scrap", "18% increased Armour, Evasion and Energy Shield"

    if "quarterstaff" in low_base or any(
        tok in low_base
        for tok in (
            "bow",
            "crossbow",
            "greathammer",
            "greatmace",
            "two hand",
            "maul",
            "greatsword",
            "longsword",
            "greataxe",
            "poleaxe",
        )
    ):
        return 2, "Blacksmith's Whetstone", "18% increased Physical Damage"

    if "staff" in low_base:
        return 2, None, "30% increased Spell Damage"

    if "wand" in low_base:
        return 1, None, "30% increased Spell Damage"

    if "weapon" in low_slot or any(
        tok in low_base
        for tok in ("mace", "spear", "sword", "axe", "dagger", "claw", "flail", "talon")
    ):
        return 1, "Blacksmith's Whetstone", "18% increased Physical Damage"

    return 0, None, None


def _synthesize_topoff_pob_item_text(
    pob_text: str,
    *,
    equipment_slot: str,
    base_type: str,
    corrupted: bool,
    resolve_price_fn: Any,
) -> tuple[str, list[str], float]:
    """Virtually top off an uncorrupted (or empty-socket corrupted) PoB2 item with
    `Quality: 20` and `Greater Iron Rune` sockets so raw market listings compete fairly
    against pre-qualitied/pre-socketed listings.
    """
    if not pob_text.strip():
        return pob_text, [], 0.0

    max_sockets, qual_currency, rune_mod_line = _classify_topoff_slot_profile(
        equipment_slot, base_type
    )
    if max_sockets == 0 and qual_currency is None:
        return pob_text, [], 0.0

    lines = pob_text.splitlines()
    cur_quality = 0
    quality_line_idx: int | None = None
    cur_sockets = 0
    sockets_line_idx: int | None = None
    implicits_line_idx: int | None = None
    implicits_count = 0
    rune_spec_indices: list[int] = []
    rune_mod_count = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Quality:"):
            quality_line_idx = idx
            m_q = re.search(r"\d+", stripped)
            if m_q:
                cur_quality = int(m_q.group(0))
        elif stripped.startswith("Sockets:"):
            sockets_line_idx = idx
            cur_sockets = stripped.split(":", 1)[1].count("S")
        elif stripped.startswith("Rune:"):
            rune_spec_indices.append(idx)
        elif stripped.startswith("Implicits:"):
            implicits_line_idx = idx
            m_imp = re.search(r"\d+", stripped)
            if m_imp:
                implicits_count = int(m_imp.group(0))
        elif stripped.startswith("{rune}"):
            rune_mod_count += 1

    existing_filled_runes = max(len(rune_spec_indices), rune_mod_count)
    applied: list[str] = []
    total_cost_ex = 0.0
    out_lines = list(lines)

    header_insert_idx = (
        implicits_line_idx if implicits_line_idx is not None else min(3, len(out_lines))
    )

    if not corrupted and qual_currency is not None and cur_quality < 20:
        qual_needed = 20 - cur_quality
        if quality_line_idx is not None:
            out_lines[quality_line_idx] = "Quality: 20"
        else:
            out_lines.insert(header_insert_idx, "Quality: 20")
            header_insert_idx += 1
            if sockets_line_idx is not None and sockets_line_idx >= header_insert_idx - 1:
                sockets_line_idx += 1
            if implicits_line_idx is not None:
                implicits_line_idx += 1
            rune_spec_indices = [
                ri + 1 if ri >= header_insert_idx - 1 else ri for ri in rune_spec_indices
            ]
        total_cost_ex += float(resolve_price_fn(qual_currency, float(qual_needed)))
        applied.append(f"Quality {cur_quality}% -> 20% ({qual_needed}x {qual_currency})")

    target_sockets = cur_sockets if corrupted else max(cur_sockets, max_sockets)
    artificers_needed = 0 if corrupted else max(0, target_sockets - cur_sockets)
    empty_rune_slots = max(0, target_sockets - existing_filled_runes) if rune_mod_line else 0

    if artificers_needed > 0:
        total_cost_ex += float(resolve_price_fn("Artificer's Orb", float(artificers_needed)))
        applied.append(f"+{artificers_needed}x Artificer's Orb")

    if empty_rune_slots > 0 and rune_mod_line:
        sock_line = "Sockets: " + " ".join(["S"] * target_sockets)
        if sockets_line_idx is not None:
            out_lines[sockets_line_idx] = sock_line
            rune_insert_at = (
                rune_spec_indices[-1] + 1 if rune_spec_indices else sockets_line_idx + 1
            )
        else:
            ins_at = implicits_line_idx if implicits_line_idx is not None else header_insert_idx
            out_lines.insert(ins_at, sock_line)
            if implicits_line_idx is not None:
                implicits_line_idx += 1
            rune_insert_at = ins_at + 1

        if rune_mod_count > 0 and not rune_spec_indices and implicits_line_idx is not None:
            out_lines[implicits_line_idx] = f"Implicits: {implicits_count + empty_rune_slots}"
            for _ in range(empty_rune_slots):
                out_lines.insert(implicits_line_idx + 1, f"{{rune}}{rune_mod_line}")
        else:
            for _ in range(empty_rune_slots):
                out_lines.insert(rune_insert_at, "Rune: Greater Iron Rune")
                rune_insert_at += 1

        total_cost_ex += float(resolve_price_fn("Greater Iron Rune", float(empty_rune_slots)))
        applied.append(f"+{empty_rune_slots}x Greater Iron Rune ({rune_mod_line})")

    if not applied:
        return pob_text, [], 0.0

    return "\n".join(out_lines), applied, round(total_cost_ex, 2)


def _rewrite_item_runes(raw_text: str, keep_locked: Sequence[str], new_runes: Sequence[str]) -> str:
    """Rewrite overwriteable `Rune:` lines on PoB2 item text while preserving locked runes."""
    final_runes = [*keep_locked, *new_runes]
    out_lines: list[str] = []
    inserted = False
    for ln in raw_text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("Rune:"):
            if not inserted:
                out_lines.extend(f"Rune: {rn}" for rn in final_runes)
                inserted = True
            continue
        if stripped.startswith("{enchant}{rune}"):
            continue
        out_lines.append(ln)
        if not inserted and stripped.startswith("Sockets:"):
            out_lines.extend(f"Rune: {rn}" for rn in final_runes)
            inserted = True
    return "\n".join(out_lines)


def _edit_pob_item_text(
    raw_text: str,
    *,
    add_mod_lines: Sequence[str] | None = None,
    remove_mod_substrings: Sequence[str] | None = None,
    set_quality: int | None = None,
) -> str:
    """Apply surgical mod-line additions, substring removals, or Quality edits to a PoB2 item."""
    lines = raw_text.splitlines()
    if not lines:
        raise Pob2BridgeError("Cannot edit empty PoB2 item text")

    implicits_idx: int | None = None
    implicit_count = 0
    quality_idx: int | None = None
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Quality:"):
            quality_idx = idx
        elif s.startswith("Implicits:"):
            implicits_idx = idx
            m = re.search(r"\d+", s)
            if m:
                implicit_count = int(m.group(0))
            break

    if set_quality is not None:
        q_line = f"Quality: {int(set_quality)}"
        if quality_idx is not None:
            lines[quality_idx] = q_line
        else:
            ins_at = implicits_idx if implicits_idx is not None else min(3, len(lines))
            lines.insert(ins_at, q_line)
            if implicits_idx is not None:
                implicits_idx += 1

    explicit_start = (
        (implicits_idx + 1 + implicit_count) if implicits_idx is not None else min(3, len(lines))
    )
    remove_subs = [s.strip().lower() for s in (remove_mod_substrings or ()) if s.strip()]
    if remove_subs:
        kept_head = lines[:explicit_start]
        kept_tail = [
            ln for ln in lines[explicit_start:] if not any(sub in ln.lower() for sub in remove_subs)
        ]
        lines = kept_head + kept_tail

    for mod_ln in add_mod_lines or ():
        cleaned = str(mod_ln).strip()
        if cleaned:
            lines.append(cleaned)

    return "\n".join(lines)


def _fallback_fetch_entry_to_pob2_text(entry: Mapping[str, Any]) -> str:
    """Lazy-imports canonical trade2_fetch_to_pob2_text, with minimal inline fallback."""
    try:
        from poe2brain.trade.item_parser import trade2_fetch_to_pob2_text

        return trade2_fetch_to_pob2_text(dict(entry))
    except Exception:
        item = entry.get("item") or {}
        rarity = str(item.get("rarity") or "Rare").upper()
        name = str(item.get("name") or "").strip()
        base_type = str(item.get("baseType") or item.get("typeLine") or "Attuned Wand").strip()
        lines = [f"Rarity: {rarity}"]
        if name:
            lines.append(name)
        lines.append(base_type)
        lines.append("Implicits: 0")
        for group_key in ("implicitMods", "runeMods", "fracturedMods", "explicitMods"):
            for mod in item.get(group_key) or []:
                desc = mod.get("description") if isinstance(mod, Mapping) else str(mod)
                if desc:
                    cleaned = re.sub(
                        r"\[([^\]|]+)\|?([^\]]*)\]", lambda m: m.group(2) or m.group(1), str(desc)
                    )
                    lines.append(cleaned)
        return "\n".join(lines)


def _evaluate_hc_rip_risks(
    sust_stats: BuildStats,
    *,
    raw_pob_stats: Mapping[str, Any] | None = None,
    items: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[HcRipRisk], bool, list[str]]:
    """Evaluate PoE2 Patch 0.5.5 Pinnacle Boss & Hardcore one-shot / sustain hazards."""
    raw = raw_pob_stats or {}
    risks: list[HcRipRisk] = []

    has_ci = (
        ("Chaos Inoculation" in sust_stats.keystones)
        or bool(raw.get("ChaosInoculation"))
        or (sust_stats.life == 1.0 and sust_stats.chaos_resist >= 100.0)
    )

    charms: list[str] = []
    has_bleed_charm = False
    has_poison_charm = False
    for it in items or ():
        if not isinstance(it, Mapping):
            continue
        slot_nm = str(it.get("slot") or "")
        if slot_nm.startswith("Charm"):
            c_name = str(it.get("name") or "").strip()
            c_base = str(it.get("baseName") or "").strip()
            if c_name and c_base and c_base.lower() not in c_name.lower():
                nm = f"{c_name} ({c_base})"
            else:
                nm = c_name or c_base
            if nm:
                charms.append(nm)
            low_nm = nm.lower()
            if "staunching" in low_nm or "bleed" in low_nm:
                has_bleed_charm = True
            if "antidote" in low_nm or "poison" in low_nm or "amethyst" in low_nm:
                has_poison_charm = True

    if sust_stats.level >= 65:
        for res_name, val in (
            ("Fire", sust_stats.fire_resist),
            ("Cold", sust_stats.cold_resist),
            ("Lightning", sust_stats.lightning_resist),
        ):
            if val < 75.0:
                risks.append(
                    HcRipRisk(
                        risk_id=f"uncapped_{res_name.lower()}_resist",
                        severity="critical",
                        title=f"Uncapped {res_name} Resistance ({val:.0f}% < 75%)",
                        summary=(
                            f"Endgame {res_name} Resistance is {val:.0f}% under the -60% penalty, "
                            f"increasing {res_name} hit & DoT damage taken by "
                            f"+{((100.0 - val) / 25.0 - 1.0) * 100.0:.0f}% vs 75% cap."
                        ),
                        metric_value=round(val, 1),
                        threshold_value=75.0,
                        mitigation=(
                            f"Socket {res_name} Runes / Soul Cores of Citaqualotl or upgrade "
                            f"suffixes to >= 75% {res_name} Res (+15% overcap vs Exposure)."
                        ),
                    )
                )

    if (
        not has_ci
        and sust_stats.energy_shield >= 400.0
        and (
            sust_stats.energy_shield > sust_stats.life_unreserved
            or sust_stats.life_unreserved < 2000.0
        )
    ):
        sev: Literal["critical", "high"] = (
            "critical"
            if (
                sust_stats.life_unreserved < 1600.0
                or sust_stats.energy_shield >= 2.0 * max(sust_stats.life_unreserved, 1.0)
            )
            else "high"
        )
        charm_status = []
        if not has_bleed_charm:
            charm_status.append("no Staunching Charm")
        if not has_poison_charm:
            charm_status.append("no Antidote Charm")
        charm_note = f" ({', '.join(charm_status)})" if charm_status else " (Belt Charms present)"
        risks.append(
            HcRipRisk(
                risk_id="bleed_poison_es_bypass",
                severity=sev,
                title=(
                    f"Bleed & Poison Bypass {sust_stats.energy_shield:,.0f} ES -> "
                    f"{sust_stats.life_unreserved:,.0f} Life"
                ),
                summary=(
                    f"In PoE2 0.5.5, BOTH Bleeding AND Poison completely bypass Energy Shield "
                    f"({sust_stats.energy_shield:,.0f} ES) and drain your "
                    f"{sust_stats.life_unreserved:,.0f} unreserved Life pool directly without "
                    f"Chaos Inoculation{charm_note}."
                ),
                metric_value=round(sust_stats.life_unreserved, 1),
                threshold_value=2000.0,
                mitigation=(
                    "Allocate Chaos Inoculation (immune to BOTH Chaos Damage AND Bleeding) or "
                    "stack >= 2,200 unreserved Life, 75% Chaos Res, and Staunching/Antidote Charms."
                ),
            )
        )

    if not has_ci:
        if sust_stats.energy_shield > 0 and (
            sust_stats.chaos_resist < 75.0 or sust_stats.chaos_max_hit < 12000.0
        ):
            c_sev: Literal["critical", "high"] = (
                "critical"
                if (sust_stats.chaos_max_hit < 7500.0 or sust_stats.chaos_resist < 30.0)
                else "high"
            )
            risks.append(
                HcRipRisk(
                    risk_id="chaos_2x_es_vulnerability",
                    severity=c_sev,
                    title=(
                        f"Chaos Hits Deal 2x ES Damage (Chaos Max Hit "
                        f"{sust_stats.chaos_max_hit:,.0f} @ {sust_stats.chaos_resist:.0f}% Res)"
                    ),
                    summary=(
                        "In PoE2 0.5.5, Chaos damage removes 2x Energy Shield (200% to ES). "
                        f"At {sust_stats.chaos_resist:.0f}% Chaos Res, Chaos Max Hit is "
                        f"{sust_stats.chaos_max_hit:,.0f} (effective raw pool vs Chaos = "
                        f"{sust_stats.life_unreserved + sust_stats.energy_shield * 0.5:,.0f})."
                    ),
                    metric_value=round(sust_stats.chaos_max_hit, 1),
                    threshold_value=12000.0,
                    mitigation=(
                        "Cap Chaos Resistance to 75% (Amethyst Ring / Soul Core of Tacati / "
                        "suffixes) or pivot to Chaos Inoculation."
                    ),
                )
            )
        elif sust_stats.level >= 75 and (
            sust_stats.chaos_resist < 50.0 or sust_stats.chaos_max_hit < 8000.0
        ):
            risks.append(
                HcRipRisk(
                    risk_id="low_chaos_mitigation",
                    severity="high" if sust_stats.chaos_resist < 20.0 else "medium",
                    title=(
                        f"Low Chaos Mitigation (Chaos Max Hit {sust_stats.chaos_max_hit:,.0f}, "
                        f"{sust_stats.chaos_resist:.0f}% Chaos Res)"
                    ),
                    summary=(
                        f"Chaos Resistance is {sust_stats.chaos_resist:.0f}% with a "
                        f"{sust_stats.chaos_max_hit:,.0f} Chaos Max Hit vs Xesht/Breach/Ritual."
                    ),
                    metric_value=round(sust_stats.chaos_max_hit, 1),
                    threshold_value=10000.0,
                    mitigation="Raise Chaos Resistance toward 75% via Amethyst Rings and Runes.",
                )
            )

    if sust_stats.level >= 75 and sust_stats.phys_max_hit < 12000.0:
        p_sev: Literal["critical", "high"] = (
            "critical" if sust_stats.phys_max_hit < 8000.0 else "high"
        )
        risks.append(
            HcRipRisk(
                risk_id="low_pinnacle_phys_max_hit",
                severity=p_sev,
                title=(
                    f"Pinnacle Physical Slam One-Shot Risk "
                    f"(Phys Max Hit {sust_stats.phys_max_hit:,.0f})"
                ),
                summary=(
                    f"Sustainable Physical Max Hit Taken is {sust_stats.phys_max_hit:,.0f} "
                    f"(< 12,000 HC Arbiter/Xesht slam benchmark; Armour={sust_stats.armour:,.0f}, "
                    f"Deflect={sust_stats.deflect_chance:.0f}%, "
                    f"Block={sust_stats.effective_block_chance:.0f}%)."
                ),
                metric_value=round(sust_stats.phys_max_hit, 1),
                threshold_value=12000.0,
                mitigation=(
                    "Increase raw ES/Life pool, Physical Taken as Elemental (Cloak of Flame / "
                    "Helmet mods), or Deflection (40% hit mitigation)."
                ),
            )
        )

    if sust_stats.spirit_unreserved < 0:
        risks.append(
            HcRipRisk(
                risk_id="negative_spirit_reservation",
                severity="critical",
                title=(
                    f"Over-Reserved Spirit ({sust_stats.spirit_reserved}/{sust_stats.spirit_total})"
                ),
                summary=(
                    f"Unreserved Spirit is {sust_stats.spirit_unreserved}, silently disabling "
                    "persistent defensive/offensive Spirit buffs, Heralds, or Meta-Gems."
                ),
                metric_value=float(sust_stats.spirit_unreserved),
                threshold_value=0.0,
                mitigation=(
                    "Add +Spirit on Sceptre, Solar Amulet, Body Armour, or Shield, or drop a "
                    "Spirit reservation."
                ),
            )
        )

    if not sust_stats.attributes_met:
        deficits = []
        if sust_stats.str_total < sust_stats.req_str:
            deficits.append(f"Str {sust_stats.str_total}/{sust_stats.req_str}")
        if sust_stats.dex_total < sust_stats.req_dex:
            deficits.append(f"Dex {sust_stats.dex_total}/{sust_stats.req_dex}")
        if sust_stats.int_total < sust_stats.req_int:
            deficits.append(f"Int {sust_stats.int_total}/{sust_stats.req_int}")
        risks.append(
            HcRipRisk(
                risk_id="unmet_attribute_requirements",
                severity="critical",
                title=f"Unmet Attribute Requirements ({', '.join(deficits)})",
                summary=(
                    f"Character attributes ({', '.join(deficits)}) do not meet equipped gear or "
                    "gem requirements (watch Giant's Blood 3x martial weapon requirement!)."
                ),
                mitigation=(
                    "Switch travel attribute passives (+5 Str/Dex/Int) or add attributes on gear."
                ),
            )
        )

    if not sust_stats.mana_payable and sust_stats.mana_cost > 0:
        risks.append(
            HcRipRisk(
                risk_id="unpayable_mana_cost",
                severity="critical",
                title=(
                    f"Main Skill Mana Cost ({sust_stats.mana_cost:.0f}) Exceeds Unreserved Pool "
                    f"({sust_stats.mana_unreserved:.0f})"
                ),
                summary="Main skill upfront Mana cost exceeds available unreserved Mana.",
                metric_value=round(sust_stats.mana_unreserved, 1),
                threshold_value=round(sust_stats.mana_cost, 1),
                mitigation=(
                    "Increase Maximum Mana, reduce gem level, or socket Inspiration / Cost mods."
                ),
            )
        )
    elif not sust_stats.mana_sustainable and sust_stats.mana_per_second_cost > max(
        sust_stats.mana_regen * 1.35, 25.0
    ):
        risks.append(
            HcRipRisk(
                risk_id="mana_sustain_deficit",
                severity="medium",
                title=(
                    f"Net Mana Deficit ({sust_stats.mana_net_sustain:+.1f} Mana/s: "
                    f"{sust_stats.mana_regen:.1f} regen vs "
                    f"{sust_stats.mana_per_second_cost:.1f}/s)"
                ),
                summary=(
                    "Continuous Pinnacle boss casting outpaces passive Mana regeneration, "
                    "requiring Mana Flask charges, Mana Remnants, or Mana Leech."
                ),
                metric_value=round(sust_stats.mana_regen, 1),
                threshold_value=round(sust_stats.mana_per_second_cost, 1),
                mitigation=(
                    "Add Mana Regeneration Rate, Mana on Hit/Kill, Inspiration, or Mana Flasks."
                ),
            )
        )

    overcap_block = float(raw.get("BlockChanceOverCap") or 0.0)
    if overcap_block >= 5.0 and sust_stats.block_chance_max <= 50.0:
        risks.append(
            HcRipRisk(
                risk_id="passive_block_overcap",
                severity="info",
                title=f"Passive Block Overcapped Above 50% Base Cap (+{overcap_block:.0f}% wasted)",
                summary=(
                    f"PoE2 0.5.5 base maximum Passive Block is 50% (not PoE1's 75%); "
                    f"+{overcap_block:.0f}% Block investment is currently capped out."
                ),
                metric_value=round(sust_stats.block_chance + overcap_block, 1),
                threshold_value=sust_stats.block_chance_max,
                mitigation=(
                    "Refund excess Block passives for Life/ES or add +% to maximum Block chance."
                ),
            )
        )

    pinnacle_ready = not any(r.severity in ("critical", "high") for r in risks)
    return risks, pinnacle_ready, charms


class BuildService:
    """High-level PoE2 Build & Path of Building 2 intelligence service."""

    def __init__(
        self,
        *,
        bridge: Pob2Bridge | None = None,
        owns_bridge: bool | None = None,
        http_client: Poe2HttpClient | None = None,
        trade_client: Any = None,
        economy_service: Any = None,
    ) -> None:
        self._owns_bridge = False if owns_bridge is None else bool(owns_bridge)
        self.bridge = bridge if bridge is not None else get_default_pob2_bridge()
        self._owns_http = http_client is None
        self.http_client = http_client or Poe2HttpClient()
        self._trade_client = trade_client
        self._economy_service = economy_service

    def close(self) -> None:
        if self._owns_bridge:
            self.bridge.close()
        if self._owns_http:
            self.http_client.close()

    def __enter__(self) -> BuildService:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 1. Multi-slot lifecycle
    # ------------------------------------------------------------------

    def list_slots(self) -> dict[str, Any]:
        return dict(self.bridge.call("list_slots"))

    def switch_slot(self, slot: str, *, create_if_missing: bool = True) -> BuildStats:
        self.bridge.call("switch_slot", {"slot": slot, "createIfMissing": create_if_missing})
        return self.get_stats(slot=slot)

    def clone_slot(
        self,
        source: str = "main",
        target: str = "candidate",
        *,
        activate: bool = False,
    ) -> dict[str, Any]:
        return dict(
            self.bridge.call(
                "clone_slot",
                {"source": source, "target": target, "activate": activate},
            )
        )

    def reset_slot(
        self,
        slot: str = "main",
        *,
        class_name: str | None = None,
        ascendancy: str | None = None,
        level: int | None = None,
        name: str | None = None,
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot}
        if class_name is not None:
            params["className"] = class_name
        if ascendancy is not None:
            params["ascendancy"] = ascendancy
        if level is not None:
            params["level"] = level
        if name is not None:
            params["name"] = name
        self.bridge.call("reset_slot", params)
        return self.get_stats(slot=slot)

    def delete_slot(self, slot: str) -> dict[str, Any]:
        return dict(self.bridge.call("delete_slot", {"slot": slot}))

    @contextmanager
    def _scratch_slot(
        self,
        source_slot: str,
        *,
        prefix: str = "tmp",
        dewater: bool = False,
        restore_slot: str | None = None,
    ) -> Iterator[str]:
        """Clone `source_slot` into an isolated scratch slot and always clean it up on exit."""
        scratch = f"__{prefix}_{uuid.uuid4().hex[:8]}"
        self.clone_slot(source_slot, scratch, activate=False)
        try:
            if dewater:
                self.audit_build(slot=scratch, apply_dewater=True)
            yield scratch
        finally:
            try:
                if restore_slot is not None:
                    self.bridge.call(
                        "switch_slot", {"slot": restore_slot, "createIfMissing": False}
                    )
            finally:
                try:
                    self.bridge.call("delete_slot", {"slot": scratch})
                except Exception:
                    pass

    def undo(self, slot: str = "main") -> BuildStats:
        self.bridge.call("undo", {"slot": slot})
        return self.get_stats(slot=slot)

    def redo(self, slot: str = "main") -> BuildStats:
        self.bridge.call("redo", {"slot": slot})
        return self.get_stats(slot=slot)

    # ------------------------------------------------------------------
    # 2. Universal build loader & exporter
    # ------------------------------------------------------------------

    def _fetch_pobbin_code(self, url: str) -> str:
        match = _POBBIN_URL_RE.match(url.strip())
        if not match:
            raise ValueError(f"Unsupported pobb.in URL format: {url!r}")
        paste_id = match.group(1)
        raw_url = f"https://pobb.in/{paste_id}/raw"
        resp = self.http_client.sync_client.get(raw_url, headers={"Accept": "text/plain, */*"})
        resp.raise_for_status()
        code = resp.text.strip()
        if not code:
            raise ValueError(f"Empty share code returned from {raw_url}")
        return code

    def _fetch_remote_poe2_build_xml(self, url: str) -> tuple[str, str | None]:
        import tempfile

        from poe2brain.ninja.service import NinjaBuildsService
        from poe2brain.ninja.xml_inspector import decode_pob2_code_to_xml

        is_mocked = self.http_client._sync_transport is not None
        if is_mocked:
            with tempfile.TemporaryDirectory(prefix="poe2brain_mock_url_") as tmp_dir:
                ninja_svc = NinjaBuildsService(
                    http_client=self.http_client,
                    cache_dir=Path(tmp_dir),
                    min_uncached_char_interval_seconds=0.0,
                )
                imp = ninja_svc.fetch_character(url, load_into_pob=False, force_refresh=True)
        else:
            ninja_svc = NinjaBuildsService(http_client=self.http_client)
            imp = ninja_svc.fetch_character(url, load_into_pob=False)
        return decode_pob2_code_to_xml(imp.pob_code), imp.character

    def load_build(
        self,
        source: str | Path,
        *,
        slot: str = "main",
        name: str | None = None,
        level: int | None = None,
    ) -> BuildStats:
        text: str
        inferred_name = name
        if isinstance(source, Path):
            if not source.is_file():
                raise FileNotFoundError(f"Build file does not exist: {source}")
            text = source.read_text(encoding="utf-8")
            inferred_name = inferred_name or source.stem
        else:
            raw_str = str(source).strip()
            if not raw_str:
                raise ValueError("Build source cannot be empty")
            if _POBBIN_URL_RE.match(raw_str):
                text = self._fetch_pobbin_code(raw_str)
            elif raw_str.lower().startswith(("https://", "http://")):
                text, char_name = self._fetch_remote_poe2_build_xml(raw_str)
                inferred_name = inferred_name or char_name
            elif (
                len(raw_str) < 512
                and "\n" not in raw_str
                and not raw_str.startswith("<")
                and not raw_str.startswith("{")
                and Path(raw_str).expanduser().is_file()
            ):
                p = Path(raw_str).expanduser()
                text = p.read_text(encoding="utf-8")
                inferred_name = inferred_name or p.stem
            else:
                text = raw_str

        stripped = text.strip()
        if stripped.startswith("<") or "<PathOfBuilding" in stripped[:500]:
            self.bridge.call(
                "load_build_xml",
                {"slot": slot, "xml": stripped, "name": inferred_name},
            )
        elif stripped.startswith("{"):
            params: dict[str, Any] = {"slot": slot, "json": stripped}
            if level is not None:
                params["level"] = level
            self.bridge.call("load_ggg_build", params)
        else:
            self.bridge.call(
                "load_build_code",
                {"slot": slot, "code": stripped, "name": inferred_name},
            )
        return self.get_stats(slot=slot)

    def import_ggg_character_json(
        self,
        character_json: str,
        *,
        slot: str = "main",
        name: str | None = None,
    ) -> BuildStats:
        """Import official GGG OAuth `/character/poe2/{name}` JSON via PoB2 ImportTab."""
        params: dict[str, Any] = {"slot": slot, "characterJson": character_json}
        if name is not None:
            params["name"] = name
        self.bridge.call("import_ggg_character_json", params)
        return self.get_stats(slot=slot)

    def export_build(
        self,
        slot: str = "main",
        *,
        fmt: Literal["code", "xml", "ggg_json"] = "code",
        name: str | None = None,
        author: str | None = None,
    ) -> str:
        if fmt == "code":
            res = self.bridge.call("export_build_code", {"slot": slot})
            return str(res["code"])
        if fmt == "xml":
            res = self.bridge.call("export_build_xml", {"slot": slot})
            return str(res["xml"])
        if fmt == "ggg_json":
            params: dict[str, Any] = {"slot": slot}
            if name is not None:
                params["name"] = name
            if author is not None:
                params["author"] = author
            res = self.bridge.call("export_ggg_build", params)
            return str(res["json"])
        raise ValueError(
            f"Unsupported export format {fmt!r} (expected 'code', 'xml', or 'ggg_json')"
        )

    # ------------------------------------------------------------------
    # 3. Build inspection & editing
    # ------------------------------------------------------------------

    def get_stats(self, slot: str = "main") -> BuildStats:
        summary = self.bridge.call("get_build_summary", {"slot": slot, "includeItemRaw": False})
        return _to_build_stats(
            summary.get("stats") or {},
            character=summary.get("character"),
            skills=summary.get("skills"),
            tree=summary.get("tree"),
        )

    def inspect_build(
        self,
        slot: str = "main",
        *,
        include_item_raw: bool = False,
    ) -> dict[str, Any]:
        raw = self.bridge.call(
            "get_build_summary",
            {"slot": slot, "includeItemRaw": include_item_raw},
        )
        typed_stats = _to_build_stats(
            raw.get("stats") or {},
            character=raw.get("character"),
            skills=raw.get("skills"),
            tree=raw.get("tree"),
        )
        return {
            "slot": raw.get("slot", slot),
            "rev": raw.get("rev", 1),
            "character": raw.get("character") or {},
            "stats": typed_stats,
            "raw_pob_stats": raw.get("stats") or {},
            "skills": raw.get("skills") or {},
            "tree": raw.get("tree") or {},
            "items": raw.get("items") or [],
            "config": raw.get("config") or {},
        }

    def list_classes(self) -> list[dict[str, Any]]:
        res = self.bridge.call("list_classes")
        return list(res.get("classes") or [])

    def create_character(
        self,
        class_name: str,
        ascendancy: str | None = None,
        *,
        level: int = 1,
        slot: str = "main",
        name: str | None = None,
    ) -> BuildStats:
        return self.reset_slot(
            slot=slot,
            class_name=class_name,
            ascendancy=ascendancy,
            level=level,
            name=name,
        )

    def equip_item(
        self,
        item_text: str,
        *,
        target_slot: str | None = None,
        slot: str = "main",
        active: bool | None = None,
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot, "itemText": item_text}
        if target_slot is not None:
            params["targetSlot"] = target_slot
        if active is not None:
            params["active"] = active
        self.bridge.call("equip_item", params)
        return self.get_stats(slot=slot)

    def unequip_item(self, target_slot: str, *, slot: str = "main") -> BuildStats:
        self.bridge.call("unequip_item", {"slot": slot, "targetSlot": target_slot})
        return self.get_stats(slot=slot)

    def set_gem_links(
        self,
        gems_or_paste_text: str | Sequence[str],
        *,
        slot: str = "main",
        label: str | None = None,
        slot_name: str | None = None,
        include_in_full_dps: bool = True,
        set_main: bool = True,
    ) -> BuildStats:
        if isinstance(gems_or_paste_text, str):
            lines = [_format_gem_paste_line(ln) for ln in gems_or_paste_text.splitlines()]
        else:
            lines = [_format_gem_paste_line(str(g)) for g in gems_or_paste_text]
        paste_text = "\n".join(ln for ln in lines if ln) + "\n"
        params: dict[str, Any] = {
            "slot": slot,
            "text": paste_text,
            "includeInFullDPS": include_in_full_dps,
            "setMain": set_main,
        }
        if label is not None:
            params["label"] = label
        if slot_name is not None:
            params["slotName"] = slot_name
        self.bridge.call("paste_socket_group", params)
        return self.get_stats(slot=slot)

    def list_valid_supports(
        self,
        slot: str = "main",
        *,
        group_index: int | None = None,
        tier: int | None = None,
        include_lineage: bool = True,
        rank_by_dps: bool = False,
        top_n: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "slot": slot,
            "includeLineage": include_lineage,
            "rankByDps": rank_by_dps,
        }
        if group_index is not None:
            params["groupIndex"] = int(group_index)
        if tier is not None:
            params["tier"] = int(tier)
        if top_n is not None:
            params["topN"] = int(top_n)
        res = self.bridge.call("list_valid_supports", params)
        return list(res.get("supports") or [])

    def estimate_gold_respec_cost(
        self,
        level: int | None = None,
        *,
        refunded_passives: int = 0,
        refunded_ascendancy: int = 0,
        allocated_new: int = 0,
        slot: str = "main",
    ) -> GoldRespecEstimate:
        eff_level = level if level is not None else self.get_stats(slot=slot).level
        return estimate_poe2_gold_respec(
            eff_level,
            refunded_passives=refunded_passives,
            refunded_ascendancy=refunded_ascendancy,
            allocated_new=allocated_new,
        )

    def compare_slots(
        self,
        slot_a: str,
        slot_b: str,
        *,
        dewater: bool = False,
    ) -> BuildSlotComparison:
        """Compare two loaded PoB2 slots (e.g. early league vs current) across stats, gear,
        skills/supports, passives/keystones, and exact PoE2 0.5.5 Gold respec cost.
        """
        insp_a = self.inspect_build(slot_a)
        insp_b = self.inspect_build(slot_b)

        stats_a: BuildStats = insp_a["stats"]
        stats_b: BuildStats = insp_b["stats"]
        if dewater:
            stats_a = self.audit_build(slot_a, apply_dewater=False).sustainable_pinnacle_stats
            stats_b = self.audit_build(slot_b, apply_dewater=False).sustainable_pinnacle_stats

        eff_a = stats_a.full_dps if stats_a.full_dps > 0 else stats_a.effective_dps
        eff_b = stats_b.full_dps if stats_b.full_dps > 0 else stats_b.effective_dps
        eff_delta = round(eff_b - eff_a, 2)
        eff_pct = (
            round((eff_delta / eff_a) * 100.0, 2) if eff_a > 0 else (100.0 if eff_b > 0 else 0.0)
        )

        ehp_a = stats_a.total_ehp
        ehp_b = stats_b.total_ehp
        ehp_delta = round(ehp_b - ehp_a, 2)
        ehp_pct = (
            round((ehp_delta / ehp_a) * 100.0, 2) if ehp_a > 0 else (100.0 if ehp_b > 0 else 0.0)
        )

        stat_deltas = {
            "life": round(stats_b.life - stats_a.life, 2),
            "energy_shield": round(stats_b.energy_shield - stats_a.energy_shield, 2),
            "mana": round(stats_b.mana - stats_a.mana, 2),
            "mana_regen": round(stats_b.mana_regen - stats_a.mana_regen, 2),
            "spirit_total": float(stats_b.spirit_total - stats_a.spirit_total),
            "spirit_unreserved": float(stats_b.spirit_unreserved - stats_a.spirit_unreserved),
            "phys_max_hit": round(stats_b.phys_max_hit - stats_a.phys_max_hit, 2),
            "fire_max_hit": round(stats_b.fire_max_hit - stats_a.fire_max_hit, 2),
            "cold_max_hit": round(stats_b.cold_max_hit - stats_a.cold_max_hit, 2),
            "lightning_max_hit": round(stats_b.lightning_max_hit - stats_a.lightning_max_hit, 2),
            "chaos_max_hit": round(stats_b.chaos_max_hit - stats_a.chaos_max_hit, 2),
            "armour": round(stats_b.armour - stats_a.armour, 2),
            "evasion": round(stats_b.evasion - stats_a.evasion, 2),
            "evade_chance": round(stats_b.evade_chance - stats_a.evade_chance, 2),
            "deflect_chance": round(stats_b.deflect_chance - stats_a.deflect_chance, 2),
            "effective_block_chance": round(
                stats_b.effective_block_chance - stats_a.effective_block_chance, 2
            ),
        }

        # Equipment & Unique diffs
        items_a = {
            str(it["slot"]): it
            for it in insp_a["items"]
            if isinstance(it, Mapping) and it.get("slot")
        }
        items_b = {
            str(it["slot"]): it
            for it in insp_b["items"]
            if isinstance(it, Mapping) and it.get("slot")
        }
        all_eq_slots = list(dict.fromkeys([*items_a.keys(), *items_b.keys()]))

        def _clean_item_title(it: Mapping[str, Any]) -> str:
            raw_nm = str(it.get("title") or it.get("name") or "").strip()
            base_nm = str(it.get("baseName") or "").strip()
            if raw_nm and base_nm and raw_nm.endswith(f", {base_nm}"):
                return raw_nm[: -(len(base_nm) + 2)].strip()
            return raw_nm or base_nm

        uniques_a = {
            _clean_item_title(it)
            for it in items_a.values()
            if str(it.get("rarity") or "").upper() == "UNIQUE" and _clean_item_title(it)
        }
        uniques_b = {
            _clean_item_title(it)
            for it in items_b.values()
            if str(it.get("rarity") or "").upper() == "UNIQUE" and _clean_item_title(it)
        }
        added_uniques = sorted(uniques_b - uniques_a)
        removed_uniques = sorted(uniques_a - uniques_b)

        equipment_changes: list[EquipmentSlotDiff] = []
        for eq_slot in all_eq_slots:
            ia = items_a.get(eq_slot)
            ib = items_b.get(eq_slot)
            if ia is None and ib is not None:
                equipment_changes.append(
                    EquipmentSlotDiff(
                        equipment_slot=eq_slot,
                        after_name=str(ib.get("name") or "") or None,
                        after_base=str(ib.get("baseName") or "") or None,
                        after_rarity=str(ib.get("rarity") or "") or None,
                        change_type="added",
                    )
                )
            elif ia is not None and ib is None:
                equipment_changes.append(
                    EquipmentSlotDiff(
                        equipment_slot=eq_slot,
                        before_name=str(ia.get("name") or "") or None,
                        before_base=str(ia.get("baseName") or "") or None,
                        before_rarity=str(ia.get("rarity") or "") or None,
                        change_type="removed",
                    )
                )
            elif ia is not None and ib is not None:
                sig_a = (ia.get("name"), ia.get("baseName"), ia.get("rarity"))
                sig_b = (ib.get("name"), ib.get("baseName"), ib.get("rarity"))
                equipment_changes.append(
                    EquipmentSlotDiff(
                        equipment_slot=eq_slot,
                        before_name=str(ia.get("name") or "") or None,
                        before_base=str(ia.get("baseName") or "") or None,
                        before_rarity=str(ia.get("rarity") or "") or None,
                        after_name=str(ib.get("name") or "") or None,
                        after_base=str(ib.get("baseName") or "") or None,
                        after_rarity=str(ib.get("rarity") or "") or None,
                        change_type="unchanged" if sig_a == sig_b else "upgraded",
                    )
                )

        # Skill & Support diffs
        def _index_skill_groups(
            skills_blk: Mapping[str, Any],
        ) -> dict[str, tuple[int | None, set[str]]]:
            out_map: dict[str, tuple[int | None, set[str]]] = {}
            for grp in skills_blk.get("socketGroups") or []:
                if not isinstance(grp, Mapping) or not grp.get("enabled", True):
                    continue
                gems = [
                    g
                    for g in (grp.get("gems") or [])
                    if isinstance(g, Mapping) and g.get("enabled", True)
                ]
                act_name = grp.get("mainActiveSkillName")
                act_lvl: int | None = None
                supps: set[str] = set()
                for g in gems:
                    g_nm = str(g.get("name") or "").strip()
                    if not g_nm:
                        continue
                    if g.get("isSupport"):
                        supps.add(g_nm)
                    elif act_lvl is None:
                        if not act_name:
                            act_name = g_nm
                        act_lvl = int(g.get("level") or 1)
                if isinstance(act_name, str) and act_name:
                    prev = out_map.get(act_name)
                    if prev is None:
                        out_map[act_name] = (act_lvl, supps)
                    else:
                        out_map[act_name] = (
                            max(act_lvl or 0, prev[0] or 0) or None,
                            prev[1] | supps,
                        )
            return out_map

        sk_a = _index_skill_groups(insp_a["skills"])
        sk_b = _index_skill_groups(insp_b["skills"])
        added_skills = sorted(set(sk_b) - set(sk_a))
        removed_skills = sorted(set(sk_a) - set(sk_b))
        skill_changes: list[SkillGroupDiff] = []
        for sk_nm in dict.fromkeys([*sk_a.keys(), *sk_b.keys()]):
            if sk_nm in sk_b and sk_nm not in sk_a:
                lvl_b, sup_b = sk_b[sk_nm]
                skill_changes.append(
                    SkillGroupDiff(
                        active_skill=sk_nm,
                        change_type="added",
                        added_supports=sorted(sup_b),
                        level_after=lvl_b,
                    )
                )
            elif sk_nm in sk_a and sk_nm not in sk_b:
                lvl_a, sup_a = sk_a[sk_nm]
                skill_changes.append(
                    SkillGroupDiff(
                        active_skill=sk_nm,
                        change_type="removed",
                        removed_supports=sorted(sup_a),
                        level_before=lvl_a,
                    )
                )
            else:
                lvl_a, sup_a = sk_a[sk_nm]
                lvl_b, sup_b = sk_b[sk_nm]
                add_s = sorted(sup_b - sup_a)
                rem_s = sorted(sup_a - sup_b)
                changed = bool(add_s or rem_s or lvl_a != lvl_b)
                skill_changes.append(
                    SkillGroupDiff(
                        active_skill=sk_nm,
                        change_type="modified" if changed else "unchanged",
                        added_supports=add_s,
                        removed_supports=rem_s,
                        level_before=lvl_a,
                        level_after=lvl_b,
                    )
                )

        # Tree / Keystone / Notable / Ascendancy / Gold Respec diff
        tree_a = insp_a["tree"]
        tree_b = insp_b["tree"]
        ks_a = {
            str(n["name"])
            for n in (tree_a.get("keystones") or [])
            if isinstance(n, Mapping) and n.get("name")
        }
        ks_b = {
            str(n["name"])
            for n in (tree_b.get("keystones") or [])
            if isinstance(n, Mapping) and n.get("name")
        }
        not_a = {
            str(n["name"])
            for n in (tree_a.get("notables") or [])
            if isinstance(n, Mapping) and n.get("name")
        }
        not_b = {
            str(n["name"])
            for n in (tree_b.get("notables") or [])
            if isinstance(n, Mapping) and n.get("name")
        }

        asc_map_a = {
            int(n["id"]): str(n.get("name") or "")
            for n in (tree_a.get("ascendancyNodes") or [])
            if isinstance(n, Mapping)
            and isinstance(n.get("id"), int)
            and n.get("type") != "AscendClassStart"
        }
        asc_map_b = {
            int(n["id"]): str(n.get("name") or "")
            for n in (tree_b.get("ascendancyNodes") or [])
            if isinstance(n, Mapping)
            and isinstance(n.get("id"), int)
            and n.get("type") != "AscendClassStart"
        }
        asc_names_a = {nm for nm in asc_map_a.values() if nm}
        asc_names_b = {nm for nm in asc_map_b.values() if nm}

        alloc_ids_a = {
            int(nid) for nid in (tree_a.get("allocatedNodeIds") or []) if int(nid) < 65536
        }
        alloc_ids_b = {
            int(nid) for nid in (tree_b.get("allocatedNodeIds") or []) if int(nid) < 65536
        }
        removed_ids = alloc_ids_a - alloc_ids_b
        added_ids = alloc_ids_b - alloc_ids_a
        refunded_asc = len(removed_ids & set(asc_map_a.keys()))
        refunded_passives = max(0, len(removed_ids) - refunded_asc)

        respec_est = estimate_poe2_gold_respec(
            max(stats_a.level, stats_b.level),
            refunded_passives=refunded_passives,
            refunded_ascendancy=refunded_asc,
            allocated_new=len(added_ids),
        )

        return BuildSlotComparison(
            slot_a=slot_a,
            slot_b=slot_b,
            dewatered=dewater,
            stats_a=stats_a,
            stats_b=stats_b,
            level_delta=stats_b.level - stats_a.level,
            effective_dps_before=round(eff_a, 2),
            effective_dps_after=round(eff_b, 2),
            effective_dps_delta=eff_delta,
            effective_dps_pct=eff_pct,
            total_ehp_before=round(ehp_a, 2),
            total_ehp_after=round(ehp_b, 2),
            total_ehp_delta=ehp_delta,
            total_ehp_pct=ehp_pct,
            stat_deltas=stat_deltas,
            added_uniques=added_uniques,
            removed_uniques=removed_uniques,
            equipment_changes=equipment_changes,
            added_skills=added_skills,
            removed_skills=removed_skills,
            skill_changes=skill_changes,
            added_keystones=sorted(ks_b - ks_a),
            removed_keystones=sorted(ks_a - ks_b),
            added_notables=sorted(not_b - not_a),
            removed_notables=sorted(not_a - not_b),
            added_ascendancy_nodes=sorted(asc_names_b - asc_names_a),
            removed_ascendancy_nodes=sorted(asc_names_a - asc_names_b),
            passive_nodes_added_count=len(added_ids),
            passive_nodes_removed_count=len(removed_ids),
            respec_estimate=respec_est,
        )

    def set_config(
        self,
        values: Mapping[str, Any] | None = None,
        *,
        custom_mods: str | None = None,
        reset_keys: Sequence[str] | None = None,
        slot: str = "main",
    ) -> BuildStats:
        params: dict[str, Any] = {"slot": slot}
        if values is not None:
            params["values"] = dict(values)
        if custom_mods is not None:
            params["customMods"] = custom_mods
        if reset_keys is not None:
            params["resetKeys"] = list(reset_keys)
        self.bridge.call("set_config", params)
        return self.get_stats(slot=slot)

    def alloc_passive_nodes(
        self,
        node_ids: Sequence[int],
        *,
        slot: str = "main",
        connect_path: bool = True,
    ) -> BuildStats:
        self.bridge.call(
            "alloc_nodes",
            {"slot": slot, "nodeIds": [int(n) for n in node_ids], "connectPath": connect_path},
        )
        return self.get_stats(slot=slot)

    def dealloc_passive_nodes(self, node_ids: Sequence[int], *, slot: str = "main") -> BuildStats:
        self.bridge.call(
            "dealloc_nodes",
            {"slot": slot, "nodeIds": [int(n) for n in node_ids]},
        )
        return self.get_stats(slot=slot)

    def search_passive_nodes(
        self,
        query: str = "",
        *,
        node_type: str | None = None,
        limit: int = 25,
        slot: str = "main",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"slot": slot, "query": query, "limit": limit}
        if node_type is not None:
            params["nodeType"] = node_type
        res = self.bridge.call("search_nodes", params)
        return list(res.get("nodes") or [])

    # ------------------------------------------------------------------
    # 4. Dewatering / PoB-Warrior Audit
    # ------------------------------------------------------------------

    def audit_build(
        self,
        slot: str = "main",
        *,
        apply_dewater: bool = False,
        keep: Sequence[str] | None = None,
    ) -> DewateredAudit:
        params: dict[str, Any] = {"slot": slot, "apply": apply_dewater}
        if keep:
            params["keep"] = list(keep)
        res = self.bridge.call(
            "audit_build",
            params,
            timeout=max(self.bridge.request_timeout, 120.0),
        )

        char_info = res.get("character")
        skills_info = res.get("skills")
        tree_info = res.get("tree")

        raw_stats = _to_build_stats(
            res.get("rawStats") or {},
            character=char_info,
            skills=skills_info,
            tree=tree_info,
        )
        sust_stats = _to_build_stats(
            res.get("sustainableStats") or {},
            character=char_info,
            skills=skills_info,
            tree=tree_info,
        )

        stripped: list[StrippedPobFlag] = []
        for c in res.get("culprits") or []:
            stripped.append(
                StrippedPobFlag(
                    flag=str(c.get("key") or c.get("id") or "unknown"),
                    raw_value=c.get("originalValue"),
                    dps_inflation_pct=round(float(c.get("dpsInflationPct") or 0.0), 2),
                    ehp_inflation_pct=round(float(c.get("ehpInflationPct") or 0.0), 2),
                    reason=str(c.get("label") or c.get("category") or ""),
                )
            )

        warnings: list[str] = []
        total_dps_inf = float(res.get("totalDpsInflationPct") or 0.0)
        total_ehp_inf = float(res.get("totalEhpInflationPct") or 0.0)
        if total_dps_inf >= 25.0:
            warnings.append(
                f"Configured DPS is inflated by +{total_dps_inf:.1f}% above "
                "sustainable Pinnacle Boss baseline."
            )
        if total_ehp_inf >= 25.0:
            warnings.append(
                f"Configured EHP is inflated by +{total_ehp_inf:.1f}% above "
                "sustainable Pinnacle Boss baseline."
            )
        for res_name, val in (
            ("Fire", sust_stats.fire_resist),
            ("Cold", sust_stats.cold_resist),
            ("Lightning", sust_stats.lightning_resist),
        ):
            if sust_stats.level >= 65 and val < 75.0:
                warnings.append(
                    f"Endgame {res_name} Resistance is uncapped "
                    f"({val:.0f}% < 75%) under -60% penalty."
                )
        if sust_stats.spirit_unreserved < 0:
            warnings.append(
                f"Spirit is over-reserved ({sust_stats.spirit_unreserved} unreserved / "
                f"{sust_stats.spirit_total} total)."
            )
        if not apply_dewater and not res.get("restoredVerified", True):
            warnings.append("Warning: Post-audit snapshot verification drifted.")

        support_padding_flags = _detect_conditional_support_padding(skills_info, sust_stats)
        for sp_warn in support_padding_flags:
            warnings.append(f"[PADDING] {sp_warn}")

        summary_items = res.get("items") or (
            self.bridge.call("get_build_summary", {"slot": slot, "includeItemRaw": False}).get(
                "items"
            )
            or []
        )
        hc_risks, pinnacle_ready, _ = _evaluate_hc_rip_risks(
            sust_stats,
            raw_pob_stats=res.get("sustainableStats") or {},
            items=summary_items,
        )
        for risk in hc_risks:
            if risk.severity in ("critical", "high") and risk.risk_id in (
                "bleed_poison_es_bypass",
                "chaos_2x_es_vulnerability",
                "low_pinnacle_phys_max_hit",
                "unmet_attribute_requirements",
                "unpayable_mana_cost",
            ):
                warnings.append(f"[{risk.severity.upper()}] {risk.title}")

        # Attribute headroom margins (catches razor-thin gear-swap bricks)
        attr_margins: dict[str, int] = {}
        for attr_label, have_v, req_v in (
            ("str", sust_stats.str_total, sust_stats.req_str),
            ("dex", sust_stats.dex_total, sust_stats.req_dex),
            ("int", sust_stats.int_total, sust_stats.req_int),
        ):
            if req_v > 0:
                attr_margins[attr_label] = int(have_v - req_v)

        # Weapon Swap Safety Audit (Reddit/Bilibili #1 hidden death trap)
        weapon_swap_audit: WeaponSwapAudit | None = None
        ws_raw = res.get("weaponSwap")
        if isinstance(ws_raw, Mapping) and ws_raw.get("hasWeaponSwap"):
            alt_stats = _to_build_stats(
                ws_raw.get("alternateStats") or {},
                character=char_info,
                skills=skills_info,
                tree=tree_info,
            )
            swap_warns: list[str] = []
            if not alt_stats.attributes_met:
                swap_warns.append(
                    f"Weapon Set {ws_raw.get('alternateSet', 2)} FAILS attribute requirements "
                    f"(Str {alt_stats.str_total}/{alt_stats.req_str}, "
                    f"Dex {alt_stats.dex_total}/{alt_stats.req_dex}, "
                    f"Int {alt_stats.int_total}/{alt_stats.req_int}) — "
                    "swapping silently disables gear/gems!"
                )
            if alt_stats.spirit_unreserved < 0:
                swap_warns.append(
                    f"Weapon Set {ws_raw.get('alternateSet', 2)} over-reserves Spirit "
                    f"({alt_stats.spirit_unreserved} unreserved / {alt_stats.spirit_total} total) "
                    "— swapping turns off buffs/minions!"
                )
            for rn, base_r, alt_r in (
                ("Fire", sust_stats.fire_resist, alt_stats.fire_resist),
                ("Cold", sust_stats.cold_resist, alt_stats.cold_resist),
                ("Lightning", sust_stats.lightning_resist, alt_stats.lightning_resist),
            ):
                if sust_stats.level >= 65 and base_r >= 75.0 and alt_r < 75.0:
                    swap_warns.append(
                        f"Swapping to Weapon Set {ws_raw.get('alternateSet', 2)} "
                        f"uncaps {rn} Resistance ({base_r:.0f}% -> {alt_r:.0f}%)."
                    )
            ehp_drop = (
                ((sust_stats.total_ehp - alt_stats.total_ehp) / sust_stats.total_ehp * 100.0)
                if sust_stats.total_ehp > 0
                else 0.0
            )
            if ehp_drop >= 20.0:
                swap_warns.append(
                    f"Swapping to Weapon Set {ws_raw.get('alternateSet', 2)} cuts TotalEHP by "
                    f"-{ehp_drop:.1f}% ({sust_stats.total_ehp:,.0f} -> {alt_stats.total_ehp:,.0f})."
                )
            weapon_swap_audit = WeaponSwapAudit(
                has_weapon_swap=True,
                active_set=int(ws_raw.get("activeSet") or 1),
                alternate_set=int(ws_raw.get("alternateSet") or 2),
                weapon_set1_passives=int(ws_raw.get("weaponSet1Passives") or 0),
                weapon_set2_passives=int(ws_raw.get("weaponSet2Passives") or 0),
                alternate_full_dps=round(alt_stats.effective_dps, 2),
                alternate_total_ehp=round(alt_stats.total_ehp, 2),
                ehp_drop_pct=round(ehp_drop, 2),
                alternate_spirit_unreserved=alt_stats.spirit_unreserved,
                alternate_attributes_met=alt_stats.attributes_met,
                alternate_resists={
                    "fire": round(alt_stats.fire_resist, 1),
                    "cold": round(alt_stats.cold_resist, 1),
                    "lightning": round(alt_stats.lightning_resist, 1),
                    "chaos": round(alt_stats.chaos_resist, 1),
                },
                swap_warnings=swap_warns,
            )
            for sw in swap_warns:
                warnings.append(f"[WEAPON SWAP] {sw}")

        bottlenecks: list[str] = []
        if weapon_swap_audit and weapon_swap_audit.swap_warnings:
            bottlenecks.append(f"Weapon Swap Trap: {weapon_swap_audit.swap_warnings[0]}")
        tight_attrs = [f"{k.upper()} +{v}" for k, v in attr_margins.items() if 0 <= v <= 10]
        if tight_attrs:
            bottlenecks.append(
                f"Razor-thin attribute margin ({', '.join(tight_attrs)}) — "
                "swapping jewelry/weapons may silently disable gems or armour."
            )
        uncapped_ele = [
            f"{rn} {val:.0f}%"
            for rn, val in (
                ("Fire", sust_stats.fire_resist),
                ("Cold", sust_stats.cold_resist),
                ("Lightning", sust_stats.lightning_resist),
            )
            if sust_stats.level >= 65 and val < 75.0
        ]
        if uncapped_ele:
            bottlenecks.append(
                f"Cap Elemental Resistances first ({', '.join(uncapped_ele)} < 75%) "
                "via Rings/Boots/Runes."
            )
        if (
            not sust_stats.chaos_immune
            and sust_stats.level >= 75
            and sust_stats.chaos_resist < 50.0
        ):
            bottlenecks.append(
                f"Low Chaos Resistance ({sust_stats.chaos_resist:.0f}%) — Chaos deals 2x to ES; "
                "prioritize Chaos Res on Amethyst Ring / Belt / Soul Cores."
            )
        if sust_stats.spirit_unreserved < 0:
            bottlenecks.append(
                f"Over-reserved Spirit ({sust_stats.spirit_unreserved} unreserved / "
                f"{sust_stats.spirit_total} total) — add Spirit on Body Armour/Amulet/Sceptre "
                "or disable a secondary aura."
            )
        if not sust_stats.mana_sustainable and sust_stats.mana_per_second_cost > 0:
            bottlenecks.append(
                f"Unsustainable Mana ({sust_stats.mana_regen:.0f}/s vs "
                f"{sust_stats.mana_per_second_cost:.0f}/s cost) — check `poe2 pob calc-breakdown "
                "ManaCost` or Mana Remnants / Efficiency support."
            )
        if sust_stats.level >= 75 and sust_stats.phys_max_hit < 6000.0:
            bottlenecks.append(
                f"Low Pinnacle Physical Max Hit ({sust_stats.phys_max_hit:,.0f} < 6,000) — "
                "run `poe2 pob hc-audit` for highest-EHP defensive passives/gear."
            )
        if sust_stats.level >= 70 and 0.0 < sust_stats.movement_speed_mod < 1.2:
            bottlenecks.append(
                f"Slow map mobility ({(sust_stats.movement_speed_mod - 1.0) * 100:+.0f}% MS) — "
                "upgrade Boots MS (30–35%) or add a travel skill (Blink/Shattering Palm/Pounce)."
            )
        if sust_stats.level >= 78 and sust_stats.item_rarity_pct > 165.0:
            bottlenecks.append(
                f"Over-invested Player IIR ({sust_stats.item_rarity_pct:.0f}% > 150% soft cap) — "
                "PoE2 Item Rarity hits steep diminishing returns past ~125–150%; "
                "reclaim suffixes for DPS/EHP/MS."
            )
        elif sust_stats.level >= 78 and 0.0 <= sust_stats.item_rarity_pct < 35.0:
            warnings.append(
                f"Low Player Item Rarity ({sust_stats.item_rarity_pct:.0f}% IIR) — "
                "for endgame mapping (Breach/Wildwood/Expedition), targeting ~100–125% IIR "
                "on rings/helm gives the steepest drop-tier gain."
            )
        elif sust_stats.item_rarity_pct < 0.0:
            warnings.append(
                f"Negative Player Item Rarity ({sust_stats.item_rarity_pct:+.0f}% IIR) — "
                "configured for white/Exceptional ilvl 82 crafting-base farming "
                "rather than raw currency/unique drops."
            )
        if total_dps_inf >= 30.0:
            top_culprit = stripped[0].flag if stripped else "config"
            bottlenecks.append(
                f"High PoB inflation (+{total_dps_inf:.0f}%, top culprit: {top_culprit}) — "
                "evaluate upgrades against sustainable Pinnacle DPS rather than padded tooltip."
            )
        if support_padding_flags:
            bottlenecks.append(support_padding_flags[0])

        return DewateredAudit(
            slot_name=slot,
            raw_stats=raw_stats,
            sustainable_pinnacle_stats=sust_stats,
            dps_inflation_pct=round(total_dps_inf, 2),
            ehp_inflation_pct=round(total_ehp_inf, 2),
            stripped_flags=stripped,
            warnings=warnings,
            bottlenecks=bottlenecks[:6],
            hc_rip_risks=hc_risks,
            attribute_margins=attr_margins,
            weapon_swap_audit=weapon_swap_audit,
            pinnacle_ready=pinnacle_ready,
        )

    def audit_hc_rip_risks(
        self,
        slot: str = "main",
        *,
        apply_dewater: bool = False,
        top_defensive_nodes: int = 5,
    ) -> HcRipAuditReport:
        """Run a Hardcore Pinnacle Boss (Arbiter/Xesht) survivability, ES-bypass, Chaos 2x ES,
        Max-Hit, and Gold-respec defensive audit on `slot`.
        """
        dewatered = self.audit_build(slot=slot, apply_dewater=apply_dewater)
        sust = dewatered.sustainable_pinnacle_stats
        insp = self.inspect_build(slot=slot)
        _, _, charms = _evaluate_hc_rip_risks(
            sust,
            raw_pob_stats=insp.get("raw_pob_stats") or {},
            items=insp.get("items") or [],
        )

        has_ci = ("Chaos Inoculation" in sust.keystones) or (
            sust.life == 1.0 and sust.chaos_resist >= 100.0
        )
        respec_unit = estimate_poe2_gold_respec(sust.level).gold_per_passive_point

        if top_defensive_nodes <= 0:
            def_passives_raw: list[Any] = []
        elif apply_dewater:
            def_passives_raw = self.rank_passive_upgrades(
                slot=slot,
                mode="perPoint",
                stat="TotalEHP",
                max_path_dist=6,
                top_n=top_defensive_nodes,
            )
        else:
            with self._scratch_slot(
                slot, prefix="hc_dewater", dewater=True, restore_slot=slot
            ) as def_slot:
                def_passives_raw = self.rank_passive_upgrades(
                    slot=def_slot,
                    mode="perPoint",
                    stat="TotalEHP",
                    max_path_dist=6,
                    top_n=top_defensive_nodes,
                )
        def_options: list[DefensivePassiveRespecOption] = []
        for p in def_passives_raw:
            dist = p.path_dist or 1
            def_options.append(
                DefensivePassiveRespecOption(
                    node_id=p.node_id,
                    name=p.name,
                    path_dist=dist,
                    stats=p.stats,
                    ehp_delta=round(p.full_path_delta, 2),
                    ehp_pct=round(p.full_path_pct, 2),
                    per_point_ehp_delta=round(p.per_point_delta, 2),
                    per_point_ehp_pct=round(p.per_point_pct, 2),
                    respec_gold_cost=dist * respec_unit,
                )
            )

        chaos_es_pool = (
            sust.energy_shield
            if has_ci
            else round(sust.life_unreserved + sust.energy_shield * 0.5, 1)
        )
        bleed_poison_pool = None if has_ci else round(sust.life_unreserved, 1)

        return HcRipAuditReport(
            slot_name=slot,
            dewatered_audit=dewatered,
            max_hits={
                "physical": round(sust.phys_max_hit, 1),
                "fire": round(sust.fire_max_hit, 1),
                "cold": round(sust.cold_max_hit, 1),
                "lightning": round(sust.lightning_max_hit, 1),
                "chaos": round(sust.chaos_max_hit, 1),
            },
            chaos_es_effective_pool=chaos_es_pool,
            bleed_poison_life_pool=bleed_poison_pool,
            has_chaos_inoculation=has_ci,
            equipped_charms=charms,
            rip_risks=dewatered.hc_rip_risks,
            top_defensive_passives=def_options,
            respec_gold_per_point=respec_unit,
            pinnacle_ready=dewatered.pinnacle_ready,
        )

    # ------------------------------------------------------------------
    # 5. Single & Batch Candidate Item Upgrade Diffs
    # ------------------------------------------------------------------

    def diff_item(
        self,
        item_text: str,
        *,
        target_slot: str | None = None,
        slot: str = "main",
        label: str | None = None,
        price_exalted: float | None = None,
    ) -> ItemUpgradeDiff:
        params: dict[str, Any] = {"slot": slot, "itemText": item_text}
        if target_slot is not None:
            params["targetSlot"] = target_slot
        if label is not None:
            params["label"] = label
        raw = self.bridge.call("diff_item", params)
        return _to_item_upgrade_diff(raw, item_text=item_text, price_exalted=price_exalted)

    def diff_items_batch(
        self,
        items: Sequence[Mapping[str, Any] | str],
        *,
        target_slot: str | None = None,
        slot: str = "main",
    ) -> list[ItemUpgradeDiff]:
        normalized: list[dict[str, Any]] = []
        prices_by_idx: dict[int, float | None] = {}
        texts_by_idx: dict[int, str] = {}

        for idx, entry in enumerate(items, start=1):
            if isinstance(entry, str):
                item_txt = entry
                req: dict[str, Any] = {"id": str(idx), "itemText": item_txt}
                if target_slot is not None:
                    req["targetSlot"] = target_slot
                prices_by_idx[idx] = None
                texts_by_idx[idx] = item_txt
            else:
                item_txt = str(
                    entry.get("itemText")
                    or entry.get("item_text")
                    or entry.get("pob_item_text")
                    or ""
                )
                req = {
                    "id": str(entry.get("id") or idx),
                    "itemText": item_txt,
                }
                lbl = entry.get("label") or entry.get("candidate_label")
                if lbl is not None:
                    req["label"] = str(lbl)
                tslot = entry.get("targetSlot") or entry.get("target_slot") or target_slot
                if tslot is not None:
                    req["targetSlot"] = str(tslot)
                raw_price = entry.get("price_exalted")
                prices_by_idx[idx] = float(raw_price) if raw_price is not None else None
                texts_by_idx[idx] = item_txt
            normalized.append(req)

        res = self.bridge.call("diff_items_batch", {"slot": slot, "items": normalized})
        diffs: list[ItemUpgradeDiff] = []
        for idx, r in enumerate(res.get("results") or [], start=1):
            if not isinstance(r, Mapping) or not r.get("ok"):
                continue
            diffs.append(
                _to_item_upgrade_diff(
                    r,
                    item_text=texts_by_idx.get(idx),
                    price_exalted=prices_by_idx.get(idx),
                )
            )
        return diffs

    # ------------------------------------------------------------------
    # 6. Passive Node Diff & Notable / Anoint Ranking
    # ------------------------------------------------------------------

    def rank_passive_upgrades(
        self,
        slot: str = "main",
        *,
        mode: Literal["perPoint", "total", "anoint"] = "perPoint",
        stat: str = "FullDPS",
        max_path_dist: int = 8,
        top_n: int = 15,
        include_keystones: bool = False,
    ) -> list[PassiveNodeUpgrade]:
        res = self.bridge.call(
            "rank_passive_notables",
            {
                "slot": slot,
                "sortBy": mode,
                "stat": stat,
                "maxPathDist": max_path_dist,
                "topN": top_n,
                "includeKeystones": include_keystones,
            },
            timeout=max(self.bridge.request_timeout, 120.0),
        )
        cur_level = self.get_stats(slot=slot).level
        gold_per_pt = estimate_poe2_gold_respec(cur_level).gold_per_passive_point
        out: list[PassiveNodeUpgrade] = []
        for row in res.get("notables") or []:
            if not isinstance(row, Mapping):
                continue
            p_dist = int(row["pathDist"]) if isinstance(row.get("pathDist"), int) else None
            emotions, ssf_tier = _resolve_anoint_emotions(row.get("recipe"))
            respec_swap_gold = (
                (p_dist * gold_per_pt) if (mode != "anoint" and p_dist is not None) else None
            )
            out.append(
                PassiveNodeUpgrade(
                    node_id=int(row["nodeId"]),
                    string_id=str(row["stringId"]) if row.get("stringId") else None,
                    name=str(row.get("name") or ""),
                    node_type=str(row.get("type") or "Notable"),
                    stats=[str(s) for s in (row.get("stats") or [])],
                    path_dist=p_dist,
                    path_node_ids=[int(pid) for pid in (row.get("pathNodeIds") or [])],
                    single_node_delta=float(row.get("singleNodeDelta") or 0.0),
                    single_node_pct=float(row.get("singleNodePct") or 0.0),
                    full_path_delta=float(row.get("fullPathDelta") or 0.0),
                    full_path_pct=float(row.get("fullPathPct") or 0.0),
                    per_point_delta=float(row.get("perPointDelta") or 0.0),
                    per_point_pct=float(row.get("perPointPct") or 0.0),
                    anoint_emotions=emotions,
                    ssf_anoint_tier=ssf_tier,
                    respec_gold_if_swapped=respec_swap_gold,
                )
            )
        return out

    def diff_passive_nodes(
        self,
        *,
        add_node_ids: Sequence[int] | None = None,
        remove_node_ids: Sequence[int] | None = None,
        slot: str = "main",
        include_path: bool = False,
        include_dependents: bool = False,
    ) -> dict[str, Any]:
        return dict(
            self.bridge.call(
                "diff_passive_nodes",
                {
                    "slot": slot,
                    "addNodeIds": [int(n) for n in (add_node_ids or [])],
                    "removeNodeIds": [int(n) for n in (remove_node_ids or [])],
                    "includePath": include_path,
                    "includeDependents": include_dependents,
                },
            )
        )

    # ------------------------------------------------------------------
    # 7. Slot Marginal Stat Weights & Live Closed-Loop Upgrade Finder
    # ------------------------------------------------------------------

    def compute_slot_trade_weights(
        self,
        slot_name: str,
        *,
        build_slot: str = "main",
        metric: Literal["FullDPS", "TotalEHP", "balanced"] = "FullDPS",
        league: str = DEFAULT_LEAGUE,
        max_price_exalted: float | None = None,
        securable_only: bool = True,
        min_rune_sockets: int = 0,
        top_n: int = 35,
    ) -> SlotStatWeights:
        del league  # Reserved for economy-adjusted filters if needed
        if metric == "FullDPS":
            stat_weights = [{"stat": "FullDPS", "weightMult": 1.0}]
        elif metric == "TotalEHP":
            stat_weights = [{"stat": "TotalEHP", "weightMult": 1.0}]
        elif metric == "balanced":
            stat_weights = [
                {"stat": "FullDPS", "weightMult": 1.0},
                {"stat": "TotalEHP", "weightMult": 0.6},
            ]
        else:
            raise ValueError(
                f"Unsupported metric {metric!r} (expected FullDPS, TotalEHP, balanced)"
            )

        params: dict[str, Any] = {
            "slot": build_slot,
            "targetSlot": slot_name,
            "statWeights": stat_weights,
            "statusOption": "securable" if securable_only else "available",
            "sockets": min_rune_sockets,
            "topN": top_n,
        }
        if max_price_exalted is not None and max_price_exalted > 0:
            params["maxPrice"] = max_price_exalted

        res = self.bridge.call("slot_stat_weights", params)
        base_summary = self.get_stats(slot=build_slot)
        base_dps = max(base_summary.full_dps, base_summary.total_dps, 1.0)
        base_ehp = max(base_summary.total_ehp, 1.0)

        entries: list[StatWeightEntry] = []
        count_filters: list[dict[str, Any]] = []
        raw_weights = [w for w in (res.get("weights") or []) if isinstance(w, Mapping)]
        for w in raw_weights:
            wt = float(w.get("weight") or 0.0)
            mean_diff = float(w.get("meanStatDiff") or 0.0)
            # PoB2 TradeQueryGenerator scales ratio by 1000: weight = (delta_ratio * 1000) / value
            dps_unit = (wt / 1000.0) * base_dps if metric in ("FullDPS", "balanced") else 0.0
            ehp_unit = (wt / 1000.0) * base_ehp if metric in ("TotalEHP", "balanced") else 0.0
            stat_id = str(w["tradeModId"])
            entries.append(
                StatWeightEntry(
                    stat_id=stat_id,
                    stat_label=str(w.get("text") or stat_id),
                    weight=round(wt, 4),
                    dps_per_unit=round(dps_unit, 3),
                    ehp_per_unit=round(ehp_unit, 3),
                )
            )
            if wt > 0 and len(count_filters) < 8:
                midpoint = abs(mean_diff / wt) if abs(wt) > 1e-6 else 1.0
                min_roll = max(1, int(round(midpoint * 0.55)))
                count_filters.append({"id": stat_id, "value": {"min": min_roll}})

        # Note: GGG's unauthenticated trade2 API rejects `type: "weight"` (complexity >= 54
        # requires POESESSID login), whereas `type: "count"` with PoB2-derived midpoint min
        # thresholds succeeds with HTTP 200 unauthenticated and feeds real candidates into
        # PoB2's exact GetMiscCalculator() ranker.
        raw_payload = res.get("trade2Payload")
        raw_query = (
            dict(raw_payload["query"])
            if isinstance(raw_payload, Mapping) and isinstance(raw_payload.get("query"), Mapping)
            else {}
        )
        raw_filters = dict(raw_query.get("filters") or {})
        clean_filters = {
            k: v
            for k, v in raw_filters.items()
            if k in ("type_filters", "trade_filters", "equipment_filters", "req_filters")
        }
        min_count = 3 if len(count_filters) >= 5 else (2 if len(count_filters) >= 2 else 1)
        sort_spec = (
            {"price": "desc"}
            if (max_price_exalted is not None and max_price_exalted > 0)
            else {"price": "asc"}
        )
        unauth_trade2_payload: dict[str, Any] = (
            {
                "query": {
                    "status": raw_query.get(
                        "status",
                        {"option": "securable" if securable_only else "available"},
                    ),
                    "filters": clean_filters,
                    "stats": (
                        [
                            {
                                "type": "count",
                                "value": {"min": min_count},
                                "filters": count_filters,
                            }
                        ]
                        if count_filters
                        else []
                    ),
                },
                "sort": sort_spec,
            }
            if raw_query
            else {}
        )

        return SlotStatWeights(
            equipment_slot=slot_name,
            objective=metric,
            weights=entries,
            trade2_weighted_filter=unauth_trade2_payload,
        )

    def _resolve_currency_to_exalted(self, currency: str, amount: float, league: str) -> float:
        cur = currency.strip().lower()
        if cur in ("exalted", "exalt", "ex", "exalted-orb"):
            return amount
        econ = self._economy_service
        if econ is None:
            try:
                from poe2brain.trade.economy import EconomyService

                self._economy_service = EconomyService(client=self.http_client)
                econ = self._economy_service
            except Exception:
                econ = None
        if econ is not None:
            try:
                rates = econ.get_reference_rates(league)
                div_ex = float(getattr(rates, "divine_in_exalted", 450.0) or 450.0)
                chaos_ex = float(getattr(rates, "chaos_in_exalted", 35.0) or 35.0)
                if cur in ("divine", "div", "divine-orb"):
                    return amount * div_ex
                if cur in ("chaos", "c", "chaos-orb"):
                    return amount * chaos_ex
                quote = econ.get_price(cur, league)
                if quote is not None and getattr(quote, "exalted", None):
                    return amount * float(quote.exalted)
            except Exception:
                pass
        # Conservative fallback ratios if offline
        fallback = {
            "divine": 450.0,
            "chaos": 35.0,
            "regal": 0.25,
            "vaal": 1.0,
            "alch": 0.5,
            "armourer's scrap": 0.05,
            "blacksmith's whetstone": 0.08,
            "artificer's orb": 0.5,
            "greater iron rune": 0.5,
        }
        return amount * fallback.get(cur, 1.0)

    def _execute_trade2_weighted_search(
        self,
        *,
        league: str,
        payload: dict[str, Any],
        limit: int,
    ) -> tuple[str | None, str | None, int, list[TradeListing]]:
        # 1. Use injected or lazy Trade2Client if available and compatible
        tclient = self._trade_client
        if tclient is None:
            try:
                from poe2brain.trade.trade2_client import Trade2Client

                self._trade_client = Trade2Client(client=self.http_client)
                tclient = self._trade_client
            except Exception:
                tclient = None

        if tclient is not None:
            for method_name in ("search_and_fetch", "price_check_query", "search_items"):
                fn = getattr(tclient, method_name, None)
                if callable(fn):
                    try:
                        res = fn(payload, league=league, limit=limit)
                    except TypeError:
                        try:
                            res = fn(league, payload, limit=limit)
                        except TypeError:
                            continue
                    if hasattr(res, "listings"):
                        listings = list(res.listings)
                        for lst in listings:
                            if not lst.pob_item_text and getattr(lst, "raw_entry", None):
                                lst.pob_item_text = _fallback_fetch_entry_to_pob2_text(
                                    lst.raw_entry
                                )
                        return (
                            getattr(res, "query_id", None),
                            getattr(res, "trade_url", None),
                            int(getattr(res, "total_matches", len(listings)) or 0),
                            listings[:limit],
                        )

        # 2. Direct official GGG Trade2 search + batched fetch via shared Poe2HttpClient
        encoded_league = url_quote(league, safe="")
        search_url = f"{GGG_TRADE2_BASE_URL}/search/poe2/{encoded_league}"
        search_resp = self.http_client.post_json(search_url, json_body=payload)
        query_id = str(search_resp.get("id") or "") or None
        total_matches = int(search_resp.get("total") or 0)
        result_ids = [str(rid) for rid in (search_resp.get("result") or [])]
        trade_url = f"{GGG_TRADE2_WEB_BASE_URL}/{encoded_league}/{query_id}" if query_id else None
        if not result_ids:
            return query_id, trade_url, total_matches, []

        # Fetch up to 2x limit (capped at 20 = 2 chunks) so account deduplication leaves `limit`
        target_ids = result_ids[: min(len(result_ids), max(limit * 2, 10), 20)]
        raw_entries: list[dict[str, Any]] = []
        for offset in range(0, len(target_ids), 10):
            chunk = target_ids[offset : offset + 10]
            fetch_url = f"{GGG_TRADE2_BASE_URL}/fetch/{','.join(chunk)}"
            params = {"realm": "poe2"}
            if query_id:
                params["query"] = query_id
            fetch_resp = self.http_client.get_json(fetch_url, params=params)
            for row in fetch_resp.get("result") or []:
                if isinstance(row, dict):
                    raw_entries.append(row)

        seen_accounts: set[str] = set()
        listings: list[TradeListing] = []
        for entry in raw_entries:
            listing_info = entry.get("listing") or {}
            item_info = entry.get("item") or {}
            acct_info = listing_info.get("account") or {}
            acct_name = str(acct_info.get("name") or "unknown")
            if acct_name in seen_accounts:
                continue
            seen_accounts.add(acct_name)

            price_info = listing_info.get("price") or {}
            p_amount = float(price_info.get("amount") or 0.0)
            p_curr = str(price_info.get("currency") or "exalted")
            p_exalted = (
                self._resolve_currency_to_exalted(p_curr, p_amount, league) if p_amount > 0 else 0.0
            )
            fee_raw = listing_info.get("fee")
            fee_int = int(fee_raw) if isinstance(fee_raw, (int, float)) else None
            whisper = listing_info.get("whisper")
            sale_type = (
                SaleType.SECURABLE
                if fee_int is not None
                else (SaleType.WHISPER if whisper else SaleType.UNKNOWN)
            )
            pob_text = _fallback_fetch_entry_to_pob2_text(entry)

            mod_lines: list[str] = []
            for mgroup in ("implicitMods", "runeMods", "fracturedMods", "explicitMods"):
                for m in item_info.get(mgroup) or []:
                    desc = m.get("description") if isinstance(m, Mapping) else str(m)
                    if desc:
                        mod_lines.append(
                            re.sub(
                                r"\[([^\]|]+)\|?([^\]]*)\]",
                                lambda match: match.group(2) or match.group(1),
                                str(desc),
                            )
                        )

            listings.append(
                TradeListing(
                    listing_id=str(entry.get("id") or ""),
                    item_name=str(item_info.get("name") or "") or None,
                    base_type=str(
                        item_info.get("baseType") or item_info.get("typeLine") or "Unknown"
                    ),
                    item_level=(
                        int(item_info["ilvl"]) if isinstance(item_info.get("ilvl"), int) else None
                    ),
                    corrupted=bool(item_info.get("corrupted")),
                    price_amount=p_amount,
                    price_currency=p_curr,
                    price_exalted=round(p_exalted, 2),
                    sale_type=sale_type,
                    fee=fee_int,
                    whisper=str(whisper) if whisper else None,
                    account_name=acct_name,
                    online=acct_info.get("online") is not None,
                    indexed_at=str(listing_info.get("indexed") or "") or None,
                    pob_item_text=pob_text,
                    mod_lines=mod_lines,
                )
            )
            if len(listings) >= limit:
                break

        return query_id, trade_url, total_matches, listings

    def find_live_slot_upgrades(
        self,
        slot_name: str,
        *,
        build_slot: str = "main",
        metric: Literal["FullDPS", "TotalEHP", "balanced"] = "FullDPS",
        league: str = DEFAULT_LEAGUE,
        max_price_exalted: float | None = None,
        min_spirit: int = 0,
        securable_only: bool = True,
        min_rune_sockets: int = 0,
        candidate_limit: int = 10,
        sort_by_value: bool = False,
        normalize_craft_topoff: bool = True,
    ) -> LiveSlotUpgradeReport:
        slot_weights = self.compute_slot_trade_weights(
            slot_name,
            build_slot=build_slot,
            metric=metric,
            league=league,
            max_price_exalted=max_price_exalted,
            securable_only=securable_only,
            min_rune_sockets=min_rune_sockets,
        )
        payload = dict(slot_weights.trade2_weighted_filter)
        if not payload or not payload.get("query"):
            raise Pob2BridgeError(
                f"PoB2 could not generate a weighted trade2 query for slot {slot_name!r}"
            )

        if min_spirit > 0:
            from poe2brain.trade.stat_matcher import resolve_natural_stat, resolve_slot_or_category

            query_block = dict(payload.get("query") or {})
            cat = resolve_slot_or_category(slot_name) or ""
            if cat == "weapon.sceptre":
                filters_block = dict(query_block.get("filters") or {})
                eq_block = dict(filters_block.get("equipment_filters") or {})
                eq_filters = dict(eq_block.get("filters") or {})
                eq_filters["spirit"] = {"min": min_spirit}
                eq_block["filters"] = eq_filters
                filters_block["equipment_filters"] = eq_block
                query_block["filters"] = filters_block
            else:
                spirit_cands = resolve_natural_stat(
                    "spirit", category=cat or "accessory.amulet", limit=1
                )
                if spirit_cands and spirit_cands[0].trade_ids:
                    stats_list = list(query_block.get("stats") or [])
                    stats_list.append(
                        {
                            "type": "count",
                            "value": {"min": 1},
                            "filters": [
                                {"id": tid, "value": {"min": min_spirit}}
                                for tid in spirit_cands[0].trade_ids
                            ],
                        }
                    )
                    query_block["stats"] = stats_list
            payload["query"] = query_block

        query_id, trade_url, total_matches, listings = self._execute_trade2_weighted_search(
            league=league,
            payload=payload,
            limit=candidate_limit,
        )
        if not listings:
            return LiveSlotUpgradeReport(
                league=league,
                build_slot=build_slot,
                equipment_slot=slot_name,
                metric=metric,
                max_price_exalted=max_price_exalted,
                securable_only=securable_only,
                normalize_craft_topoff=normalize_craft_topoff,
                query_id=query_id,
                trade_url=trade_url,
                total_market_matches=total_matches,
                slot_weights=slot_weights,
                candidates=[],
            )

        batch_inputs: list[dict[str, Any]] = []
        listings_by_id: dict[str, TradeListing] = {}
        topoff_meta_by_id: dict[str, tuple[str, list[str], float]] = {}
        unit_price_cache: dict[str, float | None] = {}

        def _cached_price(cur: str, amt: float) -> float | None:
            if cur not in unit_price_cache:
                unit_price_cache[cur] = self._resolve_currency_to_exalted(cur, 1.0, league)
            unit = unit_price_cache[cur]
            return unit * amt if unit is not None else None

        for idx, lst in enumerate(listings, start=1):
            lid = lst.listing_id or f"cand_{idx}"
            listings_by_id[lid] = lst
            cand_label = f"{lst.item_name + ' ' if lst.item_name else ''}{lst.base_type}".strip()
            raw_txt = lst.pob_item_text or ""
            batch_inputs.append(
                {
                    "id": lid,
                    "label": cand_label,
                    "targetSlot": slot_name,
                    "itemText": raw_txt,
                }
            )
            if normalize_craft_topoff:
                top_txt, top_applied, top_cost = _synthesize_topoff_pob_item_text(
                    raw_txt,
                    equipment_slot=slot_name,
                    base_type=lst.base_type,
                    corrupted=lst.corrupted,
                    resolve_price_fn=_cached_price,
                )
                if top_applied and top_txt != raw_txt:
                    topoff_meta_by_id[lid] = (top_txt, top_applied, top_cost)
                    batch_inputs.append(
                        {
                            "id": f"__topoff__{lid}",
                            "label": f"{cand_label} (20% Qual + Runes)",
                            "targetSlot": slot_name,
                            "itemText": top_txt,
                        }
                    )

        raw_batch = self.bridge.call(
            "diff_items_batch",
            {"slot": build_slot, "items": batch_inputs},
        )
        results_by_id: dict[str, Mapping[str, Any]] = {}
        for raw_res in raw_batch.get("results") or []:
            if isinstance(raw_res, Mapping) and raw_res.get("ok") and raw_res.get("id"):
                results_by_id[str(raw_res["id"])] = raw_res

        scored: list[tuple[float, LiveUpgradeCandidate]] = []
        for lid, lst in listings_by_id.items():
            raw_res = results_by_id.get(lid)
            if raw_res is None:
                continue
            raw_delta_block = raw_res.get("delta") or {}
            raw_dps_delta = float(raw_delta_block.get("FullDPS") or 0.0)
            raw_ehp_delta = float(raw_delta_block.get("TotalEHP") or 0.0)

            base_price_ex = lst.price_exalted if lst.price_exalted > 0 else None
            chosen_res = raw_res
            chosen_text = lst.pob_item_text
            top_applied: list[str] = []
            top_cost = 0.0
            if lid in topoff_meta_by_id and f"__topoff__{lid}" in results_by_id:
                top_res = results_by_id[f"__topoff__{lid}"]
                top_txt, cand_applied, cand_cost = topoff_meta_by_id[lid]
                within_budget = (
                    max_price_exalted is None
                    or base_price_ex is None
                    or (base_price_ex + cand_cost) <= max_price_exalted
                )
                top_delta_block = top_res.get("delta") or {}
                top_dps_delta = float(top_delta_block.get("FullDPS") or 0.0)
                top_ehp_delta = float(top_delta_block.get("TotalEHP") or 0.0)
                adopt_topoff = within_budget and (
                    top_dps_delta > raw_dps_delta + 1e-6 or top_ehp_delta > raw_ehp_delta + 1e-6
                )
                if adopt_topoff and sort_by_value and base_price_ex is not None:
                    all_in_ex = base_price_ex + cand_cost
                    if metric == "TotalEHP":
                        adopt_topoff = (top_ehp_delta / all_in_ex) >= (
                            raw_ehp_delta / base_price_ex
                        )
                    else:
                        adopt_topoff = (top_dps_delta / all_in_ex) >= (
                            raw_dps_delta / base_price_ex
                        )
                if adopt_topoff:
                    chosen_res = top_res
                    chosen_text = top_txt
                    top_applied = cand_applied
                    top_cost = cand_cost

            effective_price_ex = (
                round(base_price_ex + top_cost, 2)
                if base_price_ex is not None and base_price_ex > 0
                else None
            )
            diff_model = _to_item_upgrade_diff(
                chosen_res,
                item_text=chosen_text,
                price_exalted=effective_price_ex,
            )
            ehp_per_ex = (
                round(diff_model.total_ehp_delta / effective_price_ex, 4)
                if effective_price_ex is not None and effective_price_ex > 0
                else None
            )
            warns = [str(w) for w in (chosen_res.get("warnings") or [])]

            if sort_by_value:
                if metric == "TotalEHP" and ehp_per_ex is not None:
                    score = ehp_per_ex
                elif diff_model.dps_per_exalted is not None:
                    score = diff_model.dps_per_exalted
                else:
                    score = diff_model.full_dps_delta
            elif metric == "TotalEHP":
                score = diff_model.total_ehp_pct
            elif metric == "balanced":
                score = diff_model.full_dps_pct + 0.6 * diff_model.total_ehp_pct
            else:
                score = diff_model.full_dps_delta

            scored.append(
                (
                    score,
                    LiveUpgradeCandidate(
                        rank=0,
                        listing=lst,
                        upgrade_diff=diff_model,
                        raw_full_dps_delta=round(raw_dps_delta, 2),
                        raw_total_ehp_delta=round(raw_ehp_delta, 2),
                        top_off_applied=top_applied,
                        top_off_cost_exalted=top_cost,
                        effective_total_cost_exalted=effective_price_ex,
                        dps_gain_per_exalted=diff_model.dps_per_exalted,
                        ehp_gain_per_exalted=ehp_per_ex,
                        warnings=warns,
                    ),
                )
            )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        ranked_candidates: list[LiveUpgradeCandidate] = []
        for rank_idx, (_, cand) in enumerate(scored, start=1):
            cand.rank = rank_idx
            ranked_candidates.append(cand)

        return LiveSlotUpgradeReport(
            league=league,
            build_slot=build_slot,
            equipment_slot=slot_name,
            metric=metric,
            max_price_exalted=max_price_exalted,
            securable_only=securable_only,
            normalize_craft_topoff=normalize_craft_topoff,
            query_id=query_id,
            trade_url=trade_url,
            total_market_matches=total_matches,
            slot_weights=slot_weights,
            candidates=ranked_candidates,
        )

    # ------------------------------------------------------------------
    # 8. Deep Calculation Breakdown, Support Sweeper, Radius Jewels & What-If
    # ------------------------------------------------------------------

    def explain_calc(
        self,
        query: str,
        *,
        slot: str = "main",
        skill_group: int | None = None,
        max_mods: int = 40,
    ) -> CalcBreakdownReport:
        """Return full PoB2 calculation breakdown & modifier provenance (`BASE`/`INC`/`MORE`/`FLAG`)
        for any stat or damage/defense mechanic on `slot`.
        """
        params: dict[str, Any] = {
            "slot": slot,
            "query": query,
            "limit": max_mods,
        }
        if skill_group is not None:
            params["skillGroup"] = int(skill_group)

        res = self.bridge.call("calc_breakdown", params)
        breakdown_by_key: dict[str, Mapping[str, Any]] = {
            str(fb.get("key")): fb
            for fb in (res.get("formulaBreakdowns") or [])
            if isinstance(fb, Mapping) and fb.get("key")
        }
        matched_map: dict[str, CalcStatMatch] = {}
        for mo in res.get("matchedOutputs") or []:
            if not isinstance(mo, Mapping) or not mo.get("key"):
                continue
            k = str(mo["key"])
            fb = breakdown_by_key.get(k, {})
            matched_map[k] = CalcStatMatch(
                key=k,
                value=mo.get("value"),
                lines=[str(ln) for ln in (fb.get("lines") or [])],
                slots=[dict(s) for s in (fb.get("slots") or []) if isinstance(s, Mapping)],
                reservations=[
                    dict(r) for r in (fb.get("reservations") or []) if isinstance(r, Mapping)
                ],
            )
        for k, fb in breakdown_by_key.items():
            if k not in matched_map:
                matched_map[k] = CalcStatMatch(
                    key=k,
                    value=fb.get("outputValue"),
                    lines=[str(ln) for ln in (fb.get("lines") or [])],
                    slots=[dict(s) for s in (fb.get("slots") or []) if isinstance(s, Mapping)],
                    reservations=[
                        dict(r) for r in (fb.get("reservations") or []) if isinstance(r, Mapping)
                    ],
                )

        src_totals: list[CalcSourceTotal] = []
        raw_src_totals = res.get("sourceTotals")
        if isinstance(raw_src_totals, Mapping):
            for s_type, bucket in sorted(raw_src_totals.items()):
                if not isinstance(bucket, Mapping):
                    continue
                src_totals.append(
                    CalcSourceTotal(
                        source_type=str(s_type),
                        base_total=round(float(bucket.get("BASE") or 0.0), 4),
                        inc_total=round(float(bucket.get("INC") or 0.0), 2),
                        more_pct=round(float(bucket.get("MORE_PCT") or 0.0), 2),
                        mod_count=int(bucket.get("count") or 0),
                    )
                )

        mods = [
            CalcModProvenanceRow(
                stat_name=str(row.get("statName") or ""),
                mod_type=str(row.get("modType") or "BASE"),
                value=row.get("value"),
                source_type=str(row.get("sourceType") or "Other"),
                source_label=str(row.get("sourceLabel") or ""),
                flags=str(row.get("flags") or ""),
            )
            for row in (res.get("modifiers") or [])
            if isinstance(row, Mapping)
        ]
        return CalcBreakdownReport(
            slot=str(res.get("slot") or slot),
            query=str(res.get("query") or query),
            skill_group=int(res.get("skillGroup") or 1),
            active_skill_name=str(res.get("activeSkillName") or "None"),
            matched_stats=list(matched_map.values()),
            modifier_count_total=int(res.get("modifierCountTotal") or len(mods)),
            source_totals=src_totals,
            modifiers=mods,
            warnings=[str(w) for w in (res.get("warnings") or [])],
        )

    def rank_support_gems(
        self,
        *,
        slot: str = "main",
        group_index: int | None = None,
        replace_gem_slot: int | None = None,
        replace_gem: str | None = None,
        best_swap: bool = False,
        dewater: bool = False,
        max_tier: int | None = None,
        include_lineage: bool = True,
        exclude_lineage_conflicts: bool = False,
        top_n: int = 15,
    ) -> SupportGemRankingReport:
        """Rank every compatible PoE2 0.5.5 Support Gem (Tier I-III + Lineage) for `group_index`,
        returning DPS/EHP deltas alongside Mana sustain, Spirit reservation, and Lineage conflicts.
        """
        params: dict[str, Any] = {
            "slot": slot,
            "includeLineage": include_lineage,
            "excludeLineageConflicts": exclude_lineage_conflicts,
            "bestSwap": best_swap,
            "rankByDps": True,
            "topN": top_n,
        }
        if group_index is not None:
            params["groupIndex"] = int(group_index)
        if replace_gem_slot is not None:
            params["gemSlot"] = int(replace_gem_slot)
        if replace_gem:
            params["replaceGem"] = str(replace_gem)
        if max_tier is not None:
            params["maxTier"] = int(max_tier)

        timeout_s = max(self.bridge.request_timeout, 120.0)
        if dewater:
            with self._scratch_slot(
                slot, prefix="supp_dewater", dewater=True, restore_slot=slot
            ) as dewater_slot:
                params["slot"] = dewater_slot
                res = self.bridge.call("list_valid_supports", params, timeout=timeout_s)
        else:
            res = self.bridge.call("list_valid_supports", params, timeout=timeout_s)

        cands: list[SupportGemCandidate] = []
        for row in res.get("supports") or []:
            if not isinstance(row, Mapping):
                continue
            tier_val = row.get("supportTier")
            sp_before = int(row.get("spiritUnreservedBefore") or 0)
            cands.append(
                SupportGemCandidate(
                    gem_id=str(row.get("gemId") or ""),
                    name=str(row.get("name") or ""),
                    gem_family=str(row["gemFamily"]) if row.get("gemFamily") else None,
                    support_tier=int(tier_val) if isinstance(tier_val, (int, float)) else None,
                    is_lineage=bool(row.get("isLineage")),
                    lineage_conflict_skill=(
                        str(row["lineageConflictSkill"])
                        if row.get("lineageConflictSkill")
                        else None
                    ),
                    replaces_gem=str(row["replacesGem"]) if row.get("replacesGem") else None,
                    already_socketed=bool(row.get("alreadySocketed")),
                    dps_before=float(row.get("dpsBefore") or 0.0),
                    dps_after=float(row.get("dpsAfter") or 0.0),
                    dps_delta=float(row.get("dpsDelta") or 0.0),
                    dps_pct=float(row.get("dpsPct") or 0.0),
                    ehp_delta=float(row.get("ehpDelta") or 0.0),
                    mana_cost_before=float(row.get("manaCostBefore") or 0.0),
                    mana_cost_after=float(row.get("manaCostAfter") or 0.0),
                    mana_sustainable_after=bool(row.get("manaSustainableAfter", True)),
                    spirit_unreserved_before=sp_before,
                    spirit_unreserved_after=int(row.get("spiritUnreservedAfter") or 0),
                    spirit_already_overflowing=bool(
                        row.get("spiritAlreadyOverflowing", sp_before < 0)
                    ),
                    spirit_overflow=bool(row.get("spiritOverflow")),
                    attributes_met_after=bool(row.get("attributesMetAfter", True)),
                )
            )
        current_supports = [
            str(g.get("name"))
            for g in (res.get("socketedGems") or [])
            if isinstance(g, Mapping) and g.get("isSupport") and g.get("name")
        ]
        supports_full = bool(res.get("supportsFull", len(current_supports) >= 5))
        hypothetical_extra = bool(
            res.get(
                "hypotheticalExtraSocket",
                supports_full
                and replace_gem_slot is None
                and not replace_gem
                and not best_swap,
            )
        )
        return SupportGemRankingReport(
            slot=slot,
            group_index=int(res.get("groupIndex") or (group_index or 1)),
            replace_gem_slot=(
                int(res["replacedSupportSlot"])
                if isinstance(res.get("replacedSupportSlot"), (int, float))
                else replace_gem_slot
            ),
            active_skill_label=str(res.get("activeSkillName") or ""),
            current_supports=current_supports,
            max_support_sockets=5,
            supports_full=supports_full,
            hypothetical_extra_socket=hypothetical_extra,
            best_swap=best_swap,
            dewatered=dewater,
            max_tier=max_tier,
            include_lineage=include_lineage,
            total_valid=int(res.get("totalValid") or res.get("count") or len(cands)),
            candidates=cands,
            warnings=[str(w) for w in (res.get("warnings") or [])],
        )

    def rank_radius_jewels(
        self,
        jewel_name: str = "Controlled Metamorphosis",
        *,
        slot: str = "main",
        stat: str = "FullDPS",
        max_path_dist: int = 10,
        top_n: int = 10,
    ) -> RadiusJewelRankingReport:
        """Rank every Passive Tree jewel socket x radius ring for a PoE2 radius jewel
        (`Controlled Metamorphosis` or `Against the Darkness`).
        """
        res = self.bridge.call(
            "rank_radius_jewel_sockets",
            {
                "slot": slot,
                "jewelName": jewel_name,
                "stat": stat,
                "maxPathDist": max_path_dist,
                "topN": top_n,
            },
            timeout=max(self.bridge.request_timeout, 120.0),
        )
        cands: list[RadiusJewelCandidate] = []
        for idx, row in enumerate(res.get("candidates") or [], start=1):
            if not isinstance(row, Mapping):
                continue
            cands.append(
                RadiusJewelCandidate(
                    rank=idx,
                    jewel_name=str(row.get("jewelName") or jewel_name),
                    socket_node_id=int(row.get("socketNodeId") or 0),
                    socket_name=str(row.get("socketName") or "Jewel Socket"),
                    socket_allocated=bool(row.get("socketAllocated")),
                    socket_path_dist=int(row.get("socketPathDist") or 0),
                    ring_index=int(row.get("ringIndex") or 1),
                    ring_label=str(row.get("ringLabel") or ""),
                    allocated_notables_in_radius=int(row.get("allocatedNotablesInRadius") or 0),
                    allocated_smalls_in_radius=int(row.get("allocatedSmallsInRadius") or 0),
                    ring_notables=[str(n) for n in (row.get("ringNotables") or [])],
                    nearby_unallocated_notables=[
                        str(n) for n in (row.get("nearbyUnallocatedNotables") or [])
                    ],
                    total_points_spent=int(row.get("totalPointsSpent") or 1),
                    normal_points_saved=int(row.get("normalPointsSaved") or 0),
                    stat=str(row.get("stat") or stat),
                    before=float(row.get("before") or 0.0),
                    after=float(row.get("after") or 0.0),
                    delta=float(row.get("delta") or 0.0),
                    pct=float(row.get("pct") or 0.0),
                    per_point_delta=float(row.get("perPointDelta") or 0.0),
                    per_point_pct=float(row.get("perPointPct") or 0.0),
                )
            )
        return RadiusJewelRankingReport(
            slot=str(res.get("slot") or slot),
            jewel_name=str(res.get("jewelName") or jewel_name),
            stat=str(res.get("stat") or stat),
            base_value=float(res.get("baseValue") or 0.0),
            candidates=cands,
        )

    def what_if(
        self,
        *,
        slot: str = "main",
        equip_items: Mapping[str, str] | None = None,
        edit_slot: str | None = None,
        add_mod_lines: Sequence[str] | None = None,
        remove_mod_substrings: Sequence[str] | None = None,
        set_quality: int | None = None,
        alloc_node_ids: Sequence[int] | None = None,
        dealloc_node_ids: Sequence[int] | None = None,
        set_config: Mapping[str, Any] | None = None,
        borrow_from_slot: str | None = None,
        borrow_slots: Sequence[str] | None = None,
        borrow_main_skill: bool = False,
        include_scenario_matrix: bool = False,
        apply: bool = False,
    ) -> WhatIfReport:
        """Run an atomic, rollback-safe multi-mutation PoB2 sandbox on `slot`:
        - In-place item mod surgery (`edit_slot` + `add_mod_lines` / `remove_mod_substrings`)
        - Direct multi-slot item swaps (`equip_items`)
        - Piece-by-piece marginal attribution + grafting from `borrow_from_slot`
        - Passive tree node swaps (`alloc_node_ids` / `dealloc_node_ids`) & `set_config`
        - Optional 3-preset scenario matrix (`as_configured`, `sustainable_pinnacle`, `mapping`)
        Never mutates `slot` unless `apply=True`.
        """
        with self._scratch_slot(slot, prefix="whatif") as work_slot:
            baseline_stats = self.get_stats(slot=work_slot)
            base_dps = baseline_stats.effective_dps
            base_ehp = baseline_stats.total_ehp

            edited_item_text: str | None = None
            attributions: list[WhatIfPieceAttribution] = []
            warnings: list[str] = []

            def _record_piece_probe(
                label: str,
                kind: Literal["equipment_slot", "main_skill_group", "edited_item", "passive_tree"],
                mutate_fn: Any,
            ) -> None:
                with self._scratch_slot(slot, prefix="whatif_probe") as probe_slot:
                    mutate_fn(probe_slot)
                    p_stats = self.get_stats(slot=probe_slot)
                    d_dps = p_stats.effective_dps - base_dps
                    p_dps = (d_dps / base_dps * 100.0) if base_dps > 0 else 0.0
                    d_ehp = p_stats.total_ehp - base_ehp
                    p_ehp = (d_ehp / base_ehp * 100.0) if base_ehp > 0 else 0.0
                    attributions.append(
                        WhatIfPieceAttribution(
                            label=label,
                            kind=kind,
                            effective_dps_delta=round(d_dps, 2),
                            effective_dps_pct=round(p_dps, 2),
                            total_ehp_delta=round(d_ehp, 2),
                            total_ehp_pct=round(p_ehp, 2),
                            spirit_unreserved_after=p_stats.spirit_unreserved,
                            mana_sustainable_after=p_stats.mana_sustainable,
                        )
                    )

            # 1. Borrow equipment slots and/or main skill from a reference slot (`ref`)
            if borrow_from_slot:
                ref_summary = self.bridge.call(
                    "get_build_summary",
                    {"slot": borrow_from_slot, "includeItemRaw": True},
                )
                ref_items_by_slot: dict[str, str] = {}
                for it in ref_summary.get("items") or []:
                    if isinstance(it, Mapping) and it.get("slot") and it.get("raw"):
                        s_nm = str(it["slot"])
                        if not s_nm.startswith("Flask") and not s_nm.startswith("Charm"):
                            ref_items_by_slot[s_nm] = str(it["raw"])

                target_borrow_slots = (
                    [str(s) for s in borrow_slots]
                    if borrow_slots is not None
                    else ([] if borrow_main_skill else list(ref_items_by_slot.keys()))
                )
                for b_slot in target_borrow_slots:
                    raw_it = ref_items_by_slot.get(b_slot)
                    if not raw_it:
                        warnings.append(
                            f"Reference slot {borrow_from_slot!r} has no item in {b_slot!r}."
                        )
                        continue
                    _record_piece_probe(
                        f"{borrow_from_slot}:{b_slot}",
                        "equipment_slot",
                        lambda target_s, s=b_slot, r=raw_it: self.bridge.call(
                            "equip_item",
                            {"slot": target_s, "targetSlot": s, "itemText": r},
                        ),
                    )
                    self.bridge.call(
                        "equip_item",
                        {"slot": work_slot, "targetSlot": b_slot, "itemText": raw_it},
                    )

                if borrow_main_skill:
                    _record_piece_probe(
                        f"{borrow_from_slot}:Main Skill Links",
                        "main_skill_group",
                        lambda target_s: self.bridge.call(
                            "copy_main_skill_from_slot",
                            {"source": borrow_from_slot, "target": target_s},
                        ),
                    )
                    self.bridge.call(
                        "copy_main_skill_from_slot",
                        {"source": borrow_from_slot, "target": work_slot},
                    )

            # 2. In-place mod surgery on a currently equipped item
            if edit_slot:
                cur_summary = self.bridge.call(
                    "get_build_summary",
                    {"slot": work_slot, "includeItemRaw": True},
                )
                cur_raw = ""
                for it in cur_summary.get("items") or []:
                    if isinstance(it, Mapping) and str(it.get("slot") or "") == edit_slot:
                        cur_raw = str(it.get("raw") or "")
                        break
                if not cur_raw:
                    raise Pob2BridgeError(
                        f"Slot {slot!r} has no equipped item in {edit_slot!r} to edit"
                    )
                edited_item_text = _edit_pob_item_text(
                    cur_raw,
                    add_mod_lines=add_mod_lines,
                    remove_mod_substrings=remove_mod_substrings,
                    set_quality=set_quality,
                )
                if borrow_from_slot or equip_items or alloc_node_ids or dealloc_node_ids:
                    _record_piece_probe(
                        f"edit:{edit_slot}",
                        "edited_item",
                        lambda target_s, s=edit_slot, r=edited_item_text: self.bridge.call(
                            "equip_item",
                            {"slot": target_s, "targetSlot": s, "itemText": r},
                        ),
                    )
                self.bridge.call(
                    "equip_item",
                    {"slot": work_slot, "targetSlot": edit_slot, "itemText": edited_item_text},
                )

            # 3. Explicit equipment swaps
            for eq_slot, item_txt in (equip_items or {}).items():
                if len(equip_items or {}) > 1 or borrow_from_slot or edit_slot:
                    _record_piece_probe(
                        f"equip:{eq_slot}",
                        "equipment_slot",
                        lambda target_s, s=eq_slot, r=item_txt: self.bridge.call(
                            "equip_item",
                            {"slot": target_s, "targetSlot": s, "itemText": r},
                        ),
                    )
                self.bridge.call(
                    "equip_item",
                    {"slot": work_slot, "targetSlot": eq_slot, "itemText": item_txt},
                )

            # 4. Passive tree node allocations / removals
            if dealloc_node_ids or alloc_node_ids:
                if dealloc_node_ids:
                    self.bridge.call(
                        "dealloc_nodes",
                        {
                            "slot": work_slot,
                            "nodeIds": [int(n) for n in dealloc_node_ids],
                            "includeDependents": False,
                        },
                    )
                if alloc_node_ids:
                    self.bridge.call(
                        "alloc_nodes",
                        {
                            "slot": work_slot,
                            "nodeIds": [int(n) for n in alloc_node_ids],
                            "includePath": True,
                        },
                    )

            # 5. Config overrides
            if set_config:
                self.bridge.call(
                    "set_config",
                    {"slot": work_slot, "values": dict(set_config)},
                )

            after_stats = self.get_stats(slot=work_slot)
            if not after_stats.attributes_met:
                warnings.append(
                    f"Unmet Attribute requirements after mutation "
                    f"(Str {after_stats.str_total}/{after_stats.req_str}, "
                    f"Dex {after_stats.dex_total}/{after_stats.req_dex}, "
                    f"Int {after_stats.int_total}/{after_stats.req_int})."
                )
            if after_stats.spirit_unreserved < 0:
                warnings.append(
                    f"Spirit overflow after mutation ({after_stats.spirit_reserved}/"
                    f"{after_stats.spirit_total} reserved)."
                )
            if not after_stats.mana_payable:
                warnings.append(
                    f"Main skill Mana cost ({after_stats.mana_cost:.0f}) exceeds "
                    f"unreserved Mana ({after_stats.mana_unreserved:.0f})."
                )

            attributions.sort(key=lambda a: a.effective_dps_pct, reverse=True)

            scenario_matrix: list[WhatIfScenarioPoint] = []
            if include_scenario_matrix:
                # Preset 1: As Configured
                as_cfg_d = after_stats.effective_dps - base_dps
                as_cfg_p = (as_cfg_d / base_dps * 100.0) if base_dps > 0 else 0.0
                scenario_matrix.append(
                    WhatIfScenarioPoint(
                        scenario="as_configured",
                        effective_dps=round(after_stats.effective_dps, 2),
                        effective_dps_delta_vs_baseline=round(as_cfg_d, 2),
                        effective_dps_pct_vs_baseline=round(as_cfg_p, 2),
                        total_ehp=round(after_stats.total_ehp, 2),
                        total_ehp_delta_vs_baseline=round(after_stats.total_ehp - base_ehp, 2),
                        mana_sustainable=after_stats.mana_sustainable,
                        spirit_unreserved=after_stats.spirit_unreserved,
                    )
                )
                # Preset 2: Sustainable Pinnacle Boss (dewatered)
                base_dewater = self.audit_build(slot=slot, apply_dewater=False)
                after_dewater = self.audit_build(slot=work_slot, apply_dewater=False)
                b_sust = base_dewater.sustainable_pinnacle_stats
                a_sust = after_dewater.sustainable_pinnacle_stats
                sust_d = a_sust.effective_dps - b_sust.effective_dps
                sust_p = (
                    (sust_d / b_sust.effective_dps * 100.0) if b_sust.effective_dps > 0 else 0.0
                )
                scenario_matrix.append(
                    WhatIfScenarioPoint(
                        scenario="sustainable_pinnacle",
                        effective_dps=round(a_sust.effective_dps, 2),
                        effective_dps_delta_vs_baseline=round(sust_d, 2),
                        effective_dps_pct_vs_baseline=round(sust_p, 2),
                        total_ehp=round(a_sust.total_ehp, 2),
                        total_ehp_delta_vs_baseline=round(a_sust.total_ehp - b_sust.total_ehp, 2),
                        mana_sustainable=a_sust.mana_sustainable,
                        spirit_unreserved=a_sust.spirit_unreserved,
                    )
                )
                # Preset 3: Mapping Clear (non-boss enemy + on-kill active)
                map_cfg = {
                    "enemyIsBoss": "None",
                    "conditionKilledRecently": True,
                    "conditionCritRecently": True,
                }
                with self._scratch_slot(slot, prefix="whatif_map") as probe_slot:
                    self.bridge.call("set_config", {"slot": probe_slot, "values": map_cfg})
                    b_map = self.get_stats(slot=probe_slot)

                with self._scratch_slot(work_slot, prefix="whatif_map") as probe_slot:
                    self.bridge.call("set_config", {"slot": probe_slot, "values": map_cfg})
                    a_map = self.get_stats(slot=probe_slot)

                map_d = a_map.effective_dps - b_map.effective_dps
                map_p = (map_d / b_map.effective_dps * 100.0) if b_map.effective_dps > 0 else 0.0
                scenario_matrix.append(
                    WhatIfScenarioPoint(
                        scenario="mapping_clear",
                        effective_dps=round(a_map.effective_dps, 2),
                        effective_dps_delta_vs_baseline=round(map_d, 2),
                        effective_dps_pct_vs_baseline=round(map_p, 2),
                        total_ehp=round(a_map.total_ehp, 2),
                        total_ehp_delta_vs_baseline=round(a_map.total_ehp - b_map.total_ehp, 2),
                        mana_sustainable=a_map.mana_sustainable,
                        spirit_unreserved=a_map.spirit_unreserved,
                    )
                )

            if apply:
                self.clone_slot(work_slot, slot, activate=True)
            else:
                self.bridge.call("switch_slot", {"slot": slot, "createIfMissing": False})

            dps_delta = after_stats.effective_dps - base_dps
            dps_pct = (dps_delta / base_dps * 100.0) if base_dps > 0 else 0.0
            ehp_delta = after_stats.total_ehp - base_ehp
            ehp_pct = (ehp_delta / base_ehp * 100.0) if base_ehp > 0 else 0.0

            return WhatIfReport(
                slot=slot,
                applied=apply,
                rolled_back=not apply,
                baseline_stats=baseline_stats,
                after_stats=after_stats,
                effective_dps_delta=round(dps_delta, 2),
                effective_dps_pct=round(dps_pct, 2),
                total_ehp_delta=round(ehp_delta, 2),
                total_ehp_pct=round(ehp_pct, 2),
                edited_equipment_slot=edit_slot,
                edited_item_text=edited_item_text,
                borrowed_from_slot=borrow_from_slot,
                piece_attributions=attributions,
                scenario_matrix=scenario_matrix,
                warnings=warnings,
            )

    # ------------------------------------------------------------------
    # 12. Megalomaniac Build-Specific Notable & Trade2 Finder
    # ------------------------------------------------------------------

    def find_megalomaniac_upgrades(
        self,
        slot: str = "main",
        *,
        stat: str = "FullDPS",
        top_notables: int = 20,
        top_combos: int = 10,
        min_match_count: int = 2,
        max_exalted: float | None = None,
        securable_only: bool = True,
        league: str = DEFAULT_LEAGUE,
    ) -> MegalomaniacSearchReport:
        """Rank unallocated Notables for a Megalomaniac Jewel and compile a Trade2 search."""
        from poe2brain.trade.ee2_bundle import get_default_bundle

        require_international("Megalomaniac trade search")
        ranked = self.rank_passive_upgrades(
            slot=slot,
            mode="anoint",
            stat=stat,
            top_n=max(top_notables, 25),
        )
        positive = [n for n in ranked if n.single_node_delta > 0]
        picked = positive[:top_notables]

        # Resolve `Allocates <Notable>` official Trade2 explicit stat IDs
        bundle = get_default_bundle()
        explicit_map = bundle.official_stats_by_domain.get("explicit") or {}
        enchant_map = bundle.official_stats_by_domain.get("enchant") or {}

        stat_filters: list[dict[str, Any]] = []
        top_notable_rows: list[dict[str, Any]] = []
        for n in picked:
            alloc_text = f"Allocates {n.name}"
            stat_ids = explicit_map.get(alloc_text) or enchant_map.get(alloc_text) or []
            stat_id = stat_ids[0] if stat_ids else None
            if stat_id:
                stat_filters.append({"id": stat_id})
            top_notable_rows.append(
                {
                    "name": n.name,
                    "node_id": n.node_id,
                    "single_node_delta": round(n.single_node_delta, 2),
                    "single_node_pct": round(n.single_node_pct, 2),
                    "stats": n.stats[:3],
                    "trade2_stat_id": stat_id,
                }
            )

        pool_for_combos = picked[: min(12, len(picked))]
        combos_2: list[MegalomaniacCombo] = []
        for i in range(len(pool_for_combos)):
            for j in range(i + 1, len(pool_for_combos)):
                a, b = pool_for_combos[i], pool_for_combos[j]
                combos_2.append(
                    MegalomaniacCombo(
                        notables=[a.name, b.name],
                        combined_delta=round(a.single_node_delta + b.single_node_delta, 2),
                        combined_pct=round(a.single_node_pct + b.single_node_pct, 2),
                        note="Additive estimate of two independent unallocated Notables.",
                    )
                )
        combos_2.sort(key=lambda c: c.combined_delta, reverse=True)

        combos_3: list[MegalomaniacCombo] = []
        top_triple = picked[: min(8, len(picked))]
        for i in range(len(top_triple)):
            for j in range(i + 1, len(top_triple)):
                for k in range(j + 1, len(top_triple)):
                    a, b, c = top_triple[i], top_triple[j], top_triple[k]
                    combos_3.append(
                        MegalomaniacCombo(
                            notables=[a.name, b.name, c.name],
                            combined_delta=round(
                                a.single_node_delta + b.single_node_delta + c.single_node_delta, 2
                            ),
                            combined_pct=round(
                                a.single_node_pct + b.single_node_pct + c.single_node_pct, 2
                            ),
                            note="3-notable chase Megalomaniac combination.",
                        )
                    )
        combos_3.sort(key=lambda c: c.combined_delta, reverse=True)

        trade_filters: dict[str, Any] = {}
        if max_exalted is not None and max_exalted > 0:
            trade_filters["trade_filters"] = {
                "filters": {
                    "price": {"option": "exalted", "max": int(max_exalted)},
                }
            }
        # Unauth GGG trade2 allows up to 35 filters cleanly in a single `count` group
        capped_filters = stat_filters[:30]
        trade2_query: dict[str, Any] = {
            "query": {
                "status": {"option": "securable" if securable_only else "available"},
                "name": "Megalomaniac",
                "type": "Diamond",
                "stats": (
                    [
                        {
                            "type": "count",
                            "value": {"min": max(1, min(min_match_count, len(capped_filters)))},
                            "filters": capped_filters,
                        }
                    ]
                    if capped_filters
                    else []
                ),
                **({"filters": trade_filters} if trade_filters else {}),
            },
            "sort": {"price": "asc"},
        }
        enc_league = url_quote(league, safe="")
        q_encoded = url_quote(json.dumps(trade2_query, separators=(",", ":")), safe="")
        trade_url = f"{GGG_TRADE2_WEB_BASE_URL}/{enc_league}?q={q_encoded}"

        return MegalomaniacSearchReport(
            slot_name=slot,
            stat=stat,
            league=league,
            top_notables=top_notable_rows,
            top_2_notable_combos=combos_2[:top_combos],
            top_3_notable_combos=combos_3[: min(top_combos, 6)],
            matched_trade2_stat_count=len(capped_filters),
            trade2_query=trade2_query,
            trade_url=trade_url,
            usage_note=(
                f"Open trade_url to list live Megalomaniac Diamonds matching at least "
                f"{min_match_count} of your build's top {len(capped_filters)} unallocated "
                f"{stat} Notables; paste promising hits into `poe2 pob eval-item --equip-slot "
                '"Jewel 1"` to verify exact synergistic DPS/EHP.'
            ),
        )

    # ------------------------------------------------------------------
    # 13. Rune & Soul Core Socket "Resist Tetris" Optimizer
    # ------------------------------------------------------------------

    def optimize_rune_sockets(
        self,
        slot: str = "main",
        *,
        objective: Literal["balanced", "dps", "ehp", "spirit", "rarity"] = "balanced",
        max_budget_exalted: float | None = None,
        league: str = DEFAULT_LEAGUE,
        offline_prices: bool = False,
    ) -> RuneOptimizationReport:
        """Solve Resist Tetris & optimal Armour/Weapon Rune/Soul Core socket allocation in PoB2."""
        from poe2brain.knowledge.service import KnowledgeService

        stats = self.get_stats(slot=slot)
        summary = self.bridge.call("get_build_summary", {"slot": slot, "includeItemRaw": True})
        items = summary.get("items") or []

        armour_slots = {"Body Armour", "Helmet", "Gloves", "Boots"}
        weapon_slots = {"Weapon 1", "Weapon 2"}

        # Look up verified slot effects & socket-bound status directly from SQLite 0.5.5 augments
        ks = KnowledgeService()
        _aug_cache: dict[str, tuple[dict[str, Any], bool]] = {}

        def _lookup_aug(aug_name: str) -> tuple[dict[str, Any], bool]:
            if aug_name not in _aug_cache:
                eff_map: dict[str, Any] = {}
                overw = True
                try:
                    for h in ks.inspect_augment(aug_name, limit=4):
                        if h.name.lower() == aug_name.lower():
                            eff_map = dict(h.slot_effects or {})
                            overw = h.can_overwrite_in_socket and (not h.is_socket_bound)
                            break
                except Exception:
                    pass
                _aug_cache[aug_name] = (eff_map, overw)
            return _aug_cache[aug_name]

        # Catalog available overwriteable sockets (skip locked Socket-bound augments!)
        slot_entries: list[tuple[str, str, str, int]] = []
        raw_by_slot: dict[str, str] = {}
        locked_by_slot: dict[str, list[str]] = {}
        locked_notes: list[str] = []
        armour_socks = 0
        weapon_socks = 0
        for it in items:
            if not isinstance(it, Mapping):
                continue
            s_name = str(it.get("slot") or "")
            raw_s_cnt = int(it.get("runeSockets") or 0)
            if raw_s_cnt <= 0:
                continue
            item_type = str(it.get("type") or "").lower()
            item_title = str(it.get("name") or it.get("baseName") or s_name)
            equipped_runes = [str(r) for r in (it.get("runes") or []) if r]
            locked_names = [r for r in equipped_runes if not _lookup_aug(r)[1]]
            s_cnt = max(0, raw_s_cnt - len(locked_names))
            if locked_names:
                locked_by_slot[s_name] = locked_names
                locked_notes.append(
                    f"Locked socket-bound augment(s) preserved on {item_title} ({s_name}): "
                    f"{', '.join(locked_names)} ({len(locked_names)}/{raw_s_cnt} socket(s) "
                    "cannot be overwritten)."
                )
            if s_cnt <= 0:
                continue
            if it.get("raw"):
                raw_by_slot[s_name] = str(it["raw"])
            if s_name in armour_slots:
                cat: Literal["armour", "weapon", "shield", "focus", "other"] = "armour"
                armour_socks += s_cnt
            elif s_name in weapon_slots:
                if "shield" in item_type:
                    cat = "shield"
                    armour_socks += s_cnt
                elif "focus" in item_type:
                    cat = "focus"
                    armour_socks += s_cnt
                else:
                    cat = "weapon"
                    weapon_socks += s_cnt
            else:
                continue
            slot_entries.append((s_name, item_title, cat, s_cnt))

        # Default fallback socket budget if PoB2 item metadata had 0 explicit sockets listed
        if not slot_entries and not locked_notes:
            slot_entries = [
                ("Body Armour", "Body Armour (2 sockets)", "armour", 2),
                ("Helmet", "Helmet (1 socket)", "armour", 1),
                ("Gloves", "Gloves (1 socket)", "armour", 1),
                ("Boots", "Boots (1 socket)", "armour", 1),
                ("Weapon 1", "Weapon 1 (2 sockets)", "weapon", 2),
            ]
            armour_socks = 5
            weapon_socks = 2

        res_deficits = {
            "fire": round(max(0.0, 75.0 - stats.fire_resist), 1),
            "cold": round(max(0.0, 75.0 - stats.cold_resist), 1),
            "lightning": round(max(0.0, 75.0 - stats.lightning_resist), 1),
            "chaos": (0.0 if stats.chaos_immune else round(max(0.0, 60.0 - stats.chaos_resist), 1)),
        }

        cn = get_game_realm() == GameRealm.CN
        if cn and max_budget_exalted is not None:
            require_international("Rune budget pricing")
        offline_prices = offline_prices or cn
        price_cache: dict[str, float | None] = {}
        econ = None if offline_prices else self._economy_service
        if not offline_prices and econ is None:
            try:
                from poe2brain.trade.economy import EconomyService

                self._economy_service = EconomyService(client=self.http_client)
                econ = self._economy_service
            except Exception:
                econ = None

        def _quote_ex(aug_name: str) -> float | None:
            if aug_name in price_cache:
                return price_cache[aug_name]
            val: float | None = None
            if econ is not None:
                try:
                    q = econ.get_price(aug_name, league=league)
                    if q is not None and q.exalted > 0:
                        val = round(float(q.exalted), 2)
                except Exception:
                    val = None
            price_cache[aug_name] = val
            return val

        def _aug_slot_mod(
            aug_name: str, slot_keys: tuple[str, ...], fallback: str
        ) -> tuple[str, bool]:
            eff_map, overw = _lookup_aug(aug_name)
            for sk in slot_keys:
                vals = eff_map.get(sk)
                if vals:
                    line = vals[0] if isinstance(vals, list) else str(vals)
                    return str(line), overw
            return fallback, overw

        # Allocate Armour sockets: 1) cap Fire/Cold/Lightning -> 2) Chaos -> 3) objective
        remaining_deficits = dict(res_deficits)
        recs: list[RuneSocketRecommendation] = []
        custom_mod_lines: list[str] = []
        running_cost = 0.0
        notes: list[str] = list(locked_notes)

        # SQLite-verified Patch 0.5.5 Armour resistance runes (+18% single ele, +13% chaos)
        ele_rune_specs = [
            ("fire", "Greater Desert Rune", 18.0, "+18% to Fire Resistance"),
            ("cold", "Greater Glacial Rune", 18.0, "+18% to Cold Resistance"),
            ("lightning", "Greater Storm Rune", 18.0, "+18% to Lightning Resistance"),
        ]
        is_es_heavy = stats.energy_shield > stats.life * 1.1
        is_caster = any(
            k in " ".join(stats.active_skills).lower()
            for k in ("spark", "comet", "arc", "ball", "orb", "nova", "curse", "totem", "minion")
        )

        def _can_afford(unit_ex: float | None) -> bool:
            if max_budget_exalted is None or unit_ex is None:
                return True
            return (running_cost + unit_ex) <= max_budget_exalted

        for s_name, item_title, cat, s_cnt in slot_entries:
            free_socks = s_cnt
            slot_allocs: dict[tuple[str, str, str], int] = {}

            if cat in ("armour", "shield", "focus"):
                # Priority 1: Cap Elemental Resistances to 75% (+18% Greater Ele Runes)
                for res_key, r_name, gain_pct, fallback_line in ele_rune_specs:
                    while free_socks > 0 and remaining_deficits[res_key] > 0:
                        u_cost = _quote_ex(r_name)
                        if not _can_afford(u_cost):
                            break
                        mod_line, _ = _aug_slot_mod(r_name, ("Armour",), fallback_line)
                        slot_allocs[(r_name, mod_line, f"Cap {res_key.title()} Resistance")] = (
                            slot_allocs.get(
                                (r_name, mod_line, f"Cap {res_key.title()} Resistance"), 0
                            )
                            + 1
                        )
                        remaining_deficits[res_key] = max(
                            0.0, remaining_deficits[res_key] - gain_pct
                        )
                        custom_mod_lines.append(mod_line)
                        if u_cost is not None:
                            running_cost += u_cost
                        free_socks -= 1

                # Priority 2: Chaos Resistance (+13% Soul Core of Tacati, unless CI)
                while free_socks > 0 and remaining_deficits["chaos"] >= 10.0:
                    r_name = "Soul Core of Tacati"
                    u_cost = _quote_ex(r_name)
                    if not _can_afford(u_cost):
                        break
                    mod_line, _ = _aug_slot_mod(r_name, ("Armour",), "+13% to Chaos Resistance")
                    slot_allocs[(r_name, mod_line, "Mitigate Chaos 2x ES / Poison hits")] = (
                        slot_allocs.get((r_name, mod_line, "Mitigate Chaos 2x ES / Poison hits"), 0)
                        + 1
                    )
                    remaining_deficits["chaos"] = max(0.0, remaining_deficits["chaos"] - 13.0)
                    custom_mod_lines.append(mod_line)
                    if u_cost is not None:
                        running_cost += u_cost
                    free_socks -= 1

                # Priority 3: Fill remaining Armour sockets by objective (0.5.5 slot effects)
                while free_socks > 0:
                    if objective == "rarity" and s_name == "Gloves":
                        r_name = "Soul Core of Azcapa"
                        mod_line, _ = _aug_slot_mod(
                            r_name,
                            ("Gloves",),
                            "10% increased Quantity of Gold Dropped by Slain Enemies",
                        )
                        purpose = "Scale Gloves Gold / farm utility"
                    elif objective == "dps" and s_name == "Helmet" and is_caster:
                        r_name = "Soul Core of Zalatl"
                        mod_line, _ = _aug_slot_mod(
                            r_name, ("Helmet",), "5% increased maximum Mana"
                        )
                        purpose = "Scale Caster / Archmage Max Mana"
                    elif is_es_heavy:
                        r_name = "Greater Iron Rune"
                        mod_line, _ = _aug_slot_mod(
                            r_name, ("Armour",), "18% increased Armour, Evasion and Energy Shield"
                        )
                        purpose = "Scale global Energy Shield / Defences"
                    else:
                        r_name = "Greater Body Rune"
                        mod_line, _ = _aug_slot_mod(r_name, ("Armour",), "+60 to maximum Life")
                        purpose = "Scale Maximum Life EHP"
                    u_cost = _quote_ex(r_name)
                    if not _can_afford(u_cost):
                        r_name = "Greater Body Rune" if not is_es_heavy else "Greater Iron Rune"
                        mod_line, _ = _aug_slot_mod(
                            r_name,
                            ("Armour",),
                            "+60 to maximum Life"
                            if not is_es_heavy
                            else "18% increased Armour, Evasion and Energy Shield",
                        )
                        u_cost = _quote_ex(r_name)
                    slot_allocs[(r_name, mod_line, purpose)] = (
                        slot_allocs.get((r_name, mod_line, purpose), 0) + 1
                    )
                    custom_mod_lines.append(mod_line)
                    if u_cost is not None:
                        running_cost += u_cost
                    free_socks -= 1

            elif cat == "weapon":
                is_wand_or_staff = (
                    any(k in item_title.lower() for k in ("wand", "staff", "sceptre")) or is_caster
                )
                while free_socks > 0:
                    if (
                        objective == "spirit" or stats.spirit_unreserved < 0
                    ) and not is_wand_or_staff:
                        r_name = "Soul Core of Azcapa"
                        mod_line, _ = _aug_slot_mod(r_name, ("Martial Weapon",), "+15 to Spirit")
                        purpose = "Add Martial Weapon Spirit headroom"
                    elif is_wand_or_staff:
                        r_name = "Greater Iron Rune"
                        mod_line, _ = _aug_slot_mod(
                            r_name, ("Wand or Staff",), "30% increased Spell Damage"
                        )
                        purpose = "Scale Wand/Staff Spell Damage"
                    else:
                        r_name = "Greater Iron Rune"
                        mod_line, _ = _aug_slot_mod(
                            r_name, ("Martial Weapon",), "18% increased Physical Damage"
                        )
                        purpose = "Scale Martial Weapon Physical DPS"
                    u_cost = _quote_ex(r_name)
                    slot_allocs[(r_name, mod_line, purpose)] = (
                        slot_allocs.get((r_name, mod_line, purpose), 0) + 1
                    )
                    custom_mod_lines.append(mod_line)
                    if u_cost is not None:
                        running_cost += u_cost
                    free_socks -= 1

            for (r_name, mod_line, purpose), cnt in slot_allocs.items():
                u_cost = _quote_ex(r_name)
                _, overwritable = _aug_slot_mod(
                    r_name, ("Armour", "Martial Weapon", "Wand or Staff"), mod_line
                )
                recs.append(
                    RuneSocketRecommendation(
                        equipment_slot=s_name,
                        item_name=item_title,
                        socket_category=cat,
                        sockets_allocated=cnt,
                        augment_name=r_name,
                        effect_summary=mod_line,
                        purpose=purpose,
                        unit_cost_exalted=u_cost,
                        total_cost_exalted=round(u_cost * cnt, 2) if u_cost is not None else None,
                        can_overwrite_in_socket=overwritable,
                    )
                )

        # Verify exact NET DPS/EHP/Resist outcome inside PoB2 by replacing overwriteable
        # Rune lines on equipped gear (preserving locked Socket-bound runes).
        dps_delta = 0.0
        ehp_delta = 0.0
        res_after = {
            "fire": round(stats.fire_resist, 1),
            "cold": round(stats.cold_resist, 1),
            "lightning": round(stats.lightning_resist, 1),
            "chaos": round(stats.chaos_resist, 1),
        }
        new_runes_by_slot: dict[str, list[str]] = {}
        for rec in recs:
            new_runes_by_slot.setdefault(rec.equipment_slot, []).extend(
                [rec.augment_name] * rec.sockets_allocated
            )
        modified_equip = {
            s_nm: _rewrite_item_runes(raw_by_slot[s_nm], locked_by_slot.get(s_nm, []), r_names)
            for s_nm, r_names in new_runes_by_slot.items()
            if s_nm in raw_by_slot
        }
        fallback_cfg = (
            {
                "customMods": "\n".join(
                    "18% increased Global Defences"
                    if "increased Armour, Evasion and Energy Shield" in ln
                    else ln
                    for ln in custom_mod_lines
                )
            }
            if not modified_equip and custom_mod_lines
            else None
        )
        if modified_equip or fallback_cfg:
            try:
                with self._scratch_slot(slot, prefix="runes", restore_slot=slot) as rune_slot:
                    for equipment_slot, item_text in modified_equip.items():
                        self.bridge.call(
                            "equip_item",
                            {
                                "slot": rune_slot,
                                "targetSlot": equipment_slot,
                                "itemText": item_text,
                            },
                        )
                    if fallback_cfg:
                        self.bridge.call("set_config", {"slot": rune_slot, "values": fallback_cfg})
                    after = self.get_stats(slot=rune_slot)
                dps_delta = after.effective_dps - stats.effective_dps
                ehp_delta = after.total_ehp - stats.total_ehp
                res_after = {
                    "fire": round(after.fire_resist, 1),
                    "cold": round(after.cold_resist, 1),
                    "lightning": round(after.lightning_resist, 1),
                    "chaos": round(after.chaos_resist, 1),
                }
            except Exception as exc:
                notes.append(f"PoB2 delta verification skipped: {exc}")

        if any(v > 0 for v in remaining_deficits.values()):
            uncapped = ", ".join(
                f"{k.title()} (-{v:.0f}%)" for k, v in remaining_deficits.items() if v > 0
            )
            notes.append(
                f"Remaining resist gaps after socket cap ({uncapped}): add 1 Ring/Boots resist "
                "roll or Artificer's Orb sockets on un-socketed gear."
            )
        notes.append(
            "In PoE2 0.5.5, standard Runes & Soul Cores can be freely overwritten in sockets; "
            "Masterwork / Bonded runes require Shaman Bonded or Verisium Anvil planning."
        )

        priced_recs = [r for r in recs if r.total_cost_exalted is not None]
        est_total_cost: float | None = (
            round(running_cost, 2) if (recs and len(priced_recs) == len(recs)) else None
        )
        if recs and est_total_cost is None:
            notes.append(
                "Rune cost estimate unavailable (offline mode or unquoted augments; omitted rather "
                "than defaulting missing prices to 0.0)."
            )

        if cn:
            notes.append("国服符文价格未接入；推荐基于本地 PoB2 配装。")
        return RuneOptimizationReport(
            slot_name=slot,
            objective=objective,
            league=None if cn else league,
            total_available_sockets=armour_socks + weapon_socks,
            armour_sockets=armour_socks,
            weapon_sockets=weapon_socks,
            resist_deficits_before=res_deficits,
            recommendations=recs,
            estimated_total_cost_exalted=est_total_cost,
            pob_verified_dps_delta=round(dps_delta, 2),
            pob_verified_ehp_delta=round(ehp_delta, 2),
            pob_verified_resists_after=res_after,
            notes=notes,
        )


_DEFAULT_SERVICE: BuildService | None = None


def get_default_build_service() -> BuildService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = BuildService()
    return _DEFAULT_SERVICE


def _close_default_build_service() -> None:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is not None:
        try:
            _DEFAULT_SERVICE.close()
        except Exception:
            pass
        _DEFAULT_SERVICE = None
    close_default_pob2_bridge()


atexit.register(_close_default_build_service)
