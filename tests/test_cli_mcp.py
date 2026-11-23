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
    assert len(skill_tools) == 20
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
            assert len(names) == 20
            assert "poe2_status" in names
            assert "poe2_craft_plan" in names

            st_res = await session.call_tool("poe2_status", {})
            st_json = json.loads(st_res.content[0].text)  # type: ignore[union-attr]
            assert st_json["patch"] == "0.5.5"
            assert st_json["gamedata_db_ready"] is True
            assert st_json["pob2_runtime_ready"] is True

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
