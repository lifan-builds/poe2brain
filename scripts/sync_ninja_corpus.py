#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Compile the Patch 0.5.5 Forbidden Rites poe.ninja build corpus (`data/ninja/`).

Reads the MIT-licensed `ackness/pobr` PoE2 0.5 Forbidden Rites fixtures (353 builds,
including all 23 Ascendancies' top-10 leaderboards), enriches every `<PathOfBuilding2>`
build against `data/poe2_0.5.5.sqlite` (`base_items`, `gems`) and `TreeData/0_5`, and
writes a deterministic gzip JSON bundle (`data/ninja/forbidden_rites_0.5.5_builds.json.gz`).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import tarfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from poe2brain.config import (
    BUNDLED_DATA_DIR,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    USER_AGENT,
    get_cache_dir,
)
from poe2brain.ninja.xml_inspector import inspect_pob2_xml_or_code

POBR_TARBALL_URL = "https://codeload.github.com/ackness/pobr/tar.gz/HEAD"
FORBIDDEN_RITES_SUITES: tuple[str, ...] = (
    "examples/reference-builds/ninja-top10-2026-10-26",
    "examples/reference-builds/batch-2026-10-27",
    "examples/reference-builds/tuned",
    "examples/reference-builds/holdout",
    "examples/wasm-benchmark",
)


def ensure_pobr_cache(cache_root: Path | None = None) -> Path:
    root = (cache_root or get_cache_dir()) / "pobr"
    if (root / "LICENSE").is_file() and (root / "examples").is_dir():
        return root

    root.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(POBR_TARBALL_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        archive_bytes = resp.read()

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue
            rel = Path(*parts[1:])
            if ".." in rel.parts:
                continue
            dest = root / rel
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is not None:
                    dest.write_bytes(extracted.read())
    return root


def build_ninja_corpus(
    *,
    pobr_dir: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    src_root = pobr_dir if pobr_dir is not None else ensure_pobr_cache()
    target_dir = out_dir if out_dir is not None else (BUNDLED_DATA_DIR / "ninja")
    target_dir.mkdir(parents=True, exist_ok=True)

    lic_src = src_root / "LICENSE"
    if lic_src.is_file():
        shutil.copyfile(lic_src, target_dir / "LICENSE.pobr")

    records: list[dict[str, Any]] = []
    ascendancies_seen: set[str] = set()
    unique_identities: set[tuple[str, str]] = set()

    for suite_rel in FORBIDDEN_RITES_SUITES:
        suite_dir = src_root / suite_rel
        if not suite_dir.is_dir():
            raise FileNotFoundError(f"Missing expected suite directory: {suite_dir}")

        for meta_path in sorted(suite_dir.rglob("meta.json")):
            code_path = meta_path.with_name("code.txt")
            if not code_path.is_file():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            src = meta.get("source") or {}
            if src.get("league") != DEFAULT_LEAGUE:
                continue
            ev = src.get("game_version_evidence") or {}
            if ev.get("xml_tree_version") != "0_5":
                continue

            code = code_path.read_text(encoding="utf-8").strip()
            inspected = inspect_pob2_xml_or_code(code)

            account = str(src.get("account") or "").strip()
            character = str(src.get("character") or "").strip()
            if not account or not character:
                continue

            char_meta = meta.get("character") or {}
            cls_name = inspected.class_name or str(char_meta.get("class") or "Unknown")
            asc_name = inspected.ascendancy_name or (
                str(char_meta["ascendancy"]) if char_meta.get("ascendancy") else None
            )
            main_skill = inspected.main_skill or (
                str(char_meta["main_skill"]) if char_meta.get("main_skill") else None
            )
            ninja_url = str(src.get("url") or "").strip() or (
                "https://poe.ninja/poe2/builds/forbiddenrites/character/"
                f"{quote(account, safe='')}/{quote(character, safe='')}"
            )
            rank_raw = src.get("rank_within_ascendancy")
            rank_int = int(rank_raw) if isinstance(rank_raw, int) else None

            if asc_name:
                ascendancies_seen.add(asc_name)
            unique_identities.add((account.lower(), character.lower()))

            records.append(
                {
                    "suite": Path(suite_rel).name,
                    "fixture_id": str(meta.get("name") or meta_path.parent.name),
                    "fetched_at": str(src.get("fetched_at") or ""),
                    "account": account,
                    "character": character,
                    "league": DEFAULT_LEAGUE,
                    "rank_within_ascendancy": rank_int,
                    "class_name": cls_name,
                    "ascendancy_name": asc_name,
                    "level": inspected.level,
                    "main_skill": main_skill,
                    "active_skills": inspected.active_skills,
                    "support_gems": inspected.support_gems,
                    "unique_items": inspected.unique_items,
                    "weapon_types": inspected.weapon_types,
                    "keystones": inspected.keystones,
                    "ascendancy_notables": inspected.ascendancy_notables,
                    "anoints": inspected.anoints,
                    "total_dps": inspected.total_dps,
                    "combined_dps": inspected.combined_dps,
                    "full_dps": inspected.full_dps,
                    "effective_dps": inspected.effective_dps,
                    "total_ehp": inspected.total_ehp,
                    "life": inspected.life,
                    "energy_shield": inspected.energy_shield,
                    "mana": inspected.mana,
                    "spirit": inspected.spirit,
                    "ninja_url": ninja_url,
                    "pob_code": code,
                }
            )

    # Propagate rank_within_ascendancy across duplicate (account, character) snapshots
    rank_by_identity: dict[tuple[str, str], int] = {}
    for rec in records:
        ident = (rec["account"].lower(), rec["character"].lower())
        rnk = rec["rank_within_ascendancy"]
        if isinstance(rnk, int) and (
            ident not in rank_by_identity or rnk < rank_by_identity[ident]
        ):
            rank_by_identity[ident] = rnk
    for rec in records:
        ident = (rec["account"].lower(), rec["character"].lower())
        if rec["rank_within_ascendancy"] is None and ident in rank_by_identity:
            rec["rank_within_ascendancy"] = rank_by_identity[ident]

    payload = {
        "patch": POE2_PATCH,
        "league": DEFAULT_LEAGUE,
        "upstream": "ackness/pobr (MIT License, Copyright (c) 2026 PoBR contributors)",
        "fixture_count": len(records),
        "unique_character_count": len(unique_identities),
        "ascendancy_count": len(ascendancies_seen),
        "builds": records,
    }

    out_path = target_dir / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw_json)
    out_path.write_bytes(buf.getvalue())
    return out_path


T0_CHASE_UNIQUES: frozenset[str] = frozenset(
    {
        "Mageblood",
        "Headhunter",
        "Temporalis",
        "Kalandra's Touch",
        "Astramentis",
        "Ingenuity",
    }
)

MOBILITY_SKILL_TAGS: dict[str, str] = {
    "Blink": "blink_teleport",
    "Lightning Warp": "blink_teleport",
    "Shattering Palm": "dash_melee",
    "Flicker Strike": "dash_melee",
    "Whirling Slash": "dash_melee",
    "Leap Slam": "dash_melee",
    "Shield Charge": "dash_melee",
    "Thunderous Leap": "dash_melee",
    "Pounce": "dash_melee",
    "Stampede": "dash_melee",
    "Herald of Ice": "herald_chain",
    "Herald of Thunder": "herald_chain",
    "Herald of Ash": "herald_chain",
    "Herald of Plague": "herald_chain",
    "Herald of Blood": "herald_chain",
    "Cast on Critical": "trigger_clear",
    "Cast on Elemental Ailment": "trigger_clear",
    "Cast on Freeze": "trigger_clear",
    "Cast on Dodge": "trigger_clear",
    "Mirage Archer": "auto_clear",
    "Mirage Deadeye": "auto_clear",
    "Wind Dancer": "evade_speed",
    "Ghost Dance": "evade_speed",
}


def compute_mobility_and_chase_tags(rec: dict[str, Any]) -> tuple[list[str], list[str]]:
    tags: set[str] = set()
    for sk in rec.get("active_skills") or ():
        tag = MOBILITY_SKILL_TAGS.get(str(sk))
        if tag:
            tags.add(tag)
    uniques = [str(u) for u in (rec.get("unique_items") or ())]
    if "Temporalis" in uniques:
        tags.add("temporalis_zero_cd")
    if "Queen of the Forest" in uniques:
        tags.add("qotf_speed")
    if "The Fall of the Axe" in uniques:
        tags.add("onslaught_speed")
    chase = sorted(u for u in uniques if u in T0_CHASE_UNIQUES)
    return sorted(tags), chase


def enrich_corpus_with_pob2(corpus_path: Path) -> Path:
    """Enrich every build in an existing corpus bundle with headless PoB2 audit + move speed."""
    from poe2brain.pob.service import BuildService

    with gzip.open(corpus_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)

    records: list[dict[str, Any]] = payload.get("builds") or []
    code_cache: dict[str, dict[str, Any]] = {}
    svc = BuildService()
    try:
        for idx, rec in enumerate(records, start=1):
            mob_tags, chase_uniques = compute_mobility_and_chase_tags(rec)
            rec["mobility_tags"] = mob_tags
            rec["t0_chase_uniques"] = chase_uniques

            code = str(rec.get("pob_code") or "").strip()
            if not code:
                continue
            cached = code_cache.get(code)
            if cached is None:
                try:
                    svc.load_build(code, slot="corpus_enrich")
                    audit = svc.audit_build(slot="corpus_enrich")
                    sus = audit.sustainable_pinnacle_stats
                    cached = {
                        "dewatered_dps": round(float(sus.effective_dps), 2),
                        "dps_inflation_pct": round(float(audit.dps_inflation_pct), 2),
                        "dewatered_ehp": round(float(sus.total_ehp), 2),
                        "phys_max_hit": round(float(sus.phys_max_hit), 1),
                        "chaos_max_hit": round(float(sus.chaos_max_hit), 1),
                        "chaos_immune": bool(sus.chaos_immune),
                        "movement_speed_mod": round(float(sus.movement_speed_mod), 4),
                        "movement_speed_while_using_skill": round(
                            float(sus.movement_speed_while_using_skill), 4
                        ),
                    }
                except Exception:
                    cached = {}
                code_cache[code] = cached

            for k, v in cached.items():
                rec[k] = v

            if idx % 50 == 0 or idx == len(records):
                print(
                    f"[sync_ninja_corpus] Enriched {idx}/{len(records)} builds "
                    f"({len(code_cache)} unique PoB2 audits)...",
                    flush=True,
                )
    finally:
        svc.close()

    payload["pob2_enriched"] = True
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw_json)
    corpus_path.write_bytes(buf.getvalue())
    return corpus_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pobr-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--enrich-pob",
        action="store_true",
        help="Run headless PoB2 dewatering audit + movement speed extraction across corpus builds.",
    )
    parser.add_argument(
        "--enrich-existing",
        action="store_true",
        help="Enrich the existing bundled forbidden_rites_0.5.5_builds.json.gz in place.",
    )
    args = parser.parse_args()
    if args.enrich_existing:
        target_dir = args.out_dir if args.out_dir is not None else (BUNDLED_DATA_DIR / "ninja")
        out_path = target_dir / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"
        enrich_corpus_with_pob2(out_path)
    else:
        out_path = build_ninja_corpus(pobr_dir=args.pobr_dir, out_dir=args.out_dir)
        if args.enrich_pob:
            enrich_corpus_with_pob2(out_path)
    with gzip.open(out_path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    print(
        f"Wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB): "
        f"{data['fixture_count']} fixtures, "
        f"{data['unique_character_count']} unique characters, "
        f"{data['ascendancy_count']} ascendancies, "
        f"pob2_enriched={bool(data.get('pob2_enriched'))}."
    )


if __name__ == "__main__":
    main()
