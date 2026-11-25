#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pobr-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()
    out_path = build_ninja_corpus(pobr_dir=args.pobr_dir, out_dir=args.out_dir)
    with gzip.open(out_path, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    print(
        f"Wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB): "
        f"{data['fixture_count']} fixtures, "
        f"{data['unique_character_count']} unique characters, "
        f"{data['ascendancy_count']} ascendancies."
    )


if __name__ == "__main__":
    main()
