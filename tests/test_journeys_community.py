# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""End-to-end tests for community-sourced Reddit, Bilibili, and YouTube PoE2 0.5.5 user journeys."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from poe2brain.crafting.profit import CraftProfitScanner
from poe2brain.knowledge.service import KnowledgeService
from poe2brain.knowledge.waystone_regex import build_waystone_regex_plan
from poe2brain.mcp_server import (
    poe2_craft_odds,
    poe2_waystone_regex,
    split_multi_item_clipboard_dump,
)
from poe2brain.models.items import ItemRarity
from poe2brain.models.trade import CurrencyQuote
from poe2brain.pob.service import BuildService
from poe2brain.trade.advisor import TradeAdvisor
from poe2brain.trade.economy import EconomyService
from poe2brain.trade.item_parser import parse_clipboard_item

CORPUS_PATH = (
    Path(__file__).resolve().parent / "fixtures/sample_builds_0.5.5.json.gz"
)


def test_generated_unique_jewels_and_strict_title_lookup() -> None:
    ks = KnowledgeService()
    for query, expected_name in (
        ("Megalomaniac", "Megalomaniac"),
        ("Against the Darkness", "Against the Darkness"),
        ("From Nothing", "From Nothing"),
        ("Prism of Belief", "Prism of Belief"),
        ("Heart of the Well", "Heart of the Well"),
        ("妄想症", "Megalomaniac"),
        ("力抗黑暗", "Against the Darkness"),
        ("无中生有", "From Nothing"),
        ("信仰棱镜", "Prism of Belief"),
    ):
        insp = ks.inspect_unique(query)
        assert insp.name == expected_name

    with pytest.raises(LookupError):
        ks.inspect_unique("Nonexistent PoE1 Darkness Ring XYZ")


def test_augments_filter_dnt_and_zh_runeforge() -> None:
    ks = KnowledgeService()
    augs = ks.inspect_augment("符文", limit=12)
    assert augs
    assert all("DNT" not in a.name for a in augs)
    assert all(a.is_live_poe2 for a in augs)

    recipes = ks.search_runeforge("维里西姆", limit=5)
    assert len(recipes) >= 1


def test_bilibili_zh_clipboard_spaced_headers_and_dump_triage() -> None:
    zh_boots = """物品类别: 鞋子
稀 有 度: 稀有
灵魂 步伐
丝绸长靴
--------
能量护盾: 148 (augmented)
--------
需求:
等级: 72
智慧: 112
--------
物品等级: 82
--------
移动速度提高 35%
+42 最大生命
+38% 火焰抗性"""
    zh_divine = """物品类别: 可堆叠通货
稀 有 度: 通货
神圣石
--------
堆叠数量: 1/20"""

    parsed_boots = parse_clipboard_item(zh_boots)
    assert parsed_boots.item_class == "Boots"
    assert parsed_boots.rarity == ItemRarity.RARE
    assert parsed_boots.item_level == 82
    assert parsed_boots.energy_shield == 148

    parsed_div = parse_clipboard_item(zh_divine)
    assert parsed_div.item_class == "Stackable Currency"
    assert parsed_div.rarity == ItemRarity.CURRENCY
    assert parsed_div.base_type == "Divine Orb"

    dump = f"{zh_boots}\n\n{zh_divine}"
    chunks = split_multi_item_clipboard_dump(dump)
    assert len(chunks) == 2

    advisor = TradeAdvisor()
    report = advisor.triage_clipboard_dump(dump, live_trade=False)
    assert report.total_items == 2


def test_magic_precursor_tablet_parsing_and_tablet_relic_craft_odds() -> None:
    tablet_txt = """Item Class: Tablets
Rarity: Magic
Surpassing Expedition Precursor Tablet of the Verisium
--------
Item Level: 82
--------
10 uses remaining
--------
Maps contain an Expedition Encounter
28% increased Rarity of Items found in your Maps"""
    parsed = parse_clipboard_item(tablet_txt)
    assert parsed.rarity == ItemRarity.MAGIC
    assert parsed.base_type == "Expedition Tablet"

    for base_q, tgt in (
        ("Precursor Tablet", "Rarity"),
        ("Expedition Precursor Tablet", "Logbooks"),
        ("裂隙碑牌", "Wombgifts"),
        ("Tapestry Relic", "Honour Resistance"),
        ("Sapphire", "Energy Shield"),
    ):
        res = poe2_craft_odds(base=base_q, ilvl=82, lever="Orb of Transmutation", target=tgt)
        assert res.get("ok", True) is True
        assert float(res["hit_probability"]) > 0.0


def test_journey11_vendor_sekhema_relic_and_tablet_regex_under_50_chars() -> None:
    plan = build_waystone_regex_plan(
        archetypes=["ci", "archmage_mom"],
        tablet_preset="expedition_verisium",
        vendor_preset="campaign_caster",
        relic_preset="sekhema_quant_farm",
    )
    for rx in (
        plan.brick_regex,
        plan.safe_runnable_regex,
        plan.tablet_regex,
        plan.vendor_regex,
        plan.relic_regex,
    ):
        assert rx is not None
        assert rx.within_client_limit is True
        assert rx.char_length <= 50

    mcp_out = poe2_waystone_regex(
        archetypes="ci",
        vendor_preset="salvage_sockets",
        relic_preset="sekhema_starter",
    )
    assert mcp_out["vendor_regex"]["within_client_limit"] is True
    assert mcp_out["relic_regex"]["within_client_limit"] is True


def test_journey14_boss_ev_and_zh_economy_and_tablet_jewel_profit_recipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    econ = EconomyService()
    mock_quotes = {
        "divine orb": 430.0,
        "exalted orb": 1.0,
        "fracturing orb": 2900.0,
        "an audience with the king": 6.0,
        "breachstone": 1400.0,
        "simulacrum": 1420.0,
        "faded crisis fragment": 2700.0,
        "ancient crisis fragment": 850.0,
        "weathered crisis fragment": 990.0,
        "expedition logbook": 80.0,
        "olroth's resolve": 9900.0,
        "svalinn": 400.0,
        "controlled metamorphosis": 930.0,
        "morior invictus": 1080.0,
        "against the darkness": 2300.0,
        "the adorned": 4650.0,
        "from nothing": 115.0,
        "ingenuity": 2.0,
        "megalomaniac": 2.0,
        "melting maelstrom": 30.0,
        "prism of belief": 1.0,
        "sacred flame": 120.0,
        "hand of wisdom and action": 250.0,
        "temporalis": 2500000.0,
    }

    class _FakeRates:
        divine_exalted = 430.0
        chaos_exalted = 35.0

    monkeypatch.setattr(econ, "get_reference_rates", lambda *a, **kw: _FakeRates())

    def _fake_get_price(query_or_id: str, *a: object, **kw: object) -> CurrencyQuote | None:
        from poe2brain.knowledge.zh_aliases import translate_zh_query

        norm = translate_zh_query(query_or_id.strip()).lower()
        val = mock_quotes.get(norm)
        if val is None:
            return None
        return CurrencyQuote(
            id=norm.replace(" ", "-"),
            name=query_or_id,
            category="currency",
            league="Forbidden Rites",
            exalted=val,
            chaos=val / 35.0,
            divine=val / 430.0,
        )

    monkeypatch.setattr(econ, "get_price", _fake_get_price)
    assert econ.get_price("神圣石") is not None
    assert econ.get_price("破裂宝珠") is not None

    boss_rep = econ.compare_boss_ev()
    assert len(boss_rep["encounters"]) == 7
    olroth = next(e for e in boss_rep["encounters"] if e["id"] == "olroth_expedition")
    assert olroth["entry_cost_exalted"] == 80.0
    assert olroth["jackpot_to_ticket_ratio"] > 100.0

    # Verify new Tablet & Sapphire Jewel profit recipes simulate cleanly offline
    scanner = CraftProfitScanner()
    report = scanner.scan(live_trade=False)
    ids = {r.recipe_id for r in report.evaluations}
    assert "expedition_tablet_verisium_roll" in ids
    assert "sapphire_es_caster_jewel" in ids


def test_journeys_12_and_13_pob2_weapon_swap_megalomaniac_and_rune_optimizer() -> None:
    with gzip.open(CORPUS_PATH, "rt", encoding="utf-8") as f:
        builds = json.load(f).get("builds", [])
    code = next(b["pob_code"] for b in builds if b.get("pob_code"))

    svc = BuildService()
    try:
        svc.load_build(code, slot="comm_j12")
        # Equip a Weapon 1 Swap to exercise Weapon Swap Safety Audit
        svc.bridge.call(
            "equip_item",
            {
                "slot": "comm_j12",
                "targetSlot": "Weapon 1 Swap",
                "itemText": "Rarity: Normal\nWooden Club\n",
            },
        )
        audit = svc.audit_build(slot="comm_j12")
        assert audit.attribute_margins
        assert audit.weapon_swap_audit is not None
        assert audit.weapon_swap_audit.has_weapon_swap is True
        assert audit.weapon_swap_audit.alternate_full_dps > 0

        # Journey 13a: Megalomaniac Build-Specific Finder + clickable Trade2 URL
        mega = svc.find_megalomaniac_upgrades(slot="comm_j12", top_notables=15, top_combos=5)
        assert mega.matched_trade2_stat_count >= 10
        assert len(mega.top_2_notable_combos) >= 3
        assert mega.trade_url and "pathofexile.com/trade2/search/poe2/" in mega.trade_url

        # Journey 13b: Rune & Soul Core Socket Optimizer with PoB2 verification
        runes = svc.optimize_rune_sockets(
            slot="comm_j12", objective="balanced", offline_prices=True
        )
        assert runes.total_available_sockets > 0
        assert len(runes.recommendations) >= 2
        assert runes.pob_verified_dps_delta > 0
        assert runes.pob_verified_ehp_delta > 0
    finally:
        svc.close()


def test_conditional_support_and_speed_padding_detection() -> None:
    from poe2brain.models.builds import BuildStats
    from poe2brain.pob.service import _detect_conditional_support_padding

    skills_padded = {
        "socketGroups": [
            {
                "enabled": True,
                "isMainSkill": True,
                "mainActiveSkillName": "Ice Shot",
                "gems": [
                    {"name": "Ice Shot", "enabled": True, "isSupport": False},
                    {"name": "Rageforged II", "enabled": True, "isSupport": True},
                    {"name": "Minion Pact I", "enabled": True, "isSupport": True},
                    {"name": "Ambush", "enabled": True, "isSupport": True},
                    {"name": "Martial Tempo", "enabled": True, "isSupport": True},
                ],
            }
        ]
    }
    stats_padded = BuildStats(
        attack_cast_speed=11.4,
        mana_regen=85.0,
        mana_per_second_cost=420.0,
        mana_sustainable=False,
    )
    flags = _detect_conditional_support_padding(skills_padded, stats_padded)
    joined = "\n".join(flags)
    assert "Rageforged II" in joined
    assert "Minion Pact I" in joined
    assert "Ambush" in joined
    assert "Martial Tempo" not in joined
    assert "11.40/s" in joined

    skills_clean = {
        "socketGroups": [
            {
                "enabled": True,
                "isMainSkill": True,
                "gems": [
                    {"name": "Spark", "enabled": True, "isSupport": False},
                    {"name": "Considered Casting", "enabled": True, "isSupport": True},
                ],
            }
        ]
    }
    stats_clean = BuildStats(
        attack_cast_speed=4.2,
        mana_regen=220.0,
        mana_per_second_cost=150.0,
        mana_sustainable=True,
    )
    assert _detect_conditional_support_padding(skills_clean, stats_clean) == []
