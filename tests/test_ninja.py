# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import base64
import gzip
import json
import os
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from poe2brain.config import BUNDLED_DATA_DIR, POE2_PATCH
from poe2brain.http import Poe2HttpClient
from poe2brain.ninja import (
    NinjaBuildsService,
    decode_ndic_dictionary,
    decode_pob2_code_to_xml,
    decode_search_result,
    encode_ndic_dictionary,
    encode_varint,
    inspect_pob2_xml_or_code,
)

LIVE_ENABLED = os.environ.get("POE2_LIVE_TESTS") == "1"


def _pb_tag(field_no: int, wire_type: int) -> bytes:
    return encode_varint((field_no << 3) | wire_type)


def _pb_bytes(field_no: int, payload: bytes) -> bytes:
    return _pb_tag(field_no, 2) + encode_varint(len(payload)) + payload


def _pb_str(field_no: int, text: str) -> bytes:
    return _pb_bytes(field_no, text.encode("utf-8"))


def _pb_uint(field_no: int, val: int) -> bytes:
    return _pb_tag(field_no, 0) + encode_varint(val)


def _pb_packed(field_no: int, vals: list[int]) -> bytes:
    return _pb_bytes(field_no, b"".join(encode_varint(v) for v in vals))


def _make_mock_http_client(
    handler_fn: Any,
) -> Poe2HttpClient:
    transport = httpx.MockTransport(handler_fn)
    sync_c = httpx.Client(transport=transport)
    client = Poe2HttpClient()
    client._sync_client = sync_c
    return client


def _mock_ninja_handler_factory(sample_pob_code: str):
    build_index = {
        "leagueBuilds": [
            {
                "leagueName": "Forbidden Rites",
                "leagueUrl": "forbiddenrites",
                "total": 124092,
                "status": 0,
                "category": 0,
                "hardcore": False,
                "statistics": [
                    {"class": "Gemling Legionnaire", "percentage": 31.85, "trend": -1},
                    {"class": "Martial Artist", "percentage": 10.72, "trend": 1},
                    {"class": "Oracle", "percentage": 7.30, "trend": 1},
                ],
            },
            {
                "leagueName": "HC Forbidden Rites",
                "leagueUrl": "forbiddenriteshc",
                "total": 6396,
                "status": 0,
                "category": 0,
                "hardcore": True,
                "statistics": [
                    {"class": "Smith of Kitava", "percentage": 22.40, "trend": 1},
                ],
            },
            {
                "leagueName": "SSF Forbidden Rites",
                "leagueUrl": "forbiddenritesssf",
                "total": 8248,
                "status": 0,
                "category": 0,
                "hardcore": False,
                "statistics": [
                    {"class": "Deadeye", "percentage": 16.10, "trend": 0},
                ],
            },
            {
                "leagueName": "HC SSF Forbidden Rites",
                "leagueUrl": "forbiddenriteshcssf",
                "total": 3731,
                "status": 0,
                "category": 0,
                "hardcore": True,
                "statistics": [
                    {"class": "Titan", "percentage": 18.50, "trend": 1},
                ],
            },
        ]
    }
    snap_by_slug = {
        "forbiddenrites": ("Forbidden Rites", "forbidden-rites", "aa" * 32),
        "forbiddenriteshc": ("HC Forbidden Rites", "hc-forbidden-rites", "bb" * 32),
        "forbiddenritesssf": ("SSF Forbidden Rites", "ssf-forbidden-rites", "cc" * 32),
        "forbiddenriteshcssf": ("HC SSF Forbidden Rites", "hc-ssf-forbidden-rites", "dd" * 32),
    }
    seen_urls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        path = request.url.path
        if path.endswith("/build-index-state"):
            return httpx.Response(200, json=build_index)
        if path.endswith("/snapshot-versions"):
            slug = request.url.params.get("league", "forbiddenrites")
            name, snap_name, ver = snap_by_slug.get(
                slug, ("Forbidden Rites", "forbidden-rites", "aa" * 32)
            )
            return httpx.Response(
                200,
                json=[
                    {
                        "url": slug,
                        "name": name,
                        "timeMachineLabels": ["hour-6", "day-1", "week-1", "week-3"],
                        "version": ver,
                        "snapshotName": snap_name,
                        "overviewType": 0,
                        "passiveTree": "PassiveTree-0.5",
                    }
                ],
            )
        if path == "/poe2/pob/raw/overview/code":
            acct = request.url.params.get("account", "")
            char = request.url.params.get("name", "")
            if char == "MissingOrPrivateChar" or acct == "missing-0000":
                return httpx.Response(404, json={"status": 404, "title": "Not Found"})
            if char == "JOJOTENF":
                return httpx.Response(404, json={"status": 404, "title": "Not Found"})
            return httpx.Response(200, text=sample_pob_code)
        if path in (
            "/poe2/pob/raw/Share123",
            "/pob/Poe2db456/raw",
            "/poe2/api/pob/Max789",
            "/Pobbin999/raw",
        ):
            return httpx.Response(200, text=sample_pob_code)
        if path.startswith("/api/lookup/poe2/builds/dictionary/"):
            ndic = encode_ndic_dictionary(["Abyssal Lich", "Gemling Legionnaire", "Oracle"])
            return httpx.Response(200, content=ndic)
        return httpx.Response(404, text="not found")

    return _handler, seen_urls


def test_ndic_and_search_result_protobuf_roundtrip() -> None:
    vocab_classes = ["Abyssal Lich", "Gemling Legionnaire", "Martial Artist", "Oracle"]
    vocab_skills = ["Comet", "Flicker Strike", "Lightning Bolt", "Twister"]

    ndic_blob = encode_ndic_dictionary(vocab_classes)
    assert ndic_blob[:4] == b"NDIC"
    assert decode_ndic_dictionary(ndic_blob) == vocab_classes

    # Legacy non-NDIC protobuf string dictionary fallback (repeated field 2)
    legacy_blob = b"".join(_pb_str(2, s) for s in vocab_skills)
    assert decode_ndic_dictionary(legacy_blob) == vocab_skills

    with pytest.raises(ValueError, match="Truncated NDIC"):
        decode_ndic_dictionary(b"NDIC\x02\x00")

    # Build full NinjaSearchResult protobuf wire message including nulls and sparse row_ids
    dim_class = (
        _pb_str(1, "class")
        + _pb_str(2, "class")
        + _pb_bytes(3, _pb_uint(1, 1) + _pb_uint(2, 39522))
        + _pb_bytes(3, _pb_uint(1, 3) + _pb_uint(2, 9058))
    )
    dim_skills = (
        _pb_str(1, "skills")
        + _pb_str(2, "skills")
        + _pb_bytes(3, _pb_uint(1, 3) + _pb_uint(2, 12000))
    )
    dict_ref_cls = _pb_str(1, "class") + _pb_str(2, "4b3dfeccb2f4aa52ccc458925cd87f4575a4c25b")
    dict_ref_sk = _pb_str(1, "skills") + _pb_str(2, "99887766554433221100aabbccddeeff00112233")

    col_name = (
        _pb_str(1, "name")
        + _pb_str(7, "ResurrectForbidden")
        + _pb_str(7, "하데스일주년")
        + _pb_str(7, "FR_obi")
        + _pb_uint(13, 3)
    )
    col_acct = (
        _pb_str(1, "account")
        + _pb_str(7, "heygyus-0416")
        + _pb_str(7, "박태준-2959")
        + _pb_str(7, "0bibea-1410")
        + _pb_uint(13, 3)
    )
    col_cls = (
        _pb_str(1, "class") + _pb_packed(6, [1, 2, 3]) + _pb_str(11, "class") + _pb_uint(13, 3)
    )
    col_dps = _pb_str(1, "dps") + _pb_packed(6, [305663, 1016031, 4265765]) + _pb_uint(13, 3)
    # Sparse column: delve_depth only populated on row 0 and row 2 (row 1 omitted via row_ids)
    col_sparse = (
        _pb_str(1, "depth") + _pb_packed(6, [420, 615]) + _pb_uint(13, 3) + _pb_packed(14, [0, 2])
    )
    # Nullable column: row 1 marked null via field 10
    col_es = (
        _pb_str(1, "energyShield")
        + _pb_packed(6, [3031, 9075])
        + _pb_packed(10, [1])
        + _pb_uint(13, 3)
    )

    inner = (
        _pb_uint(1, 124092)
        + _pb_bytes(2, dim_class)
        + _pb_bytes(2, dim_skills)
        + _pb_bytes(6, dict_ref_cls)
        + _pb_bytes(6, dict_ref_sk)
        + _pb_bytes(12, col_name)
        + _pb_bytes(12, col_acct)
        + _pb_bytes(12, col_cls)
        + _pb_bytes(12, col_dps)
        + _pb_bytes(12, col_sparse)
        + _pb_bytes(12, col_es)
    )
    wire = _pb_bytes(1, inner)

    parsed = decode_search_result(wire, {"class": vocab_classes, "skills": vocab_skills})
    assert parsed.total == 124092
    assert len(parsed.dictionaries) == 2
    assert parsed.dimensions["class"][0].label == "Gemling Legionnaire"
    assert parsed.dimensions["class"][0].count == 39522
    assert parsed.dimensions["skills"][0].label == "Twister"
    assert len(parsed.roster) == 3
    assert parsed.roster[1]["name"] == "하데스일주년"
    assert parsed.roster[1]["class"] == "Martial Artist"
    assert parsed.roster[0]["depth"] == 420
    assert parsed.roster[1]["depth"] is None
    assert parsed.roster[2]["depth"] == 615
    assert parsed.columns["energyShield"] == [3031, None, 9075]


def test_bundled_forbidden_rites_corpus_integrity() -> None:
    lic_path = BUNDLED_DATA_DIR / "ninja" / "LICENSE.pobr"
    corpus_path = BUNDLED_DATA_DIR / "ninja" / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
    assert lic_path.is_file()
    assert "MIT License" in lic_path.read_text(encoding="utf-8")
    assert corpus_path.is_file()

    with gzip.open(corpus_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    assert payload["patch"] == POE2_PATCH
    assert payload["fixture_count"] == 353
    assert payload["ascendancy_count"] == 23

    top10_counts: dict[str, int] = {}
    for rec in payload["builds"]:
        if rec["suite"] == "ninja-top10-2026-10-26":
            asc = rec["ascendancy_name"]
            top10_counts[asc] = top10_counts.get(asc, 0) + 1
        # Lineage supports must never leak into active_skills
        assert "Rakiata's Flow" not in rec["active_skills"]
        assert "Dialla's Desire" not in rec["active_skills"]
        assert rec["weapon_types"], f"Empty weapon_types on {rec['fixture_id']}"

    assert len(top10_counts) == 23
    assert all(cnt == 10 for cnt in top10_counts.values())


def test_multi_facet_search_and_guardrails(tmp_path: Path) -> None:
    corpus_path = BUNDLED_DATA_DIR / "ninja" / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
    with gzip.open(corpus_path, "rt", encoding="utf-8") as handle:
        sample_code = json.load(handle)["builds"][0]["pob_code"]

    handler, _ = _mock_ninja_handler_factory(sample_code)
    http_client = _make_mock_http_client(handler)

    with NinjaBuildsService(
        http_client=http_client,
        cache_dir=tmp_path / "cache",
        min_uncached_char_interval_seconds=0.0,
    ) as svc:
        # 1. Snapshot state across all 4 modes
        snap = svc.get_snapshot_state("HC Forbidden Rites")
        assert snap.primary.mode == "hc_trade"
        assert snap.primary.hardcore is True
        assert snap.primary.snapshot_name == "hc-forbidden-rites"
        assert set(snap.modes) == {"sc_trade", "hc_trade", "ssf", "hc_ssf"}
        assert snap.modes["sc_trade"].total_characters == 124092
        assert snap.modes["ssf"].ssf is True
        assert snap.modes["hc_ssf"].hardcore is True and snap.modes["hc_ssf"].ssf is True

        # 2. Multi-facet search: shorthand ascendancy + skill + unique + keystone + weapon
        rep_all = svc.search_builds(limit=20)
        assert rep_all.corpus_total_builds >= 340
        assert rep_all.matched_builds == rep_all.corpus_total_builds
        assert len(rep_all.builds) == 20
        assert rep_all.top_ascendancies
        assert rep_all.top_skills
        assert rep_all.top_supports
        assert rep_all.top_uniques
        assert rep_all.top_keystones
        assert rep_all.top_weapons
        assert (
            rep_all.dps_distribution.min
            <= rep_all.dps_distribution.median
            <= rep_all.dps_distribution.p90
            <= rep_all.dps_distribution.max
        )

        # Shorthand alias ("gemling" -> "Gemling Legionnaire")
        rep_gemling = svc.search_builds(ascendancy="gemling", limit=10)
        assert rep_gemling.matched_builds >= 10
        assert all(b.ascendancy_name == "Gemling Legionnaire" for b in rep_gemling.builds)

        # Weapon alias ("quarterstaff" matches Warstaff/Quarterstaff builds)
        rep_qs = svc.search_builds(weapon_type="quarterstaff", limit=10)
        assert rep_qs.matched_builds > 0
        assert all("Quarterstaff" in b.weapon_types for b in rep_qs.builds)

        # Unarmed / Hollow Palm filter
        rep_unarmed = svc.search_builds(weapon_type="hollow palm", limit=10)
        assert rep_unarmed.matched_builds > 0

        # Combined filters: Unique + Support Gem + Keystone + sort_by="es"
        rep_combo = svc.search_builds(
            unique_item="Temporalis",
            support_gem="Dialla's Desire",
            keystone="Pain Attunement",
            sort_by="es",
            include_pob_code=True,
            limit=5,
        )
        assert rep_combo.matched_builds >= 1
        top_b = rep_combo.builds[0]
        assert "Temporalis" in top_b.unique_items
        assert "Dialla's Desire" in top_b.support_gems
        assert "Pain Attunement" in top_b.keystones
        assert top_b.pob_code is not None and top_b.pob_code.startswith("eN")

        # PoE1 / unreleased 1.0 trap guardrails
        for bad_cls in ("Necromancer", "Shadow", "Trickster", "Marauder", "Elementalist"):
            with pytest.raises(LookupError):
                svc.search_builds(class_name=bad_cls)


def test_character_url_routing_pacing_and_fallbacks(tmp_path: Path) -> None:
    corpus_path = BUNDLED_DATA_DIR / "ninja" / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
    with gzip.open(corpus_path, "rt", encoding="utf-8") as handle:
        builds = json.load(handle)["builds"]
    sample_code = builds[0]["pob_code"]

    handler, seen_urls = _mock_ninja_handler_factory(sample_code)
    http_client = _make_mock_http_client(handler)

    slept: list[float] = []
    mono_clock = [100.0]

    def _fake_mono() -> float:
        return mono_clock[0]

    def _fake_sleep(sec: float) -> None:
        slept.append(sec)
        mono_clock[0] += sec

    with NinjaBuildsService(
        http_client=http_client,
        cache_dir=tmp_path / "cache",
        min_uncached_char_interval_seconds=1.0,
        monotonic_fn=_fake_mono,
        sleep_fn=_fake_sleep,
    ) as svc:
        # 1. Character URL with #1234 normalized to -1234 and ?timemachine=week-3 extracted
        imp1 = svc.fetch_character(
            "https://poe.ninja/poe2/builds/forbiddenrites/character/acct%231234/TestChar?timemachine=week-3"
        )
        assert imp1.source_kind == "poeninja_live_pob"
        assert imp1.account == "acct-1234"
        assert imp1.character == "TestChar"
        assert imp1.timemachine == "week-3"
        assert imp1.tree_version == "0_5"
        assert imp1.equipped_items
        assert imp1.skill_groups

        # Cached repeat call should NOT hit network or sleep
        url_count_before = len(seen_urls)
        imp1_cached = svc.fetch_character(
            "https://poe.ninja/poe2/builds/forbiddenrites/character/acct-1234/TestChar",
            timemachine="week-3",
        )
        assert len(seen_urls) == url_count_before
        assert imp1_cached.pob_code == imp1.pob_code

        # Second uncached character triggers polite 1.0s pacing
        mono_clock[0] += 0.25
        svc.fetch_character("acct2-9999", "SecondChar")
        assert len(slept) == 1 and pytest.approx(slept[0], rel=1e-2) == 0.75

        # 2. Multi-site PoB2 share links
        for url, expected_kind in (
            ("https://poe.ninja/poe2/pob/Share123", "poeninja_share_pob"),
            ("https://poe2db.tw/pob/Poe2db456", "poe2db_pob"),
            ("https://maxroll.gg/poe2/pob/Max789", "maxroll_pob"),
            ("https://pobb.in/u/someone/Pobbin999", "pobbin_pob"),
        ):
            res = svc.fetch_character(url)
            assert res.source_kind == expected_kind
            assert res.pob_code == sample_code

        # 3. Live 404 on archived corpus character -> transparent corpus fallback
        archived = svc.fetch_character("1031-5196", "JOJOTENF")
        assert archived.source_kind == "poeninja_archived_corpus"
        assert archived.ascendancy_name == "Amazon"
        assert archived.notes

        # 4. Live 404 on unknown character -> LookupError
        with pytest.raises(LookupError):
            svc.fetch_character("missing-0000", "MissingOrPrivateChar")

        # 5. Refusal of /api/protected/ URLs and PoE1 <PathOfBuilding> XML
        with pytest.raises(ValueError, match="protected"):
            svc.fetch_character(
                "https://poe.ninja/api/protected/poe2/builds/abc/search?overview=forbidden-rites"
            )

        poe1_xml = '<PathOfBuilding><Build className="Scion" level="90"/></PathOfBuilding>'
        poe1_code = base64.urlsafe_b64encode(zlib.compress(poe1_xml.encode("utf-8"))).decode(
            "ascii"
        )
        with pytest.raises(ValueError, match="PathOfBuilding2"):
            inspect_pob2_xml_or_code(poe1_code)

        # 6. NDIC dictionary fetch via service
        dict_vals = svc.fetch_dictionary("4b3dfeccb2f4aa52ccc458925cd87f4575a4c25b")
        assert dict_vals == ["Abyssal Lich", "Gemling Legionnaire", "Oracle"]


@pytest.mark.skipif(not LIVE_ENABLED, reason="Set POE2_LIVE_TESTS=1 for live poe.ninja tests")
def test_live_poeninja_snapshots_dictionary_and_pob2_roundtrip(tmp_path: Path) -> None:
    with NinjaBuildsService(cache_dir=tmp_path / "live_cache") as svc:
        # 1. Verify live build-index-state + snapshot-versions across all 4 Forbidden Rites modes
        snap_rep = svc.get_snapshot_state("Forbidden Rites", force_refresh=True)
        assert snap_rep.primary.total_characters > 50_000
        assert snap_rep.primary.snapshot_name == "forbidden-rites"
        assert snap_rep.primary.version and len(snap_rep.primary.version) >= 32
        assert snap_rep.primary.passive_tree == "PassiveTree-0.5"
        assert "week-3" in snap_rep.primary.time_machine_labels

        for mode_key, expected_snap in (
            ("hc_trade", "hc-forbidden-rites"),
            ("ssf", "ssf-forbidden-rites"),
            ("hc_ssf", "hc-ssf-forbidden-rites"),
        ):
            m_info = snap_rep.modes[mode_key]
            assert m_info.total_characters > 1_000
            assert m_info.snapshot_name == expected_snap
            assert m_info.version is not None

        # 2. Verify live NDIC v2 dictionary decode
        classes = svc.fetch_dictionary(
            "4b3dfeccb2f4aa52ccc458925cd87f4575a4c25b", force_refresh=True
        )
        assert "Gemling Legionnaire" in classes
        assert "Abyssal Lich" in classes
        assert "Martial Artist" in classes
        assert len(classes) >= 31

        # 3. Verify live UTF-8 Korean character URL fetch + <2ms XML inspection
        kor_import = svc.fetch_character(
            "https://poe.ninja/poe2/builds/forbiddenrites/character/박태준-2959/하데스일주년",
            load_into_pob=False,
            force_refresh=True,
        )
        assert kor_import.source_kind == "poeninja_live_pob"
        assert kor_import.ascendancy_name == "Martial Artist"
        assert kor_import.level >= 95
        assert kor_import.equipped_items

        # 4. Verify live timemachine=week-3 + headless Pob2Bridge load_into_pob!
        cur_obi = svc.fetch_character(
            "0bibea-1410",
            "FR_obi",
            load_into_pob=True,
            slot="ninja_live",
            force_refresh=True,
        )
        tm_obi = svc.fetch_character(
            "0bibea-1410", "FR_obi", timemachine="week-3", force_refresh=True
        )
        assert cur_obi.source_kind == "poeninja_live_pob"
        assert cur_obi.loaded_into_pob is True
        assert cur_obi.pob_stats is not None
        assert cur_obi.pob_stats.level >= 95
        assert cur_obi.pob_stats.total_dps > 100_000
        assert cur_obi.pob_stats.total_ehp > 10_000
        assert tm_obi.source_kind == "poeninja_live_pob"
        assert tm_obi.timemachine == "week-3"
        assert "<PathOfBuilding2>" in decode_pob2_code_to_xml(tm_obi.pob_code)

        # 5. Verify search_builds with refresh_live_top=1 on CJK Abyssal Lich
        search_rep = svc.search_builds(
            ascendancy="Abyssal Lich",
            skill="Comet",
            refresh_live_top=1,
            limit=3,
        )
        assert search_rep.matched_builds >= 1
        assert search_rep.builds[0].live_refreshed is True
        assert search_rep.builds[0].effective_dps > 100_000
