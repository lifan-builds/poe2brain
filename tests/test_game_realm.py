# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from poe2brain import cli
from poe2brain import mcp_server as server
from poe2brain.cli import app
from poe2brain.config import CN_CONVERTER_URL, CN_MARKET_URL, GameRealm
from poe2brain.pob.cn import cn_export_name, cn_export_url, read_cn_export_code


def test_cli_mcp_share_realm_and_keep_historical_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    slot = tmp_path / "pob_slots" / "main.xml"
    slot.parent.mkdir()
    slot.write_text("international")
    runner = CliRunner()
    assert server.poe2_status()["game_realm"] is None
    assert server.get_pob_slot_snapshot_path() == slot
    assert runner.invoke(app, ["init", "cn", "--json"]).exit_code == 0
    assert server.poe2_status()["game_realm"] == "cn"
    assert server.poe2_status()["default_league"] is None
    assert server.get_pob_slot_snapshot_path() != slot
    assert server.poe2_init(GameRealm.INTERNATIONAL)["game_realm"] == "international"
    assert runner.invoke(app, ["pob", "slots", "--json"]).exit_code == 0
    assert server.get_pob_slot_snapshot_path().read_text() == "international"
    assert {"poe2_status", "poe2_init"} <= {
        tool.name for tool in server.get_mcp_server("core")._tool_manager.list_tools()
    }


def test_cn_prices_and_character_list_do_not_use_international_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    server.poe2_init(GameRealm.CN)
    assert CN_CONVERTER_URL in server.poe2_pob_import_character(list_only=True)["instructions"]
    assert CN_CONVERTER_URL in server.poe2_pob_import_character("unconverted-name")["error"]
    assert CN_MARKET_URL in server.poe2_currency_price("Divine Orb")["hint"]
    assert CN_MARKET_URL in server.poe2_price_check("Divine Orb")["hint"]
    assert server.poe2_ninja_search()["error_type"] == "RealmUnavailableError"
    assert server.poe2_meta_ladder()["game_realm"] == "cn"
    result = CliRunner().invoke(app, ["econ", "price", "Divine Orb", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["game_realm"] == "cn"


def test_converted_character_page_accepts_encoded_names():
    url = cn_export_url("游侠 005")
    assert cn_export_name(url) == "游侠 005"
    assert cn_export_name("https://example.org/cn/qq_build?name=other") is None
    with pytest.raises(ValueError, match="poe2db.tw/cn/account"):
        cn_export_name("https://www.wegame.com.cn/helper/poe2/#/share/example")
    assert read_cn_export_code('<input value="export" id="pobCode">') == "export"
    with pytest.raises(ValueError, match="没有可导入"):
        read_cn_export_code("<h1>Unknown character</h1>")


def test_cn_restyle_keeps_player_rules_and_marks_price_source(tmp_path, monkeypatch):
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    server.poe2_init(GameRealm.CN)
    source = 'Show\n    BaseType == "Divine Orb"\n    SetFontSize 40\n'
    result = server.poe2_filter_restyle(source=source)
    assert result["league"] is None
    assert result["price_source"] == "source_filter_priorities"
    assert result["filter_text"] == source
    assert result["injected_override_blocks"] == 0
    assert result["validation"]["valid"]


def test_first_terminal_use_prompts_once_and_json_is_noninteractive(tmp_path, monkeypatch):
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["status", "--json"])
    assert json.loads(result.stdout)["game_realm"] is None
    answers = iter([GameRealm.CN])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_prompt_realm", lambda: next(answers))
    cli._choose_first_realm()
    cli._choose_first_realm()
    assert server.poe2_status()["game_realm"] == "cn"


def test_cn_filter_install_and_reference_prices(tmp_path, monkeypatch):
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    server.poe2_init(GameRealm.CN)
    presets = server.poe2_filter_presets()
    assert presets["detected_poe2_filter_dir"] is None
    assert presets["default_league"] is None
    assert "--install-dir" in presets["install_hint"]
    managed = tmp_path / "WeGameFilters" / "current"
    blocked = server.poe2_filter_generate(install=True, install_dir=str(managed))
    assert "WeGameFilters" in blocked["error"]
    assert not managed.exists()

    preview = tmp_path / "preview.html"
    compiled = server.poe2_filter_restyle(
        source='Show\n    BaseType == "Divine Orb"\n',
        preview_html=str(preview),
        install=True,
        install_dir=str(tmp_path / "client"),
    )
    assert compiled["validation"]["valid"]
    assert (tmp_path / "client" / (compiled["filter_name"] + ".filter")).is_file()
    assert "国服" in preview.read_text()
    prices = server.poe2_craft_odds(
        base="Attuned Wand", lever="Greater Exalted Orb", target=["SpellDamageOnWeapon8_"],
    )
    assert prices["league"] is None
    assert "offline_reference" in prices["price_source"]
