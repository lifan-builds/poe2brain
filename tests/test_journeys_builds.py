# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from poe2brain.http import Poe2HttpClient
from poe2brain.knowledge import KnowledgeService
from poe2brain.models.common import SaleType
from poe2brain.ninja.service import NinjaBuildsService, sort_time_machine_labels
from poe2brain.pob import BuildService
from poe2brain.pob.service import GOLD_RESPEC_PRICES_0_5_5, estimate_poe2_gold_respec

LIVE_ENABLED = os.environ.get("POE2_LIVE_TESTS") == "1"


@pytest.fixture(scope="module")
def shared_build_service() -> Iterator[BuildService]:
    svc = BuildService()
    try:
        yield svc
    finally:
        svc.close()


# ============================================================================
# Journey 1: "Week-1 vs Week-11 Time-Machine Progression Blueprint"
# ============================================================================


def test_journey1_timeline_discovery_and_slot_progression_diff(
    shared_build_service: BuildService,
    tmp_path: Path,
) -> None:
    # 1. Verify chronological sorting of scrambled poe.ninja timeMachineLabels
    scrambled = [
        "hour-6",
        "week-11",
        "week-3",
        "day-2",
        "week-1",
        "hour-3",
        "week-5",
        "day-1",
        "week-2",
    ]
    assert sort_time_machine_labels(scrambled) == [
        "hour-3",
        "hour-6",
        "day-1",
        "day-2",
        "week-1",
        "week-2",
        "week-3",
        "week-5",
        "week-11",
    ]

    # Build two realistic progression snapshots in PoB2:
    # - Early (week-5 starter): Lv 78 Stormweaver, Rare wand + Rare shield, Life/ES hybrid
    # - Late (current week-11): Lv 96 Stormweaver, Siphoning Wand + Svalinn + CI + Tier III supports
    bsvc = shared_build_service
    bsvc.create_character("Sorceress", "Stormweaver", level=78, slot="j1_early", name="LateRite")
    bsvc.set_gem_links(
        ["Spark 18/0", "Rapid Casting I", "Arcane Tempo I"],
        slot="j1_early",
        label="Main Spark",
        include_in_full_dps=True,
        set_main=True,
    )
    bsvc.equip_item(
        """Rarity: RARE
Starter Branch
Attuned Wand
Implicits: 0
45% increased Spell Damage
+60 to maximum Mana
12% increased Cast Speed""",
        target_slot="Weapon 1",
        slot="j1_early",
    )
    bsvc.equip_item(
        """Rarity: RARE
Starter Robe
Hexer's Robe
Implicits: 0
+110 to maximum Energy Shield
+35% to Fire Resistance
+35% to Cold Resistance
+35% to Lightning Resistance""",
        target_slot="Body Armour",
        slot="j1_early",
    )
    near_nodes = [
        n
        for n in bsvc.search_passive_nodes("", limit=20, slot="j1_early")
        if not n.get("allocated") and isinstance(n.get("pathDist"), int) and n["pathDist"] <= 3
    ]
    assert len(near_nodes) >= 4
    bsvc.alloc_passive_nodes([int(near_nodes[0]["id"]), int(near_nodes[1]["id"])], slot="j1_early")
    early_code = bsvc.export_build("j1_early", fmt="code")

    # Build Late snapshot in j1_late (refunds one branch, allocates another + CI, adds Svalinn)
    bsvc.clone_slot("j1_early", "j1_late", activate=True)
    bsvc.bridge.call("set_character", {"slot": "j1_late", "level": 96})
    bsvc.dealloc_passive_nodes([int(near_nodes[0]["id"])], slot="j1_late")
    bsvc.alloc_passive_nodes([int(near_nodes[2]["id"]), int(near_nodes[3]["id"])], slot="j1_late")
    ci_matches = bsvc.search_passive_nodes(
        "Chaos Inoculation", node_type="Keystone", slot="j1_late"
    )
    assert ci_matches
    bsvc.alloc_passive_nodes([int(ci_matches[0]["id"])], slot="j1_late", connect_path=False)

    bsvc.reset_slot(
        "j1_late_clean", class_name="Sorceress", ascendancy="Stormweaver", level=96, name="LateRite"
    )
    bsvc.alloc_passive_nodes(
        [int(near_nodes[1]["id"]), int(near_nodes[2]["id"]), int(near_nodes[3]["id"])],
        slot="j1_late_clean",
    )
    bsvc.alloc_passive_nodes([int(ci_matches[0]["id"])], slot="j1_late_clean", connect_path=True)
    bsvc.set_gem_links(
        ["Spark 20/20", "Rapid Casting III", "Controlled Destruction III"],
        slot="j1_late_clean",
        label="Main Spark",
        include_in_full_dps=True,
        set_main=True,
    )
    bsvc.set_gem_links(
        ["Archmage 20/20"],
        slot="j1_late_clean",
        label="Archmage",
        include_in_full_dps=False,
        set_main=False,
    )
    bsvc.equip_item(
        """Rarity: RARE
Tempest Sceptre
Siphoning Wand
Implicits: 0
110% increased Spell Damage
85% increased Lightning Damage
+4 to Level of all Lightning Spell Skills
+170 to maximum Mana
30% increased Cast Speed""",
        target_slot="Weapon 1",
        slot="j1_late_clean",
    )
    bsvc.equip_item(
        """Rarity: UNIQUE
Svalinn
Crucible Tower Shield
Implicits: 2
Grants Skill: Raise Shield
Grants Skill: Level 18 Cast on Block
+90 to maximum Runic Ward
260% increased Armour
Chance to Block Damage is Lucky
You take 15% of damage from Blocked Hits""",
        target_slot="Weapon 2",
        slot="j1_late_clean",
    )
    bsvc.equip_item(
        """Rarity: RARE
Endgame Mantle
Hexer's Robe
Implicits: 0
+420 to maximum Energy Shield
100% increased Energy Shield
+55 to Spirit
+45% to Fire Resistance
+45% to Cold Resistance
+45% to Lightning Resistance""",
        target_slot="Body Armour",
        slot="j1_late_clean",
    )
    bsvc.equip_item(
        """Rarity: RARE
Titan Collar
Amber Amulet
Implicits: 0
+140 to Strength
+140 to Intelligence
+120 to maximum Energy Shield
+35% to Fire Resistance
+35% to Cold Resistance
+35% to Lightning Resistance""",
        target_slot="Amulet",
        slot="j1_late_clean",
    )
    # Add PoB-warrior inflation on late snapshot to verify compare_slots(dewater=True) strips it
    bsvc.set_config(
        {"enemyIsBoss": "None", "buffOnslaught": True},
        custom_mods="100% more Spell Damage",
        slot="j1_late_clean",
    )
    late_code = bsvc.export_build("j1_late_clean", fmt="code")

    # 2. Mock poe.ninja where character didn't exist in week-1..week-4 (HTTP 404), joined in week-5!
    tm_requests: list[str] = []
    all_weeks = [f"week-{w}" for w in range(1, 12)]

    def _ninja_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/build-index-state"):
            return httpx.Response(
                200,
                json={
                    "leagueBuilds": [
                        {
                            "leagueName": "Forbidden Rites",
                            "leagueUrl": "forbiddenrites",
                            "total": 124092,
                            "hardcore": False,
                            "statistics": [],
                        }
                    ]
                },
            )
        if path.endswith("/snapshot-versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "url": "forbiddenrites",
                        "name": "Forbidden Rites",
                        "timeMachineLabels": list(reversed(all_weeks)),  # Deliberately reversed!
                        "version": "ab" * 32,
                        "snapshotName": "forbidden-rites",
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                    }
                ],
            )
        if path == "/poe2/pob/raw/overview/code":
            tm = request.url.params.get("timemachine", "current")
            tm_requests.append(tm)
            if tm in ("week-1", "week-2", "week-3", "week-4"):
                return httpx.Response(404, json={"status": 404, "title": "Not Found"})
            if tm in ("week-5", "week-6", "week-7", "week-8", "week-9", "week-10"):
                return httpx.Response(200, text=early_code)
            return httpx.Response(200, text=late_code)
        return httpx.Response(404, text="not found")

    mock_http = Poe2HttpClient(sync_transport=httpx.MockTransport(_ninja_handler))
    try:
        with NinjaBuildsService(
            http_client=mock_http,
            build_service=bsvc,
            cache_dir=tmp_path / "j1_cache",
            min_uncached_char_interval_seconds=0.0,
        ) as nsvc:
            # Binary-search timeline probe pinpoints week-5 in <= 7 probes (never all 11+1)!
            timeline = nsvc.probe_character_timeline("LateStarter#1234", "LateRite")
            assert timeline.account == "LateStarter-1234"
            assert timeline.available_time_machine_labels == all_weeks
            assert timeline.earliest_available_label == "week-5"
            assert timeline.latest_available_label == "current"
            assert len(tm_requests) <= 7
            assert any("week-5" in n for n in timeline.notes)

            # Strict mode raises LookupError on missing week-1
            with pytest.raises(LookupError, match="week-1"):
                nsvc.fetch_character(
                    "LateStarter-1234",
                    "LateRite",
                    timemachine="week-1",
                    fallback_nearest_timemachine=False,
                )

            # Default graceful mode discovers nearest available week-5 and loads into PoB2 'early'
            early_imp = nsvc.fetch_character(
                "LateStarter-1234",
                "LateRite",
                timemachine="week-1",
                load_into_pob=True,
                slot="early",
            )
            assert early_imp.timemachine == "week-5"
            assert early_imp.level == 78
            assert any("week-5" in n for n in early_imp.notes)

            late_imp = nsvc.fetch_character(
                "LateStarter-1234",
                "LateRite",
                load_into_pob=True,
                slot="late",
            )
            assert late_imp.timemachine is None
            assert late_imp.level == 96

            # 3. Compare dewatered progression between 'early' and 'late'
            raw_diff = bsvc.compare_slots("early", "late", dewater=False)
            dew_diff = bsvc.compare_slots("early", "late", dewater=True)
            assert dew_diff.dewatered is True
            assert dew_diff.level_delta == 18
            # Dewatering stripped the fake '100% more Spell Damage' customMod on 'late'
            assert dew_diff.effective_dps_after < raw_diff.effective_dps_after * 0.6
            assert dew_diff.effective_dps_delta > 0
            assert dew_diff.total_ehp_delta > 0
            assert "Svalinn" in dew_diff.added_uniques
            assert "Archmage" in dew_diff.added_skills
            assert "Chaos Inoculation" in dew_diff.added_keystones

            spark_diff = next(s for s in dew_diff.skill_changes if s.active_skill == "Spark")
            assert spark_diff.change_type == "modified"
            assert spark_diff.level_before == 18 and spark_diff.level_after == 20
            assert "Rapid Casting III" in spark_diff.added_supports
            assert "Rapid Casting I" in spark_diff.removed_supports

            assert dew_diff.passive_nodes_removed_count >= 1
            assert dew_diff.passive_nodes_added_count >= 2
            assert dew_diff.respec_estimate.character_level == 96
            assert dew_diff.respec_estimate.gold_per_passive_point == GOLD_RESPEC_PRICES_0_5_5[95]
            assert (
                dew_diff.respec_estimate.total_gold_cost
                == dew_diff.respec_estimate.refunded_passive_points * GOLD_RESPEC_PRICES_0_5_5[95]
            )
    finally:
        mock_http.close()


# ============================================================================
# Journey 2: "SSF Chase Drop Reroll Architect (Svalinn / Choir / Rakiata's)"
# ============================================================================


def test_journey2_ssf_chase_drop_reroll_architect(
    shared_build_service: BuildService,
    tmp_path: Path,
) -> None:
    # 1. Verify 0.5.5 mechanics via read-only KnowledgeService
    ksvc = KnowledgeService()
    svalinn = ksvc.inspect_unique("Svalinn")
    assert svalinn.base_type == "Crucible Tower Shield"
    assert any("Lucky" in mod for mod in svalinn.explicits)
    assert any("Cast on Block" in mod for mod in svalinn.implicits)

    choir = ksvc.inspect_unique("Choir of the Storm")
    assert choir.base_type == "Jade Amulet"
    assert any("Lightning Bolt" in mod for mod in choir.implicits)
    assert any("Ignore Enemy Monster Lightning Resistance" in mod for mod in choir.explicits)

    rakiata = ksvc.inspect_gem("Rakiata's Flow")
    assert rakiata.gem_kind == "lineage_support"
    assert rakiata.max_copies_per_char == 1

    # 2. Search poe.ninja for SSF-viable ascendancies & skills (with mock snapshot headers)
    def _ssf_snap_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/build-index-state"):
            return httpx.Response(
                200,
                json={
                    "leagueBuilds": [
                        {
                            "leagueName": "SSF Forbidden Rites",
                            "leagueUrl": "forbiddenritesssf",
                            "total": 8248,
                            "hardcore": False,
                            "statistics": [{"class": "Shaman", "percentage": 12.4, "trend": 1}],
                        }
                    ]
                },
            )
        if request.url.path.endswith("/snapshot-versions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "url": "forbiddenritesssf",
                        "name": "SSF Forbidden Rites",
                        "timeMachineLabels": ["week-1", "week-11"],
                        "version": "cc" * 32,
                        "snapshotName": "ssf-forbidden-rites",
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                    }
                ],
            )
        return httpx.Response(404, text="not found")

    sample_fixture = Path(__file__).resolve().parent / "fixtures" / "sample_builds_0.5.5.json.gz"
    mock_http = Poe2HttpClient(sync_transport=httpx.MockTransport(_ssf_snap_handler))
    try:
        with NinjaBuildsService(
            http_client=mock_http,
            corpus_path=sample_fixture,
            cache_dir=tmp_path / "j2_cache",
            min_uncached_char_interval_seconds=0.0,
        ) as nsvc:
            # Choir of the Storm in SSF must warn that 100% of SC corpus rows co-equip Temporalis!
            choir_rep = nsvc.search_builds("SSF Forbidden Rites", unique_item="Choir of the Storm")
            assert choir_rep.matched_builds >= 5
            assert any("Temporalis" in n and "Spark" in n for n in choir_rep.notes)

            # Filter Svalinn builds excluding T0 trade belts/chests for true SSF viability
            ssf_svalinn = nsvc.search_builds(
                "SSF Forbidden Rites",
                unique_item="Svalinn",
                exclude_uniques=["Temporalis", "Mageblood", "Headhunter", "Kalandra's Touch"],
                ssf_viable_first=True,
                include_pob_code=True,
                limit=5,
            )
            assert ssf_svalinn.matched_builds >= 5
            for b in ssf_svalinn.builds:
                assert "Svalinn" in b.unique_items
                assert not (
                    {"Temporalis", "Mageblood", "Headhunter", "Kalandra's Touch"}
                    & set(b.unique_items)
                )

            # Also grab a real corpus caster Svalinn build where FullDPS == 0 in poe.ninja XML
            caster_svalinn = nsvc.search_builds(
                "SSF Forbidden Rites",
                unique_item="Svalinn",
                skill="Spark",
                include_pob_code=True,
                limit=3,
            )
            assert caster_svalinn.builds
            spark_entry = caster_svalinn.builds[0]
            assert spark_entry.pob_code is not None
    finally:
        mock_http.close()

    # 3. Load real Svalinn Spark build into PoB2: verify Tier III supports, Anoints, & Sustain!
    bsvc = shared_build_service
    stats = bsvc.load_build(spark_entry.pob_code, slot="j2_svalinn")

    # Mana / Spirit / Attribute self-sufficiency fields on BuildStats
    assert stats.effective_dps > 100_000
    assert stats.mana > 1_000
    assert stats.mana_unreserved > 0
    assert stats.mana_regen > 0
    assert stats.mana_cost > 0
    assert stats.mana_per_second_cost > 0
    assert stats.mana_payable is True
    assert stats.spirit_total >= stats.spirit_reserved
    assert stats.spirit_unreserved >= 0
    assert stats.str_total >= stats.req_str
    assert stats.dex_total >= stats.req_dex
    assert stats.int_total >= stats.req_int
    assert stats.attributes_met is True

    # Default group_index=None inspects mainSocketGroup (Spark) rather than socket group 1,
    # filters to 0.5.5 Tier III + Lineage supports, and ranks by exact PoB2 DPS gain!
    tier3_supports = bsvc.list_valid_supports(
        "j2_svalinn",
        tier=3,
        include_lineage=True,
        rank_by_dps=True,
        top_n=12,
    )
    assert len(tier3_supports) >= 5
    assert all(s["supportTier"] == 3 or s["isLineage"] is True for s in tier3_supports)
    assert any((s.get("dpsDelta") or 0) > 0 for s in tier3_supports)
    for s in tier3_supports:
        if s["isLineage"]:
            assert s["maxCopiesPerChar"] == 1

    # Full-tree anoint ranking MUST work even when poe.ninja XML has FullDPS == 0,
    # AND return exact 0.5.5 3x Liquid Emotion recipes + SSF tier classification!
    anoints = bsvc.rank_passive_upgrades("j2_svalinn", mode="anoint", stat="FullDPS", top_n=6)
    assert len(anoints) == 6
    assert anoints[0].single_node_delta > 0
    assert anoints[0].single_node_pct > 0
    assert len(anoints[0].anoint_emotions) == 3
    assert all("Liquid" in emo for emo in anoints[0].anoint_emotions)
    assert anoints[0].ssf_anoint_tier in (
        "farmable_delirium",
        "concentrated_high_tier",
        "endgame_simulacrum",
    )


# ============================================================================
# Journey 3: "Hardcore Pinnacle Boss One-Shot & Respec Audit"
# ============================================================================


def test_journey3_hc_pinnacle_boss_oneshot_and_respec_audit(
    shared_build_service: BuildService,
    tmp_path: Path,
) -> None:
    # Verify exact Level 92 Gold respec constants from PoB2 Data/Misc.lua
    lvl92_est = estimate_poe2_gold_respec(
        92, refunded_passives=6, refunded_ascendancy=1, allocated_new=6
    )
    assert lvl92_est.gold_per_passive_point == 5952
    assert lvl92_est.gold_per_ascendancy_point == 29760
    assert lvl92_est.total_gold_cost == 6 * 5952 + 29760

    # Pull a real non-CI high-ES build from corpus (e.g. Invoker with >10k ES, ~1.5k Life, no CI)
    def _noop_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="offline")

    sample_fixture = Path(__file__).resolve().parent / "fixtures" / "sample_builds_0.5.5.json.gz"
    mock_http = Poe2HttpClient(sync_transport=httpx.MockTransport(_noop_handler))
    try:
        with NinjaBuildsService(
            http_client=mock_http,
            corpus_path=sample_fixture,
            cache_dir=tmp_path / "j3_cache",
            min_uncached_char_interval_seconds=0.0,
        ) as nsvc:
            es_rep = nsvc.search_builds(
                "Forbidden Rites", sort_by="es", include_pob_code=True, limit=30
            )
            non_ci_es = [
                b
                for b in es_rep.builds
                if "Chaos Inoculation" not in b.keystones and b.energy_shield > 3000
            ]
            assert non_ci_es
            hc_cand = non_ci_es[0]
            assert hc_cand.pob_code is not None
    finally:
        mock_http.close()

    bsvc = shared_build_service
    bsvc.load_build(hc_cand.pob_code, slot="j3_hc")
    bsvc.bridge.call("set_character", {"slot": "j3_hc", "level": 92})
    # Inject PoB-warrior config inflation + low Chaos res to stress-test dewatering + HC audit
    bsvc.set_config(
        {"enemyIsBoss": "None", "buffOnslaught": True},
        custom_mods="+3000 to maximum Energy Shield\n60% more Attack Damage",
        slot="j3_hc",
    )

    hc_report = bsvc.audit_hc_rip_risks("j3_hc", apply_dewater=False, top_defensive_nodes=5)
    sust = hc_report.dewatered_audit.sustainable_pinnacle_stats

    # 1. Verify PoB-warrior inflation stripped & all MaxHit + avoidance fields populated
    assert hc_report.dewatered_audit.ehp_inflation_pct > 5.0
    assert sust.phys_max_hit > 0
    assert sust.fire_max_hit > 0
    assert sust.cold_max_hit > 0
    assert sust.lightning_max_hit > 0
    assert sust.chaos_max_hit > 0
    assert sust.evade_chance >= 0
    assert sust.deflect_chance >= 0
    assert sust.effective_block_chance >= 0
    assert sust.mana_regen >= 0
    assert set(hc_report.max_hits) == {"physical", "fire", "cold", "lightning", "chaos"}

    # 2. Verify HC Rip-Risk Diagnostics catch Bleed/Poison ES bypass AND Chaos 2x ES vulnerability!
    assert hc_report.has_chaos_inoculation is False
    assert hc_report.bleed_poison_life_pool is not None and hc_report.bleed_poison_life_pool < 2200
    risk_ids = {r.risk_id for r in hc_report.rip_risks}
    assert "bleed_poison_es_bypass" in risk_ids
    assert hc_report.pinnacle_ready is False

    # 3. Verify defensive passive respec ranking + Level 92 Gold respec cost
    assert hc_report.respec_gold_per_point == 5952
    assert len(hc_report.top_defensive_passives) >= 3
    top_def = hc_report.top_defensive_passives[0]
    assert top_def.ehp_delta > 0
    assert top_def.respec_gold_cost == top_def.path_dist * 5952


# ============================================================================
# Live End-to-End Verification Across Journeys 1 & 3 (POE2_LIVE_TESTS=1)
# ============================================================================


@pytest.mark.skipif(not LIVE_ENABLED, reason="Set POE2_LIVE_TESTS=1 for live PoE2 journey tests")
def test_live_journeys_1_and_3_end_to_end(
    shared_build_service: BuildService,
    tmp_path: Path,
) -> None:
    bsvc = shared_build_service
    with NinjaBuildsService(
        build_service=bsvc,
        cache_dir=tmp_path / "live_journeys_cache",
        min_uncached_char_interval_seconds=0.6,
    ) as nsvc:
        # Live Journey 1A: Character absent at week-1 -> smart timeline probe + nearest discovery
        tl = nsvc.probe_character_timeline("xxmice-2877", "daidai_kitava_FR")
        assert tl.available_time_machine_labels == sort_time_machine_labels(
            tl.available_time_machine_labels
        )
        assert tl.earliest_available_label is not None
        assert tl.earliest_available_label != "week-1"

        # Live Journey 1B: Week-1 vs Current full progression blueprint in PoB2
        early_live = nsvc.fetch_character(
            "Lugi-3501",
            "BIG_MACE_WINDU",
            timemachine="week-1",
            load_into_pob=True,
            slot="live_early",
        )
        late_live = nsvc.fetch_character(
            "Lugi-3501",
            "BIG_MACE_WINDU",
            load_into_pob=True,
            slot="live_late",
        )
        assert early_live.level < late_live.level
        live_diff = bsvc.compare_slots("live_early", "live_late", dewater=True)
        assert live_diff.level_delta > 0
        assert live_diff.respec_estimate.gold_per_passive_point > 0

        # Live Journey 3: Level 92 HC Pinnacle Boss Audit + <= 15 Div Securable EHP Upgrade!
        bsvc.create_character("Sorceress", "Stormweaver", level=92, slot="live_hc_92")
        bsvc.set_gem_links(["Spark 20/20", "Rapid Casting III"], slot="live_hc_92")
        bsvc.equip_item(
            """Rarity: RARE
Starter Circlet
Twig Circlet
Implicits: 0
+25 to maximum Energy Shield
+15% to Cold Resistance""",
            target_slot="Helmet",
            slot="live_hc_92",
        )
        bsvc.equip_item(
            """Rarity: RARE
Hybrid Vest
Hexer's Robe
Implicits: 0
+320 to maximum Energy Shield
80% increased Energy Shield
+45% to Fire Resistance
+45% to Cold Resistance
+45% to Lightning Resistance""",
            target_slot="Body Armour",
            slot="live_hc_92",
        )
        bsvc.equip_item(
            """Rarity: RARE
Scholar Torc
Amber Amulet
Implicits: 0
+160 to Intelligence
+90 to maximum Energy Shield""",
            target_slot="Amulet",
            slot="live_hc_92",
        )
        hc_audit = bsvc.audit_hc_rip_risks("live_hc_92")
        assert hc_audit.respec_gold_per_point == 5952
        assert any(r.risk_id == "bleed_poison_es_bypass" for r in hc_audit.rip_risks)

        # Query live trade2 (1 query, <= 6 budget) for <= 15 Div securable Helmet upgrade
        div_ex = bsvc._resolve_currency_to_exalted("divine", 1.0, "Forbidden Rites")
        upg_rep = bsvc.find_live_slot_upgrades(
            "Helmet",
            build_slot="live_hc_92",
            metric="TotalEHP",
            league="Forbidden Rites",
            max_price_exalted=round(15.0 * div_ex, 1),
            securable_only=True,
            candidate_limit=5,
        )
        assert upg_rep.securable_only is True
        assert upg_rep.total_market_matches > 0
        assert len(upg_rep.candidates) >= 1
        best = upg_rep.candidates[0]
        assert best.listing.sale_type == SaleType.SECURABLE
        assert best.listing.fee is not None and best.listing.fee > 0
        assert best.upgrade_diff.total_ehp_delta > 0
