# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from poe2brain.cli import app
from poe2brain.knowledge.meta import MetaService
from poe2brain.knowledge.service import KnowledgeService
from poe2brain.knowledge.zh_aliases import (
    normalize_zh_item_clipboard,
    translate_zh_query,
)
from poe2brain.pob.bridge import Pob2Bridge
from poe2brain.pob.daemon import Pob2SocketDaemon
from poe2brain.pob.local_sync import (
    candidate_local_pob2_build_dirs,
    discover_local_pob2_builds,
    resolve_local_pob2_build,
)
from poe2brain.pob.oauth import (
    DEFAULT_OAUTH_SCOPES,
    GggOAuthClient,
    OAuthTokenSet,
    _build_authorize_url,
    _pkce_pair,
)
from poe2brain.trade.item_parser import parse_clipboard_item


def test_zh_aliases_and_knowledge_queries() -> None:
    assert "Stormweaver" in translate_zh_query("风暴编织者")
    assert "Templar" in translate_zh_query("圣堂武僧")
    assert "Energy Shield" in translate_zh_query("能量护盾")
    assert "Rakiata's Flow" in translate_zh_query("拉基亚塔的流光")
    assert translate_zh_query("Spark Stormweaver") == "Spark Stormweaver"

    meta = MetaService()
    insp = meta.inspect_ascendancy("风暴编织者")
    assert insp.class_name == "Sorceress"
    assert insp.ascendancies[0].name == "Stormweaver"

    with pytest.raises(LookupError, match="Templar"):
        meta.inspect_ascendancy("圣堂武僧")

    ks = KnowledgeService()
    mech = ks.explain_mechanic("能量护盾")
    assert len(mech.facts) >= 1 or len(mech.keywords) >= 1

    gem = ks.inspect_gem("拉基亚塔的流光")
    assert gem.name == "Rakiata's Flow"


def test_zh_clipboard_item_normalization_and_parse() -> None:
    zh_wand = """物品類別: 法杖
稀有度: 稀有
風暴號角
Attuned Wand
--------
品質: +20% (已強化)
--------
需求: 等級 78, 142 智慧
--------
插槽: S
--------
物品等級: 82
--------
35% increased Spell Damage (符文)
--------
84% increased Spell Damage (破裂)
+4 to Level of all Lightning Spell Skills
26% increased Cast Speed
+115 to maximum Mana (褻瀆)
"""
    norm = normalize_zh_item_clipboard(zh_wand)
    assert "Item Class: Wands" in norm
    assert "Rarity: Rare" in norm
    assert "Item Level: 82" in norm
    assert "(fractured)" in norm
    assert "(desecrated)" in norm

    parsed = parse_clipboard_item(zh_wand)
    assert parsed.item_class == "Wands"
    assert parsed.rarity.value == "rare"
    assert parsed.item_level == 82
    assert parsed.quality == 20
    assert any(m.domain.value == "rune" for m in parsed.mods)
    assert any(m.domain.value == "fractured" for m in parsed.mods)
    assert any(m.domain.value == "desecrated" for m in parsed.mods)


def test_local_pob2_build_sync_discovery_and_resolution(tmp_path: Path) -> None:
    win_dirs = candidate_local_pob2_build_dirs(
        platform_override="win32", home_dir=Path("/Users/demo")
    )
    assert any("Path of Building (PoE2)" in str(d) for d in win_dirs)

    builds_dir = tmp_path / "Builds"
    builds_dir.mkdir()
    older = builds_dir / "Starter.xml"
    newer = builds_dir / "Endgame_Spark.xml"
    xml_template = (
        '<PathOfBuilding2><Build level="{lvl}" className="Sorceress" '
        'ascendClassName="Stormweaver" mainSocketGroup="1"/>'
        '<Tree activeSpec="1"><Spec treeVersion="0_5" classId="0" nodes=""/></Tree>'
        '<Skills activeSkillSet="1"><SkillSet id="1"/></Skills>'
        '<Items activeItemSet="1"><ItemSet id="1"/></Items></PathOfBuilding2>'
    )
    older.write_text(xml_template.format(lvl=42), encoding="utf-8")
    newer.write_text(xml_template.format(lvl=95), encoding="utf-8")
    # Ensure deterministic mtime order
    old_ts = time.time() - 100
    import os

    os.utime(older, (old_ts, old_ts))

    disc = discover_local_pob2_builds(builds_dir=builds_dir)
    assert len(disc.builds) == 2
    assert disc.builds[0].name == "Endgame_Spark"
    assert disc.builds[0].level == 95
    assert disc.builds[0].ascendancy_name == "Stormweaver"

    assert resolve_local_pob2_build(None, builds_dir=builds_dir) == newer.resolve()
    assert resolve_local_pob2_build("starter", builds_dir=builds_dir) == older.resolve()


def test_ggg_oauth_pkce_status_and_character_list_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier, challenge = _pkce_pair()
    assert len(verifier) >= 43
    assert len(challenge) == 43
    url = _build_authorize_url(
        client_id="poe2brain",
        redirect_uri="http://localhost:54321",
        scopes=DEFAULT_OAUTH_SCOPES,
        state="deadbeef",
        code_challenge=challenge,
    )
    assert "client_id=poe2brain" in url
    assert "code_challenge_method=S256" in url
    assert "account%3Acharacters" in url

    tok_path = tmp_path / "oauth_tokens.json"
    oauth = GggOAuthClient(client_id="poe2brain", token_path=tok_path)
    st0 = oauth.status()
    assert st0.authenticated is False

    oauth.save_tokens(
        OAuthTokenSet(
            access_token="tok_live_123",
            refresh_token="ref_live_456",
            expires_at=time.time() + 3600,
            scope="account:profile account:characters",
            username="lifan_test",
            client_id="poe2brain",
        )
    )
    st1 = oauth.status()
    assert st1.authenticated is True
    assert st1.username == "lifan_test"
    assert "account:characters" in st1.scopes

    monkeypatch.setattr(
        oauth,
        "_api_get_json",
        lambda path: {
            "characters": [
                {
                    "name": "SeasonOneSpark",
                    "realm": "poe2",
                    "class": "Stormweaver",
                    "league": "Forbidden Rites",
                    "level": 91,
                    "current": True,
                }
            ]
        },
    )
    chars = oauth.list_characters(realm="poe2")
    assert len(chars) == 1
    assert chars[0].name == "SeasonOneSpark"
    assert chars[0].level == 91

    out = oauth.logout()
    assert out["had_saved_token"] is True
    assert oauth.status().authenticated is False

    runner = CliRunner()
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path))
    res = runner.invoke(app, ["auth", "status", "--json"])
    assert res.exit_code == 0
    assert json.loads(res.stdout)["authenticated"] is False


def test_tcp_loopback_daemon_transport_works_cross_platform(tmp_path: Path) -> None:
    desc_path = tmp_path / "pob.tcp.json"
    daemon = Pob2SocketDaemon(socket_path=desc_path, lazy=False)
    daemon._use_tcp = True
    try:
        daemon.start()
        meta = json.loads(desc_path.read_text(encoding="utf-8"))
        assert meta["transport"] == "tcp"
        assert meta["host"] == "127.0.0.1"
        assert isinstance(meta["port"], int) and meta["port"] > 0

        with Pob2Bridge(socket_path=desc_path) as client:
            pong = client.ping()
            assert pong["ok"] is True
    finally:
        daemon.close()


def test_agpl_attribution_in_cli_version_and_mcp() -> None:
    from poe2brain import ATTRIBUTION, LICENSE_SPDX
    from poe2brain.config import REPO_ROOT
    from poe2brain.mcp_server import mcp

    runner = CliRunner()
    res_flag = runner.invoke(app, ["--version"])
    assert res_flag.exit_code == 0
    assert ATTRIBUTION in res_flag.stdout
    assert LICENSE_SPDX in res_flag.stdout

    res_cmd = runner.invoke(app, ["version", "--json"])
    assert res_cmd.exit_code == 0
    payload = json.loads(res_cmd.stdout)
    assert payload["attribution"] == ATTRIBUTION
    assert payload["license"] == LICENSE_SPDX

    assert mcp.instructions and ATTRIBUTION in mcp.instructions
    assert (REPO_ROOT / "ATTRIBUTION.md").is_file()
    assert (REPO_ROOT / "CLA.md").is_file()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in (REPO_ROOT / "LICENSE").read_text(
        encoding="utf-8"
    )
