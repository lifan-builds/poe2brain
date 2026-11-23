from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

import poe2brain.mcp_server as mcp_mod
from poe2brain.cli import app
from poe2brain.config import REPO_ROOT, get_pob2_paths

SAMPLE_RARE_WAND_CTRL_C = """Item Class: Wands
Rarity: Rare
Storm Horn
Attuned Wand
--------
Quality: +20% (augmented)
--------
Requires: Level 78, 142 Int
--------
Sockets: S
--------
Item Level: 82
--------
35% increased Spell Damage (rune)
--------
84% increased Spell Damage (fractured)
+4 to Level of all Lightning Spell Skills
26% increased Cast Speed
+115 to maximum Mana (desecrated)
"""


@pytest.fixture()
def isolated_cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Provide an isolated POE2BRAIN_CACHE_DIR linked to the bootstrapped PoB2 runtime."""
    real_pob2 = get_pob2_paths().root_dir
    assert real_pob2.is_dir(), "Expected ~/.cache/poe2brain/pob2 to be bootstrapped"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "pob2").symlink_to(real_pob2, target_is_directory=True)

    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(cache_dir))
    mcp_mod._HYDRATED_SLOTS.clear()

    env = os.environ.copy()
    env["POE2BRAIN_CACHE_DIR"] = str(cache_dir)
    return env


def test_skill_readme_mcp_and_cli_1_to_1_parity() -> None:
    skill_text = (REPO_ROOT / ".agents" / "skills" / "poe2brain" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    skill_tools = set(re.findall(r"\|\s*`?(poe2_[a-z0-9_]+)`?\s*\|", skill_text))
    readme_tools = set(re.findall(r"\|\s*`?(poe2_[a-z0-9_]+)`?\s*\|", readme_text))
    assert len(skill_tools) == 24
    assert skill_tools == readme_tools

    registered_tools = {t.name for t in mcp_mod.mcp._tool_manager.list_tools()}
    assert registered_tools == skill_tools

    runner = CliRunner()
    for group in ("wiki", "pob", "econ", "trade", "craft", "sync", "bootstrap"):
        res = runner.invoke(app, [group, "--help"])
        assert res.exit_code == 0, f"Failed --help for group {group}: {res.stdout}"


def test_cli_wiki_and_trade_parse(isolated_cli_env: dict[str, str]) -> None:
    del isolated_cli_env
    runner = CliRunner()

    s_res = runner.invoke(app, ["wiki", "search", "Deflection", "--limit", "5", "--json"])
    assert s_res.exit_code == 0
    s_data = json.loads(s_res.stdout)
    assert s_data["count"] >= 1

    m_res = runner.invoke(app, ["wiki", "mechanic", "Chaos vs Energy Shield", "--json"])
    assert m_res.exit_code == 0
    m_data = json.loads(m_res.stdout)
    assert len(m_data["facts"]) >= 1

    g_res = runner.invoke(app, ["wiki", "gem", "Grim Feast", "--json"])
    assert g_res.exit_code == 0
    g_data = json.loads(g_res.stdout)
    assert g_data["name"] == "Grim Feast"

    b_res = runner.invoke(app, ["wiki", "base", "Expert Hexer's Robe", "--ilvl", "82", "--json"])
    assert b_res.exit_code == 0
    b_data = json.loads(b_res.stdout)
    assert b_data.get("warning")

    a_res = runner.invoke(app, ["wiki", "augment", "Iron Rune", "--json"])
    assert a_res.exit_code == 0
    a_data = json.loads(a_res.stdout)
    assert len(a_data["augments"]) >= 1

    rf_res = runner.invoke(app, ["wiki", "runeforge", "Ward", "--limit", "5", "--json"])
    assert rf_res.exit_code == 0
    rf_data = json.loads(rf_res.stdout)
    assert rf_data["count"] >= 1

    p_res = runner.invoke(app, ["trade", "parse", "--text", SAMPLE_RARE_WAND_CTRL_C, "--json"])
    assert p_res.exit_code == 0
    p_data = json.loads(p_res.stdout)
    assert p_data["base_type"] == "Attuned Wand"
    assert p_data["fractured"] is True
    assert "Rarity: RARE" in p_data["pob_item_text"]

    # New Pillar 1 Ascendancy list + inspection + PoE1 LookupError JSON trap
    asc_list_res = runner.invoke(app, ["wiki", "ascendancy", "--json"])
    assert asc_list_res.exit_code == 0
    asc_list = json.loads(asc_list_res.stdout)
    assert asc_list["class_count"] == 8
    assert asc_list["count"] == 23

    gemling_res = runner.invoke(app, ["wiki", "ascendancy", "Gemling Legionnaire", "--json"])
    assert gemling_res.exit_code == 0
    gemling_data = json.loads(gemling_res.stdout)
    assert gemling_data["primary"]["name"] == "Gemling Legionnaire"
    assert gemling_data["primary"]["choice_option_count"] == 3

    abyss_res = runner.invoke(app, ["wiki", "ascendancy", "Abyssal Lich", "--json"])
    assert abyss_res.exit_code == 0
    abyss_data = json.loads(abyss_res.stdout)
    assert abyss_data["primary"]["replaces_ascendancy"] == "Lich"
    assert "Steward of Kulemak" in abyss_data["primary"]["notable_names"]

    necro_res = runner.invoke(app, ["wiki", "ascendancy", "Necromancer", "--json"])
    assert necro_res.exit_code == 1
    assert "Traceback" not in necro_res.stdout
    necro_err = json.loads(necro_res.stdout)
    assert necro_err["ok"] is False
    assert necro_err["error_type"] == "LookupError"
    assert "Necromancer" in necro_err["error"]


def test_cli_craft_odds_plan_and_removed_lever_structured_error(
    isolated_cli_env: dict[str, str],
) -> None:
    del isolated_cli_env
    runner = CliRunner()

    # 1. Single-step odds on Greater Exalted Orb without item_text (auto-infers Rare state)
    odds_res = runner.invoke(
        app,
        [
            "craft",
            "odds",
            "--base",
            "Attuned Wand",
            "--ilvl",
            "82",
            "--lever",
            "Greater Exalted Orb",
            "--target",
            "SpellDamageOnWeapon8_",
            "--offline-prices",
            "--json",
        ],
    )
    assert odds_res.exit_code == 0, odds_res.stdout
    odds_data = json.loads(odds_res.stdout)
    assert odds_data["assumed_rarity"] == "rare"
    assert odds_data["hit_probability"] > 0.0
    assert odds_data["step_cost_exalted"] > 0.0

    # 2. Multi-step crafting plan with buy-vs-craft verdict
    plan_res = runner.invoke(
        app,
        [
            "craft",
            "plan",
            "--base",
            "Attuned Wand",
            "--ilvl",
            "82",
            "--target",
            "SpellDamageOnWeapon8_",
            "--buy-exalted",
            "900",
            "--sims",
            "400",
            "--offline-prices",
            "--json",
        ],
    )
    assert plan_res.exit_code == 0, plan_res.stdout
    plan_data = json.loads(plan_res.stdout)
    assert plan_data["expected_cost_exalted"] > 0
    assert plan_data["p80_cost_exalted"] >= plan_data["p50_cost_exalted"]
    assert plan_data["trade_vs_craft"]["recommendation"] in ("buy", "craft", "tie")

    # 3. Removed Patch 0.4 Omen of Homogenising Exaltation -> structured JSON error, zero traceback
    rem_res = runner.invoke(
        app,
        [
            "craft",
            "odds",
            "--base",
            "Attuned Wand",
            "--ilvl",
            "82",
            "--lever",
            "Exalted Orb",
            "--omen",
            "Omen of Homogenising Exaltation",
            "--offline-prices",
            "--json",
        ],
    )
    assert rem_res.exit_code == 1
    assert "Traceback" not in rem_res.stdout
    rem_err = json.loads(rem_res.stdout)
    assert rem_err["ok"] is False
    assert rem_err["error_type"] == "RemovedLeverError"
    assert "0.4" in rem_err["error"]

    # 4. Profitable craft scanner (`poe2 craft profit --budget low --offline-prices --json`)
    prof_res = runner.invoke(
        app,
        [
            "craft",
            "profit",
            "--budget",
            "low",
            "--offline-prices",
            "--sims",
            "250",
            "--json",
        ],
    )
    assert prof_res.exit_code == 0, prof_res.stdout
    prof_data = json.loads(prof_res.stdout)
    assert prof_data["total_recipes_evaluated"] >= 2
    assert prof_data["profitable_count"] >= 1
    assert prof_data["top_overall"][0]["expected_roi_pct"] > 0.0
    assert len(prof_data["top_overall"][0]["execution_steps"]) >= 3


def test_cli_meta_and_arbitrage_commands_mocked(
    isolated_cli_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_cli_env
    import httpx

    from poe2brain.http import GGGRateLimiter, Poe2HttpClient
    from poe2brain.knowledge import MetaService
    from poe2brain.trade import ArbitrageScanner, EconomyService, Trade2Client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/build-index-state"):
            return httpx.Response(
                200,
                json={
                    "leagueBuilds": [
                        {
                            "leagueName": "Forbidden Rites",
                            "leagueUrl": "forbidden-rites",
                            "total": 1000,
                            "status": 0,
                            "statistics": [
                                {"class": "Gemling Legionnaire", "count": 320, "trend": 1},
                                {"class": "Martial Artist", "count": 120, "trend": 1},
                                {"class": "Disciple of Varashta", "count": 90, "trend": 1},
                            ],
                        }
                    ]
                },
            )
        if "/api/ladders" in path:
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/ReferenceCurrencies"):
            return httpx.Response(
                200,
                json=[
                    {"ApiId": "exalted", "Text": "Exalted Orb", "CurrentPrice": 1.0},
                    {"ApiId": "chaos", "Text": "Chaos Orb", "CurrentPrice": 36.0},
                    {"ApiId": "divine", "Text": "Divine Orb", "CurrentPrice": 450.0},
                ],
            )
        if path.endswith("/Items"):
            return httpx.Response(
                200,
                json=[
                    {
                        "ItemId": 1,
                        "ApiId": "divine",
                        "Text": "Divine Orb",
                        "CategoryApiId": "currency",
                        "CurrentPrice": 450.0,
                    },
                    {
                        "ItemId": 2,
                        "ApiId": "chaos",
                        "Text": "Chaos Orb",
                        "CategoryApiId": "currency",
                        "CurrentPrice": 36.0,
                    },
                    {
                        "ItemId": 3,
                        "ApiId": "origin-spark",
                        "Text": "Origin Spark",
                        "CategoryApiId": "fragments",
                        "CurrentPrice": 1800.0,
                    },
                    {
                        "ItemId": 4,
                        "ApiId": "origin-cradle",
                        "Text": "Origin Cradle",
                        "CategoryApiId": "fragments",
                        "CurrentPrice": 2000.0,
                    },
                    {
                        "ItemId": 5,
                        "ApiId": "origin-core",
                        "Text": "Origin Core",
                        "CategoryApiId": "fragments",
                        "CurrentPrice": 4600.0,
                    },
                ],
            )
        if path.endswith("/economy/exchange/current/overview"):
            extype = request.url.params.get("type", "Currency")
            if extype == "Fragments":
                return httpx.Response(
                    200,
                    json={
                        "core": {
                            "primary": "divine",
                            "secondary": "chaos",
                            "rates": {"exalted": 450.0, "chaos": 12.5},
                            "items": [],
                        },
                        "items": [
                            {"id": "origin-spark", "name": "Origin Spark"},
                            {"id": "origin-cradle", "name": "Origin Cradle"},
                            {"id": "origin-core", "name": "Origin Core"},
                        ],
                        "lines": [
                            {
                                "id": "origin-spark",
                                "primaryValue": 4.0,
                                "volumePrimaryValue": 80.0,
                            },
                            {
                                "id": "origin-cradle",
                                "primaryValue": 4.4,
                                "volumePrimaryValue": 85.0,
                            },
                            {
                                "id": "origin-core",
                                "primaryValue": 10.2,
                                "volumePrimaryValue": 120.0,
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "core": {
                        "primary": "divine",
                        "secondary": "chaos",
                        "rates": {"exalted": 450.0, "chaos": 12.5},
                        "items": [],
                    },
                    "items": [],
                    "lines": [],
                },
            )
        return httpx.Response(200, json={"result": []})

    limiter = GGGRateLimiter(safety_buffer=0.0, sleep_sync=lambda _: None)
    http_client = Poe2HttpClient(rate_limiter=limiter, sync_transport=httpx.MockTransport(handler))
    econ = EconomyService(http_client=http_client, cache_dir=tmp_path / "http_mock")
    t2 = Trade2Client(http_client=http_client, economy=econ, cache_dir=tmp_path / "http_mock")
    meta_svc = MetaService(
        http_client=http_client,
        economy_service=econ,
        cache_dir=tmp_path / "http_mock",
    )
    arb_scanner = ArbitrageScanner(
        economy=econ,
        trade2=t2,
        db=mcp_mod.get_knowledge_service().db,
    )
    monkeypatch.setattr(mcp_mod, "_META_SVC", meta_svc)
    monkeypatch.setattr(mcp_mod, "_ARBITRAGE_SCANNER", arb_scanner)

    runner = CliRunner()
    meta_res = runner.invoke(app, ["wiki", "meta", "--no-economy", "--json"])
    assert meta_res.exit_code == 0, meta_res.stdout
    meta_data = json.loads(meta_res.stdout)
    assert meta_data["ascendancy_rankings"][0]["ascendancy_name"] == "Gemling Legionnaire"
    assert len(meta_data["ascendancy_rankings"]) == 23

    meta_human = runner.invoke(app, ["wiki", "meta", "--no-economy"])
    assert meta_human.exit_code == 0, meta_human.stdout
    assert "Gemling Legionnaire" in meta_human.stdout

    arb_res = runner.invoke(
        app,
        ["econ", "arbitrage", "--no-live-books", "--min-volume-divine", "2.0", "--json"],
    )
    assert arb_res.exit_code == 0, arb_res.stdout
    arb_data = json.loads(arb_res.stdout)
    assert len(arb_data["opportunities"]) >= 1
    assert arb_data["opportunities"][0]["profit_ex_per_10k_gold"] > 0
    assert len(arb_data["trap_warnings"]) >= 3

    arb_human = runner.invoke(
        app,
        ["econ", "arbitrage", "--no-live-books", "--min-volume-divine", "2.0"],
    )
    assert arb_human.exit_code == 0, arb_human.stdout


def test_cross_process_cli_pob_slot_persistence(isolated_cli_env: dict[str, str]) -> None:
    def _run_poe2(*args: str) -> dict[str, object]:
        proc = subprocess.run(
            [sys.executable, "-m", "poe2brain.cli", *args, "--json"],
            cwd=REPO_ROOT,
            env=isolated_cli_env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    # Process 1: Create Lv85 Stormweaver with PoB-warrior config inflation & equip wand
    created = _run_poe2(
        "pob",
        "slots",
        "--create",
        "main",
        "--class",
        "Sorceress",
        "--ascendancy",
        "Stormweaver",
        "--level",
        "85",
        "--gems",
        "Spark 20/20  1\nArcane Tempo 1/0  1\nControlled Destruction 1/0  1",
        "--config",
        "buffOnslaught=true",
        "--config",
        "conditionEnemyShocked=true",
        "--config",
        "conditionShockEffect=60",
        "--config",
        "enemyIsBoss=None",
    )
    assert created["slot"] == "main"
    assert float(created["full_dps"]) > 100.0  # type: ignore[arg-type]

    # Process 2: Export share code in a brand-new OS process, then load into 'ref' slot
    exported = _run_poe2("pob", "export", "--slot", "main", "--format", "code")
    share_code = str(exported["exported"])
    assert share_code.startswith("eN")

    loaded_ref = _run_poe2("pob", "load", share_code, "--slot", "ref")
    assert loaded_ref["slot"] == "ref"
    assert float(loaded_ref["full_dps"]) == pytest.approx(float(created["full_dps"]), rel=1e-3)  # type: ignore[arg-type]

    # Process 3: Separate OS process runs dewatering audit & candidate item evaluation on 'main'
    audit = _run_poe2("pob", "audit", "--slot", "main")
    assert float(audit["dps_inflation_pct"]) > 50.0  # type: ignore[arg-type]
    assert len(audit["stripped_flags"]) >= 2  # type: ignore[arg-type]

    diff = _run_poe2(
        "pob",
        "eval-item",
        "--equip-slot",
        "Weapon 1",
        "--item-text",
        SAMPLE_RARE_WAND_CTRL_C,
        "--price-exalted",
        "25",
        "--slot",
        "main",
    )
    assert float(diff["full_dps_delta"]) > 0.0  # type: ignore[arg-type]
    assert float(diff["dps_per_exalted"]) > 0.0  # type: ignore[arg-type]


async def test_mcp_server_stdio_roundtrip(isolated_cli_env: dict[str, str]) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "poe2brain.mcp_server"],
        cwd=str(REPO_ROOT),
        env=isolated_cli_env,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            names = {t.name for t in tools_resp.tools}
            assert len(names) == 24
            assert {
                "poe2_status",
                "poe2_inspect_ascendancy",
                "poe2_meta_ladder",
                "poe2_scan_craft_profit",
                "poe2_scan_arbitrage",
                "poe2_craft_plan",
            }.issubset(names)

            st_res = await session.call_tool("poe2_status", {})
            st_json = json.loads(st_res.content[0].text)  # type: ignore[union-attr]
            assert st_json["patch"] == "0.5.5"
            assert st_json["gamedata_db_ready"] is True
            assert st_json["pob2_runtime_ready"] is True

            asc_res = await session.call_tool(
                "poe2_inspect_ascendancy", {"name_or_class": "Gemling Legionnaire"}
            )
            asc_json = json.loads(asc_res.content[0].text)  # type: ignore[union-attr]
            assert asc_json["primary"]["name"] == "Gemling Legionnaire"

            trap_res = await session.call_tool(
                "poe2_inspect_ascendancy", {"name_or_class": "Necromancer"}
            )
            trap_json = json.loads(trap_res.content[0].text)  # type: ignore[union-attr]
            assert trap_json["ok"] is False
            assert trap_json["error_type"] == "LookupError"

            prof_res = await session.call_tool(
                "poe2_scan_craft_profit",
                {"budget": "low", "offline_prices": True, "sims": 200},
            )
            prof_json = json.loads(prof_res.content[0].text)  # type: ignore[union-attr]
            assert prof_json["profitable_count"] >= 1

            if os.environ.get("POE2_LIVE_TESTS") == "1":
                meta_res = await session.call_tool(
                    "poe2_meta_ladder", {"league": "Forbidden Rites"}
                )
                meta_json = json.loads(meta_res.content[0].text)  # type: ignore[union-attr]
                assert len(meta_json["ascendancy_rankings"]) == 23

                arb_res = await session.call_tool(
                    "poe2_scan_arbitrage",
                    {"league": "Forbidden Rites", "live_books": False},
                )
                arb_json = json.loads(arb_res.content[0].text)  # type: ignore[union-attr]
                assert len(arb_json["opportunities"]) >= 1

            err_res = await session.call_tool(
                "poe2_craft_odds",
                {
                    "base": "Attuned Wand",
                    "ilvl": 82,
                    "lever": "Omen of Homogenising Exaltation",
                },
            )
            err_json = json.loads(err_res.content[0].text)  # type: ignore[union-attr]
            assert err_json["ok"] is False
            assert err_json["error_type"] == "RemovedLeverError"
