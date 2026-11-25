# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from poe2brain.crafting.planner import CraftingPlanner
from poe2brain.crafting.pool import CraftingMod, CraftingState, ModPoolEngine
from poe2brain.knowledge.waystone_regex import (
    POE2_STASH_REGEX_MAX_CHARS,
    WaystoneRegexEngine,
    build_revive_schedule,
    build_waystone_regex_plan,
    waystone_revives_for_mod_count,
)
from poe2brain.models.builds import BuildStats
from poe2brain.models.common import AffixSide
from poe2brain.models.items import ItemRarity
from poe2brain.models.trade import CurrencyQuote
from poe2brain.trade.advisor import TradeAdvisor
from poe2brain.trade.arbitrage import ArbitrageScanner
from poe2brain.trade.ee2_bundle import get_default_bundle
from poe2brain.trade.item_parser import (
    extract_stack_size,
    extract_unrevealed_desecrated_sides,
    parse_clipboard_item,
    resolve_trade_category,
)
from poe2brain.trade.stat_matcher import build_item_trade_query

LIVE_ENABLED = os.environ.get("POE2_LIVE_TESTS") == "1"

# ---------------------------------------------------------------------------
# Journey 4 Fixtures: 8 Diverse Dump-Tab Clipboard Items
# ---------------------------------------------------------------------------

J4_01_UNID_TIER4_ROBE = """Item Class: Body Armours
Rarity: Rare
Hexer's Robe
--------
Energy Shield: 184
--------
Requires: Level 65, 146 Int
--------
Item Level: 82
--------
Unidentified (Tier 4)
"""

J4_02_CUT_LV20_5S_GEM = """Item Class: Skill Gems
Rarity: Gem
Spark
--------
Spell, AoE, Duration, Lightning, Projectile
Level: 20 (Max)
Quality: +20% (augmented)
--------
Requires: Level 90, 205 Int
--------
Sockets: G G G G G
"""

J4_03_PRECURSOR_TABLET = """Item Class: Tablet
Rarity: Rare
Planar Challenge
Breach Precursor Tablet
--------
Item Level: 82
--------
Adds a Breach to a Map
10 uses remaining
--------
7% increased Pack Size in Map
35% increased Quantity of Waystones found in Map
"""

J4_04_WAYSTONE_6MOD_T15 = """Item Class: Waystones
Rarity: Rare
Doomed Trail
Waystone (Tier 15)
--------
Waystone Tier: 15
Revives Available: 0 (augmented)
Pack Size: +24% (augmented)
Magic Monsters: +35% (augmented)
Rare Monsters: +70% (augmented)
Waystone Drop Chance: +185% (augmented)
Item Rarity: +42% (augmented)
--------
Item Level: 80
--------
18% increased Monster Damage
Monsters have 180% increased Critical Hit Chance
Monsters have +20% Critical Damage Bonus
Monster Damage Penetrates 12% Elemental Resistances
Players are periodically Cursed with Elemental Weakness
Players have 36% less Recovery Rate of Life and Energy Shield
Monsters have 85% increased Stun Buildup
"""

J4_05_TWICE_CORRUPTED_UNIQUE = """Item Class: Helmets
Rarity: Unique
Atziri's Disdain
Gold Circlet
--------
Quality: +20% (augmented)
Energy Shield: 92 (augmented)
--------
Requires: Level 54, 104 Int
--------
Sockets: S S
--------
Item Level: 81
--------
+24% to Chaos Resistance (enchant)
--------
+85 to maximum Mana
10% of Damage taken bypasses Energy Shield
Gain 12% of maximum Life as Extra maximum Energy Shield
--------
Twice Corrupted
"""

J4_06_UNREVEALED_DESECRATED_AMULET = """Item Class: Amulets
Rarity: Rare
Gloom Collar
Solar Amulet
--------
Requires: Level 75
--------
Item Level: 81
--------
+15 to Spirit (implicit)
--------
Desecrated Prefix
+145 to maximum Mana
+3 to Level of all Spell Skills
+37% to Cold Resistance
"""

J4_07_FRACTURED_4MOD_WAND_OPEN_SUFFIX = """Item Class: Wands
Rarity: Rare
Storm Song
Galvanic Wand
--------
Requires: Level 78, 178 Int
--------
Item Level: 82
--------
Grants Skill: Level 19 Galvanic Shards
--------
98% increased Spell Damage
+175 to maximum Mana
+5 to Level of all Lightning Spell Skills (fractured)
31% increased Cast Speed
--------
Fractured Item
"""

J4_08_SOUL_CORE_STACK = """Item Class: Socketable
Rarity: Currency
Soul Core of Azcapa
--------
Stack Size: 3/10
--------
Martial Weapons: +15 to Spirit
Armour: 10% increased Rarity of Items found
--------
Place into an empty Rune Socket on a Martial Weapon or Armour to apply its effect to that item.
"""

ALL_8_DUMP_ITEMS: tuple[str, ...] = (
    J4_01_UNID_TIER4_ROBE,
    J4_02_CUT_LV20_5S_GEM,
    J4_03_PRECURSOR_TABLET,
    J4_04_WAYSTONE_6MOD_T15,
    J4_05_TWICE_CORRUPTED_UNIQUE,
    J4_06_UNREVEALED_DESECRATED_AMULET,
    J4_07_FRACTURED_4MOD_WAND_OPEN_SUFFIX,
    J4_08_SOUL_CORE_STACK,
)

# ---------------------------------------------------------------------------
# Journey 5 Fixtures: Mid-Craft 18-Div Rare (Ctrl+Alt+C & Plain Ctrl+C)
# ---------------------------------------------------------------------------

J5_HALF_FINISHED_AMULET_CTRL_ALT_C = """Item Class: Amulets
Rarity: Rare
Sorrow Torc
Solar Amulet
--------
Requires: Level 64
--------
Item Level: 82
--------
{ Implicit Modifier }
+15(10-15) to Spirit
--------
{ Prefix Modifier "Lord's" (Tier: 1) }
+49(47-50) to Spirit
{ Prefix Modifier "Resplendent" (Tier: 1) — Defences, Energy Shield }
+86(80-89) to maximum Energy Shield
{ Prefix Modifier "Hale" (Tier: 9) — Life }
+9(10-19) to maximum Life
{ Fractured Suffix Modifier "of the Sorcerer" (Tier: 1) — Caster, Gem }
+3 to Level of all Spell Skills
{ Suffix Modifier "of the Ice" (Tier: 1) — Elemental, Cold, Resistance }
+43(41-45)% to Cold Resistance
--------
Fractured Item
"""

J5_HALF_FINISHED_AMULET_PLAIN_CTRL_C = """Item Class: Amulets
Rarity: Rare
Sorrow Torc
Solar Amulet
--------
Requires: Level 64
--------
Item Level: 82
--------
+15 to Spirit (implicit)
--------
+49 to Spirit
+86 to maximum Energy Shield
+9 to maximum Life
+3 to Level of all Spell Skills (fractured)
+43% to Cold Resistance
--------
Fractured Item
"""


# ===========================================================================
# JOURNEY 4 TESTS: Quad-Tab Dump Batch Triage (8 Mixed Ctrl+C Items)
# ===========================================================================


def test_journey4_all_8_items_parse_and_build_exact_trade2_queries() -> None:
    bundle = get_default_bundle()

    # 1. Unidentified (Tier 4) Hexer's Robe (ilvl 82 Rare)
    p1 = parse_clipboard_item(J4_01_UNID_TIER4_ROBE, bundle)
    q1 = build_item_trade_query(p1, bundle=bundle)["query"]
    assert p1.identified is False and p1.unidentified_tier == 4 and p1.item_level == 82
    assert q1["type"] == "Hexer's Robe"
    assert q1["filters"]["type_filters"]["filters"]["rarity"] == {"option": "rare"}
    assert q1["filters"]["type_filters"]["filters"]["ilvl"] == {"min": 82}
    assert q1["filters"]["misc_filters"]["filters"]["identified"] == {"option": "false"}
    assert q1["filters"]["misc_filters"]["filters"]["unidentified_tier"] == {"min": 4}
    assert q1["stats"] == [{"type": "and", "filters": []}]

    # 2. Cut Lv20 5-Socket Skill Gem (Spark 20/20 uncorrupted)
    p2 = parse_clipboard_item(J4_02_CUT_LV20_5S_GEM, bundle)
    q2 = build_item_trade_query(p2, bundle=bundle)["query"]
    assert p2.rarity == ItemRarity.GEM and p2.gem_level == 20 and p2.gem_sockets == 5
    assert q2["type"] == "Spark"
    assert q2["filters"]["type_filters"]["filters"]["category"] == {"option": "gem.activegem"}
    assert q2["filters"]["type_filters"]["filters"]["quality"] == {"min": 20}
    assert q2["filters"]["misc_filters"]["filters"]["gem_level"] == {"min": 20}
    assert q2["filters"]["misc_filters"]["filters"]["gem_sockets"] == {"min": 5}
    assert q2["filters"]["misc_filters"]["filters"]["corrupted"] == {"option": "false"}

    # 3. Precursor Tablet with explicit mods & 10 uses remaining (auto-heals to 0.5.5 Breach Tablet)
    p3 = parse_clipboard_item(J4_03_PRECURSOR_TABLET, bundle)
    q3 = build_item_trade_query(p3, bundle=bundle)["query"]
    assert resolve_trade_category(p3, bundle) == "map.tablet"
    assert q3["type"] == "Breach Tablet"
    assert q3["filters"]["type_filters"]["filters"]["category"] == {"option": "map.tablet"}
    and_ids_3 = {f["id"]: f.get("value") for f in q3["stats"][0]["filters"]}
    assert "pseudo.pseudo_number_of_uses_remaining" in and_ids_3
    assert and_ids_3["pseudo.pseudo_number_of_uses_remaining"] == {"min": 9}
    assert "explicit.stat_2017682521" in and_ids_3  # Pack Size in Map
    assert "explicit.stat_2777224821" in and_ids_3  # Quantity of Waystones found in Map

    # 4. 6-mod T15 Waystone (0 revives, +185% Waystone Drop Chance)
    p4 = parse_clipboard_item(J4_04_WAYSTONE_6MOD_T15, bundle)
    q4 = build_item_trade_query(p4, bundle=bundle)["query"]
    assert p4.waystone_tier == 15 and p4.waystone_revives == 0
    assert p4.waystone_drop_chance_pct == 185
    mf4 = q4["filters"]["map_filters"]["filters"]
    assert mf4["map_tier"] == {"min": 15}
    assert mf4["map_revives"] == {"min": 0}
    assert mf4["map_bonus"] == {"min": 166}
    # Must NOT require all 6 random brick explicit mods simultaneously
    assert q4["stats"] == [{"type": "and", "filters": []}]

    # 5. Twice Corrupted Unique (Atziri's Disdain with +24% Chaos Res enchant)
    p5 = parse_clipboard_item(J4_05_TWICE_CORRUPTED_UNIQUE, bundle)
    q5 = build_item_trade_query(p5, bundle=bundle)["query"]
    assert p5.corrupted is True and p5.twice_corrupted is True
    assert q5["name"] == "Atziri's Disdain" and q5["type"] == "Gold Circlet"
    assert q5["filters"]["misc_filters"]["filters"]["twice_corrupted"] == {"option": "true"}
    and_ids_5 = {f["id"]: f.get("value") for f in q5["stats"][0]["filters"]}
    assert "enchant.stat_2923486259" in and_ids_5
    assert "explicit.stat_1228337241" in and_ids_5  # Gain % Life as Extra Max ES

    # 6. Unrevealed Desecrated Prefix Amulet (+3 All Spell Skills must stay min=3!)
    p6 = parse_clipboard_item(J4_06_UNREVEALED_DESECRATED_AMULET, bundle)
    q6 = build_item_trade_query(p6, bundle=bundle)["query"]
    assert p6.veiled_desecrated is True
    assert extract_unrevealed_desecrated_sides(J4_06_UNREVEALED_DESECRATED_AMULET) == (1, 0)
    assert q6["filters"]["misc_filters"]["filters"]["veiled"] == {"option": "true"}
    and_ids_6 = {f["id"]: f.get("value") for f in q6["stats"][0]["filters"]}
    assert and_ids_6["pseudo.pseudo_number_of_desecrated_prefix_mods"] == {"min": 1}
    assert and_ids_6["explicit.stat_124131830"] == {"min": 3}  # Never floored to 2!

    # 7. 4-mod Fractured Rare Wand (+5 Lightning Spell fractured stays fractured & min=5!)
    p7 = parse_clipboard_item(J4_07_FRACTURED_4MOD_WAND_OPEN_SUFFIX, bundle)
    q7 = build_item_trade_query(p7, bundle=bundle)["query"]
    assert p7.fractured is True
    assert q7["filters"]["misc_filters"]["filters"]["fractured_item"] == {"option": "true"}
    and_ids_7 = {f["id"]: f.get("value") for f in q7["stats"][0]["filters"]}
    assert and_ids_7["fractured.stat_1545858329"] == {"min": 5}  # Never floored to 4!

    # 8. Stack of Soul Cores (3/10 Soul Core of Azcapa)
    p8 = parse_clipboard_item(J4_08_SOUL_CORE_STACK, bundle)
    q8 = build_item_trade_query(p8, bundle=bundle)["query"]
    assert extract_stack_size(J4_08_SOUL_CORE_STACK) == (3, 10)
    assert q8["type"] == "Soul Core of Azcapa"


def test_journey4_triage_clipboard_dump_8_items_all_4_buckets() -> None:
    class StubEconomy:
        def get_price(self, query: str, league: str = "Forbidden Rites") -> CurrencyQuote | None:
            if "Azcapa" in query:
                return CurrencyQuote(
                    id="soul-core-of-azcapa",
                    name="Soul Core of Azcapa",
                    category="soulcores",
                    league=league,
                    exalted=140.0,
                    divine=0.30,
                    source="poe.ninja",
                )
            return None

        def convert_to_exalted(
            self, amount: float, currency: str, league: str = "Forbidden Rites"
        ) -> float:
            rates = {"exalted": 1.0, "divine": 460.0, "chaos": 35.0}
            return amount * rates.get(currency, 1.0)

    class StubTrade2:
        def __init__(self) -> None:
            self.queries: list[dict[str, Any]] = []

        def search(
            self, payload: dict[str, Any], league: str = "Forbidden Rites"
        ) -> dict[str, Any]:
            self.queries.append(payload)
            q = payload["query"]
            if q.get("type") == "Waystone (Tier 15)" or (
                q.get("filters", {}).get("type_filters", {}).get("filters", {}).get("category")
                == {"option": "map.waystone"}
            ):
                return {"id": "q_ws", "total": 25, "result": ["ws_1"]}
            if q.get("type") == "Spark":
                return {"id": "q_gem", "total": 18, "result": ["gem_1"]}
            if q.get("type") in ("Breach Precursor Tablet", "Breach Tablet"):
                return {"id": "q_tab", "total": 12, "result": ["tab_1"]}
            if q.get("name") == "Atziri's Disdain":
                return {"id": "q_atz", "total": 6, "result": ["atz_1"]}
            if q.get("type") == "Hexer's Robe":
                return {"id": "q_unid", "total": 15, "result": ["unid_1"]}
            return {"id": "q_rare", "total": 4, "result": ["rare_1"]}

        def fetch(self, ids: list[str], query_id: str = "") -> list[dict[str, Any]]:
            price_by_qid = {
                "q_ws": (2.0, "exalted"),
                "q_gem": (220.0, "exalted"),
                "q_tab": (38.0, "exalted"),
                "q_atz": (1.5, "divine"),
                "q_unid": (18.0, "exalted"),
                "q_rare": (3.0, "divine"),
            }
            amt, curr = price_by_qid.get(query_id, (20.0, "exalted"))
            return [
                {
                    "id": ids[0],
                    "listing": {
                        "price": {"amount": amt, "currency": curr},
                        "fee": 4500,
                        "account": {"name": "CleanSeller#1234", "online": None},
                    },
                    "item": {"baseType": "Item", "rarity": "Rare", "ilvl": 82},
                }
            ]

    stub_t2 = StubTrade2()
    advisor = TradeAdvisor(economy=StubEconomy(), trade2=stub_t2)
    report = advisor.triage_clipboard_dump(ALL_8_DUMP_ITEMS, min_sell_exalted=5.0)

    assert report.total_items == 8
    by_idx = {e.index: e for e in report.entries}

    # 0: Unidentified Tier 4 Hexer's Robe -> high_variance_id_or_unveil
    assert by_idx[0].bucket == "high_variance_id_or_unveil"
    # 1: Cut Lv20 5S Spark -> sell_instant
    assert by_idx[1].bucket == "sell_instant"
    assert by_idx[1].estimated_total_exalted == 220.0
    # 2: Breach Precursor Tablet -> sell_instant
    assert by_idx[2].bucket == "sell_instant"
    assert by_idx[2].estimated_total_exalted == 38.0
    # 3: 6-mod T15 Waystone (~2 Ex, 0 revives) -> salvage_or_vendor (3-to-1 Reforging Bench)
    assert by_idx[3].bucket == "salvage_or_vendor"
    assert "3-to-1" in by_idx[3].recommended_next_step
    # 4: Twice Corrupted Atziri's Disdain (+24% Chaos Res, 1.5 Div) -> sell_instant
    assert by_idx[4].bucket == "sell_instant"
    assert by_idx[4].estimated_total_exalted == 690.0
    # 5: Unrevealed Desecrated Prefix Amulet -> high_variance_id_or_unveil
    assert by_idx[5].bucket == "high_variance_id_or_unveil"
    assert "Well of Souls" in by_idx[5].recommended_next_step
    # 6: 4-mod Fractured +5 Lightning Wand (1P/1S open) -> craft_finish_ev
    assert by_idx[6].bucket == "craft_finish_ev"
    assert by_idx[6].open_prefixes == 1 and by_idx[6].open_suffixes == 1
    assert "Jawbone" in by_idx[6].recommended_next_step
    # 7: 3x Soul Core of Azcapa -> sell_instant via EconomyService (0 trade2 calls = 420 Ex)
    assert by_idx[7].bucket == "sell_instant"
    assert by_idx[7].stack_size == 3
    assert by_idx[7].unit_exalted == 140.0
    assert by_idx[7].estimated_total_exalted == 420.0
    assert len(stub_t2.queries) == 7  # Item #8 resolved via EconomyService with 0 trade2 calls!

    assert set(report.counts_by_bucket.keys()) == {
        "sell_instant",
        "craft_finish_ev",
        "high_variance_id_or_unveil",
        "salvage_or_vendor",
    }
    assert all(cnt >= 1 for cnt in report.counts_by_bucket.values())


# ===========================================================================
# JOURNEY 5 TESTS: Mid-Craft Bench Rescue (18-Div Half-Finished Rare)
# ===========================================================================


def test_journey5_mid_craft_rescue_whittling_100pct_snipes_life_brick() -> None:
    with ModPoolEngine() as eng:
        planner = CraftingPlanner(eng)

        for raw_txt in (J5_HALF_FINISHED_AMULET_CTRL_ALT_C, J5_HALF_FINISHED_AMULET_PLAIN_CTRL_C):
            parsed = parse_clipboard_item(raw_txt)
            state = eng.parse_state_from_item(parsed)

            # Exact mod_id, display_tier, required_level, and fractured status on BOTH formats!
            resolved_summary = [
                (m.mod_id, m.affix_side, m.display_tier, m.required_level, m.fractured)
                for m in state.mods
            ]
            assert resolved_summary == [
                ("IncreasedSpirit5", AffixSide.PREFIX, 1, 54, False),
                ("IncreasedEnergyShield10", AffixSide.PREFIX, 1, 80, False),
                ("IncreasedLife1", AffixSide.PREFIX, 9, 1, False),
                ("GlobalSpellGemsLevel3", AffixSide.SUFFIX, 1, 75, True),
                ("ColdResist8", AffixSide.SUFFIX, 1, 82, False),
            ]
            assert state.prefix_count == 3 and state.suffix_count == 2
            assert state.has_open_suffix(ItemRarity.RARE) is True
            assert state.has_open_prefix(ItemRarity.RARE) is False

            # Caller passes ONLY the missing target (`% increased maximum Energy Shield`)
            # with `protect_existing_targets=True` — existing T1 mods stay protected!
            evals = planner.evaluate_step(
                "Solar Amulet",
                82,
                ["% increased maximum Energy Shield"],
                starting_state=parsed,
                protect_existing_targets=True,
            )
            assert evals
            top = evals[0]
            assert "Omen of Whittling" in top.action_label
            assert top.notes is not None and "collateral_risk=0.0%" in top.notes
            assert "min_ilvl=1" in top.notes and "maximum Life" in top.notes

            # Raw Chaos Orb must surface exact 75.0% (3/4 non-fractured mods are T1!) brick risk
            raw_chaos = planner.evaluate_step(
                "Solar Amulet",
                82,
                ["% increased maximum Energy Shield"],
                starting_state=parsed,
                lever="Chaos Orb",
                protect_existing_targets=True,
            )[0]
            assert raw_chaos.notes is not None and "collateral_risk=75.0%" in raw_chaos.notes

            # Sinistral Crystallisation on full prefixes has 66.7% (2/3 prefixes are T1!) brick risk
            sinistral_cryst = planner.evaluate_step(
                "Solar Amulet",
                82,
                ["Global Armour, Evasion and Energy Shield"],
                starting_state=parsed,
                lever="Perfect Essence of Enhancement",
                omens=["Omen of Sinistral Crystallisation"],
                protect_existing_targets=True,
            )[0]
            assert (
                sinistral_cryst.notes is not None
                and "collateral_risk=66.7%" in sinistral_cryst.notes
            )

            # Full plan_craft from starting_state NEVER restarts and uses 0%-collateral Whittling
            plan = planner.plan_craft(
                "Solar Amulet",
                82,
                ["% increased maximum Energy Shield"],
                starting_state=parsed,
                n_sims=200,
            )
            assert plan.policy_steps
            assert "Omen of Whittling" in plan.policy_steps[0].recommended_action
            assert plan.policy_steps[0].on_miss != "restart_base"


def test_journey5_whittling_never_targets_fractured_lowest_level_mod() -> None:
    with ModPoolEngine() as eng:
        amulet_base = eng.resolve_base("Solar Amulet")
        all_mods = eng.get_mod_tiers("Amulets", "normal")
        frac_lvl1_life = next(m for m in all_mods if m.mod_id == "IncreasedLife1")  # req_lvl = 1
        brick_lvl6_dex = next(m for m in all_mods if m.mod_id == "Dexterity1")  # req_lvl = 1 or 6
        brick_lvl10_spell1 = next(
            m for m in all_mods if m.mod_id == "GlobalSpellGemsLevel1"
        )  # req_lvl = 10
        t1_es = next(m for m in all_mods if m.mod_id == "IncreasedEnergyShield10")  # req_lvl = 80
        cast_target = eng.resolve_target("Amulets", "Cast Speed", ilvl=82)

        assert frac_lvl1_life.required_level == 1
        assert brick_lvl10_spell1.required_level == 10
        assert brick_lvl6_dex.required_level >= 1

        # Fractured mod has req_lvl=1, brick has req_lvl=10, protected T1 ES has req_lvl=80:
        # Whittling MUST ignore fractured req_lvl=1 and 100% snipe req_lvl=10!
        st = CraftingState(
            base=amulet_base,
            ilvl=82,
            rarity=ItemRarity.RARE,
            mods=[
                CraftingMod.from_entry(frac_lvl1_life, fractured=True),
                CraftingMod.from_entry(brick_lvl10_spell1, fractured=False),
                CraftingMod.from_entry(t1_es, fractured=False),
            ],
        )
        res = eng.evaluate_action(
            st,
            set(cast_target.matching_mod_ids),
            "Greater Chaos Orb",
            ["Omen of Whittling"],
            protected_mod_ids={frac_lvl1_life.mod_id, t1_es.mod_id},
        )
        assert res.collateral_remove_probability == 0.0
        assert "min_ilvl=10" in res.notes


# ===========================================================================
# JOURNEY 6 TESTS: PoB2-Aware Waystone & Tablet <=50-Char Regex Generator
# ===========================================================================


def test_journey6_ci_archmage_crit_waystone_and_tablet_regex_under_50_chars() -> None:
    ci_archmage_crit = BuildStats(
        class_name="Sorceress",
        ascendancy_name="Stormweaver",
        level=96,
        main_skill="Spark",
        active_skills=["Spark", "Archmage", "Cast on Critical"],
        life=1.0,
        energy_shield=7200.0,
        mana=5100.0,
        fire_resist=76.0,
        cold_resist=76.0,
        lightning_resist=80.0,
        chaos_resist=100.0,
    )

    plan = build_waystone_regex_plan(
        ci_archmage_crit,
        avoid_keywords=[
            "Cannot Leech",
            "less Recovery Rate",
            "Elemental Weakness",
            "Penetrates",
            "Critical",
            "Stun Buildup",
        ],
        min_waystone_drop_pct=150,
        min_pack_size_pct=20,
        waystone_item=J4_04_WAYSTONE_6MOD_T15,
    )

    assert {"ci", "archmage_mom", "crit"}.issubset(set(plan.detected_archetypes))

    # Every emitted regex strictly satisfies PoE2's 50-character client limit!
    for rx in (plan.brick_regex, plan.safe_runnable_regex, plan.juicing_regex, plan.tablet_regex):
        assert rx.char_length == len(rx.regex)
        assert rx.char_length <= POE2_STASH_REGEX_MAX_CHARS
        assert rx.within_client_limit is True
        assert rx.false_negatives == 0

    assert plan.brick_regex.false_positives == 0
    assert plan.safe_runnable_regex.regex.startswith('!"')

    # Verify compiled brick regex matches EVERY tier & roll-endpoint of deadly mods in SQLite
    brick_pat = re.compile(plan.brick_regex.regex.strip('"'), re.I)
    with WaystoneRegexEngine() as eng:
        deadly_mtypes = {mt for h in plan.hazards for mt in h.mod_types}
        assert len(deadly_mtypes) >= 6
        for mt in deadly_mtypes:
            fam = eng._waystone_families[mt]
            for sample_tooltip in fam.rendered_samples:
                assert brick_pat.search(sample_tooltip) is not None, (
                    f"False negative on {mt}: {sample_tooltip!r}"
                )
        # Also matches custom "Monsters cannot be Leeched from"
        assert brick_pat.search("Monsters cannot be Leeched from") is not None

    # Verify Waystone item check & revive table (`0 mods=5` .. `6 mods=0 revives`)
    sched = {r.mod_count: r.revives_available for r in build_revive_schedule()}
    assert sched == {0: 5, 1: 5, 2: 4, 3: 3, 4: 2, 5: 1, 6: 0}
    assert waystone_revives_for_mod_count(0) == 5
    assert waystone_revives_for_mod_count(6) == 0

    assert plan.item_check is not None
    assert plan.item_check.is_brick is True
    assert plan.item_check.revives_available == 0
    assert plan.item_check.matches_brick_regex is True
    assert plan.item_check.matches_juicing_regex is True
    assert len(plan.item_check.brick_mods_hit) >= 5

    # Stress-test inspect_build dict input + extreme hazard count still fits <= 50 chars!
    extreme_plan = build_waystone_regex_plan(
        {
            "keystones": ["Acrobatics"],
            "skills": ["Archmage", "Comet"],
            "life": 1400,
            "energy_shield": 2800,
            "evasion": 9000,
            "crit_chance": 45.0,
            "is_leech": True,
        },
        include_dangerous=True,
    )
    assert extreme_plan.brick_regex.char_length <= POE2_STASH_REGEX_MAX_CHARS
    assert extreme_plan.safe_runnable_regex.char_length <= POE2_STASH_REGEX_MAX_CHARS


# ===========================================================================
# JOURNEY 7 TESTS: Broke Mapper 0.5 Div + 80k Gold -> 50 Div Compounding Ladder
# ===========================================================================


def test_journey7_budget_arbitrage_filter_and_compounding_ladder_mocked(tmp_path: Path) -> None:
    import json

    import httpx

    from poe2brain.http import GGGRateLimiter, Poe2HttpClient
    from poe2brain.trade.economy import EconomyService
    from poe2brain.trade.trade2_client import Trade2Client

    def _ninja_payload(
        lines: list[dict[str, Any]],
        items: list[dict[str, str]],
        *,
        div_ex: float = 460.0,
        div_chaos: float = 13.14,
    ) -> dict[str, Any]:
        return {
            "core": {
                "primary": "divine",
                "secondary": "chaos",
                "rates": {"exalted": div_ex, "chaos": div_chaos},
                "items": [
                    {"id": "divine", "name": "Divine Orb"},
                    {"id": "chaos", "name": "Chaos Orb"},
                    {"id": "exalted", "name": "Exalted Orb"},
                ],
            },
            "lines": lines,
            "items": items,
        }

    def _build_mock_economy_and_trade2(
        tpath: Path,
        *,
        scout_refs: list[dict[str, Any]],
        scout_items: list[dict[str, Any]],
        ninja_by_type: dict[str, dict[str, Any]],
    ) -> tuple[EconomyService, Trade2Client]:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/ReferenceCurrencies"):
                return httpx.Response(200, json=scout_refs)
            if path.endswith("/Items"):
                return httpx.Response(200, json=scout_items)
            if "/Items/" in path and path.endswith("/History"):
                return httpx.Response(200, json={"PriceHistory": []})
            if path.endswith("/economy/exchange/current/overview"):
                extype = request.url.params.get("type", "Currency")
                return httpx.Response(200, json=ninja_by_type[extype])
            if path.startswith("/api/trade2/exchange/poe2/"):
                body = json.loads(request.content.decode("utf-8"))
                hc = body["query"]["have"][0]
                wc = body["query"]["want"][0]
                return httpx.Response(200, json={"id": f"ex_{hc}_{wc}", "total": 0, "result": []})
            return httpx.Response(404, json={"error": "not found"})

        limiter = GGGRateLimiter(safety_buffer=0.0, clock=lambda: 1000.0, sleep_sync=lambda _: None)
        http_client = Poe2HttpClient(
            rate_limiter=limiter, sync_transport=httpx.MockTransport(handler)
        )
        econ = EconomyService(
            http_client=http_client,
            cache_dir=tpath / "econ_cache",
            ninja_exchange_types=tuple(ninja_by_type.keys()),
        )
        t2 = Trade2Client(http_client=http_client, economy=econ, cache_dir=tpath / "t2_cache")
        return econ, t2

    scout_refs = [
        {"ApiId": "exalted", "RelativePrice": 1.0},
        {"ApiId": "chaos", "RelativePrice": 35.0},
        {"ApiId": "divine", "RelativePrice": 460.0},
    ]
    scout_items = [
        {
            "ItemId": 1,
            "ApiId": "exalted",
            "Text": "Exalted Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 1.0,
        },
        {
            "ItemId": 2,
            "ApiId": "chaos",
            "Text": "Chaos Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 35.0,
        },
        {
            "ItemId": 3,
            "ApiId": "divine",
            "Text": "Divine Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 460.0,
        },
    ]
    ninja_by_type = {
        "Currency": _ninja_payload(
            lines=[
                {
                    "id": "divine",
                    "primaryValue": 1.0,
                    "volumePrimaryValue": 9000.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "chaos",
                    "primaryValue": 35.0 / 460.0,
                    "volumePrimaryValue": 9000.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "exalted",
                    "primaryValue": 1.0 / 460.0,
                    "volumePrimaryValue": 500.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "orb-of-extraction",
                    "primaryValue": 1.5,
                    "volumePrimaryValue": 120.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "artificers",
                    "primaryValue": 10.0 / 460.0,
                    "volumePrimaryValue": 80.0,
                    "maxVolumeCurrency": "exalted",
                },
                {
                    "id": "omen-of-whittling",
                    "primaryValue": 26.0,
                    "volumePrimaryValue": 950.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "concentrated-liquid-fear",
                    "primaryValue": 10.0 / 460.0,
                    "volumePrimaryValue": 0.8,
                    "maxVolumeCurrency": "exalted",
                },
                {
                    "id": "concentrated-liquid-suffering",
                    "primaryValue": 150.0 / 460.0,
                    "volumePrimaryValue": 8.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "refined-adaptive-catalyst",
                    "primaryValue": 140.0 / 460.0,
                    "volumePrimaryValue": 35.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "refined-neural-catalyst",
                    "primaryValue": 220.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "refined-carapace-catalyst",
                    "primaryValue": 220.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "refined-flesh-catalyst",
                    "primaryValue": 220.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "chaos",
                },
                {
                    "id": "refined-necrotic-catalyst",
                    "primaryValue": 400.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "refined-tuls-catalyst",
                    "primaryValue": 400.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "refined-eshs-catalyst",
                    "primaryValue": 400.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "refined-xophs-catalyst",
                    "primaryValue": 500.0 / 460.0,
                    "volumePrimaryValue": 30.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "refined-sibilant-catalyst",
                    "primaryValue": 2800.0 / 460.0,
                    "volumePrimaryValue": 80.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "refined-reaver-catalyst",
                    "primaryValue": 2600.0 / 460.0,
                    "volumePrimaryValue": 80.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "masterwork-rune",
                    "primaryValue": 2.2,
                    "volumePrimaryValue": 240.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "greater-iron-rune",
                    "primaryValue": 20.0 / 460.0,
                    "volumePrimaryValue": 5.0,
                    "maxVolumeCurrency": "exalted",
                },
                {
                    "id": "perfect-iron-rune",
                    "primaryValue": 6.2,
                    "volumePrimaryValue": 95.0,
                    "maxVolumeCurrency": "divine",
                },
                {
                    "id": "greater-storm-rune",
                    "primaryValue": 15.0 / 460.0,
                    "volumePrimaryValue": 4.0,
                    "maxVolumeCurrency": "exalted",
                },
                {
                    "id": "perfect-storm-rune",
                    "primaryValue": 6.8,
                    "volumePrimaryValue": 90.0,
                    "maxVolumeCurrency": "divine",
                },
            ],
            items=[
                {"id": "orb-of-extraction", "name": "Orb of Extraction"},
                {"id": "artificers", "name": "Artificer's Orb"},
                {"id": "omen-of-whittling", "name": "Omen of Whittling"},
                {"id": "concentrated-liquid-fear", "name": "Concentrated Liquid Fear"},
                {"id": "concentrated-liquid-suffering", "name": "Concentrated Liquid Suffering"},
                {"id": "refined-adaptive-catalyst", "name": "Refined Adaptive Catalyst"},
                {"id": "refined-neural-catalyst", "name": "Refined Neural Catalyst"},
                {"id": "refined-carapace-catalyst", "name": "Refined Carapace Catalyst"},
                {"id": "refined-flesh-catalyst", "name": "Refined Flesh Catalyst"},
                {"id": "refined-necrotic-catalyst", "name": "Refined Necrotic Catalyst"},
                {"id": "refined-tuls-catalyst", "name": "Refined Tul's Catalyst"},
                {"id": "refined-eshs-catalyst", "name": "Refined Esh's Catalyst"},
                {"id": "refined-xophs-catalyst", "name": "Refined Xoph's Catalyst"},
                {"id": "refined-sibilant-catalyst", "name": "Refined Sibilant Catalyst"},
                {"id": "refined-reaver-catalyst", "name": "Refined Reaver Catalyst"},
                {"id": "masterwork-rune", "name": "Masterwork Rune"},
                {"id": "greater-iron-rune", "name": "Greater Iron Rune"},
                {"id": "perfect-iron-rune", "name": "Perfect Iron Rune"},
                {"id": "greater-storm-rune", "name": "Greater Storm Rune"},
                {"id": "perfect-storm-rune", "name": "Perfect Storm Rune"},
            ],
            div_ex=460.0,
            div_chaos=460.0 / 35.0,
        )
    }

    econ, t2 = _build_mock_economy_and_trade2(
        tmp_path,
        scout_refs=scout_refs,
        scout_items=scout_items,
        ninja_by_type=ninja_by_type,
    )
    with ArbitrageScanner(economy=econ, trade2=t2) as scanner:
        # 1. Budget scan strictly blocks 26-Div Omen of Whittling & high-Gold plays
        budget_rep = scanner.scan(
            max_investment_divine=0.55,
            max_gold_per_cycle=5_000,
            min_volume_divine=0.5,
            include_live_exchange=False,
        )
        assert budget_rep.opportunities
        for opp in budget_rep.opportunities:
            assert opp.input_cost_exalted <= 0.55 * 460.0 + 1e-6
            assert opp.gold_per_cycle_est <= 5_000
            assert "whittling" not in opp.id

        # 2. Compounding ladder grows 250 Ex (~0.54 Div) -> >= 50 Div inside 80,000 Gold!
        ladder = scanner.build_compounding_ladder(
            starting_exalted=250.0,
            gold_budget=80_000,
            target_divine=50.0,
        )
        assert ladder.target_reached is True
        assert ladder.final_bankroll_divine >= 50.0
        assert ladder.total_gold_spent <= 80_000
        assert ladder.remaining_gold >= 0
        assert len(ladder.rungs) >= 3
        for r in ladder.rungs:
            assert r.unit_cost_exalted <= r.starting_bankroll_exalted + 1e-6
            assert r.ending_bankroll_exalted > r.starting_bankroll_exalted


# ===========================================================================
# LIVE INTEGRATION TESTS (`POE2_LIVE_TESTS=1`, <= 4 trade2 HTTP queries total!)
# ===========================================================================


@pytest.mark.skipif(not LIVE_ENABLED, reason="Set POE2_LIVE_TESTS=1 for live Forbidden Rites tests")
def test_live_journeys_4_and_7_end_to_end() -> None:
    # Live Journey 4 subset: 3 live trade2 queries (Unid Tier 4 Robe, Lv20 5S Spark, Breach Tablet)
    # + 1 EconomyService stackable (3x Soul Core of Azcapa = 0 trade2 queries) -> 3 trade2 queries!
    advisor = TradeAdvisor()
    live_dump = [
        J4_01_UNID_TIER4_ROBE,
        J4_02_CUT_LV20_5S_GEM,
        J4_03_PRECURSOR_TABLET,
        J4_08_SOUL_CORE_STACK,
    ]
    triage = advisor.triage_clipboard_dump(
        live_dump, league="Forbidden Rites", min_sell_exalted=5.0
    )
    assert triage.total_items == 4
    assert triage.total_estimated_exalted > 50.0
    assert triage.entries[0].bucket == "high_variance_id_or_unveil"
    assert triage.entries[0].trade_url is not None
    assert triage.entries[1].unit_exalted is not None and triage.entries[1].unit_exalted > 0
    assert triage.entries[2].unit_exalted is not None and triage.entries[2].unit_exalted > 0
    assert triage.entries[3].stack_size == 3
    assert triage.entries[3].price_source.startswith("economy:")

    # Live Journey 7 against real Forbidden Rites poe2scout + poe.ninja economy (0 trade2 calls!)
    with ArbitrageScanner() as scanner:
        budget_scan = scanner.scan(
            league="Forbidden Rites",
            max_investment_divine=0.55,
            max_gold_per_cycle=10_000,
            min_volume_divine=0.5,
            min_roi_pct=4.0,
            include_live_exchange=False,
        )
        assert len(budget_scan.opportunities) >= 1
        for o in budget_scan.opportunities:
            assert o.input_cost_exalted <= 0.55 * budget_scan.divine_exalted + 1e-6
            assert o.gold_per_cycle_est <= 10_000

        ladder = scanner.build_compounding_ladder(
            starting_exalted=250.0,
            gold_budget=80_000,
            target_divine=50.0,
            league="Forbidden Rites",
        )
        assert ladder.rungs
        assert ladder.final_bankroll_divine > ladder.starting_divine * 5.0
        assert ladder.total_gold_spent <= 80_000
        for r in ladder.rungs:
            assert r.unit_cost_exalted <= r.starting_bankroll_exalted + 1e-6
