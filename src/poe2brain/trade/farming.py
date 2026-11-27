# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Data-driven Patch 0.5.5 (Forbidden Rites) Atlas & league-start farming strategy engine.

Derives live market liquidity, top liquid drops, 7-day momentum, and entry ticket costs
dynamically from `EconomyService` category snapshots (zero hardcoded item prices or cherry-picked
drop lists) and scores build fit directly against headless PoB2 `BuildStats`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH

if TYPE_CHECKING:
    from poe2brain.models.builds import BuildStats
    from poe2brain.trade.economy import EconomyService

LeagueStage = Literal["day1", "early", "mid", "endgame"]
FarmingFocus = Literal["balanced", "steady", "jackpot", "speed"]


@dataclass(frozen=True)
class _FarmingLoopSpec:
    id: str
    name: str
    zh_name: str
    category: Literal["progression", "mapping", "trial", "bossing", "base_farming"]
    stages: tuple[LeagueStage, ...]
    # Live EconomyService category bindings (zero hardcoded drop/price tables)
    market_categories: tuple[str, ...]
    market_tokens: tuple[str, ...]
    entry_tokens: tuple[str, ...]
    min_budget_ex: float
    min_dps: float
    ideal_dps: float
    min_ehp: float
    min_phys_max_hit: float
    min_move_speed: float
    speed_weight: float
    variance: Literal["low", "medium", "high"]
    atlas_master: str
    atlas_master_nodes: tuple[str, ...]
    tablet_setup: str
    waystone_tier_plan: str
    iir_guidance: str
    creator_archetype: str
    execution_steps: tuple[str, ...]
    recommended_commands: tuple[str, ...]
    requires_non_negative_iir: bool = False
    requires_negative_iir: bool = False
    penalizes_es_bypass: bool = False
    honour_sensitive: bool = False


_FARMING_LOOPS: tuple[_FarmingLoopSpec, ...] = (
    _FarmingLoopSpec(
        id="fortress_atlas_rush",
        name="Origins of Divinity Fortress Rush (Atlas Unlocks + Tablet Flipping)",
        zh_name="起源要塞速推（开异界天赋 + 卖多余碑牌/换界石）",
        category="progression",
        stages=("day1", "early"),
        market_categories=("map", "fragments"),
        market_tokens=(),
        entry_tokens=(),
        min_budget_ex=0.0,
        min_dps=80_000.0,
        ideal_dps=350_000.0,
        min_ehp=10_000.0,
        min_phys_max_hit=3_000.0,
        min_move_speed=1.15,
        speed_weight=0.85,
        variance="low",
        atlas_master="Jado",
        atlas_master_nodes=("Partial Translations", "Long Days", "Unforeseen Treats"),
        tablet_setup=(
            "Run bare or cheap Irradiated/Overseer Tablets; sell unrun "
            "Ritual/Expedition/Breach tablets"
        ),
        waystone_tier_plan=(
            "Push T1->T15 immediately (2-mod Magic / 4-mod Rare for 4–2 revives); "
            "hunt Powerful Map Bosses for +1 Waystone drops"
        ),
        iir_guidance="0%–50% IIR is fine — never sacrifice clear speed or resists on Day 1",
        creator_archetype="Day-1 speed-progression standard (Fubgun / Zizaran / 某雷)",
        execution_steps=(
            "Skip slow full-clear mechanics in low tiers; blitz Fortress Towers, Gateways, "
            "and Enigma Chambers for Atlas Passive Points.",
            "Target Powerful Map Bosses on the Atlas to guarantee +1 tier Waystone sustain "
            "straight to T15.",
            "Defeat the Quest Arbiter of Divinity early to auto-complete Fortress sections "
            "and unlock passive clusters fast.",
            "Liquidate early-league excess Tablets, high-tier Waystones, and Boss Fragments "
            "while early demand peaks.",
        ),
        recommended_commands=(
            "poe2 wiki regex --slot main --min-waystone-drop 120",
            "poe2 econ scan --min-volume-divine 5",
        ),
    ),
    _FarmingLoopSpec(
        id="expedition_olroth",
        name="Expedition & Olroth's Saga Logbooks (Day 1–3 Consistency King)",
        zh_name="探险炸坟 + 奥洛斯日志（赛季初最稳现金流 + 自制装）",
        category="mapping",
        stages=("day1", "early", "mid", "endgame"),
        market_categories=("expedition", "verisium"),
        market_tokens=(),
        entry_tokens=("logbook", "expedition tablet"),
        min_budget_ex=40.0,
        min_dps=300_000.0,
        ideal_dps=1_500_000.0,
        min_ehp=16_000.0,
        min_phys_max_hit=4_500.0,
        min_move_speed=1.15,
        speed_weight=0.45,
        variance="low",
        atlas_master="Jado",
        atlas_master_nodes=(
            "Partial Translations",
            "Long Days",
            "Expedition Logbook/Remnant cluster",
        ),
        tablet_setup=(
            "2–3x Expedition Tablets in Map Device (Logbook drop chance + "
            "Increased Remnant/Rare monsters)"
        ),
        waystone_tier_plan=(
            "T15 4-mod Rare (2 revives) for safe Logbook sustain; "
            "6-mod only when >7k Phys Max Hit"
        ),
        iir_guidance=(
            "75%–125% IIR helps rare/currency drops, while Logbook & Artifact chests "
            "scale off Area/Tablet mods"
        ),
        creator_archetype=(
            "Early-league steady currency + self-craft loop (XTheFarmerX / BawLoch / 大白)"
        ),
        execution_steps=(
            "Chain Expedition explosives onto Logbook chance, Runic Monster markers, and "
            "Verisium/Artifact chests (avoid immune-to-your-damage Remnants!).",
            "Cash out Tujen for immediate liquid Exalted/Chaos/Divine currency and use Rog "
            "to craft high-margin early Rare gear.",
            "Run Olroth's Saga Logbooks for Verisium Remnants, Lineage Gems, and boss "
            "uniques — or bulk-sell Logbooks on Day 1–2.",
        ),
        recommended_commands=(
            "poe2 wiki regex --slot main --tablet-preset expedition",
            "poe2 econ bosses --encounter olroth",
        ),
    ),
    _FarmingLoopSpec(
        id="ritual_wildwood",
        name="Forbidden Rites Ritual + Sacred Blooms & Viridian Wildwood",
        zh_name="0.5.5 禁忌秘仪祭坛 + 圣花/翠绿荒林妖精（预兆大奖流）",
        category="mapping",
        stages=("early", "mid", "endgame"),
        market_categories=("ritual",),
        market_tokens=(),
        entry_tokens=("ritual tablet", "sacred bloom", "audience with the king"),
        min_budget_ex=75.0,
        min_dps=600_000.0,
        ideal_dps=3_000_000.0,
        min_ehp=22_000.0,
        min_phys_max_hit=6_000.0,
        min_move_speed=1.20,
        speed_weight=0.55,
        variance="high",
        atlas_master="Hilda",
        atlas_master_nodes=(
            "Breeding Season",
            "Mighty Prey",
            "Ritual Deferral / Royal Lenience cluster",
        ),
        tablet_setup=(
            "Ritual Tablets in Map Device paired with Sacred Bloom clusters on Atlas "
            "(Ritual Tablets directly boost Wildwood Wisp density!)"
        ),
        waystone_tier_plan=(
            "T15 4-mod (2 revives) early -> 6-mod (0 revives) once Phys Max Hit >= 7,500; "
            "always kill Map Boss inside Ritual first so it respawns"
        ),
        iir_guidance=(
            "100%–125% Player IIR multiplies with Wild Wisps + Area Rarity "
            "for massive currency/unique upgrades"
        ),
        creator_archetype="0.5.5 Forbidden Rites jackpot loop (poeguy007 / Operator Otter)",
        execution_steps=(
            "Apply Sacred Blooms to Atlas clusters and pair them with Ritual Tablets in the "
            "Map Device to multiply Viridian Wildwood Wisp yield.",
            "Inside the Wildwood, follow White Wisps to extend light/reveal paths while "
            "stacking Primal (Magic), Vivid (+Rare mods), and Wild (Rarity) Wisps.",
            "In Ritual maps, clear the Map Boss altar first so the boss cascades into "
            "subsequent Rituals for maximum Tribute.",
            "Defer top-value live Omens and Sacred Blooms surfaced by the live economy feed.",
        ),
        recommended_commands=(
            "poe2 wiki regex --slot main --tablet-preset ritual",
            "poe2 econ bosses --encounter king",
        ),
        requires_non_negative_iir=True,
        penalizes_es_bypass=True,
    ),
    _FarmingLoopSpec(
        id="breach_speed",
        name="Breach Speed-Mapping (Catalysts, Breach Rings & Xesht)",
        zh_name="裂隙极速清图（催化剂 + 裂隙戒指 + 裂隙石）",
        category="mapping",
        stages=("day1", "early", "mid", "endgame"),
        market_categories=("breach", "fragments"),
        market_tokens=("catalyst", "breach"),
        entry_tokens=("breach tablet", "breachstone"),
        min_budget_ex=10.0,
        min_dps=500_000.0,
        ideal_dps=2_500_000.0,
        min_ehp=18_000.0,
        min_phys_max_hit=5_000.0,
        min_move_speed=1.30,
        speed_weight=0.95,
        variance="low",
        atlas_master="Hilda",
        atlas_master_nodes=("Breeding Season", "Breach Monster Density & Clasp nodes"),
        tablet_setup=(
            "2–3x Breach Tablets in Map Device (Additional Breaches + "
            "Splinter/Monster Density + Rare Monsters)"
        ),
        waystone_tier_plan=(
            "T15 4-mod Rare (2 revives) prioritized for open layouts + Pack Size; "
            "Vaal surplus T15s for T16 ilvl 82 bases"
        ),
        iir_guidance=(
            "100%–125% Player IIR is the exact sweet spot for Breach currency & unique "
            "conversion without slowing clear"
        ),
        creator_archetype="High-velocity zoom-mapper staple (Fubgun / Crouching Tuna / ksc小波)",
        execution_steps=(
            "Slot Breach Tablets in the Map Device on wide/open map layouts so "
            "Breaches expand unimpeded.",
            "Prioritize movement speed (1.40x+) and screen-wide AoE/Herald chain clears "
            "to open every Clasped Hand and Rare pack.",
            "Bulk-sell top-priced live Catalysts and ilvl 82 Breach Rings to hideout "
            "crafters; run or sell Breachstones based on live boss EV.",
        ),
        recommended_commands=(
            "poe2 wiki regex --slot main --tablet-preset breach",
            "poe2 econ bosses --encounter xesht",
        ),
        requires_non_negative_iir=True,
    ),
    _FarmingLoopSpec(
        id="sekhema_trials",
        name="Trial of the Sekhemas (Honour Relic Farming + Ascendancy Carries)",
        zh_name="丝克玛试炼（荣誉抗性圣物速刷 + 升华带车/时光珠宝）",
        category="trial",
        stages=("day1", "early", "mid", "endgame"),
        market_categories=("sanctum", "jewel"),
        market_tokens=("desperate", "flame", "darkness", "time-lost", "changing", "peacemaker"),
        entry_tokens=("barya", "desperate alliance"),
        min_budget_ex=30.0,
        min_dps=400_000.0,
        ideal_dps=2_000_000.0,
        min_ehp=18_000.0,
        min_phys_max_hit=4_500.0,
        min_move_speed=1.35,
        speed_weight=0.80,
        variance="low",
        atlas_master="None (Instance Trial — Atlas tree inactive)",
        atlas_master_nodes=(
            "Cap 75% Honour Resistance on Relics",
            "Stack >=120% Relic Quantity for double Unique Relics",
        ),
        tablet_setup="No Tablets — uses ilvl 75–80+ Djinn Barya + Relic Altar loadout",
        waystone_tier_plan=(
            "4-Floor ilvl 80 Djinn Barya (stack Honour Res -> Merchant choices -> "
            "Movement Speed -> Relic Quant)"
        ),
        iir_guidance=(
            "Player IIR does NOT replace Relic Quantity; swap IIR suffixes for "
            "Move Speed / ES / DPS inside Trials"
        ),
        creator_archetype="Day 1–7 guaranteed income + carries (poeguy007 / z3r0pointflux / 大白)",
        execution_steps=(
            "Cap 75% Honour Resistance first using cheap Amphora/Seal Relics; Honour "
            "scales directly off your Life + ES pool.",
            "On Day 1–3, monetize 3rd/4th Ascendancy carries at Zarokh alongside "
            "Time-Lost Jewels and Royal/Arcanist chests.",
            "Scale to >=120% increased Quantity of Relics dropped by Monsters so Zarokh "
            "drops double Unique Relics.",
        ),
        recommended_commands=(
            "poe2 wiki regex --relic-preset sekhema_safe",
            "poe2 econ bosses --encounter sekhema",
        ),
        honour_sensitive=True,
    ),
    _FarmingLoopSpec(
        id="trial_of_chaos",
        name="Trial of Chaos (Guaranteed Soul Cores & Trialmaster Fates)",
        zh_name="混沌试炼（保底灵魂核心 + 命运碎片/庄严之饰）",
        category="trial",
        stages=("early", "mid", "endgame"),
        market_categories=("ultimatum", "fragments"),
        market_tokens=("soul core", "fate"),
        entry_tokens=("ultimatum", "fate"),
        min_budget_ex=40.0,
        min_dps=750_000.0,
        ideal_dps=2_500_000.0,
        min_ehp=24_000.0,
        min_phys_max_hit=6_500.0,
        min_move_speed=1.20,
        speed_weight=0.50,
        variance="low",
        atlas_master="None (Instance Trial — Atlas tree inactive)",
        atlas_master_nodes=("Skip Day 1 until Chaos Res & Phys Max Hit are online",),
        tablet_setup="No Tablets — uses 10-Room Inscribed Ultimatums",
        waystone_tier_plan=(
            "ilvl 75–80 10-Room Inscribed Ultimatum (avoid deadly stacking tribulations "
            "matching your build weakness)"
        ),
        iir_guidance=(
            "Soul Core & Fate room rewards are unaffected by Player IIR — maximize DPS "
            "& tankiness for fast 10-room clears"
        ),
        creator_archetype="Steady liquid floor Day 2+ (non-MF tanky mappers / 大白)",
        execution_steps=(
            "Wait until Day 2+ when resists, Chaos Res, and Phys Max Hit (>=6,500) "
            "comfortably survive 10 stacked tribulations.",
            "Prioritize fast-completion rooms and liquidate top live Soul Cores on Alva "
            "for zero-friction Divine flow.",
            "Assemble Deadly / Victorious / Cowardly Fates to run Trialmaster or bulk-sell.",
        ),
        recommended_commands=("poe2 econ bosses --encounter chaos",),
        penalizes_es_bypass=True,
    ),
    _FarmingLoopSpec(
        id="delirium_simulacrum",
        name="Delirium Fog & Liquid Emotions (Waystone Instilling + Simulacrum)",
        zh_name="惊悸迷雾 + 浓缩情绪（地图附魔 + 梦魇拟像）",
        category="mapping",
        stages=("early", "mid", "endgame"),
        market_categories=("delirium", "fragments"),
        market_tokens=("liquid", "emotion", "simulacrum"),
        entry_tokens=("delirium tablet", "simulacrum"),
        min_budget_ex=20.0,
        min_dps=700_000.0,
        ideal_dps=3_000_000.0,
        min_ehp=22_000.0,
        min_phys_max_hit=6_000.0,
        min_move_speed=1.25,
        speed_weight=0.70,
        variance="medium",
        atlas_master="Doryani",
        atlas_master_nodes=(
            "Improved Calibration",
            "Disengaged Safeties",
            "Delirium Splinter & Emotion cluster",
        ),
        tablet_setup=(
            "Delirium Tablets in Map Device (stack Splinter & Emotion drop multipliers)"
        ),
        waystone_tier_plan=(
            "T15 4–6 mod Rare (Delirium Fog never spreads onto Atlas-point maps; "
            "instill T15s with budget Emotions for Pack Size)"
        ),
        iir_guidance=(
            "75%–125% Player IIR; Liquid Emotions & Simulacrum Splinters scale "
            "primarily off monster density & reward depth"
        ),
        creator_archetype="Low-input steady density farmer (TranePanos / StonerMJW)",
        execution_steps=(
            "Run Delirium Tablets on linear layouts to push Delirium reward tiers quickly.",
            "Check 3-to-1 Reforging Bench Liquid Emotion upgrades (`poe2 econ arbitrage`) "
            "before selling raw emotions on Alva.",
            "Sell Simulacrums in bulk early or farm Kosis/Omniphobia once wave DPS is online.",
        ),
        recommended_commands=(
            "poe2 econ arbitrage --budget 5d --gold 50000",
            "poe2 econ bosses --encounter simulacrum",
        ),
    ),
    _FarmingLoopSpec(
        id="boss_rush_vaaltemple",
        name="Boss-Rushing, Citadel Fragments & Fate of the Vaal (Atziri Temple)",
        zh_name="极速冲王 + 堡垒碎片 + 瓦尔神庙（高单伤速刷门票）",
        category="bossing",
        stages=("early", "mid", "endgame"),
        market_categories=("incursion", "vaal", "abyss", "fragments"),
        market_tokens=(),
        entry_tokens=("crisis fragment", "invitation"),
        min_budget_ex=25.0,
        min_dps=1_200_000.0,
        ideal_dps=5_000_000.0,
        min_ehp=22_000.0,
        min_phys_max_hit=6_000.0,
        min_move_speed=1.35,
        speed_weight=0.90,
        variance="medium",
        atlas_master="Doryani",
        atlas_master_nodes=(
            "Disengaged Safeties",
            "Precursor Terraformer / Fate of the Vaal Temple cluster",
        ),
        tablet_setup=(
            "Overseer / Irradiated + Temple Precursor Tablets (Vaal Beacons & "
            "Lira Vaal Energised Crystals)"
        ),
        waystone_tier_plan=(
            "High-Waystone-Drop T15/T16 maps rushing straight to Boss room "
            "(keep 4-mod 2 revives if boss burst is risky)"
        ),
        iir_guidance=(
            "Player IIR does not scale guaranteed Citadel Fragments or Temple currencies "
            "— maximize Move Speed + Single-Target Burst"
        ),
        creator_archetype="Boss-rusher / fragment & Vaal Temple seller",
        execution_steps=(
            "Sprint straight to the Map Boss / Vaal Beacon on short layouts; ignore "
            "low-value straggler packs.",
            "Stack Waystone Drop Chance on Citadel maps to multiply Crisis Fragment drops "
            "and liquidate sets while Pinnacle demand is peaking.",
            "Target Fate of the Vaal Temple runs for top live Incursion/Vaal currencies.",
        ),
        recommended_commands=(
            "poe2 wiki regex --slot main --min-waystone-drop 140",
            "poe2 econ bosses --encounter arbiter",
        ),
    ),
    _FarmingLoopSpec(
        id="negative_rarity_bases",
        name="Negative-Rarity Exceptional & ilvl 82 Crafting-Base Farming",
        zh_name="负稀有度白装底材流（82物等 + 超凡21%品质/3孔工艺底材）",
        category="base_farming",
        stages=("mid", "endgame"),
        market_categories=("verisium", "runes", "idol"),
        market_tokens=(),
        entry_tokens=("breach tablet",),
        min_budget_ex=150.0,
        min_dps=600_000.0,
        ideal_dps=2_500_000.0,
        min_ehp=20_000.0,
        min_phys_max_hit=5_500.0,
        min_move_speed=1.30,
        speed_weight=0.75,
        variance="medium",
        atlas_master="Hilda",
        atlas_master_nodes=("Breeding Season", "Exceptional / High-ilvl Base & Pack Size nodes"),
        tablet_setup=(
            "Irradiated (+1 Monster Level -> ilvl 82+ bases!) + Breach/Pack-Size Tablets"
        ),
        waystone_tier_plan=(
            "Corrupted T16 + Irradiated (Area Level 82) with high Pack Size; "
            "avoid Area Rarity mods"
        ),
        iir_guidance=(
            "Requires NEGATIVE Player IIR (e.g. -50% to -100% via negative-rarity "
            "Ventor's Gamble) so drops stay Normal (white) bases!"
        ),
        creator_archetype="Specialized crafter-supply meta (Negative-IIR white-base farmers)",
        execution_steps=(
            "Equip negative Item Rarity gear (Ventor's Gamble / negative IIR rolls) "
            "to suppress Normal->Magic/Rare upgrades.",
            "Run Area Level 81–82 Irradiated T15/T16 maps with maximum Pack Size to flood "
            "Normal ilvl 82 meta bases & Exceptional (21%+ quality / 3-socket) drops.",
            "Pair with `poe2 filter generate --strictness strict` so Exceptional and "
            "ilvl 82 crafting bases highlight cleanly for bulk crafter sales.",
        ),
        recommended_commands=(
            "poe2 filter generate --slot main --strictness strict --out base_farm.filter",
            "poe2 craft profit --budget mid",
        ),
        requires_negative_iir=True,
    ),
)


def recommend_farming_strategies(
    econ: EconomyService,
    *,
    league: str = DEFAULT_LEAGUE,
    stage: LeagueStage = "early",
    budget_exalted: float | None = None,
    focus: FarmingFocus = "balanced",
    build_stats: BuildStats | None = None,
    slot_name: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Rank Patch 0.5.5 league-start & Atlas farming loops dynamically from live economy & PoB2."""
    rates = econ.get_reference_rates(league, force_refresh=force_refresh)
    div_ex = max(1.0, float(rates.divine_exalted or 450.0))

    build_profile: dict[str, Any] | None = None
    if build_stats is not None:
        iir_pct = float(getattr(build_stats, "item_rarity_pct", 0.0) or 0.0)
        if iir_pct < 0.0:
            iir_band = "negative_white_base_mode"
        elif iir_pct < 100.0:
            iir_band = "low_iir_below_sweet_spot"
        elif iir_pct <= 150.0:
            iir_band = "sweet_spot_100_150"
        else:
            iir_band = "diminishing_returns_over_150"
        build_profile = {
            "slot": slot_name,
            "ascendancy": build_stats.ascendancy_name or build_stats.class_name,
            "level": build_stats.level,
            "main_skill": build_stats.main_skill,
            "effective_dps": round(build_stats.effective_dps, 1),
            "total_ehp": round(build_stats.total_ehp, 1),
            "phys_max_hit": round(build_stats.phys_max_hit, 1),
            "movement_speed_mod": round(build_stats.movement_speed_mod, 3),
            "life_unreserved": round(build_stats.life_unreserved, 1),
            "energy_shield": round(build_stats.energy_shield, 1),
            "chaos_immune": build_stats.chaos_immune,
            "item_rarity_pct": round(iir_pct, 1),
            "iir_band": iir_band,
            "honour_pool_est": round(build_stats.life + build_stats.energy_shield, 1),
        }

    # Pass 1: Dynamically query live EconomyService category telemetry for every loop
    summaries: dict[str, dict[str, Any]] = {}
    for spec in _FARMING_LOOPS:
        summaries[spec.id] = econ.get_category_market_summary(
            spec.market_categories,
            league=league,
            token_filter=spec.market_tokens or None,
            entry_tokens=spec.entry_tokens or None,
            min_volume_divine=0.25,
            top_n=5,
            force_refresh=False,
        )

    # Cross-loop normalization bounds computed purely from live snapshot data
    max_log_vol = max(
        (math.log1p(max(0.0, float(m["total_volume_divine"]))) for m in summaries.values()),
        default=1.0,
    ) or 1.0
    max_log_p90 = max(
        (math.log1p(max(0.0, float(m["p90_liquid_divine"]))) for m in summaries.values()),
        default=1.0,
    ) or 1.0

    ranked: list[dict[str, Any]] = []
    for spec in _FARMING_LOOPS:
        mkt = summaries[spec.id]
        top_items = mkt["top_liquid_items"]
        entry_tickets = mkt["entry_tickets"]
        tablet_tickets = [
            t
            for t in entry_tickets
            if "tablet" in t["name"].lower() or "bloom" in t["name"].lower()
        ]
        if tablet_tickets:
            input_cost_ex = sum(float(t["exalted"]) for t in tablet_tickets[:2])
        elif entry_tickets:
            input_cost_ex = min(float(t["exalted"]) for t in entry_tickets)
        else:
            input_cost_ex = None if spec.entry_tokens else 0.0

        # Data-driven market score (0..100) from live liquidity + p90 drop depth + 7d momentum
        vol_norm = math.log1p(max(0.0, float(mkt["total_volume_divine"]))) / max_log_vol
        p90_norm = math.log1p(max(0.0, float(mkt["p90_liquid_divine"]))) / max_log_p90
        chg_7d = mkt["volume_weighted_change_7d_pct"]
        mom_norm = (
            max(-1.0, min(1.0, float(chg_7d) / 25.0)) if chg_7d is not None else 0.0
        )
        market_score = round(
            max(0.0, min(100.0, 50.0 * vol_norm + 40.0 * p90_norm + 10.0 * (0.5 + 0.5 * mom_norm))),
            1,
        )

        # Stage, budget, and focus alignment
        context_score = 60.0
        build_warnings: list[str] = []
        fit_reasons: list[str] = []

        if stage in spec.stages:
            context_score += 20.0 if spec.stages[0] == stage else 12.0
            fit_reasons.append(f"Matches {stage} league stage.")
        else:
            context_score -= 24.0
            build_warnings.append(
                f"Better suited for {'/'.join(spec.stages)} than {stage}."
            )

        if input_cost_ex is None:
            build_warnings.append("Required entry ticket price is unavailable.")
        if budget_exalted is not None:
            effective_floor = spec.min_budget_ex
            if input_cost_ex is not None:
                effective_floor = max(effective_floor, input_cost_ex * 2.0)
            if budget_exalted < effective_floor:
                context_score -= 28.0
                build_warnings.append(
                    f"Budget ({budget_exalted:.0f} Ex) is below recommended runway "
                    f"(~{effective_floor:.0f} Ex) for sustained inputs."
                )
            elif input_cost_ex is not None:
                context_score += 6.0

        if focus == "steady":
            context_score += (
                14.0
                if spec.variance == "low"
                else (-12.0 if spec.variance == "high" else 0.0)
            )
        elif focus == "jackpot":
            context_score += 14.0 if spec.variance == "high" else (6.0 * p90_norm - 4.0)
        elif focus == "speed":
            context_score += round((spec.speed_weight - 0.5) * 30.0, 1)

        if mkt["total_volume_divine"] > 0:
            fit_reasons.append(
                f"Live market depth: {mkt['total_volume_divine']:,.0f} Div vol across "
                f"{mkt['priced_count']} tracked items"
                + (f" ({chg_7d:+.1f}% 7d)." if chg_7d is not None else ".")
            )

        build_fit_score: float | None = None
        if build_stats is not None:
            bf = 100.0
            dps = build_stats.effective_dps
            ehp = build_stats.total_ehp
            phys = build_stats.phys_max_hit
            ms = build_stats.movement_speed_mod
            iir = float(getattr(build_stats, "item_rarity_pct", 0.0) or 0.0)

            if dps < spec.min_dps:
                bf -= 30.0
                build_warnings.append(
                    f"Dewatered DPS ({dps:,.0f}) is below target floor "
                    f"({spec.min_dps:,.0f}) for smooth clears."
                )
            elif dps >= spec.ideal_dps:
                bf += 6.0
                fit_reasons.append(f"DPS ({dps:,.0f}) clears {spec.id} effortlessly.")

            if ehp < spec.min_ehp:
                bf -= 20.0
                build_warnings.append(
                    f"Dewatered EHP ({ehp:,.0f}) is under {spec.min_ehp:,.0f} comfort floor."
                )
            if phys < spec.min_phys_max_hit:
                bf -= 18.0
                build_warnings.append(
                    f"Phys Max Hit ({phys:,.0f} < {spec.min_phys_max_hit:,.0f}) "
                    "risks 0-revive bricking on 6-mod maps/trials."
                )

            if ms < spec.min_move_speed:
                bf -= round(25.0 * spec.speed_weight, 1)
                build_warnings.append(
                    f"Move speed ({ms:.2f}x < {spec.min_move_speed:.2f}x) "
                    "limits Div/hr on high-velocity clears."
                )
            elif ms >= 1.50 and spec.speed_weight >= 0.75:
                bf += 10.0
                fit_reasons.append(f"High mobility ({ms:.2f}x MS) strongly amplifies Div/hr here.")

            if spec.penalizes_es_bypass and not build_stats.chaos_immune:
                if build_stats.energy_shield >= 2000.0 and build_stats.life_unreserved < 1200.0:
                    bf -= 16.0
                    build_warnings.append(
                        f"Non-CI ES build ({build_stats.life_unreserved:.0f} Life / "
                        f"{build_stats.energy_shield:.0f} ES): Bleed & Poison bypass ES "
                        "directly to Life — run Staunching/Antidote Charms."
                    )

            if spec.honour_sensitive:
                honour_est = build_stats.life + build_stats.energy_shield
                if honour_est < 3500.0:
                    bf -= 15.0
                    build_warnings.append(
                        f"Low base Life+ES ({honour_est:,.0f}) limits Sekhema Honour pool "
                        "— cap 75% Honour Resist on Relics first."
                    )

            if spec.requires_negative_iir:
                if iir >= 0.0:
                    bf -= 38.0
                    build_warnings.append(
                        f"Build currently has {iir:+.0f}% IIR — Negative-Rarity base farming "
                        "requires <0% IIR (Ventor's Gamble)."
                    )
                else:
                    bf += 18.0
                    fit_reasons.append(f"Negative IIR ({iir:+.0f}%) matches white-base farming!")
            elif spec.requires_non_negative_iir:
                if iir < 0.0:
                    bf -= 20.0
                    build_warnings.append(
                        f"Negative IIR ({iir:+.0f}%) suppresses currency/unique upgrades "
                        f"in {spec.id}."
                    )
                elif iir_band == "sweet_spot_100_150":
                    bf += 8.0
                    fit_reasons.append(
                        f"Player IIR ({iir:.0f}%) falls within the 100–150% farming band."
                    )
                elif iir < 40.0:
                    build_warnings.append(
                        f"Player IIR is {iir:.0f}% — adding ~100% IIR across rings/helm "
                        "noticeably lifts mapping currency drops."
                    )
                elif iir > 165.0:
                    build_warnings.append(
                        f"Player IIR ({iir:.0f}%) is past the ~125–150% diminishing-returns "
                        "plateau — trade excess IIR for speed/DPS."
                    )

            build_fit_score = round(max(0.0, min(100.0, bf)), 1)
            final_score = round(
                0.35 * market_score + 0.30 * context_score + 0.35 * build_fit_score, 1
            )
        else:
            if spec.requires_negative_iir and stage in ("day1", "early"):
                context_score -= 25.0
            final_score = round(0.45 * market_score + 0.55 * context_score, 1)

        top_item = top_items[0] if top_items else None
        ranked.append(
            {
                "id": spec.id,
                "name": spec.name,
                "zh_name": spec.zh_name,
                "category": spec.category,
                "score": final_score,
                "market_score": market_score,
                "build_fit_score": build_fit_score,
                "recommended_stages": list(spec.stages),
                "variance": spec.variance,
                "atlas_master": spec.atlas_master,
                "atlas_master_nodes": list(spec.atlas_master_nodes),
                "tablet_setup": spec.tablet_setup,
                "waystone_tier_plan": spec.waystone_tier_plan,
                "iir_guidance": spec.iir_guidance,
                "creator_archetype": spec.creator_archetype,
                "market_categories": list(spec.market_categories),
                "market_summary": {
                    "priced_count": mkt["priced_count"],
                    "liquid_count": mkt["liquid_count"],
                    "total_volume_divine": mkt["total_volume_divine"],
                    "volume_weighted_change_7d_pct": chg_7d,
                    "median_liquid_divine": mkt["median_liquid_divine"],
                    "p90_liquid_divine": mkt["p90_liquid_divine"],
                },
                "input_items": entry_tickets,
                "estimated_input_cost_exalted": (
                    round(input_cost_ex, 2) if input_cost_ex is not None else None
                ),
                "estimated_input_cost_divine": (
                    round(input_cost_ex / div_ex, 3) if input_cost_ex is not None else None
                ),
                "headline_payouts": top_items,
                "top_payout_item": top_item["name"] if top_item else None,
                "top_payout_divine": top_item["divine"] if top_item else None,
                "fit_reasons": fit_reasons,
                "build_warnings": build_warnings,
                "execution_steps": list(spec.execution_steps),
                "recommended_commands": list(spec.recommended_commands),
            }
        )

    ranked.sort(
        key=lambda r: (r["score"], r["market_score"], r["top_payout_divine"] or 0.0),
        reverse=True,
    )
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx

    return {
        "patch": POE2_PATCH,
        "league": league,
        "divine_exalted": round(div_ex, 2),
        "stage": stage,
        "focus": focus,
        "budget_exalted": round(budget_exalted, 2) if budget_exalted is not None else None,
        "build_profile": build_profile,
        "strategies": ranked,
        "global_rules": [
            (
                "Player IIR sweet spot is ~100–125% (steep diminishing returns past 150%, "
                "multiplicative with Area Rarity); Day 1 prioritizes clear speed & Atlas points."
            ),
            (
                "Waystone mod count dictates map revives: "
                "0 mods = 5 revives, 2 mods = 4, 4 mods = 2, 6 mods = 0 revives."
            ),
            (
                "In Patch 0.5.5, Precursor Tablets plug directly into the Map Device "
                "(not Lost Towers); Ritual Tablets scale Wildwood Wisps on Sacred Blooms."
            ),
        ],
    }
