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
    assert len(skill_tools) == 31
    assert skill_tools == readme_tools

    registered_tools = {t.name for t in mcp_mod.mcp._tool_manager.list_tools()}
    assert registered_tools == skill_tools

    runner = CliRunner()
    for group in (
        "wiki",
        "pob",
        "econ",
        "trade",
        "craft",
        "ninja",
        "filter",
        "sync",
        "bootstrap",
    ):
        res = runner.invoke(app, [group, "--help"])
        assert res.exit_code == 0, f"Failed --help for group {group}: {res.stdout}"

    for sub_args in (
        ["ninja", "snapshots", "--help"],
        ["ninja", "search", "--help"],
        ["ninja", "character", "--help"],
        ["filter", "presets", "--help"],
        ["filter", "generate", "--help"],
        ["filter", "restyle", "--help"],
        ["filter", "validate", "--help"],
    ):
        sub_res = runner.invoke(app, sub_args)
        assert sub_res.exit_code == 0, f"Failed --help for {sub_args}: {sub_res.stdout}"


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

    asc_human_res = runner.invoke(app, ["wiki", "ascendancy"])
    assert asc_human_res.exit_code == 0
    assert "Adaptive Capability" in asc_human_res.stdout

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


def test_cli_ninja_and_filter_commands_and_structured_errors(
    isolated_cli_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_cli_env
    import httpx

    from poe2brain.http import Poe2HttpClient
    from poe2brain.ninja import NinjaBuildsService

    def ninja_mock_handler(request: httpx.Request) -> httpx.Response:
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
                            "statistics": [
                                {"class": "Gemling Legionnaire", "percentage": 16.4, "trend": 1},
                                {"class": "Martial Artist", "percentage": 11.2, "trend": 1},
                            ],
                        },
                        {
                            "leagueName": "HC Forbidden Rites",
                            "leagueUrl": "forbiddenriteshc",
                            "total": 6396,
                            "hardcore": True,
                            "statistics": [],
                        },
                    ]
                },
            )
        if path.endswith("/snapshot-versions"):
            lg = request.url.params.get("league", "forbiddenrites")
            return httpx.Response(
                200,
                json=[
                    {
                        "url": lg,
                        "name": "Forbidden Rites",
                        "version": "cd" * 32,
                        "snapshotName": "forbidden-rites",
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                        "timeMachineLabels": ["day-3", "week-3"],
                    }
                ],
            )
        if path == "/poe2/pob/raw/overview/code":
            return httpx.Response(404, json={"title": "Not Found", "status": 404})
        raise AssertionError(f"Unexpected ninja mock URL: {request.url}")

    n_http = Poe2HttpClient(sync_transport=httpx.MockTransport(ninja_mock_handler))
    n_svc = NinjaBuildsService(
        http_client=n_http,
        build_service=mcp_mod.get_build_service(),
        cache_dir=tmp_path / "ninja_http_mock",
        min_uncached_char_interval_seconds=0.0,
    )
    monkeypatch.setattr(mcp_mod, "_NINJA_SVC", n_svc)

    runner = CliRunner()

    # 1. poe2 ninja snapshots (--json & human)
    snap_res = runner.invoke(app, ["ninja", "snapshots", "--json"])
    assert snap_res.exit_code == 0, snap_res.stdout
    snap_data = json.loads(snap_res.stdout)
    assert snap_data["primary"]["total_characters"] == 124092
    assert set(snap_data["modes"]) == {"sc_trade", "hc_trade", "ssf", "hc_ssf"}
    assert "week-3" in snap_data["primary"]["time_machine_labels"]
    assert runner.invoke(app, ["ninja", "snapshots"]).exit_code == 0

    # 2. poe2 ninja search (--json, human, and PoE1 trap structured LookupError)
    srch_res = runner.invoke(
        app,
        ["ninja", "search", "--ascendancy", "Martial Artist", "--limit", "5", "--json"],
    )
    assert srch_res.exit_code == 0, srch_res.stdout
    srch_data = json.loads(srch_res.stdout)
    assert srch_data["matched_builds"] >= 5
    assert len(srch_data["builds"]) == 5
    assert srch_data["dps_distribution"]["max"] >= srch_data["dps_distribution"]["median"] > 0
    assert (
        runner.invoke(app, ["ninja", "search", "--ascendancy", "Martial Artist", "--limit", "3"])
        .exit_code
        == 0
    )

    trap_srch = runner.invoke(app, ["ninja", "search", "--ascendancy", "Necromancer", "--json"])
    assert trap_srch.exit_code == 1
    trap_err = json.loads(trap_srch.stdout)
    assert trap_err["ok"] is False
    assert trap_err["error_type"] == "LookupError"

    # 3. poe2 ninja character structured LookupError (missing char) & ValueError (/api/protected/)
    miss_char = runner.invoke(
        app,
        ["ninja", "character", "nobody-0000", "--name", "GhostChar999", "--json"],
    )
    assert miss_char.exit_code == 1
    miss_err = json.loads(miss_char.stdout)
    assert miss_err["ok"] is False
    assert miss_err["error_type"] == "LookupError"

    prot_char = runner.invoke(
        app,
        ["ninja", "character", "https://poe.ninja/api/protected/poe2/builds/xyz/search", "--json"],
    )
    assert prot_char.exit_code == 1
    prot_err = json.loads(prot_char.stdout)
    assert prot_err["ok"] is False
    assert prot_err["error_type"] == "ValueError"
    assert "protected" in prot_err["error"].lower()

    # 4. poe2 filter presets, generate, restyle, install (--install-dir tmp), and validate
    presets_res = runner.invoke(app, ["filter", "presets", "--json"])
    assert presets_res.exit_code == 0, presets_res.stdout
    presets_data = json.loads(presets_res.stdout)
    assert presets_data["theme_count"] == 8
    assert presets_data["strictness_count"] == 6
    assert presets_data["sound_style_count"] == 6
    assert runner.invoke(app, ["filter", "presets"]).exit_code == 0

    out_dir = tmp_path / "filter_out"
    filter_file = out_dir / "cyber_strict.filter"
    preview_file = out_dir / "cyber_preview.html"
    install_target = tmp_path / "mock_poe2_install"

    gen_res = runner.invoke(
        app,
        [
            "filter",
            "generate",
            "--strictness",
            "strict",
            "--look",
            "cyberpunk neon magenta and cyan on deep obsidian, huge divine labels",
            "--sound-style",
            "cyber_synth",
            "--musical-key",
            "F#",
            "--scale",
            "phrygian",
            "--out",
            str(filter_file),
            "--sound-dir",
            str(out_dir),
            "--preview-html",
            str(preview_file),
            "--install",
            "--install-dir",
            str(install_target),
            "--offline",
            "--json",
        ],
    )
    assert gen_res.exit_code == 0, gen_res.stdout
    gen_data = json.loads(gen_res.stdout)
    assert gen_data["validation"]["valid"] is True
    assert gen_data["validation"]["error_count"] == 0
    assert gen_data["validation"]["warning_count"] == 0
    assert filter_file.is_file()
    assert preview_file.is_file()
    preview_txt = preview_file.read_text(encoding="utf-8")
    assert "data:audio/" in preview_txt
    assert "Agent-composed theme from &#x27;cyberpunk_neon&#x27;" in preview_txt
    assert (install_target / "cyber_strict.filter").is_file()
    assert (install_target / "poe2brain_cyber_synth" / "s_plus.wav").is_file()

    # NeverSink 0.10.4a stale BaseType + HasExplicitMod spacing auto-healing
    stale_ns = tmp_path / "stale_neversink.filter"
    stale_ns.write_text(
        "\n".join(
            [
                "# [[0100]] OVERRIDE AREA 1",
                'Show # $type->currency $tier->s',
                '\tBaseType == "Divine Orb" "Fist of Kalguur" "Fragments of the Past"',
                "\tSetFontSize 45",
                "\tSetTextColor 255 0 0 255",
                "\tSetBorderColor 255 0 0 255",
                "\tSetBackgroundColor 255 255 255 255",
                'Show # $type->rare $tier->t1',
                '\tClass == "Wands"',
                "\tRarity Rare",
                '\tHasExplicitMod >= 2 "Merciless"',
                "\tSetFontSize 38",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    healed_out = out_dir / "healed_ns.filter"
    restyle_res = runner.invoke(
        app,
        [
            "filter",
            "restyle",
            str(stale_ns),
            "--theme",
            "darkmode_obsidian",
            "--sound-style",
            "zen_minimal",
            "--out",
            str(healed_out),
            "--sound-dir",
            str(out_dir),
            "--offline",
            "--json",
        ],
    )
    assert restyle_res.exit_code == 0, restyle_res.stdout
    restyle_data = json.loads(restyle_res.stdout)
    assert restyle_data["validation"]["valid"] is True
    assert restyle_data["validation"]["error_count"] == 0
    assert "Fist of Kalguur" in restyle_data["healed_basetypes"]
    assert "Fragments of the Past" in restyle_data["healed_basetypes"]
    assert restyle_data["injected_override_blocks"] >= 1

    # Also test `poe2 filter restyle strict` remote NeverSink fetch via Poe2HttpClient.get_text
    from poe2brain.filters import NeverSinkAdapter

    ns_http = Poe2HttpClient(
        sync_transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text=stale_ns.read_text(encoding="utf-8"))
        )
    )
    monkeypatch.setattr(
        mcp_mod,
        "_NEVERSINK_ADAPTER",
        NeverSinkAdapter(generator=mcp_mod.get_filter_generator(), http_client=ns_http),
    )
    restyle_remote = runner.invoke(
        app,
        [
            "filter",
            "restyle",
            "strict",
            "--look",
            "cyberpunk neon magenta and cyan on deep obsidian, huge divine labels",
            "--offline",
            "--json",
        ],
    )
    assert restyle_remote.exit_code == 0, restyle_remote.stdout
    restyle_remote_data = json.loads(restyle_remote.stdout)
    assert restyle_remote_data["strictness"] == "strict"
    assert restyle_remote_data["validation"]["valid"] is True
    assert restyle_remote_data["validation"]["error_count"] == 0
    assert "Fist of Kalguur" in restyle_remote_data["healed_basetypes"]

    val_ok = runner.invoke(app, ["filter", "validate", str(healed_out), "--json"])
    assert val_ok.exit_code == 0
    assert json.loads(val_ok.stdout)["valid"] is True

    val_bad = runner.invoke(
        app,
        [
            "filter",
            "validate",
            'Show\n\tClass == "Body Armour"\n\tSetFontSize 50',
            "--json",
        ],
    )
    assert val_bad.exit_code == 0
    bad_report = json.loads(val_bad.stdout)
    assert bad_report["valid"] is False
    assert bad_report["error_count"] >= 2


def test_cross_process_ninja_character_to_pob_and_filter_generate(
    isolated_cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    from poe2brain.http import get_cached_json
    from poe2brain.ninja.service import (
        DEFAULT_BUILD_INDEX_TTL_SECONDS,
        DEFAULT_CHAR_POB_TTL_SECONDS,
        DEFAULT_SNAPSHOT_TTL_SECONDS,
        POENINJA_BUILD_INDEX_URL,
        POENINJA_CHAR_POB_RAW_URL,
        POENINJA_SNAPSHOT_VERSIONS_URL,
        NinjaBuildsService,
    )

    # Pick a real Martial Artist character from the bundled Forbidden Rites corpus
    corpus_svc = NinjaBuildsService()
    _, corpus_rows = corpus_svc._ensure_corpus_loaded()
    sample = next(r for r in corpus_rows if r.get("ascendancy_name") == "Martial Artist")
    acct = str(sample["account"])
    char_name = str(sample["character"])
    pob_code = str(sample["pob_code"])

    # When running offline, pre-warm HTTP disk cache inside isolated POE2BRAIN_CACHE_DIR
    if os.environ.get("POE2_LIVE_TESTS") != "1":
        cache_root = Path(isolated_cli_env["POE2BRAIN_CACHE_DIR"])
        get_cached_json(
            POENINJA_BUILD_INDEX_URL,
            ttl_seconds=DEFAULT_BUILD_INDEX_TTL_SECONDS,
            fetcher=lambda: {
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
            cache_dir=cache_root,
            force_refresh=True,
        )
        for slug in (
            "forbiddenrites",
            "forbiddenriteshc",
            "forbiddenritesssf",
            "forbiddenriteshcssf",
        ):
            get_cached_json(
                POENINJA_SNAPSHOT_VERSIONS_URL,
                params={"league": slug},
                ttl_seconds=DEFAULT_SNAPSHOT_TTL_SECONDS,
                fetcher=lambda s=slug: [
                    {
                        "url": s,
                        "name": "Forbidden Rites",
                        "version": "ef" * 32,
                        "snapshotName": "forbidden-rites",
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                        "timeMachineLabels": ["week-3"],
                    }
                ],
                cache_dir=cache_root,
                force_refresh=True,
            )
        get_cached_json(
            POENINJA_CHAR_POB_RAW_URL,
            params={
                "overview": "forbidden-rites",
                "type": "exp",
                "account": acct,
                "name": char_name,
            },
            ttl_seconds=DEFAULT_CHAR_POB_TTL_SECONDS,
            fetcher=lambda: {"code": pob_code},
            cache_dir=cache_root,
            force_refresh=True,
        )

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

    # Process 1: Import poe.ninja character into persistent PoB2 slot 'ninja_main'
    imported = _run_poe2(
        "ninja",
        "character",
        acct,
        "--name",
        char_name,
        "--load-pob",
        "--slot",
        "ninja_main",
    )
    assert imported["loaded_into_pob"] is True
    assert imported["pob_slot"] == "ninja_main"
    assert Path(str(imported["snapshot_path"])).is_file()

    # Process 2: Separate OS process compiles build-tailored filter + soundpack from 'ninja_main'
    out_filter = tmp_path / "ninja_bundle" / "martial_artist.filter"
    out_html = tmp_path / "ninja_bundle" / "martial_artist.html"
    compiled = _run_poe2(
        "filter",
        "generate",
        "--strictness",
        "strict",
        "--theme",
        "forbidden_abyss",
        "--sound-style",
        "celestial_chime",
        "--slot",
        "ninja_main",
        "--out",
        str(out_filter),
        "--preview-html",
        str(out_html),
        "--offline",
    )
    assert compiled["build_context"] is not None
    assert compiled["build_context"]["ascendancy_name"] == "Martial Artist"  # type: ignore[index]
    assert compiled["validation"]["valid"] is True  # type: ignore[index]
    assert compiled["validation"]["error_count"] == 0  # type: ignore[index]
    assert out_filter.is_file()
    assert out_html.is_file()

    # Process 3: Separate OS process validates the written .filter bundle
    validated = _run_poe2("filter", "validate", str(out_filter))
    assert validated["valid"] is True
    assert validated["error_count"] == 0


async def test_mcp_server_stdio_roundtrip(
    isolated_cli_env: dict[str, str], tmp_path: Path
) -> None:
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
            assert len(names) == 31
            assert {
                "poe2_status",
                "poe2_inspect_ascendancy",
                "poe2_meta_ladder",
                "poe2_scan_craft_profit",
                "poe2_scan_arbitrage",
                "poe2_craft_plan",
                "poe2_ninja_snapshots",
                "poe2_ninja_search",
                "poe2_ninja_character",
                "poe2_filter_presets",
                "poe2_filter_generate",
                "poe2_filter_restyle",
                "poe2_filter_validate",
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

            fp_res = await session.call_tool("poe2_filter_presets", {})
            fp_json = json.loads(fp_res.content[0].text)  # type: ignore[union-attr]
            assert fp_json["theme_count"] == 8
            assert fp_json["strictness_count"] == 6
            assert fp_json["sound_style_count"] == 6

            mcp_filter_path = tmp_path / "mcp_studio" / "synth.filter"
            mcp_html_path = tmp_path / "mcp_studio" / "synth.html"
            fg_res = await session.call_tool(
                "poe2_filter_generate",
                {
                    "strictness": "strict",
                    "look": "synthwave neon pink and cyan on obsidian, huge divine labels",
                    "sound_style": "arcade_retro",
                    "out_path": str(mcp_filter_path),
                    "preview_html": str(mcp_html_path),
                    "offline": True,
                },
            )
            fg_json = json.loads(fg_res.content[0].text)  # type: ignore[union-attr]
            assert fg_json["validation"]["valid"] is True
            assert fg_json["validation"]["error_count"] == 0
            assert fg_json["validation"]["warning_count"] == 0
            assert mcp_filter_path.is_file()
            assert mcp_html_path.is_file()
            assert "cyberpunk_neon" in mcp_html_path.read_text(encoding="utf-8")

            fv_res = await session.call_tool(
                "poe2_filter_validate",
                {"filter_file_or_text": str(mcp_filter_path)},
            )
            fv_json = json.loads(fv_res.content[0].text)  # type: ignore[union-attr]
            assert fv_json["valid"] is True
            assert fv_json["error_count"] == 0

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

                ns_res = await session.call_tool(
                    "poe2_ninja_search",
                    {"ascendancy": "Martial Artist", "limit": 3},
                )
                ns_json = json.loads(ns_res.content[0].text)  # type: ignore[union-attr]
                assert ns_json["matched_builds"] >= 3
                top_char = ns_json["builds"][0]

                nc_res = await session.call_tool(
                    "poe2_ninja_character",
                    {
                        "account_or_url": top_char["account"],
                        "character": top_char["character"],
                        "load_into_pob": True,
                        "slot": "mcp_live_ninja",
                    },
                )
                nc_json = json.loads(nc_res.content[0].text)  # type: ignore[union-attr]
                assert nc_json["loaded_into_pob"] is True
                assert nc_json["pob_stats"]["level"] >= 90

                fr_res = await session.call_tool(
                    "poe2_filter_restyle",
                    {
                        "source": "semi_strict",
                        "look": "cyberpunk neon magenta and cyan on deep obsidian",
                        "offline": True,
                    },
                )
                fr_json = json.loads(fr_res.content[0].text)  # type: ignore[union-attr]
                assert fr_json["validation"]["valid"] is True
                assert fr_json["validation"]["error_count"] == 0
                assert "Fist of Kalguur" in fr_json["healed_basetypes"]

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
