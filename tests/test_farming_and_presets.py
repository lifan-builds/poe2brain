# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from dataclasses import dataclass

from poe2brain.knowledge.service import KnowledgeService
from poe2brain.models.builds import BuildStats
from poe2brain.models.ninja import NinjaSkillGroupSummary
from poe2brain.ninja.xml_inspector import _select_best_main_skill
from poe2brain.trade.farming import recommend_farming_strategies


@dataclass
class _FakeQuote:
    exalted: float


@dataclass
class _FakeRates:
    divine_exalted: float = 400.0


class _StubEconomy:
    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = {k.lower(): v for k, v in prices.items()}

    def get_reference_rates(self, league: str, force_refresh: bool = False) -> _FakeRates:
        return _FakeRates(divine_exalted=400.0)

    def get_price(self, query: str, league: str = "Forbidden Rites", force_refresh: bool = False):
        val = self._prices.get(query.lower())
        return _FakeQuote(exalted=val) if val is not None else None


def test_farming_strategy_day1_vs_endgame_and_build_fit() -> None:
    econ = _StubEconomy(
        {
            "Expedition Logbook": 80.0,
            "Olroth's Resolve": 12000.0,
            "Svalinn": 260.0,
            "Ritual Tablet": 40.0,
            "Sacred Bloom": 45.0,
            "Omen of Whittling": 7000.0,
            "Breach Tablet": 2.0,
            "Reaver Catalyst": 480.0,
            "Breachstone": 1400.0,
            "Delirium Tablet": 2.0,
            "Concentrated Liquid Isolation": 640.0,
            "Soul Core of Azcapa": 1150.0,
            "Faded Crisis Fragment": 2800.0,
            "Architect's Orb": 4200.0,
        }
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

    # Zoom-mapper with sweet-spot IIR (115%) and high move speed should score Breach/Ritual high
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

    # Negative IIR build should unlock negative_rarity_bases
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
        build_stats=neg_build,
        slot_name="neg_mf",
    )
    assert neg_report["build_profile"]["iir_band"] == "negative_white_base_mode"
    neg_strat_after = next(
        s for s in neg_report["strategies"] if s["id"] == "negative_rarity_bases"
    )
    assert neg_strat_after["build_fit_score"] > neg_strat["build_fit_score"]


def test_xml_inspector_skips_curses_and_banners_as_main_skill() -> None:
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
