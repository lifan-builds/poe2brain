"""Fast (<2ms) pure-Python PathOfBuilding2 XML inspector for poe.ninja builds."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from poe2brain.config import get_gamedata_db_path, get_pob2_paths
from poe2brain.models.ninja import NinjaEquippedItemSummary, NinjaSkillGroupSummary

_ANOINT_RE = re.compile(r"^(?:\{enchant\})?Allocates\s+(.+)$", re.IGNORECASE)
_JEWEL_SOCKET_ALLOC_RE = re.compile(r"^\d+\s+.*sockets?$", re.IGNORECASE)

_ITEM_META_PREFIXES: tuple[str, ...] = (
    "Unique ID:",
    "Item Level:",
    "Quality:",
    "Sockets:",
    "Rune:",
    "LevelReq:",
    "Radius:",
    "Limited to:",
    "Implicits:",
    "Armour:",
    "Evasion:",
    "Energy Shield:",
    "Ward:",
    "Block:",
    "Spirit:",
    "Charm Slots:",
    "Charges:",
    "Selected Variant:",
    "Has Alt Variant",
    "Variant:",
    "League:",
    "Source:",
)

WEAPON_ITEM_CLASSES: frozenset[str] = frozenset(
    {
        "Bow",
        "Buckler",
        "Crossbow",
        "Dagger",
        "Flail",
        "Focus",
        "One Hand Axe",
        "One Hand Mace",
        "One Hand Sword",
        "Quarterstaff",
        "Quiver",
        "Sceptre",
        "Shield",
        "Spear",
        "Staff",
        "Talisman",
        "Two Hand Axe",
        "Two Hand Mace",
        "Two Hand Sword",
        "Wand",
        "Warstaff",
    }
)


@dataclass(slots=True)
class InspectedPob2Build:
    tree_version: str | None
    class_name: str
    ascendancy_name: str | None
    level: int
    main_skill: str | None
    active_skills: list[str]
    support_gems: list[str]
    unique_items: list[str]
    weapon_types: list[str]
    keystones: list[str]
    ascendancy_notables: list[str]
    anoints: list[str]
    equipped_items: list[NinjaEquippedItemSummary]
    skill_groups: list[NinjaSkillGroupSummary]
    xml_stats: dict[str, float]
    total_dps: float
    combined_dps: float
    full_dps: float
    effective_dps: float
    total_ehp: float
    life: float
    energy_shield: float
    mana: float
    spirit: int


@dataclass(slots=True)
class _InspectorCatalog:
    base_to_class: dict[str, str] = field(default_factory=dict)
    base_lower_to_canon: dict[str, str] = field(default_factory=dict)
    gem_kind_by_id: dict[str, str] = field(default_factory=dict)
    gem_kind_by_name: dict[str, str] = field(default_factory=dict)
    keystones_by_id: dict[int, str] = field(default_factory=dict)
    asc_notables_by_id: dict[int, tuple[str, dict[str, str]]] = field(default_factory=dict)
    asc_override_id_to_name: dict[int, str] = field(default_factory=dict)


_CATALOG_CACHE: dict[tuple[str, str], _InspectorCatalog] = {}


def _normalize_item_class_display(raw_cls: str) -> str:
    if raw_cls == "Warstaff":
        return "Quarterstaff"
    return raw_cls


def _load_catalog(
    db_path: Path | None = None,
    tree_json_path: Path | None = None,
) -> _InspectorCatalog:
    eff_db = db_path if db_path is not None else get_gamedata_db_path()
    eff_tree = (
        tree_json_path
        if tree_json_path is not None
        else (get_pob2_paths().src_dir / "TreeData" / "0_5" / "tree.json")
    )
    cache_key = (str(eff_db.resolve()), str(eff_tree.resolve()))
    cached = _CATALOG_CACHE.get(cache_key)
    if cached is not None:
        return cached

    cat = _InspectorCatalog()

    if eff_db.is_file():
        conn = sqlite3.connect(f"file:{eff_db}?mode=ro", uri=True)
        try:
            for b_name, i_cls in conn.execute(
                "SELECT name, item_class FROM base_items WHERE is_live_poe2 = 1"
            ):
                if b_name and i_cls:
                    disp_cls = _normalize_item_class_display(str(i_cls))
                    cat.base_to_class[str(b_name)] = disp_cls
                    cat.base_lower_to_canon[str(b_name).lower()] = str(b_name)
            for gid, gname, gkind in conn.execute("SELECT id, name, gem_kind FROM gems"):
                if gid and gkind:
                    cat.gem_kind_by_id[str(gid).lower()] = str(gkind).lower()
                if gname and gkind:
                    cat.gem_kind_by_name[str(gname).lower()] = str(gkind).lower()
        finally:
            conn.close()

    if eff_tree.is_file():
        raw_tree = json.loads(eff_tree.read_text(encoding="utf-8"))
        nodes = raw_tree.get("nodes") or {}
        for nid_str, node in nodes.items():
            if not isinstance(node, dict):
                continue
            try:
                nid = int(node.get("skill") or nid_str)
            except (TypeError, ValueError):
                continue
            name = str(node.get("name") or "").strip()
            if node.get("isKeystone") and name:
                cat.keystones_by_id[nid] = name
                try:
                    cat.keystones_by_id[int(nid_str)] = name
                except ValueError:
                    pass
                continue

            if node.get("isAscendancyStart"):
                continue
            has_asc = bool(node.get("ascendancyName") or node.get("options"))
            if not has_asc:
                continue
            is_notable_or_choice = bool(node.get("isNotable") or node.get("isMultipleChoiceOption"))
            overrides_by_asc: dict[str, str] = {}
            opts = node.get("options")
            if isinstance(opts, dict):
                for asc_key, ov in opts.items():
                    if isinstance(ov, dict):
                        ov_name = str(ov.get("name") or name).strip()
                        if is_notable_or_choice and ov_name:
                            overrides_by_asc[str(asc_key)] = ov_name
                        ov_id = ov.get("id")
                        if is_notable_or_choice and isinstance(ov_id, int) and ov_name:
                            cat.asc_override_id_to_name[ov_id] = ov_name

            if is_notable_or_choice and (name or overrides_by_asc):
                cat.asc_notables_by_id[nid] = (name, overrides_by_asc)
                try:
                    cat.asc_notables_by_id[int(nid_str)] = (name, overrides_by_asc)
                except ValueError:
                    pass

    _CATALOG_CACHE[cache_key] = cat
    return cat


def decode_pob2_code_to_xml(code_or_xml: str) -> str:
    """Decode a PathOfBuilding2 URL-safe base64 zlib/gzip share code (or pass raw XML through)."""
    trimmed = code_or_xml.strip()
    if not trimmed:
        raise ValueError("Empty PoB2 build input")
    if trimmed.startswith("<") or "<PathOfBuilding" in trimmed[:500]:
        return trimmed

    compact = re.sub(r"\s+", "", trimmed)
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    try:
        compressed = base64.urlsafe_b64decode(padded)
    except Exception as exc:
        raise ValueError(f"Invalid base64 PoB2 share code: {exc}") from exc

    try:
        xml_bytes = zlib.decompress(compressed, zlib.MAX_WBITS | 32)
    except zlib.error:
        try:
            xml_bytes = zlib.decompress(compressed, -zlib.MAX_WBITS)
        except zlib.error as exc:
            raise ValueError(f"Invalid zlib/gzip stream in PoB2 share code: {exc}") from exc

    return xml_bytes.decode("utf-8")


def _parse_single_item_element(
    raw_text: str,
    cat: _InspectorCatalog,
) -> tuple[str, str, str | None, str, int | None, list[str], str | None]:
    """Return `(display_name, base_type, item_class, rarity, ilvl, runes, anoint)`."""
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return ("Unknown", "Unknown", None, "NORMAL", None, [], None)

    rarity = "RARE"
    idx = 0
    if lines[0].upper().startswith("RARITY:"):
        rarity = lines[0].split(":", 1)[1].strip().upper() or "RARE"
        idx = 1

    header_lines: list[str] = []
    while idx < len(lines):
        cur = lines[idx]
        if any(cur.startswith(p) for p in _ITEM_META_PREFIXES):
            break
        header_lines.append(cur)
        idx += 1
        if len(header_lines) >= 2:
            break

    ilvl: int | None = None
    runes: list[str] = []
    anoint: str | None = None
    for ln in lines[idx:]:
        if ln.startswith("Item Level:"):
            m_ilvl = re.search(r"\d+", ln)
            if m_ilvl:
                ilvl = int(m_ilvl.group(0))
        elif ln.startswith("Rune:"):
            r_name = ln.split(":", 1)[1].strip()
            if r_name and r_name.lower() != "none":
                runes.append(r_name)
        else:
            m_anoint = _ANOINT_RE.match(ln)
            if m_anoint:
                cand = m_anoint.group(1).strip()
                if cand and not _JEWEL_SOCKET_ALLOC_RE.match(cand):
                    anoint = cand

    if len(header_lines) >= 2:
        title, base_type = header_lines[0], header_lines[1]
        disp_name = title if rarity == "UNIQUE" else f"{title}, {base_type}"
    elif len(header_lines) == 1:
        only = header_lines[0]
        base_type = only
        if only.lower() not in cat.base_lower_to_canon:
            # Magic items: e.g. "Brimming Ultimate Mana Flask of the Abundant"
            best_base = ""
            for b_low, b_canon in cat.base_lower_to_canon.items():
                if b_low in only.lower() and len(b_canon) > len(best_base):
                    best_base = b_canon
            if best_base:
                base_type = best_base
        disp_name = only
    else:
        disp_name, base_type = "Unknown", "Unknown"

    canon_base = cat.base_lower_to_canon.get(base_type.lower(), base_type)
    item_class = cat.base_to_class.get(canon_base)
    return (disp_name, canon_base, item_class, rarity, ilvl, runes, anoint)


def _is_support_gem(gem_el: ET.Element, cat: _InspectorCatalog) -> bool:
    gem_id = (gem_el.get("gemId") or "").strip()
    name_spec = (gem_el.get("nameSpec") or "").strip()
    skill_id = (gem_el.get("skillId") or "").strip()
    variant_id = (gem_el.get("variantId") or "").strip()

    if gem_id:
        kind = cat.gem_kind_by_id.get(gem_id.lower())
        if kind is not None:
            return "support" in kind
        if "/SupportGem" in gem_id:
            return True
        if "/SkillGem" in gem_id:
            return False
    if name_spec:
        kind = cat.gem_kind_by_name.get(name_spec.lower())
        if kind is not None:
            return "support" in kind
    if skill_id.startswith("Support") or variant_id.endswith("Support"):
        return True
    return False


def inspect_pob2_xml_or_code(
    code_or_xml: str,
    *,
    db_path: Path | None = None,
    tree_json_path: Path | None = None,
) -> InspectedPob2Build:
    """Parse `<PathOfBuilding2>` XML (or share code) into structured build metadata."""
    xml_text = decode_pob2_code_to_xml(code_or_xml)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed PoB2 XML: {exc}") from exc

    if root.tag != "PathOfBuilding2":
        raise ValueError(
            f"Unsupported root XML tag <{root.tag}>; expected <PathOfBuilding2> (PoE2 build)."
        )

    cat = _load_catalog(db_path=db_path, tree_json_path=tree_json_path)

    build_el = root.find("Build")
    class_name = (build_el.get("className") if build_el is not None else None) or "Unknown"
    raw_asc = (build_el.get("ascendClassName") if build_el is not None else None) or ""
    ascendancy_name = raw_asc if (raw_asc and raw_asc != "None") else None
    level = int((build_el.get("level") if build_el is not None else None) or 1)
    main_socket_group = int(
        (build_el.get("mainSocketGroup") if build_el is not None else None) or 1
    )

    xml_stats: dict[str, float] = {}
    if build_el is not None:
        for ps in build_el.findall("PlayerStat"):
            st_name = ps.get("stat")
            st_val = ps.get("value")
            if st_name and st_val is not None:
                try:
                    xml_stats[st_name] = float(st_val)
                except ValueError:
                    continue

    total_dps = float(xml_stats.get("TotalDPS", 0.0))
    combined_dps = float(xml_stats.get("CombinedDPS", 0.0))
    full_dps = float(xml_stats.get("FullDPS", 0.0))
    effective_dps = (
        full_dps if full_dps > 0.0 else (combined_dps if combined_dps > 0.0 else total_dps)
    )
    total_ehp = float(xml_stats.get("TotalEHP", 0.0))
    life = float(xml_stats.get("Life", 0.0))
    energy_shield = float(xml_stats.get("EnergyShield", 0.0))
    mana = float(xml_stats.get("Mana", 0.0))
    spirit = int(round(float(xml_stats.get("Spirit", 0.0))))

    # --- Items, Uniques, Weapon Types, Anoints ---
    items_el = root.find("Items")
    parsed_items_by_id: dict[
        str, tuple[str, str, str | None, str, int | None, list[str], str | None]
    ] = {}
    use_second_weapon_set = False
    slot_to_item_id: list[tuple[str, str]] = []

    if items_el is not None:
        use_second_weapon_set = (items_el.get("useSecondWeaponSet") or "").lower() == "true"
        active_set_id = items_el.get("activeItemSet") or "1"
        for it_el in items_el.findall("Item"):
            iid = it_el.get("id")
            if iid:
                # Strip child <ModRange> tags while preserving text lines
                raw_txt = "".join(it_el.itertext())
                parsed_items_by_id[iid] = _parse_single_item_element(raw_txt, cat)

        target_set = None
        for iset in items_el.findall("ItemSet"):
            if iset.get("id") == active_set_id:
                target_set = iset
                break
        if target_set is None:
            target_set = items_el.find("ItemSet")

        if target_set is not None:
            if (target_set.get("useSecondWeaponSet") or "").lower() == "true":
                use_second_weapon_set = True
            for sl in target_set.findall("Slot"):
                sname = sl.get("name") or ""
                sid = sl.get("itemId") or "0"
                if sname and sid != "0" and sid in parsed_items_by_id:
                    slot_to_item_id.append((sname, sid))

    # Tree jewel sockets
    tree_el = root.find("Tree")
    spec_el: ET.Element | None = None
    if tree_el is not None:
        active_spec = tree_el.get("activeSpec") or "1"
        specs = tree_el.findall("Spec")
        idx_spec = max(0, int(active_spec) - 1) if active_spec.isdigit() else 0
        if specs:
            spec_el = specs[idx_spec] if idx_spec < len(specs) else specs[0]
            sockets_el = spec_el.find("Sockets")
            if sockets_el is not None:
                for sock in sockets_el.findall("Socket"):
                    nid = sock.get("nodeId") or ""
                    sid = sock.get("itemId") or "0"
                    if nid and sid != "0" and sid in parsed_items_by_id:
                        slot_to_item_id.append((f"Jewel {nid}", sid))

    equipped_items: list[NinjaEquippedItemSummary] = []
    unique_items: list[str] = []
    anoints: list[str] = []
    weapon_by_slot: dict[str, str] = {}

    for slot_name, iid in slot_to_item_id:
        disp_name, base_type, item_class, rarity, ilvl, runes, anoint = parsed_items_by_id[iid]
        equipped_items.append(
            NinjaEquippedItemSummary(
                slot=slot_name,
                name=disp_name,
                base_type=base_type,
                item_class=item_class,
                rarity=rarity,
                item_level=ilvl,
                runes=runes,
                anoint=anoint,
            )
        )
        if rarity == "UNIQUE" and disp_name not in unique_items:
            unique_items.append(disp_name)
        if anoint and anoint not in anoints:
            anoints.append(anoint)
        if slot_name in ("Weapon 1", "Weapon 2", "Weapon 1 Swap", "Weapon 2 Swap") and item_class:
            weapon_by_slot[slot_name] = item_class

    primary_slots = (
        ("Weapon 1 Swap", "Weapon 2 Swap") if use_second_weapon_set else ("Weapon 1", "Weapon 2")
    )
    fallback_slots = (
        ("Weapon 1", "Weapon 2") if use_second_weapon_set else ("Weapon 1 Swap", "Weapon 2 Swap")
    )
    weapon_types: list[str] = []
    for ws in primary_slots:
        wcls = weapon_by_slot.get(ws)
        if wcls and wcls in WEAPON_ITEM_CLASSES and wcls not in weapon_types:
            weapon_types.append(wcls)
    if not weapon_types:
        for ws in fallback_slots:
            wcls = weapon_by_slot.get(ws)
            if wcls and wcls in WEAPON_ITEM_CLASSES and wcls not in weapon_types:
                weapon_types.append(wcls)
    if not weapon_types:
        weapon_types.append("Unarmed")

    # --- Tree Keystones & Ascendancy Notables ---
    tree_version: str | None = None
    keystones: list[str] = []
    ascendancy_notables: list[str] = []
    if spec_el is not None:
        tree_version = spec_el.get("treeVersion")
        node_tokens: list[str] = []
        raw_nodes = spec_el.get("nodes") or ""
        if raw_nodes:
            node_tokens.extend(raw_nodes.split(","))
        for wset_tag in ("WeaponSet1", "WeaponSet2"):
            ws_el = spec_el.find(wset_tag)
            if ws_el is not None and ws_el.get("nodes"):
                node_tokens.extend((ws_el.get("nodes") or "").split(","))

        for tok in node_tokens:
            tok_s = tok.strip()
            if not tok_s.isdigit():
                continue
            nid = int(tok_s)
            ks_name = cat.keystones_by_id.get(nid)
            if ks_name and ks_name not in keystones:
                keystones.append(ks_name)
                continue
            if nid in cat.asc_override_id_to_name:
                ov_nm = cat.asc_override_id_to_name[nid]
                if ov_nm and ov_nm not in ascendancy_notables:
                    ascendancy_notables.append(ov_nm)
                continue
            asc_entry = cat.asc_notables_by_id.get(nid)
            if asc_entry is not None:
                default_nm, ov_map = asc_entry
                eff_nm = ov_map.get(ascendancy_name, default_nm) if ascendancy_name else default_nm
                if (
                    eff_nm
                    and eff_nm.lower() != "passive point"
                    and eff_nm != ascendancy_name
                    and eff_nm not in ascendancy_notables
                ):
                    ascendancy_notables.append(eff_nm)

    # --- Skills & Supports ---
    skill_groups: list[NinjaSkillGroupSummary] = []
    active_skills: list[str] = []
    support_gems: list[str] = []
    main_skill: str | None = None

    skills_el = root.find("Skills")
    if skills_el is not None:
        active_skill_set = skills_el.get("activeSkillSet") or "1"
        target_skill_set = None
        for sset in skills_el.findall("SkillSet"):
            if sset.get("id") == active_skill_set:
                target_skill_set = sset
                break
        if target_skill_set is None:
            target_skill_set = skills_el.find("SkillSet")
        skill_elems = (
            target_skill_set.findall("Skill")
            if target_skill_set is not None
            else skills_el.findall("Skill")
        )

        for g_idx, sk_el in enumerate(skill_elems, start=1):
            if (sk_el.get("source") or "") == "Explode":
                continue
            grp_enabled = (sk_el.get("enabled") or "true").lower() != "false"
            is_main_grp = g_idx == main_socket_group
            main_act_raw = sk_el.get("mainActiveSkill") or "1"
            main_act_idx = int(main_act_raw) if main_act_raw.isdigit() else 1

            grp_actives: list[str] = []
            grp_supports: list[str] = []
            for gem_el in sk_el.findall("Gem"):
                if (gem_el.get("enabled") or "true").lower() == "false":
                    continue
                if (gem_el.get("skillId") or "") == "EnemyExplode":
                    continue
                g_name = (gem_el.get("nameSpec") or "").strip()
                if not g_name:
                    continue
                if _is_support_gem(gem_el, cat):
                    if g_name not in grp_supports:
                        grp_supports.append(g_name)
                    if grp_enabled and g_name not in support_gems:
                        support_gems.append(g_name)
                else:
                    grp_actives.append(g_name)
                    if grp_enabled and g_name not in active_skills:
                        active_skills.append(g_name)

            primary_active: str | None = None
            if grp_actives:
                pick = max(0, min(len(grp_actives) - 1, main_act_idx - 1))
                primary_active = grp_actives[pick]
                if is_main_grp and primary_active:
                    main_skill = primary_active

            skill_groups.append(
                NinjaSkillGroupSummary(
                    index=g_idx,
                    is_main_group=is_main_grp,
                    slot=sk_el.get("slot") or None,
                    active_skill=primary_active,
                    active_gems=grp_actives,
                    support_gems=grp_supports,
                )
            )

    if main_skill is None and active_skills:
        main_skill = active_skills[0]

    return InspectedPob2Build(
        tree_version=tree_version,
        class_name=class_name,
        ascendancy_name=ascendancy_name,
        level=level,
        main_skill=main_skill,
        active_skills=active_skills,
        support_gems=support_gems,
        unique_items=unique_items,
        weapon_types=weapon_types,
        keystones=keystones,
        ascendancy_notables=ascendancy_notables,
        anoints=anoints,
        equipped_items=equipped_items,
        skill_groups=skill_groups,
        xml_stats=xml_stats,
        total_dps=round(total_dps, 2),
        combined_dps=round(combined_dps, 2),
        full_dps=round(full_dps, 2),
        effective_dps=round(effective_dps, 2),
        total_ehp=round(total_ehp, 2),
        life=round(life, 2),
        energy_shield=round(energy_shield, 2),
        mana=round(mana, 2),
        spirit=spirit,
    )
