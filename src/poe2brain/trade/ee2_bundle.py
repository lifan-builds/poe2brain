# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from poe2brain.config import BUNDLED_DATA_DIR

DEFAULT_EE2_DATA_DIR: Path = BUNDLED_DATA_DIR / "ee2"

ITEM_KEEP_KEYS = (
    "name",
    "refName",
    "namespace",
    "tradeTag",
    "tradeDisc",
    "disc",
    "craftable",
    "unique",
    "map",
    "gem",
    "armour",
    "augment",
    "tags",
    "w",
    "h",
)

STAT_KEEP_KEYS = (
    "id",
    "ref",
    "dp",
    "better",
    "fromAreaMods",
    "fromUberAreaMods",
    "fromHeistAreaMods",
    "anointments",
    "matchers",
    "trade",
)


def _git_head_sha(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def extract_ee2_bundle(
    ee2_repo_dir: Path,
    out_dir: Path = DEFAULT_EE2_DATA_DIR,
    official_trade_stats_path: Path | None = None,
) -> dict[str, Any]:
    """Extract compact, MIT-attributed PoE2 lookup files from Exiled-Exchange-2."""
    out_dir.mkdir(parents=True, exist_ok=True)
    en_dir = ee2_repo_dir / "renderer" / "public" / "data" / "en"
    pub_data_dir = ee2_repo_dir / "renderer" / "public" / "data"

    license_src = ee2_repo_dir / "LICENSE"
    commit_sha = _git_head_sha(ee2_repo_dir)
    license_text = (
        license_src.read_text(encoding="utf-8") if license_src.exists() else "MIT License\n"
    )
    attribution_header = (
        "Source: https://github.com/Kvan7/Exiled-Exchange-2\n"
        f"Commit: {commit_sha}\n"
        "License: MIT\n\n"
    )
    (out_dir / "LICENSE.Exiled-Exchange-2").write_text(
        attribution_header + license_text,
        encoding="utf-8",
    )

    stats_src = en_dir / "stats.ndjson"
    stats_out = out_dir / "ee2_stats.ndjson"
    stat_count = 0
    with stats_src.open(encoding="utf-8") as fin, stats_out.open("w", encoding="utf-8") as fout:
        for raw_line in fin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            row = json.loads(raw_line)
            compact = {k: row[k] for k in STAT_KEEP_KEYS if k in row and row[k] is not None}
            fout.write(json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n")
            stat_count += 1

    items_src = en_dir / "items.ndjson"
    items_out = out_dir / "ee2_items.ndjson"
    item_count = 0
    namespace_counts: dict[str, int] = {}
    with items_src.open(encoding="utf-8") as fin, items_out.open("w", encoding="utf-8") as fout:
        for raw_line in fin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            row = json.loads(raw_line)
            compact = {k: row[k] for k in ITEM_KEEP_KEYS if k in row and row[k] is not None}
            if compact.get("tags") == []:
                compact.pop("tags", None)
            ns = compact.get("namespace", "UNKNOWN")
            namespace_counts[ns] = namespace_counts.get(ns, 0) + 1
            fout.write(json.dumps(compact, ensure_ascii=False, separators=(",", ":")) + "\n")
            item_count += 1

    remnants_src = pub_data_dir / "remnants.json"
    remnants_data = json.loads(remnants_src.read_text(encoding="utf-8"))
    (out_dir / "ee2_remnants.json").write_text(
        json.dumps(remnants_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    item_drop_src = pub_data_dir / "item-drop.json"
    item_drop_data = (
        json.loads(item_drop_src.read_text(encoding="utf-8")) if item_drop_src.exists() else []
    )
    (out_dir / "ee2_item_drop.json").write_text(
        json.dumps(item_drop_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    official_stat_count = 0
    if official_trade_stats_path and official_trade_stats_path.exists():
        raw_official = json.loads(official_trade_stats_path.read_text(encoding="utf-8"))
        compact_groups: dict[str, list[dict[str, str]]] = {}
        for grp in raw_official.get("result", []):
            gid = grp.get("id", "")
            entries = []
            for ent in grp.get("entries", []):
                eid = ent.get("id")
                etxt = ent.get("text")
                if eid and etxt:
                    entries.append({"id": eid, "text": etxt})
                    official_stat_count += 1
            compact_groups[gid] = entries
        (out_dir / "trade2_official_stats.json").write_text(
            json.dumps(compact_groups, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    manifest = {
        "source_repo": "https://github.com/Kvan7/Exiled-Exchange-2",
        "license": "MIT",
        "commit": commit_sha,
        "stat_count": stat_count,
        "item_count": item_count,
        "item_namespaces": namespace_counts,
        "remnant_recipe_count": len(remnants_data.get("recipes", {})),
        "remnant_reward_count": len(remnants_data.get("rewards", [])),
        "item_drop_group_count": len(item_drop_data),
        "official_trade2_stat_count": official_stat_count,
    }
    (out_dir / "ee2_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


@dataclass(slots=True)
class EE2Bundle:
    data_dir: Path
    manifest: dict[str, Any]
    stats: list[dict[str, Any]]
    items: list[dict[str, Any]]
    remnants: dict[str, Any]
    item_drops: list[dict[str, Any]]
    stat_by_match_str: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]
    stat_by_ref: dict[str, dict[str, Any]]
    items_by_name: dict[tuple[str, str], list[dict[str, Any]]]
    items_by_any_name: dict[str, list[dict[str, Any]]]
    base_item_names_desc: list[str]
    trade_tag_by_name: dict[str, str]
    augment_by_trade_id: dict[str, list[dict[str, Any]]]
    official_stats_by_domain: dict[str, dict[str, list[str]]]
    official_stats_flat: list[tuple[str, str, str]]

    @classmethod
    def load(cls, data_dir: Path = DEFAULT_EE2_DATA_DIR) -> EE2Bundle:
        manifest_path = data_dir / "ee2_manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"EE2 bundle not found at {data_dir}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stats: list[dict[str, Any]] = []
        stat_by_match_str: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        stat_by_ref: dict[str, dict[str, Any]] = {}

        with (data_dir / "ee2_stats.ndjson").open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                st = json.loads(line)
                stats.append(st)
                stat_by_ref[st["ref"]] = st
                for m in st.get("matchers", []):
                    s_key = m.get("string")
                    if s_key:
                        stat_by_match_str.setdefault(s_key, []).append((st, m))
                    adv_key = m.get("advanced")
                    if adv_key and adv_key != s_key:
                        stat_by_match_str.setdefault(adv_key, []).append((st, m))

        items: list[dict[str, Any]] = []
        items_by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
        items_by_any_name: dict[str, list[dict[str, Any]]] = {}
        base_names: set[str] = set()
        trade_tag_by_name: dict[str, str] = {}
        augment_by_trade_id: dict[str, list[dict[str, Any]]] = {}

        with (data_dir / "ee2_items.ndjson").open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                it = json.loads(line)
                items.append(it)
                ns = it.get("namespace", "ITEM")
                for key_name in {it.get("name", ""), it.get("refName", "")}:
                    if not key_name:
                        continue
                    items_by_name.setdefault((ns, key_name), []).append(it)
                    items_by_any_name.setdefault(key_name, []).append(it)
                    if ns == "ITEM":
                        base_names.add(key_name)
                    if it.get("tradeTag"):
                        trade_tag_by_name[key_name] = it["tradeTag"]
                        trade_tag_by_name[key_name.lower()] = it["tradeTag"]
                for aug in it.get("augment") or []:
                    for tid in aug.get("tradeId") or []:
                        augment_by_trade_id.setdefault(tid, []).append(it)

        base_item_names_desc = sorted(base_names, key=len, reverse=True)
        remnants = json.loads((data_dir / "ee2_remnants.json").read_text(encoding="utf-8"))
        item_drops = json.loads((data_dir / "ee2_item_drop.json").read_text(encoding="utf-8"))

        official_stats_by_domain: dict[str, dict[str, list[str]]] = {}
        official_stats_flat: list[tuple[str, str, str]] = []
        off_path = data_dir / "trade2_official_stats.json"
        if off_path.exists():
            raw_off = json.loads(off_path.read_text(encoding="utf-8"))
            for dom, entries in raw_off.items():
                dom_map: dict[str, list[str]] = {}
                for ent in entries:
                    txt = ent["text"]
                    eid = ent["id"]
                    dom_map.setdefault(txt, []).append(eid)
                    official_stats_flat.append((dom, eid, txt))
                official_stats_by_domain[dom] = dom_map

        return cls(
            data_dir=data_dir,
            manifest=manifest,
            stats=stats,
            items=items,
            remnants=remnants,
            item_drops=item_drops,
            stat_by_match_str=stat_by_match_str,
            stat_by_ref=stat_by_ref,
            items_by_name=items_by_name,
            items_by_any_name=items_by_any_name,
            base_item_names_desc=base_item_names_desc,
            trade_tag_by_name=trade_tag_by_name,
            augment_by_trade_id=augment_by_trade_id,
            official_stats_by_domain=official_stats_by_domain,
            official_stats_flat=official_stats_flat,
        )

    def find_item(self, name: str, namespace: str | None = None) -> list[dict[str, Any]]:
        if namespace:
            return self.items_by_name.get((namespace, name), [])
        return self.items_by_any_name.get(name, [])

    def extract_magic_base_type(self, magic_name: str) -> str | None:
        cleaned = magic_name
        for prefix in ("Superior ", "Exceptional "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
        for base in self.base_item_names_desc:
            idx = cleaned.find(base)
            if idx != -1:
                before_ok = idx == 0 or cleaned[idx - 1] == " "
                end = idx + len(base)
                after_ok = end == len(cleaned) or cleaned[end] == " "
                if before_ok and after_ok:
                    return base
        return None


_DEFAULT_BUNDLE: EE2Bundle | None = None


def get_default_bundle(data_dir: Path = DEFAULT_EE2_DATA_DIR) -> EE2Bundle:
    global _DEFAULT_BUNDLE
    if _DEFAULT_BUNDLE is None or _DEFAULT_BUNDLE.data_dir != data_dir:
        _DEFAULT_BUNDLE = EE2Bundle.load(data_dir=data_dir)
    return _DEFAULT_BUNDLE
