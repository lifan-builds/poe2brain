from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from poe2brain.http import GGGRateLimiter, Poe2HttpClient
from poe2brain.models.common import SaleType
from poe2brain.pob import BuildService, Pob2Bridge, Pob2SocketDaemon
from poe2brain.pob.bridge import _close_live_bridges

BASELINE_WAND = """Rarity: RARE
Storm Spire
Attuned Wand
Implicits: 0
55% increased Spell Damage
45% increased Lightning Damage
+2 to Level of all Lightning Spell Skills
+80 to maximum Mana
20% increased Cast Speed"""

BASELINE_CHEST = """Rarity: RARE
Glyph Wrap
Hexer's Robe
Implicits: 0
+180 to maximum Energy Shield
60% increased Energy Shield
+50 to Spirit
+42% to Fire Resistance
+40% to Cold Resistance
+38% to Lightning Resistance"""

BASELINE_FOCUS = """Rarity: RARE
Mind Ward
Hallowed Focus
Implicits: 0
+95 to maximum Energy Shield
45% increased Spell Damage
+1 to Level of all Spell Skills
+25% to Chaos Resistance"""

BASELINE_BOOTS = """Rarity: RARE
Wind Stride
Straw Sandals
Implicits: 0
+80 to Evasion Rating
25% increased Movement Speed
+35% to Fire Resistance
+35% to Cold Resistance
+40% to Lightning Resistance"""


@pytest.fixture(scope="module")
def build_service() -> Iterator[BuildService]:
    svc = BuildService(bridge=Pob2Bridge(use_daemon=False), owns_bridge=True)
    try:
        svc.create_character(
            "Sorceress",
            "Stormweaver",
            level=85,
            slot="main",
            name="Stormweaver Spark",
        )
        svc.set_gem_links(
            ["Spark 20/20", "Rapid Casting", "Controlled Destruction"],
            slot="main",
            label="Main Spark",
            include_in_full_dps=True,
            set_main=True,
        )
        svc.set_gem_links(
            ["Archmage 20/20"],
            slot="main",
            label="Archmage",
            include_in_full_dps=False,
            set_main=False,
        )
        svc.equip_item(BASELINE_WAND, target_slot="Weapon 1", slot="main")
        svc.equip_item(BASELINE_FOCUS, target_slot="Weapon 2", slot="main")
        svc.equip_item(BASELINE_CHEST, target_slot="Body Armour", slot="main")
        svc.equip_item(BASELINE_BOOTS, target_slot="Boots", slot="main")

        near = svc.search_passive_nodes("", limit=12, slot="main")
        starter_ids = [
            int(n["id"])
            for n in near
            if not n.get("allocated") and isinstance(n.get("pathDist"), int) and n["pathDist"] <= 3
        ]
        assert len(starter_ids) >= 2
        svc.alloc_passive_nodes(starter_ids[:3], slot="main")
        yield svc
    finally:
        svc.close()


def test_bridge_lifecycle_and_crash_recovery() -> None:
    with Pob2Bridge(use_daemon=False) as bridge:
        ping = bridge.ping()
        assert ping["ok"] is True
        assert ping["treeVersion"] == "0_5"
        assert bridge.is_running is True

        batch = bridge.call_batch(
            [
                {"method": "ping", "params": {}},
                {"method": "list_slots", "params": {}},
                {"method": "nonexistent_method", "params": {}},
            ]
        )
        assert len(batch) == 3
        assert batch[0]["ok"] is True
        assert batch[1]["ok"] is True
        assert batch[2]["ok"] is False

        # Simulate unexpected LuaJIT child death and verify transparent recovery
        assert bridge._proc is not None
        bridge._proc.kill()
        bridge._proc.wait(timeout=3.0)
        recovered = bridge.ping()
        assert recovered["ok"] is True
        assert bridge.is_running is True

    assert bridge.is_running is False


def test_bridge_atexit_cleanup_and_socket_daemon_reuse() -> None:
    def _is_pid_alive(pid: int) -> bool:
        if os.name == "nt":
            import ctypes

            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # 1. Unclosed direct Pob2Bridge is cleaned up by _close_live_bridges()
    leaky_bridge = Pob2Bridge(use_daemon=False)
    assert leaky_bridge.ping()["ok"] is True
    assert leaky_bridge._proc is not None
    leaky_pid = leaky_bridge._proc.pid
    assert _is_pid_alive(leaky_pid)
    _close_live_bridges()
    assert not leaky_bridge.is_running
    assert not _is_pid_alive(leaky_pid)

    # 2. Pob2SocketDaemon shares one warm LuaJIT process across clients and closes cleanly
    with Pob2SocketDaemon(lazy=False) as daemon:
        assert daemon.bridge._proc is not None
        daemon_pid = daemon.bridge._proc.pid
        assert _is_pid_alive(daemon_pid)

        with Pob2Bridge(socket_path=daemon.socket_path) as c1:
            p1 = c1.ping()
            assert p1["ok"] is True
        # Closing client 1 must keep the shared daemon alive
        assert _is_pid_alive(daemon_pid)

        with Pob2Bridge(socket_path=daemon.socket_path) as c2:
            p2 = c2.ping()
            assert p2["ok"] is True
            assert p2["bootSeconds"] == p1["bootSeconds"]

    assert not _is_pid_alive(daemon_pid)
    assert not daemon.socket_path.exists()


def test_slots_clone_undo_redo_and_isolation(build_service: BuildService) -> None:
    base_main = build_service.get_stats("main")
    assert base_main.class_name == "Sorceress"
    assert base_main.ascendancy_name == "Stormweaver"
    assert base_main.level == 85
    assert base_main.full_dps > 400.0
    assert base_main.spirit_total >= 150
    assert 85 <= base_main.spirit_reserved <= 100
    assert base_main.spirit_unreserved == base_main.spirit_total - base_main.spirit_reserved

    classes = build_service.list_classes()
    assert {c["name"] for c in classes} >= {
        "Witch",
        "Ranger",
        "Warrior",
        "Sorceress",
        "Huntress",
        "Mercenary",
        "Monk",
        "Druid",
    }

    build_service.clone_slot("main", "candidate", activate=True)
    after_unequip = build_service.unequip_item("Weapon 1", slot="candidate")
    assert after_unequip.full_dps < base_main.full_dps * 0.6

    # 'main' must remain completely isolated
    still_main = build_service.get_stats("main")
    assert math.isclose(still_main.full_dps, base_main.full_dps, rel_tol=1e-6)

    # Undo & redo on 'candidate'
    undone = build_service.undo("candidate")
    assert math.isclose(undone.full_dps, base_main.full_dps, rel_tol=1e-6)
    redone = build_service.redo("candidate")
    assert math.isclose(redone.full_dps, after_unequip.full_dps, rel_tol=1e-6)

    # Create & delete temporary Monk slot
    monk_stats = build_service.create_character("Monk", "Invoker", level=70, slot="temp_monk")
    assert monk_stats.class_name == "Monk"
    assert monk_stats.ascendancy_name == "Invoker"
    slot_names = {s["slot"] for s in build_service.list_slots()["slots"]}
    assert {"main", "candidate", "temp_monk"} <= slot_names
    build_service.delete_slot("temp_monk")


def test_universal_load_and_export_formats(build_service: BuildService, tmp_path: Path) -> None:
    base_main = build_service.get_stats("main")

    # 1. PoB2 eN... share code (padded & unpadded)
    code = build_service.export_build("main", fmt="code")
    assert code.startswith("eN")
    from_code = build_service.load_build(code.rstrip("="), slot="ref_code")
    assert math.isclose(from_code.full_dps, base_main.full_dps, rel_tol=1e-6)

    # 2. Raw XML file round-trip
    xml_text = build_service.export_build("main", fmt="xml")
    xml_path = tmp_path / "stormweaver.xml"
    xml_path.write_text(xml_text, encoding="utf-8")
    from_xml = build_service.load_build(xml_path, slot="ref_xml")
    assert math.isclose(from_xml.full_dps, base_main.full_dps, rel_tol=1e-6)

    # 3. Official GGG .build JSON file round-trip
    ggg_json = build_service.export_build("main", fmt="ggg_json", name="Stormweaver Planner")
    parsed_ggg = json.loads(ggg_json)
    assert parsed_ggg["name"] == "Stormweaver Planner"
    assert len(parsed_ggg["skills"]) == 2
    assert len(parsed_ggg["inventory_slots"]) == 4
    ggg_path = tmp_path / "stormweaver.build"
    ggg_path.write_text(ggg_json, encoding="utf-8")
    from_ggg = build_service.load_build(ggg_path, slot="ref_ggg", level=85)
    assert from_ggg.class_name == "Sorceress"
    assert from_ggg.ascendancy_name == "Stormweaver"
    assert from_ggg.full_dps > 400.0

    # 4. pobb.in URL fetch via mocked HTTP transport
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://pobb.in/TestPaste99/raw"
        return httpx.Response(200, text=code)

    mock_http = Poe2HttpClient(sync_transport=httpx.MockTransport(handler))
    url_svc = BuildService(bridge=build_service.bridge, http_client=mock_http)
    try:
        from_url = url_svc.load_build("https://pobb.in/TestPaste99", slot="ref_url")
        assert math.isclose(from_url.full_dps, base_main.full_dps, rel_tol=1e-6)
    finally:
        mock_http.close()

    # 5. poe.ninja character URL + poe.ninja/poe2db/maxroll share URLs via NinjaBuildsService
    seen_remote_urls: list[str] = []

    def multisite_handler(request: httpx.Request) -> httpx.Response:
        seen_remote_urls.append(str(request.url))
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
                        "version": "ab" * 32,
                        "snapshotName": "forbidden-rites",
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                        "timeMachineLabels": ["week-3"],
                    }
                ],
            )
        if path == "/poe2/pob/raw/overview/code":
            assert request.url.params["overview"] == "forbidden-rites"
            assert request.url.params["account"] == "0bibea-1410"
            assert request.url.params["name"] == "FR_obi"
            assert request.url.params["timemachine"] == "week-3"
            return httpx.Response(200, text=code)
        if path in (
            "/poe2/pob/raw/NinjaShare42",
            "/pob/Poe2dbShare77/raw",
            "/poe2/api/pob/MaxrollShare88",
        ):
            return httpx.Response(200, text=code)
        raise AssertionError(f"Unexpected remote build URL: {request.url}")

    ms_http = Poe2HttpClient(sync_transport=httpx.MockTransport(multisite_handler))
    ms_svc = BuildService(bridge=build_service.bridge, http_client=ms_http)
    try:
        ninja_char_stats = ms_svc.load_build(
            "https://poe.ninja/poe2/builds/forbiddenrites/character/0bibea%231410/FR_obi?timemachine=week-3",
            slot="ref_url",
        )
        assert math.isclose(ninja_char_stats.full_dps, base_main.full_dps, rel_tol=1e-6)
        assert ms_svc.inspect_build("ref_url")["character"]["name"] == "FR_obi"

        for share_url in (
            "https://poe.ninja/poe2/pob/NinjaShare42",
            "https://poe2db.tw/pob/Poe2dbShare77",
            "https://maxroll.gg/poe2/pob/MaxrollShare88",
        ):
            st = ms_svc.load_build(share_url, slot="ref_url")
            assert math.isclose(st.full_dps, base_main.full_dps, rel_tol=1e-6)

        with pytest.raises(ValueError, match="protected"):
            ms_svc.load_build(
                "https://poe.ninja/api/protected/poe2/builds/xyz/search?overview=forbidden-rites",
                slot="ref_url",
            )
    finally:
        ms_svc.switch_slot("main")
        for tmp_slot in ("ref_code", "ref_xml", "ref_ggg", "ref_url"):
            ms_svc.delete_slot(tmp_slot)
        ms_http.close()


def test_poe2_inspection_and_dewater_audit(build_service: BuildService) -> None:
    inspected = build_service.inspect_build("main", include_item_raw=True)
    assert inspected["raw_pob_stats"]["BlockChanceMax"] == 50
    assert inspected["raw_pob_stats"]["DeflectEffect"] == 40
    supports = build_service.list_valid_supports("main", group_index=1)
    assert len(supports) > 50

    build_service.clone_slot("main", "warrior_slot", activate=True)
    inflated = build_service.set_config(
        {
            "enemyIsBoss": "None",
            "resistancePenalty": 0,
            "usePowerCharges": True,
            "useFrenzyCharges": True,
            "buffOnslaught": True,
            "conditionEnemyShocked": True,
            "conditionShockEffect": 60,
            "enemyLightningResist": -40,
        },
        custom_mods="80% more Spell Damage\n+250 to maximum Energy Shield",
        slot="warrior_slot",
    )

    # Non-destructive audit (apply_dewater=False)
    audit = build_service.audit_build("warrior_slot", apply_dewater=False)
    assert audit.dps_inflation_pct > 100.0
    assert audit.raw_stats.full_dps > audit.sustainable_pinnacle_stats.full_dps * 2.0
    stripped_names = {f.flag for f in audit.stripped_flags}
    assert "customMods" in stripped_names
    assert "resistancePenalty" in stripped_names
    assert any("inflated" in w for w in audit.warnings)

    # Verify warrior_slot remained unchanged when apply_dewater=False
    after_readonly = build_service.get_stats("warrior_slot")
    assert math.isclose(after_readonly.full_dps, inflated.full_dps, rel_tol=1e-5)

    # Regression check: export eN... share code -> load into fresh slot -> audit_build
    # must preserve BOTH lines of multi-line customMods (<CustomModifierBlock>) AND strip them!
    warrior_code = build_service.export_build("warrior_slot", fmt="code")
    reloaded_warrior = build_service.load_build(warrior_code, slot="warrior_reloaded")
    assert math.isclose(reloaded_warrior.full_dps, inflated.full_dps, rel_tol=1e-5)
    assert math.isclose(reloaded_warrior.energy_shield, inflated.energy_shield, rel_tol=1e-5)
    reloaded_audit = build_service.audit_build("warrior_reloaded", apply_dewater=False)
    reloaded_flags = {f.flag: f for f in reloaded_audit.stripped_flags}
    assert "customMods" in reloaded_flags
    assert "+250 to maximum Energy Shield" in str(reloaded_flags["customMods"].raw_value)
    assert math.isclose(
        reloaded_audit.sustainable_pinnacle_stats.full_dps,
        audit.sustainable_pinnacle_stats.full_dps,
        rel_tol=1e-5,
    )

    # Apply dewatering persistently (apply_dewater=True) + verify 1-step undo
    applied_audit = build_service.audit_build("warrior_slot", apply_dewater=True)
    now_sust = build_service.get_stats("warrior_slot")
    assert math.isclose(
        now_sust.full_dps,
        applied_audit.sustainable_pinnacle_stats.full_dps,
        rel_tol=1e-5,
    )
    reverted = build_service.undo("warrior_slot")
    assert math.isclose(reverted.full_dps, inflated.full_dps, rel_tol=1e-5)


def test_item_diffs_and_passive_ranking(build_service: BuildService) -> None:
    gg_wand = """Rarity: RARE
Cataclysm Branch
Siphoning Wand
Implicits: 0
115% increased Spell Damage
90% increased Lightning Damage
+5 to Level of all Lightning Spell Skills
+180 to maximum Mana
35% increased Cast Speed"""

    weak_wand = """Rarity: RARE
Broken Stick
Attuned Wand
Implicits: 0
10% increased Spell Damage"""

    single = build_service.diff_item(
        gg_wand, target_slot="Weapon 1", slot="main", price_exalted=25.0
    )
    assert single.full_dps_delta > 300.0
    assert single.dps_per_exalted is not None and single.dps_per_exalted > 10.0

    batch = build_service.diff_items_batch(
        [
            {"label": "GG Wand", "item_text": gg_wand, "price_exalted": 25.0},
            {"label": "Weak Wand", "item_text": weak_wand, "price_exalted": 2.0},
            {
                "label": "Invalid PoE1 Base",
                "item_text": "Rarity: RARE\nBad\nProphecy Wand\nImplicits: 0",
            },
        ],
        target_slot="Weapon 1",
        slot="main",
    )
    assert len(batch) == 2  # Invalid base cleanly skipped
    assert batch[0].full_dps_delta > 300.0
    assert batch[1].full_dps_delta < 0.0

    # Reachable notable ranking + diff_passive_nodes consistency
    notables = build_service.rank_passive_upgrades(
        "main", mode="perPoint", stat="FullDPS", max_path_dist=8, top_n=8
    )
    assert len(notables) >= 3
    top = notables[0]
    assert top.per_point_delta > 0
    node_diff = build_service.diff_passive_nodes(
        add_node_ids=[top.node_id], slot="main", include_path=True
    )
    assert math.isclose(node_diff["delta"]["FullDPS"], top.full_path_delta, rel_tol=1e-4)

    # Full-tree anoint ranking (fast pre-pass + exact FullDPS refinement)
    anoints = build_service.rank_passive_upgrades("main", mode="anoint", top_n=5)
    assert len(anoints) == 5
    assert anoints[0].single_node_delta >= top.single_node_delta


def test_slot_stat_weights_occupied_and_empty(build_service: BuildService) -> None:
    wand_w = build_service.compute_slot_trade_weights(
        "Weapon 1",
        build_slot="main",
        metric="FullDPS",
        max_price_exalted=40.0,
        securable_only=True,
        top_n=15,
    )
    assert len(wand_w.weights) >= 8
    assert wand_w.weights[0].dps_per_unit > 0
    q = wand_w.trade2_weighted_filter["query"]
    assert q["status"]["option"] == "securable"
    assert q["filters"]["trade_filters"]["filters"]["price"] == {"max": 40.0}

    # Empty slot ('Ring 1')
    ring_w = build_service.compute_slot_trade_weights(
        "Ring 1",
        build_slot="main",
        metric="balanced",
        min_rune_sockets=1,
        top_n=15,
    )
    assert len(ring_w.weights) >= 5
    assert ring_w.weights[0].dps_per_unit > 0
    assert ring_w.weights[0].ehp_per_unit > 0
    rq = ring_w.trade2_weighted_filter["query"]
    assert rq["filters"]["equipment_filters"]["filters"]["rune_sockets"]["min"] == 1


def test_find_live_slot_upgrades_mocked_closed_loop(build_service: BuildService) -> None:
    fetch_items = [
        {
            "id": "listing_steal_securable",
            "listing": {
                "indexed": "2026-11-23T02:00:00Z",
                "fee": 14200,
                "account": {"name": "HonestCaster", "online": None},
                "price": {"type": "~b/o", "amount": 20, "currency": "exalted"},
            },
            "item": {
                "rarity": "Rare",
                "name": "Tempest Horn",
                "baseType": "Siphoning Wand",
                "ilvl": 82,
                "explicitMods": [
                    {
                        "description": "105% increased [SpellDamage|Spell Damage]",
                        "domain": "explicit",
                    },
                    {
                        "description": "80% increased [Lightning|Lightning] Damage",
                        "domain": "explicit",
                    },
                    {
                        "description": "+5 to Level of all [Lightning|Lightning] Spell Skills",
                        "domain": "explicit",
                    },
                    {"description": "+160 to [MaximumMana|maximum Mana]", "domain": "explicit"},
                    {"description": "30% increased [CastSpeed|Cast Speed]", "domain": "explicit"},
                ],
            },
        },
        {
            "id": "listing_duplicate_same_account",
            "listing": {
                "indexed": "2026-11-23T02:01:00Z",
                "fee": 14200,
                "account": {"name": "HonestCaster", "online": None},
                "price": {"type": "~b/o", "amount": 22, "currency": "exalted"},
            },
            "item": {
                "rarity": "Rare",
                "name": "Clone Horn",
                "baseType": "Siphoning Wand",
                "ilvl": 82,
                "explicitMods": [
                    {"description": "110% increased Spell Damage", "domain": "explicit"},
                    {
                        "description": "+5 to Level of all Lightning Spell Skills",
                        "domain": "explicit",
                    },
                ],
            },
        },
        {
            "id": "listing_expensive_whisper",
            "listing": {
                "indexed": "2026-11-23T02:05:00Z",
                "whisper": "@MirrorCrafter Hi, I would like to buy your Apocalypse Spire",
                "account": {"name": "MirrorCrafter", "online": {"league": "Forbidden Rites"}},
                "price": {"type": "~price", "amount": 1, "currency": "divine"},
            },
            "item": {
                "rarity": "Rare",
                "name": "Apocalypse Spire",
                "baseType": "Siphoning Wand",
                "ilvl": 84,
                "explicitMods": [
                    {"description": "120% increased Spell Damage", "domain": "explicit"},
                    {"description": "95% increased Lightning Damage", "domain": "explicit"},
                    {
                        "description": "+5 to Level of all Lightning Spell Skills",
                        "domain": "explicit",
                    },
                    {"description": "+190 to maximum Mana", "domain": "explicit"},
                    {"description": "35% increased Cast Speed", "domain": "explicit"},
                ],
            },
        },
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/trade2/search/poe2/" in url:
            body = json.loads(request.content.decode("utf-8"))
            assert body["query"]["stats"][0]["type"] == "count"
            assert len(body["query"]["stats"][0]["filters"]) >= 4
            return httpx.Response(
                200,
                json={
                    "id": "q_mock_storm",
                    "total": 3,
                    "result": [e["id"] for e in fetch_items],
                },
            )
        if "/api/trade2/fetch/" in url:
            return httpx.Response(200, json={"result": fetch_items})
        if "/ReferenceCurrencies" in url or "/Items" in url or "/exchange/" in url:
            return httpx.Response(404, json={"error": "offline mock"})
        raise AssertionError(f"Unexpected URL in mock test: {url}")

    limiter = GGGRateLimiter(sleep_sync=lambda _: None)
    mock_http = Poe2HttpClient(
        rate_limiter=limiter, sync_transport=httpx.MockTransport(mock_handler)
    )
    closed_loop_svc = BuildService(bridge=build_service.bridge, http_client=mock_http)
    try:
        # 1. Ranked by raw FullDPS gain
        report = closed_loop_svc.find_live_slot_upgrades(
            "Weapon 1",
            build_slot="main",
            metric="FullDPS",
            league="Forbidden Rites",
            max_price_exalted=500.0,
            candidate_limit=5,
        )
        assert report.query_id == "q_mock_storm"
        assert (
            report.trade_url
            == "https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/q_mock_storm"
        )
        # Duplicate HonestCaster listing deduplicated -> 2 unique sellers evaluated!
        assert len(report.candidates) == 2
        assert report.candidates[0].listing.item_name == "Apocalypse Spire"
        assert report.candidates[0].listing.sale_type == SaleType.WHISPER
        assert report.candidates[1].listing.item_name == "Tempest Horn"
        assert report.candidates[1].listing.sale_type == SaleType.SECURABLE
        assert report.candidates[1].listing.is_instant_buyout is True
        assert (
            report.candidates[0].upgrade_diff.full_dps_delta
            > report.candidates[1].upgrade_diff.full_dps_delta
            > 300.0
        )

        # 2. Ranked by DPS-per-Exalted value efficiency -> 20ex Tempest Horn wins!
        value_report = closed_loop_svc.find_live_slot_upgrades(
            "Weapon 1",
            build_slot="main",
            metric="FullDPS",
            league="Forbidden Rites",
            max_price_exalted=500.0,
            candidate_limit=5,
            sort_by_value=True,
        )
        assert value_report.candidates[0].listing.item_name == "Tempest Horn"
        assert (
            value_report.candidates[0].dps_gain_per_exalted is not None
            and value_report.candidates[1].dps_gain_per_exalted is not None
            and value_report.candidates[0].dps_gain_per_exalted
            > value_report.candidates[1].dps_gain_per_exalted * 10
        )
    finally:
        mock_http.close()


def test_morior_invictus_socketed_items_evaluates_per_socket_filled_in_pob2(
    build_service: BuildService,
) -> None:
    from poe2brain.trade.item_parser import trade2_fetch_to_pob2_text

    build_service.create_character("Monk", "Martial Artist", level=90, slot="morior_test")
    base_stats = build_service.get_stats(slot="morior_test")

    morior_fetch = {
        "item": {
            "name": "Morior Invictus",
            "baseType": "Grand Regalia",
            "rarity": "Unique",
            "ilvl": 82,
            "sockets": [
                {"group": 0, "type": "rune"},
                {"group": 1, "type": "rune"},
                {"group": 2, "type": "rune"},
                {"group": 3, "type": "rune"},
            ],
            "socketedItems": [
                {"socket": 0, "baseType": "Soul Core of Citaqualotl"},
                {"socket": 1, "baseType": "Soul Core of Citaqualotl"},
                {"socket": 2, "baseType": "Soul Core of Citaqualotl"},
                {"socket": 3, "baseType": "Soul Core of Tacati"},
            ],
            "explicitMods": [
                {
                    "description": "300% increased Armour, Evasion and Energy Shield",
                    "domain": "explicit",
                },
                {"description": "+14 to [Spirit] per Socket filled", "domain": "explicit"},
                {
                    "description": (
                        "+10% to all [ElementalResistances|Elemental Resistances] per Socket filled"
                    ),
                    "domain": "explicit",
                },
            ],
        }
    }
    pob_txt = trade2_fetch_to_pob2_text(morior_fetch)
    diff = build_service.diff_item(pob_txt, target_slot="Body Armour", slot="morior_test")
    assert diff.spirit_unreserved_delta == 56
    equipped = build_service.equip_item(pob_txt, target_slot="Body Armour", slot="morior_test")
    assert equipped.spirit_total == base_stats.spirit_total + 56
    assert equipped.fire_resist >= base_stats.fire_resist + 55.0


@pytest.mark.skipif(
    os.environ.get("POE2_LIVE_TESTS") != "1",
    reason="Set POE2_LIVE_TESTS=1 to run live GGG trade2 closed-loop test",
)
def test_find_live_slot_upgrades_real_trade2(build_service: BuildService) -> None:
    report = build_service.find_live_slot_upgrades(
        "Weapon 1",
        build_slot="main",
        metric="FullDPS",
        league="Forbidden Rites",
        max_price_exalted=30.0,
        securable_only=True,
        candidate_limit=3,
    )
    assert report.query_id is not None
    assert report.total_market_matches >= 0


def test_explain_calc_provenance_and_skill_group_warning(build_service: BuildService) -> None:
    build_service.clone_slot("main", "calc_prov_slot")
    report = build_service.explain_calc("EnergyShield", slot="calc_prov_slot")
    assert report.query == "EnergyShield"
    assert len(report.matched_stats) >= 1
    assert report.modifier_count_total >= 2
    source_cats = {m.source_type for m in report.modifiers}
    assert "Item" in source_cats
    assert any(st.source_type == "Item" and st.mod_count >= 2 for st in report.source_totals)

    dps_report = build_service.explain_calc("FullDPS", slot="calc_prov_slot")
    assert "Spark" in dps_report.active_skill_name
    assert len(dps_report.matched_stats) >= 1

    # Point Calcs tab at group 2 (Archmage) and verify automatic main-skill mismatch warning
    xml_raw = build_service.export_build(slot="calc_prov_slot", fmt="xml")
    skewed_xml = xml_raw.replace(
        '<Input name="skill_number" number="1"/>',
        '<Input name="skill_number" number="2"/>',
    )
    if 'name="skill_number"' not in skewed_xml:
        skewed_xml = skewed_xml.replace(
            "<Calcs>",
            '<Calcs><Input name="skill_number" number="2"/>',
            1,
        )
    build_service.load_build(skewed_xml, slot="calc_prov_skew")
    skew_report = build_service.explain_calc("AverageDamage", slot="calc_prov_skew")
    assert skew_report.skill_group == 1
    assert any("group #2" in w or "skill_number" in w for w in skew_report.warnings)


def test_rank_support_gems_tiers_and_lineage_conflicts(build_service: BuildService) -> None:
    build_service.clone_slot("main", "supp_test")
    report_all = build_service.rank_support_gems(slot="supp_test", top_n=12)
    assert "Spark" in report_all.active_skill_label
    assert report_all.total_valid >= 25
    assert len(report_all.candidates) == 12
    assert report_all.candidates[0].dps_delta > 0.0

    # Tier filter (max_tier=2 excludes Tier 3-5, Tier 0 unassigned, and Lineage supports)
    report_t2 = build_service.rank_support_gems(
        slot="supp_test",
        max_tier=2,
        include_lineage=False,
        top_n=10,
    )
    assert all(not c.is_lineage for c in report_t2.candidates)
    assert all(
        c.support_tier is not None and 1 <= c.support_tier <= 2
        for c in report_t2.candidates
    )

    # Equip top Lineage candidate on a secondary group and verify conflict detection
    lineage_cands = [c for c in report_all.candidates if c.is_lineage]
    if lineage_cands:
        chosen_lin = lineage_cands[0].name
        build_service.set_gem_links(
            ["Orb of Storms 20/20", chosen_lin],
            slot="supp_test",
            label="Secondary Orb",
            include_in_full_dps=False,
            set_main=False,
        )
        conflict_rep = build_service.rank_support_gems(slot="supp_test", top_n=25)
        flagged = [c for c in conflict_rep.candidates if c.name == chosen_lin]
        assert flagged and flagged[0].lineage_conflict_skill is not None

        filtered_rep = build_service.rank_support_gems(
            slot="supp_test",
            exclude_lineage_conflicts=True,
            top_n=25,
        )
        assert all(c.name != chosen_lin for c in filtered_rep.candidates)


def test_rank_radius_jewels_controlled_metamorphosis_and_against_darkness(
    build_service: BuildService,
) -> None:
    cm_report = build_service.rank_radius_jewels(
        "Controlled Metamorphosis",
        slot="main",
        max_path_dist=15,
        top_n=6,
    )
    assert cm_report.jewel_name == "Controlled Metamorphosis"
    assert len(cm_report.candidates) >= 1
    top = cm_report.candidates[0]
    assert top.ring_label != ""
    assert len(top.ring_notables) >= 1
    assert top.per_point_delta >= 0.0

    atd_report = build_service.rank_radius_jewels(
        "Against the Darkness",
        slot="main",
        max_path_dist=15,
        top_n=5,
    )
    assert atd_report.jewel_name == "Against the Darkness"
    assert len(atd_report.candidates) >= 1
    assert any(c.delta > 0.0 for c in atd_report.candidates)


def test_what_if_transactional_rollback_and_borrow_attribution(
    build_service: BuildService,
) -> None:
    build_service.clone_slot("main", "wi_base")
    build_service.clone_slot("main", "wi_ref")
    build_service.equip_item(
        """Rarity: RARE
God Wand
Attuned Wand
Quality: 20
Sockets: S
Rune: Greater Iron Rune
Implicits: 0
140% increased Spell Damage
110% increased Lightning Damage
+5 to Level of all Lightning Spell Skills
+220 to maximum Mana
38% increased Cast Speed""",
        target_slot="Weapon 1",
        slot="wi_ref",
    )

    before_stats = build_service.get_stats(slot="wi_base")
    wi_report = build_service.what_if(
        slot="wi_base",
        edit_slot="Body Armour",
        add_mod_lines=["+120 to maximum Energy Shield"],
        set_quality=20,
        borrow_from_slot="wi_ref",
        borrow_slots=["Weapon 1"],
        include_scenario_matrix=True,
        apply=False,
    )
    assert wi_report.applied is False
    assert wi_report.rolled_back is True
    assert wi_report.effective_dps_delta > 200.0
    assert wi_report.after_stats.energy_shield > wi_report.baseline_stats.energy_shield
    assert len(wi_report.piece_attributions) >= 2
    assert len(wi_report.scenario_matrix) >= 3

    # Verify non-destructive rollback on wi_base
    after_rollback = build_service.get_stats(slot="wi_base")
    assert after_rollback.full_dps == pytest.approx(before_stats.full_dps, rel=1e-6)
    assert after_rollback.energy_shield == pytest.approx(before_stats.energy_shield, rel=1e-6)

    # Verify apply=True commits the mutation
    applied = build_service.what_if(
        slot="wi_base",
        edit_slot="Body Armour",
        set_quality=20,
        apply=True,
    )
    assert applied.applied is True
    assert applied.rolled_back is False
    after_apply = build_service.get_stats(slot="wi_base")
    assert after_apply.energy_shield > before_stats.energy_shield


def test_find_live_slot_upgrades_craft_topoff_beats_prefinished_corrupted(
    build_service: BuildService,
) -> None:
    fetch_items = [
        {
            "id": "corrupt_prefinished",
            "listing": {
                "saleType": "securable",
                "account": {"name": "CorruptSeller"},
                "price": {"type": "~price", "amount": 40, "currency": "exalted"},
            },
            "item": {
                "name": "Vaal Rod",
                "baseType": "Attuned Wand",
                "rarity": "Rare",
                "ilvl": 82,
                "corrupted": True,
                "sockets": [{"group": 0, "type": "rune"}],
                "socketedItems": [
                    {"socket": 0, "baseType": "Greater Iron Rune"},
                ],
                "runeMods": ["30% increased Spell Damage"],
                "explicitMods": [
                    {"description": "80% increased Spell Damage", "domain": "explicit"},
                    {
                        "description": "+3 to Level of all Lightning Spell Skills",
                        "domain": "explicit",
                    },
                    {"description": "25% increased Cast Speed", "domain": "explicit"},
                ],
            },
        },
        {
            "id": "clean_unfinished",
            "listing": {
                "saleType": "securable",
                "account": {"name": "RawCraftSeller"},
                "price": {"type": "~price", "amount": 35, "currency": "exalted"},
            },
            "item": {
                "name": "Clean Spire",
                "baseType": "Attuned Wand",
                "rarity": "Rare",
                "ilvl": 82,
                "explicitMods": [
                    {"description": "95% increased Spell Damage", "domain": "explicit"},
                    {
                        "description": "+3 to Level of all Lightning Spell Skills",
                        "domain": "explicit",
                    },
                    {"description": "25% increased Cast Speed", "domain": "explicit"},
                ],
            },
        },
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/trade2/search/poe2/" in url:
            return httpx.Response(
                200,
                json={
                    "id": "q_topoff_test",
                    "total": 2,
                    "result": [e["id"] for e in fetch_items],
                },
            )
        if "/api/trade2/fetch/" in url:
            return httpx.Response(200, json={"result": fetch_items})
        return httpx.Response(404, json={"error": "offline"})

    limiter = GGGRateLimiter(sleep_sync=lambda _: None)
    mock_http = Poe2HttpClient(
        rate_limiter=limiter, sync_transport=httpx.MockTransport(mock_handler)
    )
    svc = BuildService(bridge=build_service.bridge, http_client=mock_http)
    try:
        raw_only = svc.find_live_slot_upgrades(
            "Weapon 1",
            build_slot="main",
            normalize_craft_topoff=False,
        )
        # Without top-off, the pre-socketed (+30% rune) corrupt wand has 110% inc vs 95% inc!
        assert raw_only.candidates[0].listing.item_name == "Vaal Rod"
        assert raw_only.candidates[0].top_off_applied == []

        topped = svc.find_live_slot_upgrades(
            "Weapon 1",
            build_slot="main",
            normalize_craft_topoff=True,
        )
        # With top-off, Clean Spire gets +1x Greater Iron Rune (+30% Spell Damage) and wins
        clean_cand = next(c for c in topped.candidates if c.listing.item_name == "Clean Spire")
        corrupt_cand = next(c for c in topped.candidates if c.listing.item_name == "Vaal Rod")
        assert corrupt_cand.top_off_applied == []
        assert len(clean_cand.top_off_applied) >= 1
        assert clean_cand.raw_full_dps_delta is not None
        assert clean_cand.upgrade_diff.full_dps_delta > clean_cand.raw_full_dps_delta
        assert topped.candidates[0].listing.item_name == "Clean Spire"
    finally:
        mock_http.close()
