# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import pytest

from poe2brain.crafting import (
    CraftingMod,
    CraftingPlanner,
    CraftingState,
    ModPoolEngine,
    RemovedLeverError,
    exact_three_choice_unveil_prob,
)
from poe2brain.models.common import AffixSide
from poe2brain.models.items import ItemRarity, ModDomain, ParsedItem, ParsedMod


@pytest.fixture(scope="module")
def engine() -> ModPoolEngine:
    eng = ModPoolEngine()
    yield eng
    eng.close()


@pytest.fixture(scope="module")
def planner(engine: ModPoolEngine) -> CraftingPlanner:
    return CraftingPlanner(engine)


def test_tier_ordering_and_community_weights_on_wands(engine: ModPoolEngine) -> None:
    tiers = [
        m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_type == "WeaponSpellDamage"
    ]
    assert len(tiers) == 8
    tiers.sort(key=lambda m: m.display_tier)
    t1, t8 = tiers[0], tiers[-1]
    assert t1.display_tier == 1
    assert t1.internal_rank_asc == 8
    assert t1.required_level == 80
    assert t1.weight == 50
    assert t1.weight_confidence == "verified_community"
    assert t8.display_tier == 8
    assert t8.internal_rank_asc == 1
    assert t8.required_level == 1
    assert t8.weight > t1.weight


def test_wand_lightning_spell_skills_dynamic_tag_blocking_and_withered_wand(
    engine: ModPoolEngine,
) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    withered = engine.resolve_base("Withered Wand")

    lt_target = engine.resolve_target("Wands", "GlobalLightningSpellGemsLevelWeapon5", ilvl=82)
    chaos_target = engine.resolve_target("Wands", "GlobalChaosSpellGemsLevelWeapon5", ilvl=82)
    fire_prefix = engine.resolve_target("Wands", "FireDamagePrefixOnWeapon8", ilvl=82)
    spell_prefix = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82)

    # Withered Wand carries no_lightning_spell_mods -> +5 Lightning is 0.0, +5 Chaos > 0
    w_state = CraftingState(base=withered, ilvl=82, rarity=ItemRarity.RARE)
    res_lt_withered = engine.evaluate_action(
        w_state, set(lt_target.matching_mod_ids), "Exalted Orb"
    )
    res_ch_withered = engine.evaluate_action(
        w_state, set(chaos_target.matching_mod_ids), "Exalted Orb"
    )
    assert res_lt_withered.hit_probability == 0.0
    assert res_ch_withered.hit_probability > 0.0

    # Attuned Wand: before rolling +5 Lightning Spell Skills, Fire prefix is rollable
    a_state = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE)
    fire_before = engine.evaluate_action(
        a_state, set(fire_prefix.matching_mod_ids), "Exalted Orb", ["Omen of Sinistral Exaltation"]
    )
    spell_before = engine.evaluate_action(
        a_state, set(spell_prefix.matching_mod_ids), "Exalted Orb", ["Omen of Sinistral Exaltation"]
    )
    assert fire_before.hit_probability > 0.0

    # Rolling +5 Lightning Spell Skills injects adds_tags blocking Fire/Cold/Chaos/Phys caster mods
    lt_entry = lt_target.matching_entries[0]
    assert "no_fire_spell_mods" in lt_entry.adds_tags
    a_state.mods.append(CraftingMod.from_entry(lt_entry))

    fire_after = engine.evaluate_action(
        a_state, set(fire_prefix.matching_mod_ids), "Exalted Orb", ["Omen of Sinistral Exaltation"]
    )
    spell_after = engine.evaluate_action(
        a_state, set(spell_prefix.matching_mod_ids), "Exalted Orb", ["Omen of Sinistral Exaltation"]
    )
    assert fire_after.hit_probability == 0.0
    # Pool shrinkage boosts neutral Spell Damage prefix probability!
    assert spell_after.hit_probability > spell_before.hit_probability


def test_tiered_basic_orb_floors_and_gnawed_bone_ilvl_cap(engine: ModPoolEngine) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    normal_st = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.NORMAL)

    t1_spell = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82)
    low_mana = {
        m.mod_id for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "IncreasedMana1"
    }

    p_basic = engine.evaluate_action(
        normal_st, set(t1_spell.matching_mod_ids), "Orb of Transmutation"
    ).hit_probability
    p_greater = engine.evaluate_action(
        normal_st, set(t1_spell.matching_mod_ids), "Greater Orb of Transmutation"
    ).hit_probability
    p_perfect = engine.evaluate_action(
        normal_st, set(t1_spell.matching_mod_ids), "Perfect Orb of Transmutation"
    ).hit_probability
    assert 0.0 < p_basic < p_greater < p_perfect

    # Perfect Transmutation (floor=70) culls ilvl-1 IncreasedMana1 completely
    assert (
        engine.evaluate_action(normal_st, low_mana, "Perfect Orb of Transmutation").hit_probability
        == 0.0
    )

    # Gnawed Jawbone enforces max_item_level=64
    rare_82 = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE)
    with pytest.raises(ValueError, match="max item level 64"):
        engine.evaluate_action(rare_82, set(t1_spell.matching_mod_ids), "Gnawed Jawbone")


def test_whittling_lowest_ilvl_removal_and_ties(engine: ModPoolEngine) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    t1_lt = engine.resolve_target(
        "Wands", "GlobalLightningSpellGemsLevelWeapon5", ilvl=82
    ).matching_entries[0]
    t1_sp = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
    junk_int1 = next(
        m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "Intelligence1"
    )
    target_cast = engine.resolve_target("Wands", "IncreasedCastSpeed", ilvl=82)

    st = CraftingState(
        base=attuned,
        ilvl=82,
        rarity=ItemRarity.RARE,
        mods=[
            CraftingMod.from_entry(t1_lt),  # req_lvl 81
            CraftingMod.from_entry(t1_sp),  # req_lvl 80
            CraftingMod.from_entry(junk_int1),  # req_lvl 1
        ],
    )
    protected = {t1_lt.mod_id, t1_sp.mod_id}

    raw_chaos = engine.evaluate_action(
        st, set(target_cast.matching_mod_ids), "Chaos Orb", protected_mod_ids=protected
    )
    whittle_chaos = engine.evaluate_action(
        st,
        set(target_cast.matching_mod_ids),
        "Chaos Orb",
        ["Omen of Whittling"],
        protected_mod_ids=protected,
    )
    assert raw_chaos.collateral_remove_probability == pytest.approx(2.0 / 3.0)
    assert whittle_chaos.collateral_remove_probability == 0.0
    assert whittle_chaos.hit_probability > 0.0

    # Tie check: two ilvl-1 mods -> Whittling splits 50/50 across tied min_ilvl=1 mods
    mana1 = next(m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "IncreasedMana1")
    st.mods.append(CraftingMod.from_entry(mana1))
    whittle_tied = engine.evaluate_action(
        st,
        set(target_cast.matching_mod_ids),
        "Chaos Orb",
        ["Omen of Whittling"],
        protected_mod_ids={t1_lt.mod_id, t1_sp.mod_id, mana1.mod_id},
    )
    assert whittle_tied.collateral_remove_probability == pytest.approx(0.5)


def test_perfect_essence_crystallisation_and_abyss_mark_loop(engine: ModPoolEngine) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    t1_sp = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
    t1_mana = next(
        m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "IncreasedMana11"
    )
    junk_int1 = next(
        m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "Intelligence1"
    )

    st = CraftingState(
        base=attuned,
        ilvl=82,
        rarity=ItemRarity.RARE,
        mods=[
            CraftingMod.from_entry(t1_sp),  # Prefix (protected)
            CraftingMod.from_entry(t1_mana),  # Prefix (protected)
            CraftingMod.from_entry(junk_int1),  # Suffix (junk)
        ],
    )
    protected = {t1_sp.mod_id, t1_mana.mod_id}

    # Dextral Crystallisation restricts Perfect Essence removal strictly to the junk Suffix
    perf_sorc = engine.resolve_lever("Perfect Essence of Sorcery")
    forced_id = perf_sorc.forced_mods["Wands"]

    without_omen = engine.evaluate_action(
        st, {forced_id}, "Perfect Essence of Sorcery", protected_mod_ids=protected
    )
    with_dextral = engine.evaluate_action(
        st,
        {forced_id},
        "Perfect Essence of Sorcery",
        ["Omen of Dextral Crystallisation"],
        protected_mod_ids=protected,
    )
    assert without_omen.collateral_remove_probability == pytest.approx(2.0 / 3.0)
    assert with_dextral.collateral_remove_probability == 0.0
    assert with_dextral.hit_probability == 1.0

    # Essence of the Abyss + Dextral Crystallisation places Mark of the Abyssal Lord safely
    mark_eval = engine.evaluate_action(
        st,
        {"MarkOfTheAbyssalLord"},
        "Essence of the Abyss",
        ["Omen of Dextral Crystallisation"],
        protected_mod_ids=protected,
    )
    assert mark_eval.hit_probability == 1.0
    assert mark_eval.collateral_remove_probability == 0.0


def test_desecration_three_choice_unveil_echoes_and_omen_of_light(engine: ModPoolEngine) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    amanamu_mods = [
        m for m in engine.get_mod_tiers("Wands", "desecrated") if m.boss_tag == "amanamu"
    ]
    assert len(amanamu_mods) >= 3
    target_id = {amanamu_mods[0].mod_id}

    # Verify exact_three_choice_unveil_prob on uniform N-item pool equals 3/N
    n_pool = len(amanamu_mods)
    uniform_pool = [(m, 1000) for m in amanamu_mods]
    p3 = exact_three_choice_unveil_prob(uniform_pool, target_id)
    assert p3 == pytest.approx(min(1.0, 3.0 / n_pool), rel=1e-6)

    st = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE)
    res_liege = engine.evaluate_action(st, target_id, "Ancient Jawbone", ["Omen of the Liege"])
    res_liege_echoes = engine.evaluate_action(
        st, target_id, "Ancient Jawbone", ["Omen of the Liege", "Omen of Abyssal Echoes"]
    )
    assert res_liege.hit_probability == pytest.approx(p3, rel=1e-6)
    assert res_liege_echoes.hit_probability == pytest.approx(1.0 - (1.0 - p3) ** 2, rel=1e-6)

    # Second bone is blocked until Annul + Omen of Light removes the Desecrated mod (100%)
    t1_sp = engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
    st.mods.append(CraftingMod.from_entry(t1_sp))
    st.mods.append(CraftingMod.from_entry(amanamu_mods[1], is_desecrated=True))
    blocked_bone = engine.evaluate_action(st, target_id, "Ancient Jawbone")
    assert blocked_bone.hit_probability == 0.0

    light_annul = engine.evaluate_action(
        st,
        set(),
        "Orb of Annulment",
        ["Omen of Light"],
        protected_mod_ids={t1_sp.mod_id},
    )
    assert light_annul.hit_probability == 1.0
    assert light_annul.collateral_remove_probability == 0.0


def test_fracturing_orb_and_removed_levers_guard(
    engine: ModPoolEngine, planner: CraftingPlanner
) -> None:
    attuned = engine.resolve_base("Attuned Wand")
    mods_4 = [
        CraftingMod.from_entry(
            engine.resolve_target("Wands", "SpellDamageOnWeapon8_", ilvl=82).matching_entries[0]
        ),
        CraftingMod.from_entry(
            next(
                m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "IncreasedMana11"
            )
        ),
        CraftingMod.from_entry(
            next(m for m in engine.get_mod_tiers("Wands", "normal") if m.mod_id == "Intelligence8")
        ),
        CraftingMod.from_entry(
            next(
                m
                for m in engine.get_mod_tiers("Wands", "normal")
                if m.mod_id == "IncreasedCastSpeed7"
            )
        ),
    ]
    st_3 = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE, mods=mods_4[:3])
    st_4 = CraftingState(base=attuned, ilvl=82, rarity=ItemRarity.RARE, mods=mods_4)

    assert engine.evaluate_action(st_3, {mods_4[0].mod_id}, "Fracturing Orb").hit_probability == 0.0
    assert engine.evaluate_action(
        st_4, {mods_4[0].mod_id}, "Fracturing Orb"
    ).hit_probability == pytest.approx(0.25)

    for removed_name in (
        "Omen of Homogenising Exaltation",
        "Omen of Homogenising Coronation",
        "Omen of Recombination",
        "Recombinator",
    ):
        with pytest.raises(RemovedLeverError):
            engine.resolve_lever(removed_name)
        with pytest.raises(RemovedLeverError):
            planner.evaluate_step("Attuned Wand", 82, ["T1 Spell Damage"], lever=removed_name)

    for poe1_orb in ("Orb of Scouring", "Orb of Alteration"):
        with pytest.raises(ValueError, match="Unknown PoE2 crafting lever"):
            engine.resolve_lever(poe1_orb)


def test_anti_zero_price_guard_and_end_to_end_plan_craft(planner: CraftingPlanner) -> None:
    # Refuses to price unknown currencies at 0 when fallback is disabled
    with pytest.raises(ValueError, match="refusing to price unknown currencies at 0"):
        planner.evaluate_step(
            "Attuned Wand",
            82,
            ["T2 Spell Damage"],
            lever="Perfect Orb of Transmutation",
            currency_prices_ex={},
            use_fallback_prices=False,
        )

    plan = planner.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=["T2 Spell Damage", "T2 Increased Cast Speed"],
        currency_prices_ex={"Exalted Orb": 1.0, "Divine Orb": 450.0},
        base_cost_ex=2.0,
        buy_price_ex=500.0,
        n_sims=1500,
        seed=123,
    )
    assert plan.base_type == "Attuned Wand"
    assert plan.expected_cost_exalted > 2.0
    assert plan.expected_cost_divine is not None and plan.expected_cost_divine > 0.0
    assert plan.p50_cost_exalted <= plan.p80_cost_exalted <= plan.p95_cost_exalted
    assert len(plan.policy_steps) >= 2
    assert len(plan.candidate_actions) >= 1
    assert plan.trade_vs_craft is not None
    assert plan.trade_vs_craft.recommendation in ("craft", "buy", "tie")

    # Mid-craft planning from a ParsedItem
    mid_item = ParsedItem(
        rarity=ItemRarity.RARE,
        base_type="Attuned Wand",
        item_level=82,
        mods=[
            ParsedMod(
                raw_text="(105-119)% increased Spell Damage",
                domain=ModDomain.FRACTURED,
                affix_side=AffixSide.PREFIX,
                display_tier=1,
            ),
            ParsedMod(
                raw_text="+(5-8) to Intelligence",
                domain=ModDomain.EXPLICIT,
                affix_side=AffixSide.SUFFIX,
                display_tier=8,
            ),
        ],
    )
    mid_plan = planner.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=["SpellDamageOnWeapon8_", "T2 Increased Cast Speed"],
        starting_state=mid_item,
        buy_price_ex=10.0,
        n_sims=800,
        seed=99,
    )
    assert mid_plan.expected_cost_exalted > 0.0
    assert mid_plan.trade_vs_craft is not None


def test_gamedata_no_poe1_keyword_leak_and_removed_omens_not_live(engine: ModPoolEngine) -> None:
    cur = engine._conn.cursor()
    for poe1_orb in ("Orb of Alteration", "Orb of Scouring", "Orb of Fusing", "Chromatic Orb"):
        kw_hits = cur.execute(
            "SELECT COUNT(*) FROM keywords WHERE term = ?", (poe1_orb,)
        ).fetchone()[0]
        fts_kw = cur.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE doc_type = 'keyword' AND title = ?",
            (poe1_orb,),
        ).fetchone()[0]
        assert kw_hits == 0, f"PoE1 currency {poe1_orb} leaked into keywords!"
        assert fts_kw == 0, f"PoE1 currency {poe1_orb} leaked into knowledge_fts!"

    for removed_omen in (
        "Omen of Homogenising Exaltation",
        "Omen of Homogenising Coronation",
        "Omen of Recombination",
    ):
        rows = cur.execute(
            "SELECT is_live_poe2 FROM base_items WHERE name = ?", (removed_omen,)
        ).fetchall()
        assert all(int(r[0]) == 0 for r in rows), f"{removed_omen} must have is_live_poe2=0!"


def test_cross_pool_resolution_sunk_cost_restart_and_fracture_checkpoint(
    engine: ModPoolEngine, planner: CraftingPlanner
) -> None:
    # 1. Cross-slot & cross-pool Essence / Alloy target resolution
    horror_boots = engine.resolve_target(
        "Boots_int", "60% increased effect of Socketed Augment Items", ilvl=80
    )
    assert "EssenceLocalRuneAndSoulCoreEffect1" in horror_boots.matching_mod_ids

    staff_plus5 = engine.resolve_target("Staves", "+5 to Level of all Spell Skills", ilvl=81)
    assert "GlobalSpellGemsLevelTwoHandWeapon4" in staff_plus5.matching_mod_ids
    assert "EssenceSpellSkillLevel2H1" in staff_plus5.matching_mod_ids

    wand_t2_cast = engine.resolve_target("Wands", "T2 Increased Cast Speed", ilvl=82)
    assert "AlloyCastSpeedDamageAsExtraColdHybridOneHand1" in wand_t2_cast.matching_mod_ids

    # Strict T1 Spell Damage must NOT include lower-magnitude T3 Greater Essence of Sorcery
    wand_t1_spell = engine.resolve_target("Wands", "T1 Spell Damage", ilvl=82)
    assert "SpellDamageOnWeapon8_" in wand_t1_spell.matching_mod_ids
    assert "SpellDamageOnWeapon6" not in wand_t1_spell.matching_mod_ids

    # 2. Sunk-cost restart fix: 1-mod +5 Lightning Wand never burns 9,500 Ex Erasure Omens
    league_like_prices = {
        "Exalted Orb": 1.0,
        "Divine Orb": 450.0,
        "Chaos Orb": 36.0,
        "Perfect Orb of Transmutation": 13.5,
        "Greater Orb of Transmutation": 2.5,
        "Orb of Transmutation": 2.2,
        "Omen of Sinistral Erasure": 9500.0,
        "Omen of Dextral Erasure": 6450.0,
        "Fracturing Orb": 3600.0,
        "Greater Chaos Orb": 123.0,
        "Perfect Essence of Sorcery": 56.0,
        "Omen of Sinistral Crystallisation": 110.0,
    }
    wand_1m = planner.plan_craft(
        base="Galvanic Wand",
        ilvl=81,
        target_mods=["GlobalLightningSpellGemsLevelWeapon5"],
        currency_prices_ex=league_like_prices,
        base_cost_ex=5.0,
        n_sims=1200,
        seed=42,
    )
    assert wand_1m.expected_cost_exalted < 2500.0

    # 3. 2-step Crystallisation setup discovery on Staff (+5 All Spell + T3 Cast Speed)
    staff_plan = planner.plan_craft(
        base="Paralysing Staff",
        ilvl=81,
        target_mods=["+5 to Level of all Spell Skills", "T3 Cast Speed"],
        currency_prices_ex=league_like_prices,
        base_cost_ex=10.0,
        n_sims=1000,
        seed=42,
    )
    assert staff_plan.expected_cost_exalted < 2500.0
    assert any(
        "Perfect Essence of Sorcery" in s.recommended_action for s in staff_plan.policy_steps
    )

    # 4. 3-T1 stochastic Wand discovers 4-mod Fracturing Orb checkpoint (<250k Ex vs 2.3B Ex bug)
    wand_3m = planner.plan_craft(
        base="Attuned Wand",
        ilvl=82,
        target_mods=[
            "+5 to Level of all Lightning Spell Skills",
            "T1 Spell Damage",
            "T1 Cast Speed",
        ],
        currency_prices_ex=league_like_prices,
        base_cost_ex=0.0,
        n_sims=800,
        seed=42,
    )
    assert any(s.recommended_action == "Fracturing Orb" for s in wand_3m.policy_steps)
    assert wand_3m.expected_cost_exalted < 250000.0
