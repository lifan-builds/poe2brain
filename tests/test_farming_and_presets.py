# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from poe2brain.knowledge.service import KnowledgeService
from poe2brain.models.builds import BuildStats
from poe2brain.models.ninja import NinjaSkillGroupSummary
from poe2brain.ninja.xml_inspector import _select_best_main_skill, is_non_primary_skill
from poe2brain.trade.farming import recommend_farming_strategies


@dataclass
class _FakeRates:
    divine_exalted: float = 400.0


class _StubCategoryEconomy:
    """Category-driven stub mirroring `EconomyService.get_category_market_summary`."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def get_reference_rates(self, league: str, force_refresh: bool = False) -> _FakeRates:
        return _FakeRates(divine_exalted=400.0)

    def get_category_market_summary(
        self,
        categories: tuple[str, ...],
        league: str = "Forbidden Rites",
        *,
        token_filter: tuple[str, ...] | None = None,
        entry_tokens: tuple[str, ...] | None = None,
        min_volume_divine: float = 0.25,
        top_n: int = 5,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        div_ex = 400.0
        cat_set = {c.lower() for c in categories}
        toks = tuple(t.lower() for t in (token_filter or ()))
        ent_toks = tuple(t.lower() for t in (entry_tokens or ()))

        matched = [
            e
            for e in self._entries
            if e["category"] in cat_set
            and (not toks or any(t in e["name"].lower() for t in toks))
        ]
        entries_out = [
            {
                "id": e["id"],
                "name": e["name"],
                "category": e["category"],
                "exalted": e["exalted"],
                "divine": round(e["exalted"] / div_ex, 3),
                "volume_divine": e["volume_divine"],
            }
            for e in self._entries
            if ent_toks and any(et in e["name"].lower() for et in ent_toks)
        ]
        liquid = [e for e in matched if (e["volume_divine"] or 0.0) >= min_volume_divine]
        pool = liquid or matched
        div_vals = sorted(e["exalted"] / div_ex for e in pool)
        p90 = (
            round(div_vals[min(len(div_vals) - 1, int(len(div_vals) * 0.9))], 3)
            if div_vals
            else 0.0
        )
        pool_sorted = sorted(pool, key=lambda e: e["exalted"], reverse=True)
        return {
            "categories": sorted(cat_set),
            "priced_count": len(matched),
            "liquid_count": len(liquid),
            "total_volume_divine": round(sum(e["volume_divine"] or 0.0 for e in matched), 2),
            "volume_weighted_change_7d_pct": 5.0 if matched else None,
            "median_liquid_divine": round(div_vals[len(div_vals) // 2], 3) if div_vals else 0.0,
            "p90_liquid_divine": p90,
            "top_liquid_items": [
                {
                    "id": e["id"],
                    "name": e["name"],
                    "category": e["category"],
                    "exalted": e["exalted"],
                    "divine": round(e["exalted"] / div_ex, 3),
                    "volume_divine": e["volume_divine"],
                    "change_7d_pct": 5.0,
                }
                for e in pool_sorted[:top_n]
            ],
            "entry_tickets": entries_out,
        }


def test_data_driven_farming_strategy_day1_vs_endgame_and_build_fit() -> None:
    econ = _StubCategoryEconomy(
        [
            {
                "id": "expedition-logbook",
                "name": "Expedition Logbook",
                "category": "expedition",
                "exalted": 80.0,
                "volume_divine": 120.0,
            },
            {
                "id": "perfect-flux",
                "name": "Perfect Flux",
                "category": "expedition",
                "exalted": 12000.0,
                "volume_divine": 60.0,
            },
            {
                "id": "ritual-tablet",
                "name": "Ritual Tablet",
                "category": "ritual",
                "exalted": 40.0,
                "volume_divine": 80.0,
            },
            {
                "id": "omen-sinistral-annulment",
                "name": "Omen of Sinistral Annulment",
                "category": "ritual",
                "exalted": 13000.0,
                "volume_divine": 350.0,
            },
            {
                "id": "breach-tablet",
                "name": "Breach Tablet",
                "category": "breach",
                "exalted": 2.0,
                "volume_divine": 90.0,
            },
            {
                "id": "refined-sibilant-catalyst",
                "name": "Refined Sibilant Catalyst",
                "category": "breach",
                "exalted": 6000.0,
                "volume_divine": 400.0,
            },
            {
                "id": "delirium-tablet",
                "name": "Delirium Tablet",
                "category": "delirium",
                "exalted": 2.0,
                "volume_divine": 60.0,
            },
            {
                "id": "potent-liquid-ferocity",
                "name": "Potent Liquid Ferocity",
                "category": "delirium",
                "exalted": 6500.0,
                "volume_divine": 220.0,
            },
            {
                "id": "guatelitzi-endurance",
                "name": "Guatelitzi's Soul Core of Endurance",
                "category": "ultimatum",
                "exalted": 3300.0,
                "volume_divine": 180.0,
            },
            {
                "id": "faded-crisis-fragment",
                "name": "Faded Crisis Fragment",
                "category": "fragments",
                "exalted": 2800.0,
                "volume_divine": 500.0,
            },
            {
                "id": "architects-orb",
                "name": "Architect's Orb",
                "category": "incursion",
                "exalted": 4200.0,
                "volume_divine": 310.0,
            },
            {
                "id": "the-grand-project",
                "name": "The Grand Project",
                "category": "map",
                "exalted": 2400.0,
                "volume_divine": 90.0,
            },
            {
                "id": "against-the-darkness",
                "name": "Against the Darkness",
                "category": "jewel",
                "exalted": 2100.0,
                "volume_divine": 75.0,
            },
            {
                "id": "celestial-alloy",
                "name": "Celestial Alloy",
                "category": "verisium",
                "exalted": 600.0,
                "volume_divine": 200.0,
            },
        ]
    )

    day1_report = recommend_farming_strategies(
        econ,  # type: ignore[arg-type]
        stage="day1",
        budget_exalted=30.0,
        focus="steady",
    )
    assert len(day1_report["strategies"]) == 9
    top_day1_ids = {s["id"] for s in day1_report["strategies"][:3]}
    assert "fortress_atlas_rush" in top_day1_ids
    assert "negative_rarity_bases" not in top_day1_ids
    # Dynamically surfaced live drops (zero hardcoded item names!)
    ritual = next(s for s in day1_report["strategies"] if s["id"] == "ritual_wildwood")
    assert ritual["top_payout_item"] == "Omen of Sinistral Annulment"
    assert ritual["market_score"] > 0

    zoom_build = BuildStats(
        class_name="Ranger",
        ascendancy_name="Pathfinder",
        level=98,
        main_skill="Ice Shot",
        effective_dps=4_500_000.0,
        total_ehp=32_000.0,
        phys_max_hit=8_500.0,
        movement_speed_mod=1.85,
        life=1800.0,
        life_unreserved=1800.0,
        energy_shield=3200.0,
        item_rarity_pct=115.0,
    )
    zoom_report = recommend_farming_strategies(
        econ,  # type: ignore[arg-type]
        stage="endgame",
        budget_exalted=2000.0,
        focus="speed",
        build_stats=zoom_build,
        slot_name="zoom",
    )
    assert zoom_report["build_profile"]["iir_band"] == "sweet_spot_100_150"
    assert zoom_report["strategies"][0]["id"] in ("breach_speed", "boss_rush_vaaltemple")
    neg_strat = next(s for s in zoom_report["strategies"] if s["id"] == "negative_rarity_bases")
    assert any("Negative-Rarity" in w for w in neg_strat["build_warnings"])

    neg_build = BuildStats(
        class_name="Ranger",
        ascendancy_name="Deadeye",
        level=95,
        main_skill="Lightning Arrow",
        effective_dps=3_000_000.0,
        total_ehp=28_000.0,
        phys_max_hit=7_000.0,
        movement_speed_mod=1.70,
        life=2100.0,
        life_unreserved=2100.0,
        item_rarity_pct=-75.0,
    )
    neg_report = recommend_farming_strategies(
        econ,  # type: ignore[arg-type]
        stage="endgame",
        budget_exalted=2000.0,
        focus="balanced",
        build_stats=neg_build,
        slot_name="neg_mf",
    )
    assert neg_report["build_profile"]["iir_band"] == "negative_white_base_mode"
    neg_strat_after = next(
        s for s in neg_report["strategies"] if s["id"] == "negative_rarity_bases"
    )
    assert neg_strat_after["build_fit_score"] > neg_strat["build_fit_score"]


def test_sqlite_driven_non_primary_skill_classification() -> None:
    # Classified purely via SQLite `gems.gem_kind` + `gems.tags_json` (zero hardcoded skill names)
    for utility in (
        "Despair",
        "Elemental Weakness",
        "Defiance Banner",
        "Arctic Armour",
        "Blasphemy",
        "Life Remnants",
        "Herald of Thunder",
        "Wind Dancer",
        "Ghost Dance",
        "Blink",
    ):
        assert is_non_primary_skill(utility), f"Expected utility/non-primary for {utility}"

    for primary in ("Spark", "Comet", "Ice Shot", "Poisonburst Arrow", "Enervating Nova"):
        assert not is_non_primary_skill(primary), f"Expected primary active skill for {primary}"

    groups = [
        NinjaSkillGroupSummary(
            index=1,
            is_main_group=True,
            active_skill="Elemental Weakness",
            active_gems=["Elemental Weakness"],
            support_gems=["Heightened Curse"],
        ),
        NinjaSkillGroupSummary(
            index=2,
            is_main_group=False,
            active_skill="Comet",
            active_gems=["Comet"],
            support_gems=["Spell Echo", "Concentrated Area", "Considerate Casting", "Arcane Tempo"],
        ),
    ]
    picked = _select_best_main_skill("Elemental Weakness", groups, ["Elemental Weakness", "Comet"])
    assert picked == "Comet"


def test_new_0_5_5_creator_mechanics_facts_in_sqlite() -> None:
    ks = KnowledgeService()
    for query, expected_slug in (
        ("item rarity diminishing returns", "item_rarity_diminishing_returns_and_negative_mf"),
        ("Sacred Blooms Viridian Wildwood", "forbidden_rites_sacred_blooms_wildwood_0_5_5"),
        ("Origins of Divinity Jado Hilda Doryani", "origins_of_divinity_fortress_atlas_0_5"),
        ("Trial of the Sekhemas Honour Resistance", "trial_of_sekhemas_honour_relics_0_5"),
        ("Orb of Alchemy Magic item", "alchemy_orb_magic_reroll_0_5"),
    ):
        res = ks.explain_mechanic(query)
        slugs = {f.slug for f in res.facts}
        assert expected_slug in slugs, (
            f"Expected {expected_slug} for query {query!r}, got {slugs}"
        )
