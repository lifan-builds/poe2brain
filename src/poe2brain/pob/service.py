from __future__ import annotations

import atexit
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote

from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.models.builds import (
    BuildSlotComparison,
    BuildStats,
    DefensivePassiveRespecOption,
    DewateredAudit,
    EquipmentSlotDiff,
    GoldRespecEstimate,
    HcRipAuditReport,
    HcRipRisk,
    ItemUpgradeDiff,
    SkillGroupDiff,
    SlotStatWeights,
    StatWeightEntry,
    StrippedPobFlag,
)
from poe2brain.models.common import Poe2Model, SaleType
from poe2brain.models.trade import TradeListing
from poe2brain.pob.bridge import Pob2Bridge, Pob2BridgeError

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
    dps_gain_per_exalted: float | None = None
    ehp_gain_per_exalted: float | None = None
    warnings: list[str] = Field(default_factory=list)


class LiveSlotUpgradeReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    build_slot: str = "main"
    equipment_slot: str
    metric: str = "FullDPS"
    max_price_exalted: float | None = None
    securable_only: bool = False
    query_id: str | None = None
    trade_url: str | None = None
    total_market_matches: int = 0
    slot_weights: SlotStatWeights
    candidates: list[LiveUpgradeCandidate] = Field(default_factory=list)


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

    payload: dict[str, Any] = {
        **dict(stats_dict),
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
            nm = f"{it.get('name') or ''} {it.get('baseName') or ''}".strip()
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
        http_client: Poe2HttpClient | None = None,
        trade_client: Any = None,
        economy_service: Any = None,
    ) -> None:
        self._owns_bridge = bridge is None
        self.bridge = bridge or Pob2Bridge()
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
                f"Spirit is over-reserved ({sust_stats.spirit_reserved}/{sust_stats.spirit_total})."
            )
        if not apply_dewater and not res.get("restoredVerified", True):
            warnings.append("Warning: Post-audit snapshot verification drifted.")

        summary_items = (
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

        return DewateredAudit(
            slot_name=slot,
            raw_stats=raw_stats,
            sustainable_pinnacle_stats=sust_stats,
            dps_inflation_pct=round(total_dps_inf, 2),
            ehp_inflation_pct=round(total_ehp_inf, 2),
            stripped_flags=stripped,
            warnings=warnings,
            hc_rip_risks=hc_risks,
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

        def_passives_raw = self.rank_passive_upgrades(
            slot=slot,
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
        securable_only: bool = False,
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
        fallback = {"divine": 450.0, "chaos": 35.0, "regal": 0.25, "vaal": 1.0, "alch": 0.5}
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
        securable_only: bool = False,
        min_rune_sockets: int = 0,
        candidate_limit: int = 10,
        sort_by_value: bool = False,
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
                query_id=query_id,
                trade_url=trade_url,
                total_market_matches=total_matches,
                slot_weights=slot_weights,
                candidates=[],
            )

        batch_inputs: list[dict[str, Any]] = []
        listings_by_id: dict[str, TradeListing] = {}
        for idx, lst in enumerate(listings, start=1):
            lid = lst.listing_id or f"cand_{idx}"
            listings_by_id[lid] = lst
            cand_label = f"{lst.item_name + ' ' if lst.item_name else ''}{lst.base_type}".strip()
            batch_inputs.append(
                {
                    "id": lid,
                    "label": cand_label,
                    "targetSlot": slot_name,
                    "itemText": lst.pob_item_text or "",
                }
            )

        raw_batch = self.bridge.call(
            "diff_items_batch",
            {"slot": build_slot, "items": batch_inputs},
        )

        scored: list[tuple[float, LiveUpgradeCandidate]] = []
        for raw_res in raw_batch.get("results") or []:
            if not isinstance(raw_res, Mapping) or not raw_res.get("ok"):
                continue
            lid = str(raw_res.get("id") or "")
            lst = listings_by_id.get(lid)
            if lst is None:
                continue
            price_ex = lst.price_exalted if lst.price_exalted > 0 else None
            diff_model = _to_item_upgrade_diff(
                raw_res,
                item_text=lst.pob_item_text,
                price_exalted=price_ex,
            )
            ehp_per_ex = (
                round(diff_model.total_ehp_delta / price_ex, 4)
                if price_ex is not None and price_ex > 0
                else None
            )
            warns = [str(w) for w in (raw_res.get("warnings") or [])]

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
            query_id=query_id,
            trade_url=trade_url,
            total_market_matches=total_matches,
            slot_weights=slot_weights,
            candidates=ranked_candidates,
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


atexit.register(_close_default_build_service)
