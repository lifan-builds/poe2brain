# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Tests for zero-JSON MCP setup, core MCP profile, CN mirror fallback, doctor & uninstall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poe2brain.mcp_server import _MCP_TOOL_REGISTRY, get_mcp_server
from poe2brain.pob.bootstrap import _candidate_github_urls
from poe2brain.setup_tools import (
    CORE_MCP_TOOL_NAMES,
    build_mcp_server_entry,
    run_doctor,
    run_uninstall,
    setup_mcp_clients,
)


def test_core_mcp_profile_tool_subset_is_valid() -> None:
    registered_names = {fn.__name__ for fn in _MCP_TOOL_REGISTRY}
    assert CORE_MCP_TOOL_NAMES.issubset(registered_names)

    full_server = get_mcp_server("full")
    core_server = get_mcp_server("core")
    full_tools = set(full_server._tool_manager._tools.keys())
    core_tools = set(core_server._tool_manager._tools.keys())
    assert len(full_tools) >= 44
    assert core_tools == CORE_MCP_TOOL_NAMES
    assert len(core_tools) == 17


def test_candidate_github_urls_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POE2BRAIN_GITHUB_MIRROR", raising=False)
    raw = "https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2/archive/abc.zip"

    default_urls = _candidate_github_urls(raw)
    assert default_urls[0] == raw
    assert len(default_urls) >= 3

    cn_urls = _candidate_github_urls(raw, cn=True)
    assert cn_urls[0].startswith("https://ghfast.top/")
    assert cn_urls[-1] == raw

    custom_urls = _candidate_github_urls(raw, mirror="https://my-mirror.example")
    assert custom_urls == [f"https://my-mirror.example/{raw}", raw]


def test_setup_mcp_clients_and_uninstall_roundtrip(tmp_path: Path) -> None:
    fake_repo = tmp_path / "poe2brain"
    fake_repo.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".cursor").mkdir()
    (fake_home / ".codex").mkdir()

    # Preserve an existing unrelated server in cursor config
    cursor_cfg = fake_home / ".cursor" / "mcp.json"
    cursor_cfg.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node", "args": ["x.js"]}}}),
        encoding="utf-8",
    )

    res = setup_mcp_clients(
        ["cursor", "codex", "cherry"],
        profile="core",
        repo_dir=fake_repo,
        uv_path="/usr/local/bin/uv",
        home=fake_home,
    )
    assert res["ok"] is True
    assert res["profile"] == "core"
    assert res["tool_count"] == 17
    assert "--profile" in res["entry"]["args"]
    assert "core" in res["entry"]["args"]

    cursor_after = json.loads(cursor_cfg.read_text(encoding="utf-8"))
    assert "other" in cursor_after["mcpServers"]
    assert cursor_after["mcpServers"]["poe2brain"]["command"] == "/usr/local/bin/uv"

    codex_cfg = fake_home / ".codex" / "config.toml"
    codex_text = codex_cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.poe2brain]" in codex_text
    assert '"--profile", "core"' in codex_text

    assert "cherry" in res["clients"]
    assert "poe2brain" in res["clients"]["cherry"]["json"]

    # Idempotent update to full profile + clean uninstall
    setup_mcp_clients(
        ["codex"],
        profile="full",
        repo_dir=fake_repo,
        uv_path="/usr/local/bin/uv",
        home=fake_home,
    )
    codex_full = codex_cfg.read_text(encoding="utf-8")
    assert codex_full.count("[mcp_servers.poe2brain]") == 1
    assert "--profile" not in codex_full

    un = run_uninstall(remove_cache=False, remove_mcp=True, repo_dir=fake_repo, home=fake_home)
    assert un["ok"] is True
    assert str(cursor_cfg) in un["removed_mcp_configs"]
    assert str(codex_cfg) in un["removed_mcp_configs"]
    assert "poe2brain" not in json.loads(cursor_cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert "other" in json.loads(cursor_cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert "[mcp_servers.poe2brain]" not in codex_cfg.read_text(encoding="utf-8")


def test_build_entry_and_doctor_smoke(tmp_path: Path) -> None:
    entry = build_mcp_server_entry(repo_dir=tmp_path, profile="full", uv_path="uv")
    assert entry["args"][0] == "--directory"
    assert entry["args"][-1] == "poe2brain-mcp"

    diag = run_doctor(check_network=False, home=tmp_path)
    assert diag["patch"] == "0.5.5"
    assert "cn_mirrors" in diag
    assert "mirror.nju.edu.cn" in diag["cn_mirrors"]["python_install"]
