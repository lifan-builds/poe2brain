# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from poe2brain.config import get_pob2_paths
from poe2brain.http import Poe2HttpClient
from poe2brain.knowledge.meta import (
    DEFAULT_META_TTL_SECONDS,
    GGG_LADDER_BASE_URL,
    POENINJA_BUILD_INDEX_URL,
    MetaService,
    parse_lua_table,
)
from poe2brain.trade.economy import EconomyService

EXPECTED_CLASSES: set[str] = {
    "Ranger",
    "Huntress",
    "Warrior",
    "Mercenary",
    "Druid",
    "Witch",
    "Sorceress",
    "Monk",
}

EXPECTED_23_ASCENDANCIES: set[str] = {
    "Deadeye",
    "Pathfinder",
    "Amazon",
    "Spirit Walker",
    "Ritualist",
    "Titan",
    "Warbringer",
    "Smith of Kitava",
    "Tactician",
    "Witchhunter",
    "Gemling Legionnaire",
    "Oracle",
    "Shaman",
    "Infernalist",
    "Blood Mage",
    "Lich",
    "Abyssal Lich",
    "Stormweaver",
    "Chronomancer",
    "Disciple of Varashta",
    "Martial Artist",
    "Invoker",
    "Acolyte of Chayula",
}


def _require_pob2_tree_dir() -> Path:
    tree_dir = get_pob2_paths().src_dir / "TreeData" / "0_5"
    if not tree_dir.is_dir():
        pytest.skip(f"PoB2 TreeData/0_5 not bootstrapped at {tree_dir}")
    return tree_dir


def test_list_classes_and_all_23_ascendancies() -> None:
    tree_dir = _require_pob2_tree_dir()
    with MetaService(tree_path=tree_dir) as svc:
        classes = svc.list_classes()
        assert {c.name for c in classes} == EXPECTED_CLASSES
        assert len(classes) == 8

        ascendancies = svc.list_ascendancies()
        assert len(ascendancies) == 23
        assert {a.name for a in ascendancies} == EXPECTED_23_ASCENDANCIES

        for asc in ascendancies:
            assert asc.notable_count >= 8, f"{asc.name} missing notables ({asc.notable_count})"
            detail = svc.inspect_ascendancy(asc.name).primary
            assert detail is not None
            for notable in detail.notables:
                assert notable.stats, f"{asc.name} -> {notable.name} has empty stats"


def test_abyssal_lich_overrides_and_multiple_choice_notables() -> None:
    tree_dir = _require_pob2_tree_dir()
    with MetaService(tree_path=tree_dir) as svc:
        abyss = svc.inspect_ascendancy("Abyssal Lich")
        assert abyss.matched_kind == "ascendancy"
        assert abyss.class_name == "Witch"
        assert abyss.primary is not None
        assert abyss.primary.replaces_ascendancy == "Lich"
        names = set(abyss.primary.notable_names)
        assert {
            "Steward of Kulemak",
            "Umbral Well",
            "Unwilling Offering",
            "Rupture the Flesh",
            "Eternal Life",
        }.issubset(names)

        gemling = svc.inspect_ascendancy("Gemling Legionnaire").primary
        assert gemling is not None
        implanted = next(n for n in gemling.notables if n.name == "Implanted Gems")
        assert implanted.is_multiple_choice is True
        opt_names = {o.name for o in implanted.choice_options}
        assert opt_names == {"Bolstering Implants", "Motoric Implants", "Neurological Implants"}

        pf = svc.inspect_ascendancy("Pathfinder").primary
        assert pf is not None
        concoction = next(n for n in pf.notables if n.name == "Brew Concoction")
        assert len(concoction.choice_options) == 5
        assert "Shattering Concoction" in pf.granted_skills


def test_inspect_class_aliases_and_poe1_trap_guard() -> None:
    tree_dir = _require_pob2_tree_dir()
    with MetaService(tree_path=tree_dir) as svc:
        sorc = svc.inspect_ascendancy("Sorceress")
        assert sorc.matched_kind == "class"
        assert sorc.primary is None
        assert [a.name for a in sorc.ascendancies] == [
            "Stormweaver",
            "Chronomancer",
            "Disciple of Varashta",
        ]

        witch_ascs = svc.list_ascendancies("Witch")
        assert [a.name for a in witch_ascs] == [
            "Infernalist",
            "Blood Mage",
            "Lich",
            "Abyssal Lich",
        ]

        assert svc.inspect_ascendancy("varashta").primary.name == "Disciple of Varashta"  # type: ignore[union-attr]
        assert svc.inspect_ascendancy("gemling").primary.name == "Gemling Legionnaire"  # type: ignore[union-attr]
        assert svc.inspect_ascendancy("stonefist").primary.name == "Martial Artist"  # type: ignore[union-attr]

        for trap in ("Shadow", "Duelist", "Assassin", "Necromancer", "Scion", "Marauder"):
            with pytest.raises(LookupError, match="PoE2|exist|playable"):
                svc.inspect_ascendancy(trap)


def test_standalone_lua_tree_parser(tmp_path: Path) -> None:
    lua_fixture = tmp_path / "mini_tree.lua"
    lua_fixture.write_text(
        """return {
    -- minimal PoB2 0_5 Lua table fixture
    classes={
        [1]={
            integerId=9,
            name="Mercenary",
            base_str=11,
            base_dex=11,
            base_int=7,
            ascendancies={
                [1]={
                    id="Gemling Legionnaire",
                    internalId="Mercenary3",
                    name="Gemling Legionnaire"
                }
            }
        },
        [2]={
            integerId=1,
            name="Witch",
            base_str=7,
            base_dex=7,
            base_int=15,
            ascendancies={
                [1]={
                    id="Lich",
                    internalId="Witch3",
                    name="Lich",
                    replaceBy="Abyssal Lich"
                },
                [2]={
                    id="Abyssal Lich",
                    internalId="Witch3b",
                    name="Abyssal Lich",
                    replace="Lich"
                }
            }
        }
    },
    nodes={
        [100]={
            ascendancyName="Gemling Legionnaire",
            isNotable=true,
            isMultipleChoice=true,
            name="Implanted Gems",
            skill=100,
            stats={},
            connections={
                [1]={ id=101 }
            }
        },
        [101]={
            ascendancyName="Gemling Legionnaire",
            isMultipleChoiceOption=true,
            name="Motoric Implants",
            skill=101,
            stats={
                [1]="+2 to Level of all Skills with a Dexterity requirement"
            }
        },
        [200]={
            ascendancyName="Lich",
            isNotable=true,
            name="Price of Power",
            skill=200,
            stats={
                [1]="Spells consume a Power Charge if able to deal 40% more Damage"
            },
            options={
                ["Abyssal Lich"]={
                    ascendancyName="Abyssal Lich",
                    id=201,
                    name="Steward of Kulemak",
                    stats={
                        [1]="Spells consume a Power Charge to trigger Abyssal Apparition"
                    }
                }
            }
        }
    }
}
""",
        encoding="utf-8",
    )

    parsed = parse_lua_table(lua_fixture.read_text(encoding="utf-8"))
    assert len(parsed["classes"]) == 2

    with MetaService(tree_path=lua_fixture) as svc:
        gem = svc.inspect_ascendancy("Gemling Legionnaire").primary
        assert gem is not None
        assert gem.notables[0].choice_options[0].name == "Motoric Implants"
        assert "Choose one: Motoric Implants" in gem.notables[0].stats[0]

        ab = svc.inspect_ascendancy("Abyssal Lich").primary
        assert ab is not None
        assert ab.notables[0].name == "Steward of Kulemak"


def test_ladder_meta_aggregation_30m_cache_and_demand_signals(tmp_path: Path) -> None:
    tree_dir = _require_pob2_tree_dir()
    clock = [1_700_000_000.0]
    call_counts: dict[str, int] = {"ninja_builds": 0, "ggg_ladder": 0, "econ": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(POENINJA_BUILD_INDEX_URL):
            call_counts["ninja_builds"] += 1
            return httpx.Response(
                200,
                json={
                    "leagueBuilds": [
                        {
                            "leagueName": "Forbidden Rites",
                            "leagueUrl": "forbiddenrites",
                            "total": 124112,
                            "hardcore": False,
                            "statistics": [
                                {"class": "Gemling Legionnaire", "percentage": 31.92, "trend": -1},
                                {"class": "Martial Artist", "percentage": 10.70, "trend": 1},
                                {"class": "Oracle", "percentage": 7.29, "trend": 1},
                                {"class": "Disciple of Varashta", "percentage": 7.22, "trend": 1},
                                {"class": "Spirit Walker", "percentage": 6.88, "trend": 1},
                            ],
                        },
                        {
                            "leagueName": "HC Forbidden Rites",
                            "leagueUrl": "forbiddenriteshc",
                            "total": 6386,
                            "hardcore": True,
                            "statistics": [
                                {"class": "Gemling Legionnaire", "percentage": 20.67, "trend": 1},
                                {"class": "Disciple of Varashta", "percentage": 15.97, "trend": 1},
                                {"class": "Martial Artist", "percentage": 12.22, "trend": 1},
                            ],
                        },
                        {
                            "leagueName": "SSF Forbidden Rites",
                            "leagueUrl": "forbiddenritesssf",
                            "total": 8209,
                            "hardcore": False,
                            "statistics": [
                                {"class": "Gemling Legionnaire", "percentage": 19.49, "trend": 1},
                                {"class": "Disciple of Varashta", "percentage": 14.22, "trend": 1},
                            ],
                        },
                    ]
                },
            )
        if url.startswith(GGG_LADDER_BASE_URL):
            call_counts["ggg_ladder"] += 1
            if "HC%20Forbidden%20Rites" in url:
                return httpx.Response(
                    200,
                    json={
                        "total": 6386,
                        "entries": [
                            {
                                "rank": 1,
                                "dead": False,
                                "character": {
                                    "name": "VarashtaImmortal",
                                    "level": 100,
                                    "class": "Disciple of Varashta",
                                    "experience": 4250334444,
                                    "depth": {"solo": 620},
                                },
                                "account": {"name": "hc_god#0001"},
                            },
                            {
                                "rank": 2,
                                "dead": True,
                                "character": {
                                    "name": "GemlingTank",
                                    "level": 99,
                                    "class": "Gemling Legionnaire",
                                    "experience": 3950000000,
                                },
                                "account": {"name": "hc_merc#0002"},
                            },
                        ],
                    },
                )
            return httpx.Response(404, json={"error": {"code": 1, "message": "Resource not found"}})

        call_counts["econ"] += 1
        if "/ReferenceCurrencies" in url:
            return httpx.Response(
                200,
                json=[
                    {"ApiId": "exalted", "CurrentPrice": 1.0},
                    {"ApiId": "divine", "CurrentPrice": 448.2},
                    {"ApiId": "chaos", "CurrentPrice": 35.88},
                ],
            )
        if "/Uniques/ByCategory" in url:
            cat = request.url.params.get("category")
            if cat == "accessory":
                return httpx.Response(
                    200,
                    json={
                        "Items": [
                            {
                                "Name": "Mageblood",
                                "Type": "Utility Belt",
                                "CategoryApiId": "accessory",
                                "CurrentPrice": 62259.0,
                                "CurrentQuantity": 6524,
                                "IsChanceable": False,
                            },
                            {
                                "Name": "INCOMPLETE",
                                "Type": "Dull Hatchet",
                                "CategoryApiId": "weapon",
                                "CurrentPrice": 388423.0,
                                "CurrentQuantity": 13,
                                "IsChanceable": False,
                            },
                        ]
                    },
                )
            return httpx.Response(200, json={"Items": []})
        if "/economy/exchange/current/overview" in url:
            etype = request.url.params.get("type")
            if etype == "LineageSupportGems":
                return httpx.Response(
                    200,
                    json={
                        "core": {"primary": "divine", "rates": {"exalted": 448.2, "chaos": 12.5}},
                        "items": [{"id": "rakiatas-flow", "name": "Rakiata's Flow"}],
                        "lines": [
                            {
                                "id": "rakiatas-flow",
                                "primaryValue": 206.3,
                                "volumePrimaryValue": 3301.0,
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "core": {"primary": "divine", "rates": {"exalted": 448.2}},
                    "items": [],
                    "lines": [],
                },
            )
        return httpx.Response(200, json={"Items": []})

    transport = httpx.MockTransport(handler)
    http_client = Poe2HttpClient(sync_transport=transport)
    econ = EconomyService(http_client=http_client, cache_dir=tmp_path, now_fn=lambda: clock[0])
    svc = MetaService(
        tree_path=tree_dir,
        http_client=http_client,
        economy_service=econ,
        cache_dir=tmp_path,
        ttl_seconds=DEFAULT_META_TTL_SECONDS,
        now_fn=lambda: clock[0],
    )

    rep1 = svc.get_ladder_meta("Forbidden Rites", top_n=200)
    assert rep1.primary_mode.mode == "sc_trade"
    assert rep1.primary_mode.total_characters == 124112
    assert rep1.primary_mode.ascendancy_shares[0].ascendancy_name == "Gemling Legionnaire"
    assert rep1.primary_mode.ascendancy_shares[0].share_pct == pytest.approx(31.92)
    assert rep1.primary_mode.unlisted_share_pct == pytest.approx(35.99)
    assert "hc_trade" in rep1.mode_breakdown
    assert "ssf" in rep1.mode_breakdown
    assert rep1.ascendancy_rankings[0].ascendancy_name == "Gemling Legionnaire"
    assert rep1.ascendancy_rankings[0].tier == "S"

    signal_names = {s.name for s in rep1.chase_demand_signals}
    assert "Mageblood" in signal_names
    assert "Rakiata's Flow" in signal_names
    assert "INCOMPLETE" not in signal_names

    # Second call within 30m TTL should hit disk cache with zero new ninja_builds / ggg_ladder calls
    before_ninja = call_counts["ninja_builds"]
    before_ggg = call_counts["ggg_ladder"]
    clock[0] += 600.0
    rep2 = svc.get_ladder_meta("Forbidden Rites", top_n=200)
    assert rep2.primary_mode.total_characters == 124112
    assert call_counts["ninja_builds"] == before_ninja
    assert call_counts["ggg_ladder"] == before_ggg

    # Advance past 30m (1800s) TTL -> refreshes cleanly
    clock[0] += 1300.0
    _ = svc.get_ladder_meta("Forbidden Rites", top_n=200, include_economy_signals=False)
    assert call_counts["ninja_builds"] == before_ninja + 1

    # Direct GGG ladder response on HC extracts lvl 100, dead count, XP & Delve leaders
    hc_rep = svc.get_ladder_meta("HC Forbidden Rites", top_n=50, include_economy_signals=False)
    assert hc_rep.primary_mode.source == "ggg_public_ladder"
    assert hc_rep.primary_mode.level_100_count == 1
    assert hc_rep.primary_mode.dead_count == 1
    assert hc_rep.primary_mode.xp_leaders[0].character_name == "VarashtaImmortal"
    assert hc_rep.primary_mode.delve_leaders[0].delve_depth == 620


def test_rejects_legacy_poe1_ladder_contamination(tmp_path: Path) -> None:
    tree_dir = _require_pob2_tree_dir()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(GGG_LADDER_BASE_URL):
            # Simulate GGG legacy /api/ladders?id=Standard&realm=poe2 returning PoE1 Scion/Slayer
            return httpx.Response(
                200,
                json={
                    "total": 15000,
                    "entries": [
                        {
                            "rank": 1,
                            "dead": False,
                            "character": {"name": "Poe1Guy", "level": 100, "class": "Scion"},
                        },
                        {
                            "rank": 2,
                            "dead": False,
                            "character": {"name": "Poe1Spin", "level": 100, "class": "Slayer"},
                        },
                    ],
                },
            )
        if url.startswith(POENINJA_BUILD_INDEX_URL):
            return httpx.Response(
                200,
                json={
                    "leagueBuilds": [
                        {
                            "leagueName": "Standard",
                            "leagueUrl": "standard",
                            "total": 5000,
                            "hardcore": False,
                            "statistics": [
                                {"class": "Stormweaver", "percentage": 22.5, "trend": 0}
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"Items": []})

    http_client = Poe2HttpClient(sync_transport=httpx.MockTransport(handler))
    with MetaService(tree_path=tree_dir, http_client=http_client, cache_dir=tmp_path) as svc:
        rep = svc.get_ladder_meta("Standard", top_n=20, include_economy_signals=False)
        assert rep.primary_mode.source == "poeninja_build_index"
        assert rep.primary_mode.ascendancy_shares[0].ascendancy_name == "Stormweaver"
        assert any("Rejected legacy PoE1 ladder" in n for n in rep.source_notes)
