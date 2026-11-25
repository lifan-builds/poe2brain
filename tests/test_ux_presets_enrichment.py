# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import base64
import gzip
import json
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from poe2brain.cli import app
from poe2brain.config import BUNDLED_DATA_DIR, POE2_PATCH
from poe2brain.crafting.profit import CraftProfitScanner
from poe2brain.filters.themes import parse_look_intent
from poe2brain.http import Poe2HttpClient
from poe2brain.mcp_server import (
    execute_filter_generate,
    execute_scan_arbitrage,
    parse_unified_budget,
)
from poe2brain.ninja import NinjaBuildsService
from poe2brain.pob.service import BuildService

runner = CliRunner()


def _make_mock_http_client(handler_fn: Any) -> Poe2HttpClient:
    transport = httpx.MockTransport(handler_fn)
    client = Poe2HttpClient()
    client._sync_client = httpx.Client(transport=transport)
    return client


def _encode_xml(xml_text: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml_text.encode("utf-8"), 9)).decode("ascii")


def _make_mock_ninja_http() -> Poe2HttpClient:
    build_index = {
        "leagueBuilds": [
            {
                "leagueName": "Forbidden Rites",
                "leagueUrl": "forbiddenrites",
                "total": 140000,
                "status": 0,
                "category": 0,
                "hardcore": False,
                "statistics": [
                    {"class": "Gemling Legionnaire", "percentage": 22.5, "trend": -1},
                    {"class": "Deadeye", "percentage": 14.2, "trend": 1},
                    {"class": "Invoker", "percentage": 4.2, "trend": 0},
                    {"class": "Pathfinder", "percentage": 3.1, "trend": 1},
                    {"class": "Acolyte of Chayula", "percentage": 1.1, "trend": 1},
                ],
            }
        ]
    }
    snap_versions = [
        {
            "version": "1",
            "snapshotName": "forbidden-rites",
            "overviewType": 0,
            "passiveTree": "0_5",
            "timeMachineLabels": ["week-1", "week-2", "week-4"],
        }
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "build-index-state" in url:
            return httpx.Response(200, json=build_index)
        if "snapshot-versions" in url:
            return httpx.Response(200, json=snap_versions)
        return httpx.Response(404)

    return _make_mock_http_client(_handler)


def test_pob_stats_mobility_ci_and_audit_bottlenecks() -> None:
    """Verify BuildStats exposes mobility/speed/crit/chaos_immune and audit emits bottlenecks."""
    corpus_path = BUNDLED_DATA_DIR / "ninja" / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
    with gzip.open(corpus_path, "rt", encoding="utf-8") as fh:
        builds = json.load(fh)["builds"]
    sample = next(b for b in builds if b.get("pob_code"))

    with BuildService() as svc:
        stats = svc.load_build(sample["pob_code"], slot="ux_test")
        assert stats.movement_speed_mod > 0.0
        assert stats.attack_cast_speed >= 0.0
        assert isinstance(stats.chaos_immune, bool)
        audit = svc.audit_build(slot="ux_test")
        assert isinstance(audit.bottlenecks, list)
        assert len(audit.bottlenecks) <= 5


def test_ninja_search_presets_and_bang_negation(tmp_path: Path) -> None:
    """Verify --preset (sleeper, zoom_mapper, league_start, boss_killer, hc_tank) & !Term."""
    svc = NinjaBuildsService(http_client=_make_mock_ninja_http(), cache_dir=tmp_path)
    try:
        sleeper = svc.search_builds(preset="sleeper", limit=5)
        assert sleeper.preset == "sleeper"
        assert sleeper.sort_by == "sleeper"
        assert sleeper.matched_builds >= 3
        assert len(sleeper.builds) >= 3
        # Top sleeper entries should have real scores, mobility tags/speed, and exclude Temporalis
        scores = [b.sleeper_score for b in sleeper.builds]
        assert all(s is not None and s > 0 for s in scores)
        assert scores == sorted(scores, reverse=True)
        for b in sleeper.builds:
            assert "Temporalis" not in b.unique_items
            assert b.ascendancy_share_pct is None or b.ascendancy_share_pct < 5.0
            assert b.corpus_skill_count is not None and b.corpus_skill_count >= 1

        # Negation inside existing filter fields (!Term)
        neg = svc.search_builds(
            ascendancy="!Gemling Legionnaire,!Deadeye",
            unique_item="!Temporalis,!Mageblood",
            limit=10,
        )
        assert neg.matched_builds > 0
        for b in neg.builds:
            assert b.ascendancy_name not in ("Gemling Legionnaire", "Deadeye")
            assert "Temporalis" not in b.unique_items
            assert "Mageblood" not in b.unique_items

        # Zoom mapper preset defaults to mobility sort
        zoom = svc.search_builds(preset="zoom_mapper", limit=5)
        assert zoom.sort_by == "mobility"
        assert zoom.matched_builds > 0

        # League start preset excludes T0 chase uniques & ranks lowest-unique builds first
        ls = svc.search_builds(preset="league_start", limit=10)
        assert ls.matched_builds > 0
        counts = [len(b.unique_items) for b in ls.builds]
        assert counts == sorted(counts)
        for b in ls.builds:
            assert not (
                set(b.unique_items)
                & {"Temporalis", "Mageblood", "Headhunter", "Kalandra's Touch", "Astramentis"}
            )
    finally:
        svc.close()


def test_timeline_probe_emits_budget_stepping_stone(tmp_path: Path) -> None:
    """Verify probe_character_timeline identifies the lowest-T0 earlier snapshot."""
    early_xml = """<PathOfBuilding2>
      <Build level="92" className="Monk" ascendClassName="Invoker">
        <PlayerStat stat="CombinedDPS" value="2500000"/>
        <PlayerStat stat="TotalEHP" value="32000"/>
      </Build>
      <Items>
        <Item id="1">Rarity: UNIQUE\nHowa\nWrapped Mitts</Item>
        <ItemSet id="1"><Slot name="Gloves" itemId="1"/></ItemSet>
      </Items>
    </PathOfBuilding2>"""
    current_xml = """<PathOfBuilding2>
      <Build level="98" className="Monk" ascendClassName="Invoker">
        <PlayerStat stat="CombinedDPS" value="18000000"/>
        <PlayerStat stat="TotalEHP" value="55000"/>
      </Build>
      <Items>
        <Item id="1">Rarity: UNIQUE\nMageblood\nHeavy Belt</Item>
        <Item id="2">Rarity: UNIQUE\nKalandra's Touch\nRing</Item>
        <ItemSet id="1"><Slot name="Belt" itemId="1"/><Slot name="Ring 1" itemId="2"/></ItemSet>
      </Items>
    </PathOfBuilding2>"""

    early_code = _encode_xml(early_xml)
    current_code = _encode_xml(current_xml)

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "build-index-state" in url:
            return httpx.Response(200, json={"leagueBuilds": []})
        if "snapshot-versions" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "version": "1",
                        "snapshotName": "forbidden-rites",
                        "timeMachineLabels": ["week-1", "week-2"],
                    }
                ],
            )
        if "pob/raw/overview/code" in url:
            tm = request.url.params.get("timemachine")
            if tm == "week-1":
                return httpx.Response(200, text=early_code)
            if tm is None:
                return httpx.Response(200, text=current_code)
            return httpx.Response(404)
        return httpx.Response(404)

    svc = NinjaBuildsService(
        http_client=_make_mock_http_client(_handler),
        cache_dir=tmp_path,
        min_uncached_char_interval_seconds=0.0,
    )
    try:
        report = svc.probe_character_timeline(
            "TestAcct-1234",
            "BudgetMonk",
            candidate_labels=["week-1", "current"],
        )
        assert report.stepping_stone_label == "week-1"
        assert report.stepping_stone_note is not None
        assert "Mageblood" in report.stepping_stone_note
        assert "0 T0 chase uniques vs 2" in report.stepping_stone_note
    finally:
        svc.close()


def test_filter_look_intent_absorption(tmp_path: Path) -> None:
    """Verify --look parses colorblind, 2am warm-night, and sound style automatically."""
    parsed = parse_look_intent("obsidian gold deuteranopia 2am chime")
    assert parsed["cvd_safe"] == "deuteranopia"
    assert parsed["warm_night_mode"] is True
    assert parsed["sound_style"] == "celestial_chime"

    out_filter = tmp_path / "smart.filter"
    res = execute_filter_generate(
        strictness="strict",
        look="obsidian gold deuteranopia 2am chime",
        out_path=out_filter,
        offline=True,
    )
    assert res["validation"]["valid"] is True
    assert res.get("cvd_audit") is not None
    assert res["sound_pack"] is not None
    assert res["sound_pack"]["style"] == "celestial_chime"
    assert res["sound_pack"]["params"]["warm_night_mode"] is True


def test_unified_arbitrage_budget_and_gold_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify --budget ('500', '5d') and --gold work cleanly in both scan and ladder modes."""
    assert parse_unified_budget("500") == (500.0, "ex")
    assert parse_unified_budget("5d") == (5.0, "div")
    assert parse_unified_budget("2.5div") == (2.5, "div")
    with pytest.raises(ValueError):
        parse_unified_budget("abc")

    captured: dict[str, Any] = {}

    class _DummyModel:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._p = payload

        def model_dump(self, mode: str = "json") -> dict[str, Any]:
            return self._p

    class _FakeScanner:
        def scan_all(self, **kwargs: Any) -> _DummyModel:
            captured["scan"] = kwargs
            return _DummyModel({"mode": "scan", **kwargs})

        def build_compounding_ladder(self, **kwargs: Any) -> _DummyModel:
            captured["ladder"] = kwargs
            return _DummyModel({"mode": "ladder", **kwargs})

    monkeypatch.setattr("poe2brain.mcp_server.get_arbitrage_scanner", lambda: _FakeScanner())

    res_scan = execute_scan_arbitrage(budget="5d", gold=50_000, live_books=False)
    assert res_scan["mode"] == "scan"
    assert captured["scan"]["max_investment_divine"] == 5.0
    assert captured["scan"]["max_gold_per_cycle"] == 50_000

    res_ladder = execute_scan_arbitrage(budget="600", gold=90_000, target_divine=40.0)
    assert res_ladder["mode"] == "ladder"
    assert captured["ladder"]["starting_exalted"] == 600.0
    assert captured["ladder"]["gold_budget"] == 90_000
    assert captured["ladder"]["target_divine"] == 40.0


def test_craft_profit_auto_competition_and_niche_score() -> None:
    """Verify CraftProfitScanner auto-populates competition_level, niche_score, margin_tier."""
    scanner = CraftProfitScanner()
    try:
        report = scanner.scan(
            recipes=scanner.default_recipes()[:3],
            live_economy=False,
            live_trade=False,
            n_sims=200,
        )
        assert len(report.evaluations) == 3
        for ev in report.evaluations:
            assert ev.competition_level in ("low", "medium", "high")
            assert ev.margin_tier in ("high_margin", "solid", "thin", "negative")
            assert ev.niche_score >= 0.0
    finally:
        scanner.close()


def test_cli_ninja_search_preset_sleeper_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Smoke-test `poe2 ninja search --preset sleeper --limit 3 --json` end-to-end offline."""
    svc = NinjaBuildsService(http_client=_make_mock_ninja_http(), cache_dir=tmp_path)
    monkeypatch.setattr("poe2brain.mcp_server.get_ninja_service", lambda: svc)
    try:
        res = runner.invoke(
            app, ["ninja", "search", "--preset", "sleeper", "--limit", "3", "--json"]
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["preset"] == "sleeper"
        assert data["sort_by"] == "sleeper"
        assert len(data["builds"]) == 3
    finally:
        svc.close()
