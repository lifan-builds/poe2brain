# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from poe2brain.config import POE2_PATCH
from poe2brain.knowledge import (
    GAMEDATA_TABLES,
    GamedataDB,
    KnowledgeService,
    MetaService,
    sanitize_fts5_query,
)
from poe2brain.models.knowledge import (
    BaseItemSummary,
    GemSummary,
    KnowledgeRecord,
    UniqueSummary,
)


@pytest.fixture(scope="module")
def service() -> KnowledgeService:
    return KnowledgeService()


def test_gamedata_db_readonly_and_health(service: KnowledgeService) -> None:
    health = service.health_check()
    assert health.is_healthy is True
    assert health.patch == POE2_PATCH == "0.5.5"
    assert health.client_version == "4.5.4.3"
    for table in GAMEDATA_TABLES:
        assert health.table_counts[table] > 0

    assert health.table_counts["mechanics_facts"] >= 49
    assert health.table_counts["keywords"] >= 700
    assert health.table_counts["gems"] >= 1700
    assert health.table_counts["uniques"] >= 400
    assert health.table_counts["base_items"] >= 4000
    assert health.table_counts["augments"] >= 300
    assert health.table_counts["crafting_levers"] >= 290
    assert health.table_counts["runeforge_recipes"] >= 800

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        service.db.connection.execute("CREATE TABLE forbidden_write (id INTEGER)")


def test_gamedata_db_auto_sync_when_missing(tmp_path: Path) -> None:
    target_db = tmp_path / "poe2_test.sqlite"
    fake_sync = tmp_path / "fake_sync.py"
    fake_sync.write_text(
        "import argparse, sqlite3\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--out', required=True)\n"
        "args = p.parse_args()\n"
        "conn = sqlite3.connect(args.out)\n"
        "conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')\n"
        "conn.execute(\"INSERT INTO meta VALUES ('patch', '0.5.5')\")\n"
        "conn.commit()\n"
        "conn.close()\n",
        encoding="utf-8",
    )

    with GamedataDB(target_db, auto_sync=True, sync_script=fake_sync) as db:
        assert target_db.is_file()
        assert db.get_meta()["patch"] == "0.5.5"

    missing_db = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        GamedataDB(missing_db, auto_sync=False).connect()


@pytest.mark.parametrize(
    "tricky_query",
    [
        "+5 to Level",
        "+5 lightning",
        "100% Chaos",
        "block 75%",
        "Chaos Inoculation",
        "Omen's",
        "Rakiata's Flow",
        'AND OR NOT NEAR () : * ^ - % "',
        "armor defense homogenizing scepter jeweler's",
        "",
        "   ",
        "+++---%%%:::()\"'",
    ],
)
def test_fts5_special_character_safety(service: KnowledgeService, tricky_query: str) -> None:
    results = service.search(tricky_query, limit=8)
    assert isinstance(results, list)
    for rec in results:
        assert isinstance(rec, KnowledgeRecord)

    if any(ch.isalnum() for ch in tricky_query):
        assert sanitize_fts5_query(tricky_query) != ""
        assert len(results) > 0
    else:
        assert sanitize_fts5_query(tricky_query) == ""
        assert results == []


def test_search_across_categories_and_us_uk_spelling(service: KnowledgeService) -> None:
    plus_five = service.search("+5 lightning", limit=8)
    assert any("+5" in r.title and "Lightning" in r.title for r in plus_five)

    omen_hits = service.search("Omen's", limit=8)
    assert any("Omen" in r.title for r in omen_hits)

    us_homog = service.search("homogenizing omen", limit=6)
    uk_homog = service.search("homogenising omen", limit=6)
    assert any("Homogenising" in r.title for r in us_homog)
    assert any("Homogenising" in r.title for r in uk_homog)

    for cat, q, expected_kind in [
        ("mechanic_fact", "deflection", "mechanic_fact"),
        ("keyword", "Flammability", "keyword"),
        ("gem", "Spark", "gem"),
        ("unique", "Astramentis", "unique"),
        ("base_item", "Feathered Raiment", "base_item"),
        ("augment", "Iron Rune", "augment"),
        ("crafting_lever", "Omen of Whittling", "crafting_lever"),
        ("runeforge_recipe", "Runic Fork", "runeforge_recipe"),
    ]:
        hits = service.search(q, category=cat, limit=5)
        assert hits, f"Expected search hits for query={q!r} category={cat!r}"
        assert all(h.kind == expected_kind for h in hits)


def test_anti_hallucination_mechanics_explanations(service: KnowledgeService) -> None:
    # 1. Damage Conversion: two-step only, post-conversion scaling only (no double-dipping)
    conv = service.explain_mechanic("Damage Conversion")
    assert any(f.slug == "damage_conversion_two_step" for f in conv.facts)
    conv_fact = next(f for f in conv.facts if f.slug == "damage_conversion_two_step")
    assert "two-step" in conv_fact.poe2_live_rule.lower()
    assert "0%" in conv_fact.poe2_live_rule or "only" in conv_fact.poe2_live_rule.lower()
    assert conv_fact.formula_expr is not None
    assert conv.keywords

    # 2. Passive Block 50% base cap (NOT 75%) + Active Raise Shield Heavy Stun
    block = service.explain_mechanic("block 75% cap")
    assert any(f.slug == "passive_block_cap_50_and_active_block" for f in block.facts)
    block_fact = next(f for f in block.facts if f.slug == "passive_block_cap_50_and_active_block")
    assert "50%" in block_fact.poe2_live_rule
    assert "75%" in block_fact.poe1_trap

    # 3. Energy Shield: Chaos = 2x ES (does NOT bypass), Bleed & Poison BOTH bypass ES
    es = service.explain_mechanic("chaos damage energy shield bleed poison")
    assert any(f.slug == "energy_shield_chaos_2x_and_bleed_poison_bypass" for f in es.facts)
    es_fact = next(
        f for f in es.facts if f.slug == "energy_shield_chaos_2x_and_bleed_poison_bypass"
    )
    assert "twice" in es_fact.poe2_live_rule.lower() or "2" in (es_fact.formula_expr or "")
    assert "bleed" in es_fact.poe2_live_rule.lower() and "poison" in es_fact.poe2_live_rule.lower()

    # 4. Grim Feast rework: Minion Revival, NOT ES overflow aura
    gf = service.explain_mechanic("Grim Feast")
    assert any(f.slug == "grim_feast_minion_revival_rework" for f in gf.facts)
    gf_fact = next(f for f in gf.facts if f.slug == "grim_feast_minion_revival_rework")
    assert "minion" in gf_fact.poe2_live_rule.lower()

    # 5. Support Gem 0.3+ tiered freedom vs Lineage 1-per-char limit
    sup = service.explain_mechanic("support gem limit lineage")
    slugs = {f.slug for f in sup.facts}
    assert "support_gems_0_3_tiered_freedom" in slugs or "lineage_supports_endgame" in slugs

    # 6. Homogenising Omen & Recombinators removed
    homog = service.explain_mechanic("Omen of Homogenising Exaltation")
    assert any(f.slug == "crafting_homogenising_and_recombinators_removed" for f in homog.facts)
    lever_hits = service.search("Omen of Homogenising Exaltation", category="crafting_lever")
    assert lever_hits
    assert lever_hits[0].metadata["is_active_drop_in_patch"] is False
    assert "REMOVED" in lever_hits[0].summary

    # 7. Abbreviation expansion ("CI")
    ci = service.explain_mechanic("CI")
    assert any(f.slug == "keystone_chaos_inoculation" for f in ci.facts)


def test_inspect_gem_kinds_tiers_and_lineage(service: KnowledgeService) -> None:
    # Active gem
    spark = service.inspect_gem("Spark")
    assert isinstance(spark, GemSummary)
    assert spark.gem_kind == "active"
    assert "spell" in spark.tags and "lightning" in spark.tags
    assert len(spark.recommended_supports) > 0

    # Spirit gem + Grim Feast 0.1 trap warning
    grim = service.inspect_gem("Grim Feast")
    assert grim.gem_kind == "spirit"
    assert "minion" in grim.tags
    assert grim.spirit_energy_rule is not None
    assert grim.warning is not None and "MINION" in grim.warning.upper()

    # Meta gem
    coc = service.inspect_gem("Cast on Critical")
    assert coc.gem_kind == "meta"
    assert coc.spirit_energy_rule is not None and "Energy" in coc.spirit_energy_rule

    # Tiered standard support (0.3+ unlimited copies across skills)
    pierce = service.inspect_gem("Pierce")
    assert pierce.gem_kind == "support"
    assert pierce.support_tier == 1
    assert pierce.max_copies_per_char is None
    assert pierce.tier_variants == ["Pierce I", "Pierce II", "Pierce III"]
    assert len(pierce.compatible_gems) > 0

    # Renamed 0.1 support gem -> resolved via internal metadata ID
    rapid = service.inspect_gem("Martial Tempo")
    assert rapid.name == "Rapid Attacks I"
    assert rapid.gem_kind == "support"
    assert rapid.resolved_from == "Martial Tempo"

    # Lineage support (strict 1 copy per character)
    rakiata = service.inspect_gem("Rakiata's Flow")
    assert rakiata.gem_kind == "lineage_support"
    assert rakiata.max_copies_per_char == 1
    assert "lineage" in rakiata.tags


def test_inspect_base_item_live_poe1_trap_and_expert_prefix(service: KnowledgeService) -> None:
    # Live PoE2 base + T1 mods at ilvl 82 vs ilvl 75
    wand_82 = service.inspect_base_item("Dueling Wand", ilvl=82)
    assert isinstance(wand_82, BaseItemSummary)
    assert wand_82.found is True
    assert wand_82.is_live_poe2 is True
    assert wand_82.poe2db_slug == "Wands"
    assert any("+5 to Level" in m.stat_text for m in wand_82.top_suffixes)
    assert all(m.display_tier == 1 and m.required_level <= 82 for m in wand_82.top_prefixes)

    wand_75 = service.inspect_base_item("Dueling Wand", ilvl=75)
    assert not any("+5 to Level" in m.stat_text for m in wand_75.top_suffixes)

    # PoE1 base trap ("Vaal Regalia", "Stygian Vise")
    regalia = service.inspect_base_item("Vaal Regalia")
    assert regalia.found is False
    assert regalia.is_live_poe2 is False
    assert regalia.is_poe1_base_trap is True
    assert regalia.warning is not None and "POE1 BASE TRAP" in regalia.warning
    assert "Feathered Raiment" in regalia.suggested_bases
    assert len(regalia.top_prefixes) > 0

    vise = service.inspect_base_item("Stygian Vise")
    assert vise.found is False
    assert vise.is_poe1_base_trap is True
    assert vise.poe2db_slug == "Belts"
    assert len(vise.suggested_bases) > 0

    # Removed Patch 0.1 "Expert " / "Advanced " prefix trap
    expert_bow = service.inspect_base_item("Expert Dualstring Bow")
    assert expert_bow.found is False
    assert expert_bow.is_live_poe2 is False
    assert expert_bow.is_stale_prefix_trap is True
    assert expert_bow.warning is not None and "Expert" in expert_bow.warning
    assert "Gemini Bow" in expert_bow.suggested_bases

    expert_wand = service.inspect_base_item("Expert Hexblaster Wand")
    assert expert_wand.found is False
    assert expert_wand.is_stale_prefix_trap is True
    assert "Dueling Wand" in expert_wand.suggested_bases

    # Word-boundary regression: "Orb of Scouring" must NOT match "ring" -> Rings!
    scour = service.inspect_base_item("Orb of Scouring")
    assert scour.found is False
    assert scour.is_live_poe2 is False
    assert scour.poe2db_slug is None
    assert scour.item_class != "Ring"
    assert scour.suggested_bases == []

    homog_item = service.inspect_base_item("Omen of Homogenising Exaltation")
    assert homog_item.is_live_poe2 is False
    assert homog_item.warning is not None


def test_inspect_unique_and_augment_and_runeforge(service: KnowledgeService) -> None:
    astra = service.inspect_unique("Astramentis")
    assert isinstance(astra, UniqueSummary)
    assert astra.name == "Astramentis"
    assert astra.base_type == "Stellar Amulet"
    assert astra.item_class == "Amulet"
    assert astra.is_live_poe2 is True
    assert any("Attributes" in line for line in astra.explicits)

    # Verify PoB2 variant metadata lines are stripped from explicits
    mageblood = service.inspect_unique("Mageblood")
    assert mageblood.has_variants is True
    assert not any(line.startswith("Has Alt Variant") for line in mageblood.explicits)
    assert not any(line.startswith("Selected Variant") for line in mageblood.explicits)

    # Augments: Runes (overwritable + Shaman Bonded), Socket-bound Soul Cores, Azmeri Idols
    iron_runes = service.inspect_augment("Iron Rune")
    assert iron_runes
    iron = next(a for a in iron_runes if a.name == "Iron Rune")
    assert iron.augment_type == "Rune"
    assert iron.is_socket_bound is False
    assert iron.can_overwrite_in_socket is True
    assert len(iron.slot_effects) > 0
    assert len(iron.shaman_bonded_effects) > 0

    atziri_core = service.inspect_augment("Atziri's Soul Core of Inoculation")
    assert atziri_core
    assert atziri_core[0].is_socket_bound is True
    assert atziri_core[0].can_overwrite_in_socket is False
    assert atziri_core[0].limit_count == 1

    idols = service.inspect_augment("Carved Cunning")
    assert idols and idols[0].augment_type == "Idol"
    assert len(idols[0].shaman_bonded_effects) > 0

    # Runeforging: Verisium Anvil base transforms + Kalguuran Remnant recipes
    fork_recipes = service.search_runeforge("Runic Fork")
    assert len(fork_recipes) >= 3
    assert all(r.output_item == "Runemastered Runic Fork" for r in fork_recipes)
    assert any("Projectiles" in imp for r in fork_recipes for imp in r.result_implicits)

    remnant_recipes = service.search_runeforge("Adaptive Alloy")
    assert remnant_recipes
    assert remnant_recipes[0].category == "kalguuran_remnant_recipe"
    assert remnant_recipes[0].output_item != "Adaptive"
    assert len(remnant_recipes[0].rune_sequence_ids) >= 3

    pchaos_remnants = service.search_runeforge("Perfect Chaos Orb")
    assert pchaos_remnants
    assert any(
        r.category == "kalguuran_remnant_recipe" and "Perfect Chaos Orb" in r.output_item
        for r in pchaos_remnants
    )


def test_search_classes_and_ascendancies_via_knowledge_service(service: KnowledgeService) -> None:
    assert MetaService is not None
    if not service.list_ascendancies():
        pytest.skip("PoB2 TreeData/0_5 not bootstrapped")

    for q_asc, expected_class in [
        ("Gemling Legionnaire", "Mercenary"),
        ("Martial Artist", "Monk"),
        ("Disciple of Varashta", "Sorceress"),
        ("Abyssal Lich", "Witch"),
        ("Spirit Walker", "Huntress"),
        ("Stormweaver", "Sorceress"),
    ]:
        hits = service.search(q_asc, limit=5)
        assert hits, f"Expected hits for {q_asc}"
        assert hits[0].kind == "ascendancy"
        assert hits[0].title == q_asc
        assert hits[0].category == expected_class

    notable_hits = service.search("Way of the Stonefist", category="ascendancy", limit=5)
    assert notable_hits and notable_hits[0].title == "Martial Artist"

    cls_hits = service.search("Sorceress", category="class", limit=5)
    assert cls_hits and cls_hits[0].kind == "class" and cls_hits[0].title == "Sorceress"
    assert set(cls_hits[0].metadata["ascendancies"]) == {
        "Stormweaver",
        "Chronomancer",
        "Disciple of Varashta",
    }
