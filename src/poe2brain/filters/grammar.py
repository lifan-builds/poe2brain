# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from poe2brain.config import BUNDLED_DATA_DIR, POE2_PATCH, get_gamedata_db_path

VALID_BLOCK_TYPES: frozenset[str] = frozenset({"Show", "Hide", "Minimal"})

VALID_COLORS: tuple[str, ...] = (
    "Red",
    "Green",
    "Blue",
    "Brown",
    "White",
    "Yellow",
    "Cyan",
    "Grey",
    "Orange",
    "Pink",
    "Purple",
)
VALID_COLOR_SET: frozenset[str] = frozenset(VALID_COLORS)

VALID_SHAPES: tuple[str, ...] = (
    "Circle",
    "Diamond",
    "Hexagon",
    "Square",
    "Star",
    "Triangle",
    "Cross",
    "Moon",
    "Raindrop",
    "Kite",
    "Pentagon",
    "UpsideDownHouse",
)
VALID_SHAPE_SET: frozenset[str] = frozenset(VALID_SHAPES)

VALID_NAMED_SOUNDS: tuple[str, ...] = (
    "ShAlchemy",
    "ShBlessed",
    "ShChaos",
    "ShDivine",
    "ShExalted",
    "ShFusing",
    "ShGeneral",
    "ShMirror",
    "ShRegal",
    "ShVaal",
)
VALID_NAMED_SOUND_SET: frozenset[str] = frozenset(VALID_NAMED_SOUNDS)

RARITY_ORDINAL: dict[str, int] = {
    "Normal": 0,
    "Magic": 1,
    "Rare": 2,
    "Unique": 3,
}

NUMERIC_OPERATORS: frozenset[str] = frozenset({"=", "==", "!=", "!", "<", "<=", ">", ">="})


@dataclass(frozen=True, slots=True)
class ConditionRuleSpec:
    name: str
    kind: Literal["string_list", "number", "boolean", "rarity", "explicit_mod"]
    min_val: int | None = None
    max_val: int | None = None


POE2_VALID_CONDITIONS: dict[str, ConditionRuleSpec] = {
    "Class": ConditionRuleSpec("Class", "string_list"),
    "BaseType": ConditionRuleSpec("BaseType", "string_list"),
    "Rarity": ConditionRuleSpec("Rarity", "rarity"),
    "AreaLevel": ConditionRuleSpec("AreaLevel", "number", 1, 100),
    "ItemLevel": ConditionRuleSpec("ItemLevel", "number", 1, 100),
    "DropLevel": ConditionRuleSpec("DropLevel", "number", 1, 100),
    "WaystoneTier": ConditionRuleSpec("WaystoneTier", "number", 1, 16),
    "UnidentifiedItemTier": ConditionRuleSpec("UnidentifiedItemTier", "number", 1, 5),
    "Sockets": ConditionRuleSpec("Sockets", "number", 0, 6),
    "Quality": ConditionRuleSpec("Quality", "number", 0, 100),
    "StackSize": ConditionRuleSpec("StackSize", "number", 1, 1_000_000),
    "GemLevel": ConditionRuleSpec("GemLevel", "number", 1, 21),
    "Width": ConditionRuleSpec("Width", "number", 1, 4),
    "Height": ConditionRuleSpec("Height", "number", 1, 4),
    "BaseArmour": ConditionRuleSpec("BaseArmour", "number", 0, 10_000),
    "BaseEvasion": ConditionRuleSpec("BaseEvasion", "number", 0, 10_000),
    "BaseEnergyShield": ConditionRuleSpec("BaseEnergyShield", "number", 0, 10_000),
    "BaseWard": ConditionRuleSpec("BaseWard", "number", 0, 10_000),
    "BaseDefencePercentile": ConditionRuleSpec("BaseDefencePercentile", "number", 0, 100),
    "CorruptedMods": ConditionRuleSpec("CorruptedMods", "number", 0, 10),
    "Corrupted": ConditionRuleSpec("Corrupted", "boolean"),
    "TwiceCorrupted": ConditionRuleSpec("TwiceCorrupted", "boolean"),
    "Mirrored": ConditionRuleSpec("Mirrored", "boolean"),
    "Identified": ConditionRuleSpec("Identified", "boolean"),
    "FracturedItem": ConditionRuleSpec("FracturedItem", "boolean"),
    "AnyEnchantment": ConditionRuleSpec("AnyEnchantment", "boolean"),
    "AlwaysShow": ConditionRuleSpec("AlwaysShow", "boolean"),
    "HasVaalUniqueMod": ConditionRuleSpec("HasVaalUniqueMod", "boolean"),
    "IsVaalUnique": ConditionRuleSpec("IsVaalUnique", "boolean"),
    "HasImplicitMod": ConditionRuleSpec("HasImplicitMod", "boolean"),
    "HasExplicitMod": ConditionRuleSpec("HasExplicitMod", "explicit_mod"),
    "HasEnchantment": ConditionRuleSpec("HasEnchantment", "string_list"),
}

POE1_BLOCKED_CONDITIONS: dict[str, str] = {
    "SocketGroup": ("PoE2 gear uses colorless Rune/Augment sockets; use `Sockets >= 2` instead."),
    "LinkedSockets": ("PoE2 links live on Skill Gems, not gear; remove `LinkedSockets`."),
    "MapTier": "PoE2 endgame uses Waystones; replace `MapTier` with `WaystoneTier`.",
    "ShaperItem": "PoE1 Elder/Shaper influence does not exist in PoE2.",
    "ElderItem": "PoE1 Elder/Shaper influence does not exist in PoE2.",
    "ShapedMap": "PoE1 Shaped maps do not exist in PoE2; use `WaystoneTier`.",
    "ElderMap": "PoE1 Elder maps do not exist in PoE2; use `WaystoneTier`.",
    "HasInfluence": "PoE1 Conqueror/Shaper/Elder influences do not exist in PoE2.",
    "HasSearingExarchImplicit": "PoE1 Eldritch implicits do not exist in PoE2.",
    "HasEaterOfWorldsImplicit": "PoE1 Eldritch implicits do not exist in PoE2.",
    "HasCruciblePassiveTree": "PoE1 Crucible trees do not exist in PoE2.",
    "BlightedMap": "PoE1 Blighted maps do not exist in PoE2.",
    "UberBlightedMap": "PoE1 Blight-ravaged maps do not exist in PoE2.",
    "Replica": "PoE1 Heist Replicas do not exist in PoE2.",
    "Scourged": "PoE1 Scourge items do not exist in PoE2.",
    "SynthesisedItem": "PoE1 Synthesised items do not exist in PoE2.",
    "GemQualityType": "PoE1 Alternate Gem Quality types do not exist in PoE2.",
    "AlternateQuality": "PoE1 Alternate Quality gems do not exist in PoE2.",
    "TransfiguredGem": "PoE1 Lab Transfigured gems do not exist in PoE2.",
    "EnchantmentPassiveNode": "PoE1 Cluster Jewel passive enchantments do not exist in PoE2.",
    "EnchantmentPassiveNum": "PoE1 Cluster Jewel passive counts do not exist in PoE2.",
    "ArchnemesisMod": "PoE1 Archnemesis tokens do not exist in PoE2.",
    "ZanaMemory": "PoE1 Zana Memory maps do not exist in PoE2.",
}

POE2_VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "SetFontSize",
        "SetTextColor",
        "SetBorderColor",
        "SetBackgroundColor",
        "PlayAlertSound",
        "PlayAlertSoundPositional",
        "CustomAlertSound",
        "CustomAlertSoundOptional",
        "PlayEffect",
        "MinimapIcon",
        "DisableDropSound",
        "EnableDropSound",
        "DisableDropSoundIfAlertSound",
        "EnableDropSoundIfAlertSound",
        "Continue",
    }
)


@dataclass(frozen=True, slots=True)
class FilterGamedataIndex:
    patch: str
    sqlite_to_filter_class: dict[str, str]
    valid_filter_classes: frozenset[str]
    internal_only_class_ids: dict[str, str]
    basetype_to_classes: dict[str, frozenset[str]]
    basetype_drop_levels: dict[str, int]
    live_poe2_basetypes: frozenset[str]
    dormant_or_removed_basetypes: frozenset[str]
    uniques_by_basetype: dict[str, tuple[str, ...]]


def _load_filter_metadata_json() -> dict:
    meta_path = BUNDLED_DATA_DIR / "filter" / f"item_classes_{POE2_PATCH}.json"
    return json.loads(meta_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def get_filter_gamedata_index(db_path: Path | None = None) -> FilterGamedataIndex:
    target_db = db_path or get_gamedata_db_path()
    meta = _load_filter_metadata_json()
    raw_ic: dict[str, str] = meta["item_classes"]

    sqlite_to_filter: dict[str, str] = {}
    internal_only: dict[str, str] = {}
    valid_classes: set[str] = set()
    for ic_id, display_name in raw_ic.items():
        clean_name = display_name.strip()
        if clean_name:
            sqlite_to_filter[ic_id] = clean_name
            valid_classes.add(clean_name)
            if ic_id != clean_name:
                internal_only[ic_id] = clean_name

    basetype_classes: dict[str, set[str]] = {}
    basetype_drop: dict[str, int] = {}
    live_bases: set[str] = set()
    dormant_bases: set[str] = set(meta.get("dormant_or_removed_basetypes", ()))
    uniques_by_base: dict[str, list[str]] = {}

    for nm, info in meta.get("extra_valid_basetypes", {}).items():
        fc = info.get("filter_class", "").strip()
        if fc:
            basetype_classes.setdefault(nm, set()).add(fc)
        else:
            basetype_classes.setdefault(nm, set())
        basetype_drop.setdefault(nm, int(info.get("drop_level", 1)))
        live_bases.add(nm)

    con = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
    try:
        for name, ic_id, drop_lvl, is_live in con.execute(
            "SELECT name, item_class, drop_level, is_live_poe2 FROM base_items"
        ):
            if not name or name.startswith("[DNT]"):
                continue
            if name in dormant_bases:
                continue
            fc = sqlite_to_filter.get(ic_id, "")
            s = basetype_classes.setdefault(name, set())
            if fc:
                s.add(fc)
            basetype_drop[name] = int(drop_lvl or 1)
            if is_live:
                live_bases.add(name)

        for name, gem_kind, is_live in con.execute("SELECT name, gem_kind, is_live_poe2 FROM gems"):
            if not name or name.startswith("[DNT]"):
                continue
            s = basetype_classes.setdefault(name, set())
            if gem_kind == "support":
                s.add("Support Gems")
            elif gem_kind == "active":
                s.add("Skill Gems")
            basetype_drop.setdefault(name, 1)
            if is_live:
                live_bases.add(name)

        for name, is_live in con.execute("SELECT name, is_live_poe2 FROM augments"):
            if not name or name in dormant_bases:
                continue
            basetype_classes.setdefault(name, set()).add("Augment")
            basetype_drop.setdefault(name, 1)
            if is_live:
                live_bases.add(name)

        for name, cat, is_active in con.execute(
            "SELECT name, category, is_active_drop_in_patch FROM crafting_levers"
        ):
            if not name:
                continue
            if not is_active:
                dormant_bases.add(name)
                basetype_classes.pop(name, None)
                live_bases.discard(name)
                continue
            fc = "Omen" if cat == "omen" else "Stackable Currency"
            basetype_classes.setdefault(name, set()).add(fc)
            basetype_drop.setdefault(name, 1)
            live_bases.add(name)

        for uname, btype in con.execute(
            "SELECT name, base_type FROM uniques WHERE is_live_poe2 = 1"
        ):
            if uname and btype:
                lst = uniques_by_base.setdefault(btype, [])
                if uname not in lst:
                    lst.append(uname)
    finally:
        con.close()

    return FilterGamedataIndex(
        patch=POE2_PATCH,
        sqlite_to_filter_class=sqlite_to_filter,
        valid_filter_classes=frozenset(valid_classes),
        internal_only_class_ids=internal_only,
        basetype_to_classes={k: frozenset(v) for k, v in basetype_classes.items()},
        basetype_drop_levels=basetype_drop,
        live_poe2_basetypes=frozenset(live_bases),
        dormant_or_removed_basetypes=frozenset(dormant_bases),
        uniques_by_basetype={k: tuple(v) for k, v in uniques_by_base.items()},
    )


def to_filter_class(sqlite_item_class: str, index: FilterGamedataIndex | None = None) -> str | None:
    idx = index or get_filter_gamedata_index()
    if sqlite_item_class in idx.valid_filter_classes:
        return sqlite_item_class
    mapped = idx.sqlite_to_filter_class.get(sqlite_item_class)
    return mapped if mapped else None


_TOKEN_RE = re.compile(r'"([^"]*)"|(==|!=|<=|>=|<|>|=|!)|([^\s"#]+)')
_META_TYPE_RE = re.compile(r"\$type->([A-Za-z0-9_.-]+)")
_META_TIER_RE = re.compile(r"\$tier->([A-Za-z0-9_.-]+)")


def split_code_and_comment(line: str) -> tuple[str, str | None]:
    in_quotes = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "#" and not in_quotes:
            return line[:idx].rstrip(), line[idx + 1 :].strip()
    return line.rstrip(), None


def tokenize_statement(code: str) -> list[tuple[Literal["quoted", "op", "word"], str, int]]:
    out: list[tuple[Literal["quoted", "op", "word"], str, int]] = []
    for m in _TOKEN_RE.finditer(code):
        if m.group(1) is not None:
            out.append(("quoted", m.group(1), m.start()))
        elif m.group(2) is not None:
            out.append(("op", m.group(2), m.start()))
        elif m.group(3) is not None:
            out.append(("word", m.group(3), m.start()))
    return out


@dataclass(slots=True)
class ParsedStatement:
    line: int
    raw_code: str
    keyword: str
    operator: str | None
    operator_end_col: int | None
    first_value_col: int | None
    values: list[str]
    quoted_flags: list[bool]
    comment: str | None = None

    def render(self) -> str:
        if self.keyword == "Continue":
            base = "\tContinue"
        elif self.keyword == "HasExplicitMod":
            op_part = ""
            vals = list(self.values)
            qflags = list(self.quoted_flags)
            if self.operator and vals and vals[0].isdigit():
                op_part = f" {self.operator}{vals.pop(0)}"
                qflags.pop(0)
            formatted = [
                f'"{v}"' if q or " " in v else v for v, q in zip(vals, qflags, strict=False)
            ]
            rhs = (" " + " ".join(formatted)) if formatted else ""
            base = f"\tHasExplicitMod{op_part}{rhs}"
        else:
            op_str = f" {self.operator}" if self.operator else ""
            formatted = [
                f'"{v}"' if q or " " in v else v
                for v, q in zip(self.values, self.quoted_flags, strict=False)
            ]
            rhs = (" " + " ".join(formatted)) if formatted else ""
            base = f"\t{self.keyword}{op_str}{rhs}"
        return f"{base} # {self.comment}" if self.comment else base


@dataclass(slots=True)
class ParsedFilterBlock:
    block_index: int
    line: int
    visibility: str
    header_comment: str | None = None
    meta_type: str | None = None
    meta_tier: str | None = None
    leading_comments: list[str] = field(default_factory=list)
    statements: list[ParsedStatement] = field(default_factory=list)

    def render(self) -> str:
        out: list[str] = []
        out.extend(self.leading_comments)
        head = (
            f"{self.visibility} # {self.header_comment}" if self.header_comment else self.visibility
        )
        out.append(head)
        for st in self.statements:
            out.append(st.render())
        return "\n".join(out)


@dataclass(slots=True)
class ParsedImport:
    line: int
    path: str
    optional: bool = False
    comment: str | None = None


@dataclass(slots=True)
class ParsedFilter:
    preamble_comments: list[str] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    blocks: list[ParsedFilterBlock] = field(default_factory=list)
    trailing_comments: list[str] = field(default_factory=list)
    syntax_errors: list[tuple[int, str]] = field(default_factory=list)

    def render(self, *, preserve_import_order: bool = False) -> str:
        parts: list[str] = []
        if self.preamble_comments:
            parts.append("\n".join(self.preamble_comments))
        sections: list[tuple[int, str]] = []
        for imp in self.imports:
            opt = " Optional" if imp.optional else ""
            cmt = f" # {imp.comment}" if imp.comment else ""
            sections.append((imp.line, f'Import "{imp.path}"{opt}{cmt}'))
        sections.extend((blk.line, blk.render()) for blk in self.blocks)
        if preserve_import_order:
            sections.sort(key=lambda section: section[0])
        parts.extend(section for _, section in sections)
        if self.trailing_comments:
            parts.append("\n".join(self.trailing_comments))
        return "\n\n".join(p for p in parts if p is not None) + "\n"


def parse_filter_text(text: str) -> ParsedFilter:
    parsed = ParsedFilter()
    pending_comments: list[str] = []
    current_block: ParsedFilterBlock | None = None
    seen_first_block = False

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            pending_comments.append(raw_line.rstrip())
            continue

        code, inline_cmt = split_code_and_comment(raw_line)
        if code.count('"') % 2 != 0:
            parsed.syntax_errors.append((line_no, "Unterminated double-quoted string literal."))
            continue

        tokens = tokenize_statement(code)
        if not tokens:
            continue

        first_kind, first_val, first_col = tokens[0]
        is_top_word = first_kind == "word" and (
            first_val in VALID_BLOCK_TYPES or first_val == "Import"
        )

        if is_top_word and first_val == "Import":
            if not seen_first_block and pending_comments:
                parsed.preamble_comments.extend(pending_comments)
                pending_comments = []
            if len(tokens) < 2:
                parsed.syntax_errors.append(
                    (line_no, 'Import requires a quoted file path, e.g. Import "extra.filter".')
                )
                continue
            imp_path = tokens[1][1]
            is_opt = any(t[1] == "Optional" for t in tokens[2:])
            parsed.imports.append(
                ParsedImport(line=line_no, path=imp_path, optional=is_opt, comment=inline_cmt)
            )
            continue

        if is_top_word and first_val in VALID_BLOCK_TYPES:
            if not seen_first_block:
                # Keep banner comments before the first section/waypoint header as preamble
                split_idx = 0
                for idx_c, c_line in enumerate(pending_comments):
                    if "[[" in c_line or "OVERRIDE" in c_line:
                        split_idx = idx_c
                        break
                else:
                    split_idx = max(0, len(pending_comments) - 4)
                parsed.preamble_comments.extend(pending_comments[:split_idx])
                block_comments = pending_comments[split_idx:]
                seen_first_block = True
            else:
                block_comments = pending_comments
            pending_comments = []

            if len(tokens) > 1:
                parsed.syntax_errors.append(
                    (line_no, f"Unexpected tokens after block keyword '{first_val}'.")
                )

            m_type = _META_TYPE_RE.search(inline_cmt or "")
            m_tier = _META_TIER_RE.search(inline_cmt or "")
            current_block = ParsedFilterBlock(
                block_index=len(parsed.blocks),
                line=line_no,
                visibility=first_val,
                header_comment=inline_cmt,
                meta_type=m_type.group(1) if m_type else None,
                meta_tier=m_tier.group(1) if m_tier else None,
                leading_comments=block_comments,
            )
            parsed.blocks.append(current_block)
            continue

        if current_block is None:
            parsed.syntax_errors.append(
                (line_no, f"Statement '{first_val}' appears outside any Show/Hide block.")
            )
            continue

        if pending_comments:
            # Comments inside a block are harmless; preserve on next block only if between blocks
            pending_comments = []

        kw = first_val
        op: str | None = None
        op_end_col: int | None = None
        first_val_col: int | None = None
        vals: list[str] = []
        qflags: list[bool] = []

        idx_t = 1
        if idx_t < len(tokens) and tokens[idx_t][0] == "op":
            op = tokens[idx_t][1]
            op_end_col = tokens[idx_t][2] + len(op)
            idx_t += 1

        if idx_t < len(tokens):
            first_val_col = tokens[idx_t][2]

        for t_kind, t_val, _ in tokens[idx_t:]:
            vals.append(t_val)
            qflags.append(t_kind == "quoted")

        current_block.statements.append(
            ParsedStatement(
                line=line_no,
                raw_code=code.strip(),
                keyword=kw,
                operator=op,
                operator_end_col=op_end_col,
                first_value_col=first_val_col,
                values=vals,
                quoted_flags=qflags,
                comment=inline_cmt,
            )
        )

    if pending_comments:
        if not seen_first_block:
            parsed.preamble_comments.extend(pending_comments)
        else:
            parsed.trailing_comments.extend(pending_comments)

    return parsed
