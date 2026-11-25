#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Deterministic PoE2 0.5.5 gamedata compiler -> SQLite + FTS5 (`data/poe2_0.5.5.sqlite`).

Joins MIT upstream datasets + bundled community spawn weights & mechanics rules:
1. RePoE2 (`repoe-fork/poe2` MIT): base_items, mods, augments, keywords, skill_gems
2. Exiled Exchange 2 (`Kvan7/Exiled-Exchange-2` MIT): items.ndjson whitelist & remnants.json
3. PathOfBuilding-PoE2 (`PathOfBuildingCommunity/PathOfBuilding-PoE2` MIT): Gems, Essence, Uniques
4. Bundled `data/modsview_weights.json.gz` + `data/weights_overrides.json`
5. Bundled `data/mechanics_facts.json`
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from poe2brain.config import (
    BUNDLED_DATA_DIR,
    POE2_PATCH,
    USER_AGENT,
    get_cache_dir,
    get_gamedata_db_path,
    get_pob2_paths,
)

REPOE_BASE_URL = "https://repoe-fork.github.io/poe2"
EE2_RAW_BASE_URL = (
    "https://raw.githubusercontent.com/Kvan7/Exiled-Exchange-2/master/renderer/public/data"
)

REPOE_FILES = (
    "base_items.min.json",
    "mods.min.json",
    "keywords.min.json",
    "augments.min.json",
    "skill_gems.min.json",
)

MARKUP_LINK_RE = re.compile(r"\[([^\]|]+)\|([^\]]+)\]")
MARKUP_SINGLE_RE = re.compile(r"\[([A-Za-z0-9_'\s-]+)\]")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _download_if_missing(url: str, dest: Path) -> Path:
    if dest.is_file() and dest.stat().st_size > 100:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def ensure_upstream_cache(cache_root: Path) -> tuple[Path, Path]:
    repoe_dir = cache_root / "repoe"
    ee2_dir = cache_root / "ee2"
    tmp_repoe = Path("/tmp/poe2brain-craft/raw/repoe")
    tmp_ee2 = Path("/tmp/poe2brain-trade/Exiled-Exchange-2/renderer/public/data")

    for fname in REPOE_FILES:
        target = repoe_dir / fname
        if not target.is_file() and (tmp_repoe / fname).is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((tmp_repoe / fname).read_bytes())
        _download_if_missing(f"{REPOE_BASE_URL}/{fname}", target)

    for rel in ("en/items.ndjson", "remnants.json"):
        target = ee2_dir / rel
        if not target.is_file() and (tmp_ee2 / rel).is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((tmp_ee2 / rel).read_bytes())
        _download_if_missing(f"{EE2_RAW_BASE_URL}/{rel}", target)

    return repoe_dir, ee2_dir


def clean_ggg_markup(text: str) -> str:
    if not text:
        return ""
    s = MARKUP_LINK_RE.sub(r"\2", text)
    s = MARKUP_SINGLE_RE.sub(r"\1", s)
    s = HTML_TAG_RE.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", s).strip()


def infer_keyword_category(key: str, term: str, defn: str) -> str:
    hay = f"{key} {term} {defn}".lower()
    if any(
        w in hay
        for w in (
            "omen",
            "essence",
            "desecrat",
            "fractur",
            "sanctif",
            "corrupt",
            "socket",
            "augment",
            "quality",
            "alloy",
            "catalyst",
        )
    ):
        return "crafting"
    if any(
        w in hay
        for w in (
            "waystone",
            "atlas",
            "tablet",
            "precursor",
            "ritual",
            "breach",
            "delirium",
            "expedition",
            "abyss",
        )
    ):
        return "atlas"
    if any(
        w in hay
        for w in (
            "ignite",
            "flammab",
            "shock",
            "chill",
            "freeze",
            "electrocute",
            "bleed",
            "poison",
            "stun",
            "ailment",
            "pin",
        )
    ):
        return "ailment"
    if any(
        w in hay
        for w in (
            "armour",
            "evasion",
            "deflect",
            "block",
            "energy shield",
            "ward",
            "resist",
            "charm",
            "flask",
            "leech",
        )
    ):
        return "defence"
    if any(
        w in hay
        for w in ("support", "spirit", "meta", "gem", "skill", "minion", "totem", "weapon set")
    ):
        return "gems"
    return "combat"


def map_base_to_poe2db_slug(item_class: str, name: str, tags: list[str]) -> str | None:
    tset = set(tags)
    armour_tag = None
    for candidate in ("str_dex_int", "str_dex", "str_int", "dex_int", "str", "dex", "int"):
        if f"{candidate}_armour" in tset:
            armour_tag = candidate
            break

    ic_map = {
        "Claw": "Claws",
        "Dagger": "Daggers",
        "Wand": "Wands",
        "One Hand Sword": "One_Hand_Swords",
        "One Hand Axe": "One_Hand_Axes",
        "One Hand Mace": "One_Hand_Maces",
        "Sceptre": "Sceptres",
        "Spear": "Spears",
        "Flail": "Flails",
        "Bow": "Bows",
        "Staff": "Staves",
        "Warstaff": "Quarterstaves",
        "Quarterstaff": "Quarterstaves",
        "Two Hand Sword": "Two_Hand_Swords",
        "Two Hand Axe": "Two_Hand_Axes",
        "Two Hand Mace": "Two_Hand_Maces",
        "Crossbow": "Crossbows",
        "TrapTool": "Traps",
        "Talisman": "Talismans",
        "Amulet": "Amulets",
        "Ring": "Rings",
        "Belt": "Belts",
        "Quiver": "Quivers",
        "Buckler": "Bucklers",
        "Focus": "Foci",
        "LifeFlask": "Life_Flasks",
        "ManaFlask": "Mana_Flasks",
        "UtilityFlask": "Charms",
    }
    if item_class in ic_map:
        return ic_map[item_class]

    if item_class == "Gloves" and armour_tag:
        return f"Gloves_{armour_tag}"
    if item_class == "Boots" and armour_tag:
        return f"Boots_{armour_tag}"
    if item_class == "Body Armour" and armour_tag:
        return f"Body_Armours_{armour_tag}"
    if item_class == "Helmet" and armour_tag:
        return f"Helmets_{armour_tag}"
    if item_class == "Shield":
        if "buckler" in tset or "dex_armour" in tset:
            return "Bucklers"
        if armour_tag in ("str", "str_dex", "str_int"):
            return f"Shields_{armour_tag}"
        return "Shields_str"

    if item_class == "Jewel":
        jewel_names = (
            "Time-Lost Ruby",
            "Time-Lost Emerald",
            "Time-Lost Sapphire",
            "Time-Lost Diamond",
            "Ruby",
            "Emerald",
            "Sapphire",
            "Diamond",
        )
        for jname in jewel_names:
            if jname.lower() in name.lower():
                return jname.replace(" ", "_")

    if "Tablet" in name:
        for tab in (
            "Breach",
            "Expedition",
            "Delirium",
            "Ritual",
            "Irradiated",
            "Overseer",
            "Abyss",
            "Temple",
        ):
            if tab.lower() in name.lower():
                return f"{tab}_Tablet"

    if "Waystone" in name:
        m = re.search(r"Tier\s+(\d+)", name)
        if m:
            tier_num = int(m.group(1))
            if tier_num <= 5:
                return "Waystones_low_tier"
            return "Waystones_mid_tier" if tier_num <= 10 else "Waystones_top_tier"
        return "Waystones_top_tier"

    if "Relic" in name:
        for rtype in ("Urn", "Amphora", "Vase", "Seal", "Coffer", "Tapestry", "Incense"):
            if rtype.lower() in name.lower():
                return f"{rtype}_Relic"

    return None


def parse_pob_uniques(uniques_dir: Path, live_names: set[str]) -> list[tuple]:
    rows: list[tuple] = []
    if not uniques_dir.exists():
        return rows
    block_re = re.compile(r"\[\[(.*?)\]\]", re.S)
    variant_tag_re = re.compile(r"^\{variant:([0-9,]+)\}")
    tags_prefix_re = re.compile(r"\{tags:[^}]+\}")

    seen_ids: set[str] = set()
    for lua_file in sorted(uniques_dir.glob("*.lua")):
        slot_category = lua_file.stem
        content = lua_file.read_text(encoding="utf-8", errors="replace")
        for block in block_re.findall(content):
            lines = [
                ln.strip()
                for ln in block.strip().splitlines()
                if ln.strip() and not ln.strip().startswith("--")
            ]
            if len(lines) < 2:
                continue
            name = lines[0]
            base_type = lines[1]
            variants = [ln for ln in lines[2:] if ln.startswith("Variant:")]
            current_variant_idx = len(variants) if variants else None

            implicits: list[str] = []
            explicits: list[str] = []
            implicit_count = 0
            in_implicits = False
            req_level = None

            for ln in lines[2:]:
                if ln.startswith(("Variant:", "Source:", "League:")):
                    continue
                if ln.startswith("LevelReq:"):
                    try:
                        req_level = int(ln.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    continue
                if ln.startswith("Implicits:"):
                    try:
                        implicit_count = int(ln.split(":", 1)[1].strip())
                        in_implicits = implicit_count > 0
                    except ValueError:
                        implicit_count = 0
                    continue

                vm = variant_tag_re.match(ln)
                if vm and current_variant_idx is not None:
                    allowed = {int(x) for x in vm.group(1).split(",") if x.isdigit()}
                    if current_variant_idx not in allowed:
                        continue
                    ln = variant_tag_re.sub("", ln)

                ln = tags_prefix_re.sub("", ln).strip()
                ln = re.sub(r"^\{[^}]+\}", "", ln).strip()
                if not ln:
                    continue

                if in_implicits and len(implicits) < implicit_count:
                    implicits.append(ln)
                    if len(implicits) >= implicit_count:
                        in_implicits = False
                else:
                    explicits.append(ln)

            uid = f"{slot_category}:{name}:{base_type}"
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            is_live = 1 if name in live_names else 0
            rows.append(
                (
                    uid,
                    name,
                    base_type,
                    slot_category,
                    req_level,
                    json.dumps(implicits, ensure_ascii=False),
                    json.dumps(explicits, ensure_ascii=False),
                    is_live,
                )
            )
    return rows


def parse_pob_essences(essence_lua: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not essence_lua.exists():
        return out
    text = essence_lua.read_text(encoding="utf-8", errors="replace")
    entry_re = re.compile(
        r'\["(Metadata/[^"]+)"\]\s*=\s*\{\s*name\s*=\s*"([^"]+)",\s*'
        r'type\s*=\s*"([^"]+)",\s*tierLevel\s*=\s*(\d+),\s*mods\s*=\s*\{([^}]*)\}',
        re.S,
    )
    pair_re = re.compile(r'\["([^"]+)"\]\s*=\s*"([^"]+)"')
    for meta_id, name, etype, tier_lvl, mods_body in entry_re.findall(text):
        mods_map = dict(pair_re.findall(mods_body))
        out[name] = {
            "metadata_id": meta_id,
            "essence_type": etype,
            "tier_level": int(tier_lvl),
            "forced_mods": mods_map,
        }
    return out


def parse_pob_gems(gems_lua: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not gems_lua.exists():
        return out
    text = gems_lua.read_text(encoding="utf-8", errors="replace")
    chunks = re.split(r'\n\t\["(Metadata/[^"]+)"\]\s*=\s*\{', text)
    for i in range(1, len(chunks), 2):
        meta_id = chunks[i]
        body = chunks[i + 1]
        name_m = re.search(r'name\s*=\s*"([^"]+)"', body)
        gtype_m = re.search(r'gemType\s*=\s*"([^"]+)"', body)
        tier_m = re.search(r"tier\s*=\s*(\d+)", body)
        tags_m = re.search(r"tags\s*=\s*\{([^}]*)\}", body)
        tag_list = re.findall(r"([a-zA-Z0-9_]+)\s*=\s*true", tags_m.group(1)) if tags_m else []
        req_str = int(m.group(1)) if (m := re.search(r"reqStr\s*=\s*(\d+)", body)) else 0
        req_dex = int(m.group(1)) if (m := re.search(r"reqDex\s*=\s*(\d+)", body)) else 0
        req_int = int(m.group(1)) if (m := re.search(r"reqInt\s*=\s*(\d+)", body)) else 0
        out[meta_id] = {
            "name": name_m.group(1) if name_m else meta_id.split("/")[-1],
            "gem_type": gtype_m.group(1) if gtype_m else "Unknown",
            "tier": int(tier_m.group(1)) if tier_m else None,
            "tags": tag_list,
            "req_weights": {"str": req_str, "dex": req_dex, "int": req_int},
        }
    return out


def parse_verisium_crafts(vc_lua: Path) -> list[tuple]:
    rows: list[tuple] = []
    if not vc_lua.exists():
        return rows
    text = vc_lua.read_text(encoding="utf-8", errors="replace")
    top_re = re.compile(r'\t\["([^"]+)"\]\s*=\s*\{(.*?)\n\t\},', re.S)
    sub_re = re.compile(r'\["baseTypeId"\]\s*=\s*"([^"]+)",\s*\["name"\]\s*=\s*"([^"]+)"', re.S)
    idx = 0
    for src_base, body in top_re.findall(text):
        for target_id, out_name in sub_re.findall(body):
            idx += 1
            payload = json.dumps(
                {"source_base": src_base, "target_metadata_id": target_id},
                ensure_ascii=False,
            )
            rows.append((f"verisium_transform_{idx}", payload, out_name, "verisium_base_transform"))
    return rows


def gen_type_id_to_side(gen_type_id: int, repo_gen_type: str | None, pool_name: str) -> str:
    if pool_name == "corrupted":
        return "corrupted"
    if pool_name == "corruption_upgrade":
        return repo_gen_type if repo_gen_type in ("prefix", "suffix") else "corrupted"
    if gen_type_id == 1 or repo_gen_type == "prefix":
        return "prefix"
    if gen_type_id == 2 or repo_gen_type == "suffix":
        return "suffix"
    if gen_type_id == 3:
        return "implicit"
    if gen_type_id == 5:
        return "corrupted"
    return repo_gen_type or pool_name


def build_database(
    repoe_dir: Path,
    ee2_data_dir: Path,
    pob_data_dir: Path,
    bundled_dir: Path,
    out_sqlite: Path,
) -> dict[str, str]:
    base_items_raw = json.loads((repoe_dir / "base_items.min.json").read_text(encoding="utf-8"))
    mods_raw = json.loads((repoe_dir / "mods.min.json").read_text(encoding="utf-8"))
    keywords_raw = json.loads((repoe_dir / "keywords.min.json").read_text(encoding="utf-8"))
    augments_raw = json.loads((repoe_dir / "augments.min.json").read_text(encoding="utf-8"))
    skill_gems_raw = json.loads((repoe_dir / "skill_gems.min.json").read_text(encoding="utf-8"))

    with gzip.open(bundled_dir / "modsview_weights.json.gz", "rt", encoding="utf-8") as gz:
        modsview = json.loads(gz.read())
    overrides = json.loads((bundled_dir / "weights_overrides.json").read_text(encoding="utf-8"))
    mechanics_facts = json.loads((bundled_dir / "mechanics_facts.json").read_text(encoding="utf-8"))

    ee2_items_path = ee2_data_dir / "en" / "items.ndjson"
    live_names: set[str] = set()
    ee2_by_name: dict[str, dict] = {}
    for line in ee2_items_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        for key in ("name", "refName"):
            val = (obj.get(key) or "").strip()
            if val:
                live_names.add(val)
                ee2_by_name[val] = obj

    out_sqlite.parent.mkdir(parents=True, exist_ok=True)
    if out_sqlite.exists():
        out_sqlite.unlink()

    conn = sqlite3.connect(out_sqlite)
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE keywords (
            id TEXT PRIMARY KEY,
            term TEXT NOT NULL,
            definition_clean TEXT NOT NULL,
            definition_raw TEXT NOT NULL,
            category TEXT NOT NULL
        );

        CREATE TABLE mechanics_facts (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            poe1_or_stale_trap TEXT NOT NULL,
            poe2_live_rule TEXT NOT NULL,
            formula_expr TEXT,
            source_ref TEXT NOT NULL,
            patch_verified TEXT NOT NULL
        );

        CREATE TABLE gems (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            gem_kind TEXT NOT NULL,
            color TEXT,
            support_tier INTEGER,
            max_copies_per_char INTEGER,
            tags_json TEXT NOT NULL,
            req_weights_json TEXT NOT NULL,
            recommended_supports_json TEXT NOT NULL,
            is_live_poe2 INTEGER NOT NULL
        );

        CREATE TABLE uniques (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_type TEXT NOT NULL,
            slot_category TEXT NOT NULL,
            req_level INTEGER,
            implicits_json TEXT NOT NULL,
            explicits_json TEXT NOT NULL,
            is_live_poe2 INTEGER NOT NULL
        );

        CREATE TABLE base_items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            item_class TEXT NOT NULL,
            poe2db_slug TEXT,
            drop_level INTEGER NOT NULL,
            max_prefixes INTEGER NOT NULL,
            max_suffixes INTEGER NOT NULL,
            tags_json TEXT NOT NULL,
            implicits_json TEXT NOT NULL,
            properties_json TEXT NOT NULL,
            is_live_poe2 INTEGER NOT NULL
        );

        CREATE TABLE augments (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            augment_type TEXT NOT NULL,
            required_level INTEGER NOT NULL,
            limit_count INTEGER,
            is_socket_bound INTEGER NOT NULL,
            slot_effects_json TEXT NOT NULL,
            bonded_effects_json TEXT NOT NULL,
            description TEXT NOT NULL,
            is_live_poe2 INTEGER NOT NULL
        );

        CREATE TABLE mod_tiers (
            row_id TEXT PRIMARY KEY,
            base_group_slug TEXT NOT NULL,
            source_pool TEXT NOT NULL,
            mod_id TEXT NOT NULL,
            mod_type TEXT NOT NULL,
            family_id TEXT NOT NULL,
            families_json TEXT NOT NULL,
            affix_side TEXT NOT NULL,
            affix_name TEXT NOT NULL,
            internal_rank_asc INTEGER NOT NULL,
            display_tier INTEGER NOT NULL,
            required_level INTEGER NOT NULL,
            weight INTEGER NOT NULL,
            weight_confidence TEXT NOT NULL,
            boss_tag TEXT,
            currency_name TEXT,
            is_alloy INTEGER NOT NULL DEFAULT 0,
            removes_mod INTEGER NOT NULL DEFAULT 0,
            adds_tags_json TEXT NOT NULL,
            spawn_weights_json TEXT NOT NULL,
            mod_tags_json TEXT NOT NULL,
            stats_json TEXT NOT NULL,
            stat_text TEXT NOT NULL
        );
        CREATE INDEX idx_mod_tiers_slug_pool_side
            ON mod_tiers(base_group_slug, source_pool, affix_side);
        CREATE INDEX idx_mod_tiers_mod_id ON mod_tiers(mod_id);
        CREATE INDEX idx_mod_tiers_type ON mod_tiers(base_group_slug, mod_type);
        CREATE INDEX idx_mod_tiers_family ON mod_tiers(base_group_slug, family_id);

        CREATE TABLE crafting_levers (
            lever_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            metadata_id TEXT,
            category TEXT NOT NULL,
            is_active_drop_in_patch INTEGER NOT NULL,
            applies_to_rarities TEXT NOT NULL,
            target_classes_json TEXT NOT NULL,
            max_item_level INTEGER,
            min_mod_level_floor INTEGER,
            omen_side_constraint TEXT,
            omen_boss_constraint TEXT,
            forced_mods_json TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE runeforge_recipes (
            recipe_id TEXT PRIMARY KEY,
            inputs_json TEXT NOT NULL,
            output_item TEXT NOT NULL,
            category TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            doc_type,
            doc_id,
            title,
            category,
            body,
            tokenize = 'porter unicode61'
        );
        """
    )

    fts_rows: list[tuple[str, str, str, str, str]] = []
    blacklist_names = set(overrides.get("dormant_poe1_currencies_blacklist") or [])
    blacklist_compact = {re.sub(r"[^a-z0-9]+", "", n.lower()) for n in blacklist_names}
    removed_set = set(overrides.get("removed_levers_in_patch") or [])

    kw_rows = []
    for kid, val in sorted(keywords_raw.items()):
        term = (val.get("term") or kid).strip()
        raw_def = (val.get("definition") or "").strip()
        if not raw_def:
            continue
        kid_compact = re.sub(r"[^a-z0-9]+", "", kid.lower())
        if term in blacklist_names or term in removed_set or kid_compact in blacklist_compact:
            continue
        clean_def = clean_ggg_markup(raw_def)
        cat = infer_keyword_category(kid, term, clean_def)
        kw_rows.append((kid, term, clean_def, raw_def, cat))
        fts_rows.append(("keyword", kid, term, cat, f"{term} ({kid}): {clean_def}"))
    cur.executemany("INSERT INTO keywords VALUES (?, ?, ?, ?, ?)", kw_rows)

    mf_rows = []
    for f in mechanics_facts:
        mf_rows.append(
            (
                f["slug"],
                f["title"],
                f["category"],
                json.dumps(f["tags"], ensure_ascii=False),
                f["poe1_or_stale_trap"],
                f["poe2_live_rule"],
                f.get("formula_expr"),
                f["source_ref"],
                f["patch_verified"],
            )
        )
        fts_body = (
            f"{f['title']} | Tags: {', '.join(f['tags'])} | "
            f"PoE2 0.5.5 Live Rule: {f['poe2_live_rule']} | "
            f"Formula: {f.get('formula_expr') or ''} | "
            f"Trap to Avoid: {f['poe1_or_stale_trap']}"
        )
        fts_rows.append(("mechanic_fact", f["slug"], f["title"], f["category"], fts_body))
    cur.executemany("INSERT INTO mechanics_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", mf_rows)

    pob_gems = parse_pob_gems(pob_data_dir / "Gems.lua")
    all_gem_ids = sorted(set(pob_gems.keys()) | set(skill_gems_raw.keys()))
    gem_rows = []
    seen_gem_names: set[str] = set()
    for gid in all_gem_ids:
        pg = pob_gems.get(gid) or {}
        rg = skill_gems_raw.get(gid) or {}
        bitem = rg.get("base_item") or {}
        if bitem.get("release_state") == "unreleased":
            continue
        name = (pg.get("name") or bitem.get("display_name") or "").strip()
        if not name or name.startswith("[DNT]") or "Playtest" in name:
            continue
        is_live = 1 if (name in live_names or gid in pob_gems) else 0
        if not is_live:
            continue

        tags = pg.get("tags") or []
        pob_gtype = (pg.get("gem_type") or rg.get("gem_type") or "active").lower()
        tier = pg.get("tier")
        ee2_entry = ee2_by_name.get(name) or {}

        is_support = "support" in pob_gtype or "SupportGem" in gid
        is_lineage = False
        if is_support:
            if "'" in name or (
                ee2_entry.get("craftable") is None and "Support" not in name and tier is None
            ):
                is_lineage = True
            if re.search(r"\b(I|II|III|IV|V)$", name):
                roman = name.split()[-1]
                tier = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}.get(roman, tier)
                is_lineage = False

        if is_lineage:
            gem_kind = "lineage_support"
            max_copies = 1
        elif is_support:
            gem_kind = "support"
            max_copies = None
        elif "meta" in tags or name.startswith("Cast on ") or "Invocation" in name:
            gem_kind = "meta"
            max_copies = None
        elif "persistent" in tags or "aura" in tags or "herald" in tags:
            gem_kind = "spirit"
            max_copies = None
        else:
            gem_kind = "active"
            max_copies = None

        rec_supports = rg.get("recommended_supports") or []
        gem_rows.append(
            (
                gid,
                name,
                gem_kind,
                rg.get("color"),
                tier,
                max_copies,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(pg.get("req_weights") or {}, ensure_ascii=False),
                json.dumps(rec_supports, ensure_ascii=False),
                is_live,
            )
        )
        if name not in seen_gem_names:
            seen_gem_names.add(name)
            fts_rows.append(
                (
                    "gem",
                    gid,
                    name,
                    gem_kind,
                    f"{name} ({gem_kind}, tier={tier}, "
                    f"max_copies={max_copies or 'unlimited'}): tags={' '.join(tags)}",
                )
            )
    cur.executemany("INSERT INTO gems VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", gem_rows)

    unique_rows = parse_pob_uniques(pob_data_dir / "Uniques", live_names)
    cur.executemany("INSERT INTO uniques VALUES (?, ?, ?, ?, ?, ?, ?, ?)", unique_rows)
    for uid, uname, btype, scat, _rl, imps, exps, _is_live in unique_rows:
        exp_text = "; ".join(json.loads(exps))
        imp_text = "; ".join(json.loads(imps))
        fts_rows.append(
            ("unique", uid, uname, scat, f"{uname} ({btype}, {scat}): {imp_text} | {exp_text}")
        )

    base_rows = []
    for bid, bval in sorted(base_items_raw.items()):
        if bval.get("release_state") == "unreleased":
            continue
        name = (bval.get("name") or "").strip()
        if not name or name.startswith("[DNT]") or name in blacklist_names:
            continue
        iclass = bval.get("item_class") or "Unknown"
        tags = bval.get("tags") or []
        is_live = 0 if name in removed_set else (1 if name in live_names else 0)
        if not is_live and iclass in (
            "StackableCurrency",
            "Incubator",
            "MapFragment",
            "DelveStackableSocketableCurrency",
        ):
            continue

        slug = map_base_to_poe2db_slug(iclass, name, tags)
        if iclass in ("LifeFlask", "ManaFlask", "UtilityFlask"):
            max_p, max_s = 1, 1
        elif iclass == "Jewel" or "Tablet" in name:
            max_p, max_s = 2, 2
        else:
            max_p, max_s = 3, 3

        implicits_ids = bval.get("implicits") or []
        implicit_texts = [
            clean_ggg_markup((mods_raw.get(mid) or {}).get("text") or mid) for mid in implicits_ids
        ]
        props = bval.get("properties") or {}

        base_rows.append(
            (
                bid,
                name,
                iclass,
                slug,
                int(bval.get("drop_level") or 1),
                max_p,
                max_s,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(implicit_texts, ensure_ascii=False),
                json.dumps(props, ensure_ascii=False),
                is_live,
            )
        )
        if is_live and iclass not in ("StackableCurrency", "Omen", "SoulCore"):
            fts_rows.append(
                (
                    "base_item",
                    bid,
                    name,
                    iclass,
                    f"{name} ({iclass}, poe2db={slug}, drop_lvl={bval.get('drop_level')}): "
                    f"implicits={'; '.join(implicit_texts)} tags={' '.join(tags)}",
                )
            )
    cur.executemany("INSERT INTO base_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", base_rows)

    aug_rows = []
    for aid, aval in sorted(augments_raw.items()):
        binfo = base_items_raw.get(aid) or {}
        name = (binfo.get("name") or aval.get("name") or aid.split("/")[-1]).strip()
        if not name or name.startswith("[DNT]"):
            continue
        desc = clean_ggg_markup((binfo.get("properties") or {}).get("description") or "")
        desc_lc = desc.lower()
        is_socket_bound = (
            1
            if (
                "cannot be retrieved or replaced" in desc_lc
                or "cannot be removed, replaced" in desc_lc
            )
            else 0
        )
        limit_val = int(aval["limit"]) if str(aval.get("limit") or "").isdigit() else None
        req_lvl = int(aval.get("required_level") or binfo.get("drop_level") or 1)
        aug_type = clean_ggg_markup(aval.get("type_name") or aval.get("type_id") or "Augment")

        slot_effects: dict[str, list[str]] = {}
        bonded_effects: dict[str, list[str]] = {}
        for slot_key, sdata in (aval.get("categories") or {}).items():
            st = [clean_ggg_markup(x) for x in (sdata.get("stat_text") or [])]
            if st:
                slot_effects[slot_key] = st
            bt = [clean_ggg_markup(x) for x in (sdata.get("bonded_stat_text") or [])]
            if bt:
                bonded_effects[slot_key] = bt

        is_live = 1 if name in live_names else 0
        aug_rows.append(
            (
                aid,
                name,
                aug_type,
                req_lvl,
                limit_val,
                is_socket_bound,
                json.dumps(slot_effects, ensure_ascii=False),
                json.dumps(bonded_effects, ensure_ascii=False),
                desc,
                is_live,
            )
        )
        eff_summary = " | ".join(f"{k}: {', '.join(v)}" for k, v in slot_effects.items())
        bonded_summary = " | ".join(
            f"Bonded {k}: {', '.join(v)}" for k, v in bonded_effects.items()
        )
        fts_rows.append(
            (
                "augment",
                aid,
                name,
                aug_type,
                f"{name} ({aug_type}, req_lvl={req_lvl}): {eff_summary} {bonded_summary}",
            )
        )
    cur.executemany("INSERT INTO augments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", aug_rows)

    defaults = overrides["defaults"]
    mod_overrides = overrides.get("mod_weight_overrides") or {}
    mod_tier_rows = []
    essence_forced_by_currency: dict[str, dict[str, str]] = {}
    alloy_forced_by_currency: dict[str, dict[str, str]] = {}

    for slug, cdata in modsview["classes"].items():
        has_measured = bool(cdata.get("has_measured_normal_weights"))
        pools = cdata.get("pools") or {}

        for pool_name, rows in pools.items():
            if pool_name in ("socketable", "bonded"):
                continue

            grouped: dict[tuple[str, str], list[dict]] = {}
            for idx, r in enumerate(rows):
                mid = r.get("mod_id") or f"{slug}_{pool_name}_{idx}"
                rmod = mods_raw.get(mid) or {}
                side = gen_type_id_to_side(
                    int(r.get("gen_type_id") or 0), rmod.get("generation_type"), pool_name
                )
                raw_fams = r.get("families") or rmod.get("groups") or [rmod.get("type") or mid]
                fam_list = (
                    [str(x) for x in raw_fams if x]
                    if isinstance(raw_fams, list)
                    else [str(raw_fams)]
                )
                primary_family = fam_list[0] if fam_list else mid
                mod_type = str(rmod.get("type") or primary_family or mid)
                ladder_key = (
                    f"{mod_type}:{r.get('name')}"
                    if pool_name in ("essence", "perfect_essence", "liquid")
                    else mod_type
                )
                r_enriched = {
                    **r,
                    "_mid": mid,
                    "_rmod": rmod,
                    "_side": side,
                    "_mod_type": mod_type,
                    "_family": primary_family,
                    "_families": fam_list,
                }
                grouped.setdefault((side, ladder_key), []).append(r_enriched)

            for (side, _ladder_key), fam_rows in grouped.items():
                fam_rows.sort(
                    key=lambda x: (
                        int(x.get("level") or x["_rmod"].get("required_level") or 0),
                        x["_mid"],
                    )
                )
                total_tiers = len(fam_rows)
                for zero_idx, r in enumerate(fam_rows):
                    mid = r["_mid"]
                    rmod = r["_rmod"]
                    mod_type = r["_mod_type"]
                    fam = r["_family"]
                    fam_list = r["_families"]
                    internal_rank_asc = zero_idx + 1
                    display_tier = total_tiers - zero_idx
                    req_lvl = int(r.get("level") or rmod.get("required_level") or 1)
                    dc = r.get("drop_chance")

                    override_entry = mod_overrides.get(f"{slug}:{mid}") or mod_overrides.get(
                        f"*:{mid}"
                    )
                    if override_entry:
                        weight = int(override_entry["weight"])
                        confidence = override_entry.get("confidence", "manual_override")
                    elif pool_name in ("essence", "perfect_essence", "liquid"):
                        weight = 0
                        confidence = "deterministic"
                    elif pool_name == "desecrated":
                        weight = int(defaults["desecrated_unomen_weight"])
                        confidence = "empirical_desecrated"
                    elif pool_name in ("corrupted", "corruption_upgrade", "destruction"):
                        weight = (
                            int(dc)
                            if (dc and dc > 1)
                            else int(defaults["corrupted_implicit_weight"])
                        )
                        confidence = "equal_unmeasured"
                    elif pool_name == "normal":
                        if has_measured:
                            weight = max(0, int(dc or 0))
                            confidence = "verified_community"
                        else:
                            weight = int(defaults["unmeasured_normal_pool_weight"])
                            confidence = "equal_unmeasured"
                    else:
                        weight = max(0, int(dc or 0))
                        confidence = "unknown"

                    cname = (
                        r.get("name")
                        if pool_name in ("essence", "perfect_essence", "liquid")
                        else None
                    )
                    affix_name = rmod.get("name") or ("" if cname else (r.get("name") or ""))
                    is_alloy = 1 if r.get("is_alloy") else 0
                    removes_mod = (
                        1
                        if (r.get("removes_mod") or pool_name in ("perfect_essence", "liquid"))
                        else 0
                    )

                    if cname and pool_name in ("essence", "perfect_essence"):
                        if is_alloy:
                            alloy_forced_by_currency.setdefault(cname, {})[slug] = mid
                        else:
                            essence_forced_by_currency.setdefault(cname, {})[slug] = mid

                    adds_tags = rmod.get("adds_tags") or []
                    spawn_weights = rmod.get("spawn_weights") or []
                    mod_tags = rmod.get("implicit_tags") or r.get("tags") or []
                    stats_list = rmod.get("stats") or []
                    stat_text = r.get("stat_text") or clean_ggg_markup(rmod.get("text") or "")

                    row_id = f"{slug}:{pool_name}:{mid}:{zero_idx}"
                    mod_tier_rows.append(
                        (
                            row_id,
                            slug,
                            pool_name,
                            mid,
                            mod_type,
                            fam,
                            json.dumps(fam_list, ensure_ascii=False),
                            side,
                            affix_name,
                            internal_rank_asc,
                            display_tier,
                            req_lvl,
                            weight,
                            confidence,
                            r.get("boss_tag"),
                            cname,
                            is_alloy,
                            removes_mod,
                            json.dumps(adds_tags, ensure_ascii=False),
                            json.dumps(spawn_weights, ensure_ascii=False),
                            json.dumps(mod_tags, ensure_ascii=False),
                            json.dumps(stats_list, ensure_ascii=False),
                            stat_text,
                        )
                    )

    cur.executemany(
        "INSERT OR REPLACE INTO mod_tiers VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        mod_tier_rows,
    )

    pob_essences = parse_pob_essences(pob_data_dir / "Essence.lua")
    lever_rows = []
    seen_lever_names: set[str] = set()

    orb_floors: dict[str, tuple[str, str, int | None]] = {
        "Orb of Transmutation": ("orb", "normal->magic", 1),
        "Greater Orb of Transmutation": ("orb", "normal->magic", 55),
        "Perfect Orb of Transmutation": ("orb", "normal->magic", 70),
        "Orb of Augmentation": ("orb", "magic->magic", 1),
        "Greater Orb of Augmentation": ("orb", "magic->magic", 55),
        "Perfect Orb of Augmentation": ("orb", "magic->magic", 70),
        "Regal Orb": ("orb", "magic->rare", 1),
        "Greater Regal Orb": ("orb", "magic->rare", 35),
        "Perfect Regal Orb": ("orb", "magic->rare", 50),
        "Exalted Orb": ("orb", "rare->rare", 1),
        "Greater Exalted Orb": ("orb", "rare->rare", 35),
        "Perfect Exalted Orb": ("orb", "rare->rare", 50),
        "Chaos Orb": ("orb", "rare->rare", 1),
        "Greater Chaos Orb": ("orb", "rare->rare", 35),
        "Perfect Chaos Orb": ("orb", "rare->rare", 50),
        "Orb of Alchemy": ("orb", "normal->rare", 1),
        "Orb of Annulment": ("orb", "magic|rare->same", None),
        "Fracturing Orb": ("orb", "rare(>=4mods)->fractured_rare", None),
        "Divine Orb": ("finisher", "magic|rare|unique->reroll_values", None),
        "Vaal Orb": ("finisher", "any->corrupted", None),
        "Orb of Chance": ("finisher", "normal->unique_or_destroy", None),
        "Artificer's Orb": ("socket_currency", "equipment->add_augment_socket", None),
        "Lesser Jeweller's Orb": ("gem_currency", "skill_gem->3_sockets", None),
        "Greater Jeweller's Orb": ("gem_currency", "skill_gem->4_sockets", None),
        "Perfect Jeweller's Orb": ("gem_currency", "skill_gem->5_sockets", None),
        "Architect's Orb": ("finisher", "corrupted->twice_corrupted_or_destroy", None),
        "Vaal Cultivation Orb": ("finisher", "unique->corrupted_mutated_unique", None),
        "Hinekora's Lock": ("meta_currency", "any->foresee_next_currency", None),
        "Mirror of Kalandra": ("finisher", "non_unique->mirrored_copy", None),
    }

    for bid, bval in sorted(base_items_raw.items()):
        if bval.get("release_state") == "unreleased":
            continue
        name = (bval.get("name") or "").strip()
        if (
            not name
            or name in blacklist_names
            or name in seen_lever_names
            or name.startswith("[DNT]")
        ):
            continue
        iclass = bval.get("item_class") or ""
        if iclass not in ("StackableCurrency", "Omen"):
            continue
        is_live_trade = name in live_names
        if not is_live_trade and name not in removed_set:
            continue

        desc = clean_ggg_markup((bval.get("properties") or {}).get("description") or "")
        is_active = 0 if name in removed_set else 1
        max_ilvl = None
        min_floor = None
        side_con = None
        boss_con = None
        target_classes: list[str] = []
        forced_mods: dict[str, str] = {}
        applies_to = "rare"
        cat = "currency"

        if name in orb_floors:
            cat, applies_to, min_floor = orb_floors[name]
        elif iclass == "Omen" or name.startswith("Omen of"):
            cat = "omen"
            applies_to = "active_inventory_modifier"
            if "Sinistral" in name:
                side_con = "prefix"
            elif "Dextral" in name:
                side_con = "suffix"
            if "Blackblooded" in name:
                boss_con = "kurgal"
            elif "Liege" in name:
                boss_con = "amanamu"
            elif "Sovereign" in name:
                boss_con = "ulaman"
        elif "Jawbone" in name or "Rib" in name or "Collarbone" in name:
            cat = "bone"
            applies_to = "rare->desecrated_rare"
            if "Jawbone" in name:
                target_classes = ["Weapon", "Quiver"]
            elif "Rib" in name:
                target_classes = [
                    "Body Armour",
                    "Helmet",
                    "Gloves",
                    "Boots",
                    "Shield",
                    "Buckler",
                    "Focus",
                ]
            elif "Collarbone" in name:
                target_classes = ["Amulet", "Ring", "Belt"]
            if name.startswith("Gnawed"):
                max_ilvl = 64
                min_floor = 1
            elif name.startswith(("Preserved", "Altered")):
                min_floor = 1
            elif name.startswith("Ancient"):
                min_floor = 40
        elif "Alloy" in name:
            cat = "alloy"
            applies_to = "rare->remove_1_add_1_forced"
            forced_mods = alloy_forced_by_currency.get(name) or {}
        elif "Essence" in name:
            forced_mods = (
                essence_forced_by_currency.get(name)
                or (pob_essences.get(name) or {}).get("forced_mods")
                or {}
            )
            corrupted_ess = (
                "Delirium",
                "Horror",
                "Hysteria",
                "Insanity",
                "the Breach",
                "the Abyss",
            )
            if name.startswith("Perfect Essence") or any(x in name for x in corrupted_ess):
                cat = "perfect_or_corrupted_essence"
                applies_to = "rare->remove_1_add_1_forced"
            else:
                cat = "essence"
                applies_to = "magic->rare_add_1_forced"
        elif "Catalyst" in name:
            cat = "catalyst"
            applies_to = "jewel" if name.startswith("Refined ") else "ring|amulet"
        elif "Liquid " in name:
            cat = "liquid_emotion"
            applies_to = (
                "rare_timelost_jewel|amulet"
                if name.startswith("Ancient ")
                else "rare_basic_jewel|amulet|waystone"
            )
        elif "Infuser" in name or "Orb of Sacrifice" in name:
            cat = "incursion_finisher"
            applies_to = "equipment|tablet|jewel"
        elif any(w in name for w in ("Whetstone", "Scrap", "Etcher", "Bauble", "Prism")):
            cat = "quality_currency"
            applies_to = "normal|magic|rare|unique"
        elif any(x in name for x in ("Shard", "Splinter", "Fragment", "Key", "Simulacrum")):
            continue

        lever_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        seen_lever_names.add(name)
        lever_rows.append(
            (
                lever_id,
                name,
                bid,
                cat,
                is_active,
                applies_to,
                json.dumps(target_classes, ensure_ascii=False),
                max_ilvl,
                min_floor,
                side_con,
                boss_con,
                json.dumps(forced_mods, ensure_ascii=False),
                desc,
            )
        )
        status_str = "ACTIVE in 0.5.5" if is_active else "REMOVED/LEGACY in 0.5.5"
        fts_rows.append(
            (
                "crafting_lever",
                lever_id,
                name,
                cat,
                f"{name} [{status_str}] ({cat}, applies={applies_to}, "
                f"floor={min_floor}, max_ilvl={max_ilvl}): {desc}",
            )
        )

    if "Omen of Recombination" not in seen_lever_names:
        lever_rows.append(
            (
                "omen_of_recombination",
                "Omen of Recombination",
                None,
                "omen",
                0,
                "legacy_recombinator",
                "[]",
                None,
                None,
                None,
                None,
                "{}",
                "Legacy Recombinator omen; Recombinators were phased out in Patch 0.5.",
            )
        )

    cur.executemany(
        "INSERT INTO crafting_levers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", lever_rows
    )

    rf_rows = parse_verisium_crafts(pob_data_dir / "VerisiumCrafts.lua")
    remnants_path = ee2_data_dir / "remnants.json"
    if remnants_path.exists():
        rem_data = json.loads(remnants_path.read_text(encoding="utf-8"))
        recipes_map = rem_data.get("recipes") or {}
        rewards_raw = rem_data.get("rewards")
        rewards_list = rewards_raw if isinstance(rewards_raw, list) else []
        for rkey, seq in sorted(recipes_map.items()):
            parts = rkey.split("|")
            glyph_name = parts[0]
            min_cnt = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            total_slots = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            items_meta = [
                {
                    "idx": idx,
                    "id": rewards_list[idx].get("id"),
                    "reward": rewards_list[idx].get("refReward"),
                    "count": rewards_list[idx].get("count", 1),
                    "cat": rewards_list[idx].get("cat"),
                    "gem_level": rewards_list[idx].get("gemLevel", 0),
                }
                for idx in (seq or [])
                if isinstance(idx, int) and 0 <= idx < len(rewards_list)
            ]
            slot_prefix = f"{total_slots}Slot" if total_slots is not None else ""
            top_meta = [
                m
                for m in items_meta
                if slot_prefix and str(m.get("id") or "").startswith(slot_prefix)
            ] or items_meta[:1]
            reward_labels: list[str] = []
            for m in top_meta:
                rname = str(m.get("reward") or "").strip()
                if not rname:
                    continue
                cnt = int(m.get("count") or 1)
                glvl = int(m.get("gem_level") or 0)
                lbl = f"{cnt}x {rname}" if cnt > 1 else rname
                if glvl > 0:
                    lbl = f"{lbl} (Lv.{glvl})"
                if lbl not in reward_labels:
                    reward_labels.append(lbl)
            output_reward = " / ".join(reward_labels) if reward_labels else glyph_name
            payload = json.dumps(
                {
                    "runeshape": glyph_name,
                    "min_glyph_count": min_cnt,
                    "total_slots": total_slots,
                    "rune_sequence_ids": seq,
                    "items_meta": items_meta,
                },
                ensure_ascii=False,
            )
            rf_rows.append((f"remnant:{rkey}", payload, output_reward, "kalguuran_remnant_recipe"))
    cur.executemany("INSERT OR REPLACE INTO runeforge_recipes VALUES (?, ?, ?, ?)", rf_rows)

    cur.executemany("INSERT INTO knowledge_fts VALUES (?, ?, ?, ?, ?)", fts_rows)

    counts = {
        "patch": POE2_PATCH,
        "client_version": "4.5.4.3",
        "generated_at": datetime.now(UTC).isoformat(),
        "keywords_count": str(len(kw_rows)),
        "mechanics_facts_count": str(len(mf_rows)),
        "gems_count": str(len(gem_rows)),
        "uniques_count": str(len(unique_rows)),
        "base_items_count": str(len(base_rows)),
        "augments_count": str(len(aug_rows)),
        "mod_tiers_count": str(len(mod_tier_rows)),
        "crafting_levers_count": str(len(lever_rows)),
        "runeforge_recipes_count": str(len(rf_rows)),
        "fts_docs_count": str(len(fts_rows)),
    }
    cur.executemany("INSERT INTO meta VALUES (?, ?)", list(counts.items()))

    conn.commit()
    cur.execute("VACUUM")
    conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PoE2 0.5.5 SQLite + FTS5 database")
    parser.add_argument("--bundled-dir", type=Path, default=BUNDLED_DATA_DIR)
    parser.add_argument("--cache-dir", type=Path, default=get_cache_dir() / "gamedata_upstream")
    parser.add_argument("--pob-data-dir", type=Path, default=get_pob2_paths().src_dir / "Data")
    parser.add_argument("--out", type=Path, default=get_gamedata_db_path())
    args = parser.parse_args()

    repoe_dir, ee2_data_dir = ensure_upstream_cache(args.cache_dir)
    counts = build_database(repoe_dir, ee2_data_dir, args.pob_data_dir, args.bundled_dir, args.out)
    print(f"Built {args.out} ({args.out.stat().st_size:,} bytes)")
    for k, v in counts.items():
        print(f"  {k:26s}: {v}")


if __name__ == "__main__":
    main()
