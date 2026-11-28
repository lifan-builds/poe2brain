# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from poe2brain.config import BUNDLED_DATA_DIR, DEFAULT_LEAGUE, POE2_PATCH
from poe2brain.filters.generator import FilterGenerator
from poe2brain.filters.grammar import (
    NUMERIC_OPERATORS,
    POE1_BLOCKED_CONDITIONS,
    POE2_VALID_ACTIONS,
    POE2_VALID_CONDITIONS,
    RARITY_ORDINAL,
    VALID_COLOR_SET,
    VALID_NAMED_SOUND_SET,
    VALID_SHAPE_SET,
    FilterGamedataIndex,
    ParsedFilter,
    ParsedFilterBlock,
    ParsedStatement,
    get_filter_gamedata_index,
    parse_filter_text,
    split_code_and_comment,
)
from poe2brain.filters.themes import BUILTIN_THEME_PRESETS, compose_theme, get_visual_theme
from poe2brain.filters.validator import (
    FilterValidator,
    _intervals_disjoint,
    _is_windows_or_unix_abs_path,
    _numeric_interval,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.models.filters import (
    BuildFilterContext,
    FilterCompileResult,
    FilterHealReport,
    SoundPackManifest,
    StrictnessTier,
    TierVisualStyle,
    VisualThemeSpec,
)

NEVERSINK_RAW_BASE_URL: str = (
    "https://raw.githubusercontent.com/NeverSinkDev/NeverSink-Filter-for-PoE2/main"
)

STRICTNESS_TO_NEVERSINK_FILE: dict[StrictnessTier, str] = {
    StrictnessTier.SOFT: "NeverSink's filter 2 - 0-SOFT.filter",
    StrictnessTier.REGULAR: "NeverSink's filter 2 - 1-REGULAR.filter",
    StrictnessTier.SEMI_STRICT: "NeverSink's filter 2 - 2-SEMI-STRICT.filter",
    StrictnessTier.STRICT: "NeverSink's filter 2 - 3-STRICT.filter",
    StrictnessTier.VERY_STRICT: "NeverSink's filter 2 - 4-VERY-STRICT.filter",
    StrictnessTier.UBER_STRICT: "NeverSink's filter 2 - 5-UBER-STRICT.filter",
}


def map_neversink_meta_to_semantic_tier(meta_type: str | None, meta_tier: str | None) -> str:
    mt = (meta_type or "").lower()
    tr = (meta_tier or "").lower()

    if "gold" in mt:
        if any(k in tr for k in ("stack3", "stack2", "xl")):
            return "gold_high"
        return "gold_low"
    if "waystone" in mt:
        if any(k in tr for k in ("t16", "t15", "corrupted8high")):
            return "waystone_high"
        return "waystone_prog"
    if "chancing" in mt or tr.startswith("chance"):
        return "chancing"

    if tr in (
        "s",
        "t1",
        "t1_top",
        "sekhemaring",
        "twicecorrupteduniques",
        "superexoticbases",
        "kalandrabases",
        "vaultkeysrare",
        "superkeys",
    ):
        return "s_plus" if tr in ("s", "sekhemaring", "superexoticbases", "vaultkeysrare") else "s"
    if tr in (
        "a",
        "t2",
        "t1_other",
        "t2_top",
        "spirit20",
        "skill20",
        "multispecialhigh",
        "vaalmodunique",
        "vaultkeys",
        "trialkeysanctumtop",
        "trialkeyultimatumtop",
    ):
        return "s" if tr in ("spirit20", "skill20", "multispecialhigh") else "a"
    if tr in (
        "b",
        "t3",
        "spirit19",
        "skill19",
        "anytimelost",
        "toplevelqualityflasks",
        "topqualitycharms",
        "t1ideallevel",
        "jt1ideallevel",
    ):
        return "b"
    if tr in ("c", "t4", "supplymagic", "jewellers"):
        return "c"
    if tr in ("d", "e", "t5", "supplieslow", "supplylow", "restex", "any"):
        return "d"
    return "c"


def _set_or_append_action(
    stmts: list[ParsedStatement],
    line: int,
    keyword: str,
    values: list[str],
    quoted_flags: list[bool] | None = None,
) -> None:
    qflags = quoted_flags or [False] * len(values)
    for st in stmts:
        if st.keyword == keyword:
            st.values = values
            st.quoted_flags = qflags
            return
    # Insert before `Continue` if `Continue` exists at the end of the block
    insert_at = len(stmts)
    for idx, st in enumerate(stmts):
        if st.keyword == "Continue":
            insert_at = idx
            break
    stmts.insert(
        insert_at,
        ParsedStatement(
            line=line,
            raw_code=f"{keyword} {' '.join(values)}",
            keyword=keyword,
            operator=None,
            operator_end_col=None,
            first_value_col=None,
            values=values,
            quoted_flags=qflags,
        ),
    )


def _apply_style_to_neversink_block(
    blk: ParsedFilterBlock,
    style: TierVisualStyle,
    *,
    sound_pack: SoundPackManifest | None,
    require_custom_sounds: bool,
    change_visuals: bool,
    change_sounds: bool,
) -> None:
    if blk.visibility != "Show":
        for st in blk.statements:
            if st.keyword == "SetFontSize" and st.values and st.values[0].isdigit():
                st.values = [str(max(18, min(45, int(st.values[0]))))]
        return

    has_continue = any(st.keyword == "Continue" for st in blk.statements)
    existing_actions = {st.keyword for st in blk.statements}

    # Preserve partial `Continue` decorator layers without clobbering background/sound
    if has_continue and "SetBackgroundColor" not in existing_actions:
        for st in blk.statements:
            if change_visuals and st.keyword == "SetFontSize":
                st.values = [str(style.font_size)]
        return

    if change_sounds:
        sound_actions = {
            "PlayAlertSound",
            "PlayAlertSoundPositional",
            "CustomAlertSound",
            "CustomAlertSoundOptional",
        }
        blk.statements = [st for st in blk.statements if st.keyword not in sound_actions]
        clip = sound_pack.clips.get(style.tier.value) if sound_pack else None
        if style.builtin_sound_id is not None and not (clip and require_custom_sounds):
            _set_or_append_action(
                blk.statements,
                blk.line,
                "PlayAlertSound",
                [str(style.builtin_sound_id), str(style.sound_volume)],
            )
        if clip is not None:
            kw = "CustomAlertSound" if require_custom_sounds else "CustomAlertSoundOptional"
            _set_or_append_action(
                blk.statements,
                blk.line,
                kw,
                [clip.relative_filter_path, str(style.sound_volume)],
                quoted_flags=[True, False],
            )

    if not change_visuals:
        return
    _set_or_append_action(blk.statements, blk.line, "SetFontSize", [str(style.font_size)])
    for key, color in (
        ("SetTextColor", style.text_color),
        ("SetBorderColor", style.border_color),
        ("SetBackgroundColor", style.background_color),
    ):
        _set_or_append_action(blk.statements, blk.line, key, color.to_filter_tokens().split())
    blk.statements = [
        st for st in blk.statements if st.keyword not in ("PlayEffect", "MinimapIcon")
    ]
    if style.beam is not None:
        beam_vals = [style.beam.color, "Temp"] if style.beam.temp else [style.beam.color]
        _set_or_append_action(blk.statements, blk.line, "PlayEffect", beam_vals)
    if style.minimap_icon is not None:
        if style.minimap_icon.size == -1:
            _set_or_append_action(blk.statements, blk.line, "MinimapIcon", ["-1"])
        else:
            _set_or_append_action(
                blk.statements,
                blk.line,
                "MinimapIcon",
                [str(style.minimap_icon.size), style.minimap_icon.color, style.minimap_icon.shape],
            )


_STALE_01_PREFIX_RE = re.compile(r"^(?:expert|advanced)\s*(.+)$", re.IGNORECASE)

_POE1_CLASS_ALIASES: dict[str, str] = {
    "map": "Waystones",
    "maps": "Waystones",
    "utility flask": "Charms",
    "utility flasks": "Charms",
    "utilityflask": "Charms",
    "warstaff": "Quarterstaves",
    "warstaves": "Quarterstaves",
    "active skill gem": "Skill Gems",
    "active skill gems": "Skill Gems",
    "support skill gem": "Support Gems",
    "support skill gems": "Support Gems",
    "thrusting one hand swords": "One Hand Swords",
    "thrusting one hand sword": "One Hand Swords",
    "rune daggers": "Daggers",
    "rune dagger": "Daggers",
    "scepter": "Sceptres",
    "scepters": "Sceptres",
    "body armor": "Body Armours",
    "body armors": "Body Armours",
}

_RENAMED_01_BASETYPES: dict[str, str] = {
    "dualstring bow": "Gemini Bow",
}


def _repair_raw_syntax_lines(raw_text: str, fixes: list[str]) -> str:
    repaired_lines: list[str] = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            repaired_lines.append(raw_line)
            continue
        code, cmt = split_code_and_comment(raw_line)
        if code.count('"') % 2 != 0:
            code = f'{code}"'
            fixes.append(f"L{line_no}: Closed unterminated double-quoted string literal.")
            repaired_lines.append(f"{code} # {cmt}" if cmt else code)
        else:
            repaired_lines.append(raw_line)
    return "\n".join(repaired_lines) + ("\n" if raw_text.endswith("\n") else "")


def _heal_single_class(
    val: str,
    *,
    is_exact: bool,
    index: FilterGamedataIndex,
    internal_ci: dict[str, str],
    valid_fc_ci: dict[str, str],
) -> tuple[str | None, str | None]:
    low = val.strip().lower()
    if not low:
        return None, 'Removed empty Class literal ""'
    if val in index.internal_only_class_ids:
        target = index.internal_only_class_ids[val]
        return target, f'Healed singular/internal Class "{val}" -> "{target}"'
    if low in _POE1_CLASS_ALIASES:
        target = _POE1_CLASS_ALIASES[low]
        return target, f'Migrated PoE1 Class "{val}" -> "{target}"'
    if low in internal_ci:
        target = internal_ci[low]
        return target, f'Healed singular/internal Class "{val}" -> "{target}"'
    if is_exact:
        if val in index.valid_filter_classes:
            return val, None
        if low in valid_fc_ci:
            target = valid_fc_ci[low]
            return target, f'Normalized Class casing "{val}" -> "{target}"'
        return None, f'Pruned unsupported PoE1/unknown Class "{val}"'
    if any(val in fc for fc in index.valid_filter_classes):
        return val, None
    if low in valid_fc_ci:
        target = valid_fc_ci[low]
        return target, f'Normalized Class casing "{val}" -> "{target}"'
    if any(low in fc.lower() for fc in index.valid_filter_classes):
        matched_sub = next(fc for fc in sorted(index.valid_filter_classes) if low in fc.lower())
        return matched_sub, f'Normalized Class "{val}" -> "{matched_sub}"'
    return None, f'Pruned unsupported PoE1/unknown Class "{val}"'


def _heal_single_basetype(
    val: str,
    *,
    is_exact: bool,
    index: FilterGamedataIndex,
    live_ci: dict[str, str],
    dormant_ci: frozenset[str],
) -> tuple[str | None, str | None]:
    clean = val.strip()
    low = clean.lower()
    if not clean:
        return None, 'Removed empty BaseType literal ""'

    if clean in index.dormant_or_removed_basetypes or low in dormant_ci:
        return None, f'Pruned dormant/removed BaseType "{val}"'

    if is_exact and clean in index.basetype_to_classes:
        return clean, None
    if not is_exact and any(clean in bt for bt in index.basetype_to_classes):
        return clean, None

    if low in _RENAMED_01_BASETYPES:
        renamed = _RENAMED_01_BASETYPES[low]
        if renamed in index.basetype_to_classes:
            return renamed, f'Migrated Patch 0.1 BaseType "{val}" -> "{renamed}"'

    if low in live_ci:
        canon = live_ci[low]
        return canon, f'Normalized BaseType "{val}" -> "{canon}"'

    m_stale = _STALE_01_PREFIX_RE.match(clean)
    if m_stale:
        stripped = m_stale.group(1).strip()
        cand = _RENAMED_01_BASETYPES.get(stripped.lower(), stripped)
        if cand.lower() in dormant_ci:
            return None, f'Pruned dormant/removed BaseType "{val}"'
        if cand.lower() in live_ci:
            canon = live_ci[cand.lower()]
            return canon, f'Stripped stale 0.1 prefix "{val}" -> "{canon}"'
        if not is_exact and any(cand.lower() in bt.lower() for bt in live_ci.values()):
            return cand, f'Stripped stale 0.1 prefix "{val}" -> "{cand}"'

    return None, f'Pruned unknown/PoE1 BaseType "{val}"'


def _heal_parsed_filter_detailed(
    parsed: ParsedFilter,
    index: FilterGamedataIndex,
    *,
    filter_dir: Path | None = None,
    initial_fixes: list[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Repair PoE1 / Patch 0.1 filter migrations in-place; returns `(fixes, bases, pruned)`."""
    fixes: list[str] = list(initial_fixes or ())
    healed_items: list[str] = []
    pruned_blocks = 0

    if parsed.syntax_errors:
        for err_ln, err_msg in parsed.syntax_errors:
            fixes.append(f"L{err_ln}: Dropped malformed statement ({err_msg})")
        parsed.syntax_errors.clear()

    for imp in parsed.imports:
        if not imp.optional and (filter_dir is None or not (filter_dir / imp.path).is_file()):
            imp.optional = True
            fixes.append(f'L{imp.line}: Marked missing Import "{imp.path}" Optional.')

    live_ci = {
        bt.lower(): bt
        for bt in index.basetype_to_classes
        if bt not in index.dormant_or_removed_basetypes
    }
    dormant_ci = frozenset(bt.lower() for bt in index.dormant_or_removed_basetypes)
    internal_ci = {k.lower(): v for k, v in index.internal_only_class_ids.items()}
    valid_fc_ci = {fc.lower(): fc for fc in index.valid_filter_classes}

    kept_blocks: list[ParsedFilterBlock] = []

    for blk in parsed.blocks:
        drop_entire_block = False
        had_conditions_before = False
        removed_poe1_condition = False

        # Separate `Continue` statements so actions placed after `Continue` are kept & hoisted
        first_continue_idx: int | None = None
        continue_stmt: ParsedStatement | None = None
        continue_count = 0
        non_continue_stmts: list[ParsedStatement] = []
        had_stmt_after_continue = False

        for idx_s, st in enumerate(blk.statements):
            if st.keyword == "Continue":
                continue_count += 1
                if first_continue_idx is None:
                    first_continue_idx = idx_s
                    continue_stmt = st
            else:
                if first_continue_idx is not None:
                    had_stmt_after_continue = True
                non_continue_stmts.append(st)
                if st.keyword in POE2_VALID_CONDITIONS or st.keyword in POE1_BLOCKED_CONDITIONS:
                    had_conditions_before = True

        new_stmts: list[ParsedStatement] = []
        numeric_bounds: dict[str, tuple[float, bool, float, bool]] = {}
        bool_states: dict[str, bool] = {}

        for st in non_continue_stmts:
            kw = st.keyword

            # 1. Legacy PoE1 `MapTier` -> `WaystoneTier`
            if kw == "MapTier":
                old_raw = st.values[0] if st.values else "1"
                st.keyword = "WaystoneTier"
                kw = "WaystoneTier"
                if st.values and st.values[0].lstrip("-").isdigit():
                    clamped_tier = max(1, min(16, int(st.values[0])))
                    st.values[0] = str(clamped_tier)
                fixes.append(
                    f"L{st.line}: Migrated PoE1 `MapTier {st.operator or ''}{old_raw}` -> "
                    f"`WaystoneTier {st.operator or ''}{st.values[0] if st.values else '1'}`."
                )

            # 2. Other unsupported PoE1 conditions (`SocketGroup`, `LinkedSockets`, `ShaperItem`)
            if kw in POE1_BLOCKED_CONDITIONS:
                removed_poe1_condition = True
                fixes.append(f"L{st.line}: Removed unsupported PoE1 condition `{kw}`.")
                continue

            # 3. `Class` migration & singular `.datc64` -> plural `.filter` healing
            if kw == "Class":
                if st.operator not in (None, "==", "="):
                    st.operator = "=="
                    fixes.append(f"L{st.line}: Normalized invalid `Class` operator to `==`.")
                is_exact = st.operator in ("==", "=")
                valid_c: list[str] = []
                valid_cq: list[bool] = []
                for val, qf in zip(st.values, st.quoted_flags, strict=False):
                    healed_c, fix_msg = _heal_single_class(
                        val,
                        is_exact=is_exact,
                        index=index,
                        internal_ci=internal_ci,
                        valid_fc_ci=valid_fc_ci,
                    )
                    if fix_msg:
                        fixes.append(f"L{st.line}: {fix_msg}.")
                        if healed_c != val and val not in healed_items:
                            healed_items.append(val)
                    if healed_c is not None and healed_c not in valid_c:
                        valid_c.append(healed_c)
                        valid_cq.append(True if " " in healed_c else qf)
                if not valid_c:
                    drop_entire_block = True
                    fixes.append(
                        f"L{blk.line}: Pruned block #{blk.block_index} (no valid PoE2 Class left)."
                    )
                    break
                st.values = valid_c
                st.quoted_flags = valid_cq
                new_stmts.append(st)
                continue

            # 4. `BaseType` 0.1 prefix stripping (`Expert ` / `Advanced `) + dead PoE1/0.4 pruning
            if kw == "BaseType":
                if st.operator not in (None, "==", "="):
                    st.operator = "=="
                    fixes.append(f"L{st.line}: Normalized invalid `BaseType` operator to `==`.")
                is_exact = st.operator in ("==", "=")
                valid_vals: list[str] = []
                valid_qflags: list[bool] = []
                for val, qf in zip(st.values, st.quoted_flags, strict=False):
                    healed_bt, fix_msg = _heal_single_basetype(
                        val,
                        is_exact=is_exact,
                        index=index,
                        live_ci=live_ci,
                        dormant_ci=dormant_ci,
                    )
                    if fix_msg:
                        fixes.append(f"L{st.line}: {fix_msg}.")
                        if healed_bt != val and val not in healed_items:
                            healed_items.append(val)
                    if healed_bt is not None and healed_bt not in valid_vals:
                        valid_vals.append(healed_bt)
                        valid_qflags.append(True if " " in healed_bt else qf)
                if not valid_vals:
                    drop_entire_block = True
                    fixes.append(
                        f"L{blk.line}: Pruned dead block #{blk.block_index} "
                        "(all BaseTypes removed or obsolete in 0.5.5)."
                    )
                    break
                st.values = valid_vals
                st.quoted_flags = valid_qflags
                new_stmts.append(st)
                continue

            # 5. `HasExplicitMod >= 2` whitespace glue
            if kw == "HasExplicitMod":
                if not st.values:
                    fixes.append(f"L{st.line}: Removed empty `HasExplicitMod` condition.")
                    continue
                if st.operator and st.values[0].isdigit():
                    if (
                        st.operator_end_col is not None
                        and st.first_value_col is not None
                        and st.first_value_col > st.operator_end_col
                    ):
                        fixes.append(
                            f"L{st.line}: Glued `HasExplicitMod {st.operator}{st.values[0]}` "
                            "(removed illegal space after operator)."
                        )
                    st.first_value_col = st.operator_end_col
                new_stmts.append(st)
                continue

            # 6. Numeric & boolean condition clamping + contradiction detection
            if kw in POE2_VALID_CONDITIONS:
                spec = POE2_VALID_CONDITIONS[kw]
                if spec.kind == "number":
                    if st.operator is not None and st.operator not in NUMERIC_OPERATORS:
                        st.operator = ">="
                        fixes.append(f"L{st.line}: Normalized `{kw}` operator to `>=`.")
                    if not st.values or not st.values[0].lstrip("-").isdigit():
                        fixes.append(f"L{st.line}: Removed malformed numeric condition `{kw}`.")
                        continue
                    raw_n = int(st.values[0])
                    clamped_n = raw_n
                    if spec.min_val is not None and clamped_n < spec.min_val:
                        clamped_n = spec.min_val
                    if spec.max_val is not None and clamped_n > spec.max_val:
                        clamped_n = spec.max_val
                    if clamped_n != raw_n:
                        st.values[0] = str(clamped_n)
                        fixes.append(f"L{st.line}: Clamped `{kw} {raw_n}` -> `{clamped_n}`.")
                    iv = _numeric_interval(st.operator, float(clamped_n))
                    if kw in numeric_bounds and _intervals_disjoint(numeric_bounds[kw], iv):
                        drop_entire_block = True
                        fixes.append(
                            f"L{blk.line}: Pruned contradictory `{kw}` range "
                            f"in block #{blk.block_index}."
                        )
                        break
                    numeric_bounds[kw] = iv
                elif spec.kind == "boolean":
                    if st.operator not in (None, "=", "=="):
                        st.operator = None
                    b_tok = (st.values[0] if st.values else "True").capitalize()
                    if b_tok not in ("True", "False"):
                        b_tok = "True"
                    st.values = [b_tok]
                    st.quoted_flags = [False]
                    bval = b_tok == "True"
                    if kw in bool_states and bool_states[kw] != bval:
                        drop_entire_block = True
                        fixes.append(
                            f"L{blk.line}: Pruned contradictory `{kw}` boolean "
                            f"in block #{blk.block_index}."
                        )
                        break
                    bool_states[kw] = bval
                elif spec.kind == "rarity":
                    valid_r = [
                        rv.capitalize() for rv in st.values if rv.capitalize() in RARITY_ORDINAL
                    ]
                    if not valid_r:
                        fixes.append(f"L{st.line}: Removed invalid `Rarity` condition.")
                        continue
                    st.values = valid_r
                    st.quoted_flags = [False] * len(valid_r)
                new_stmts.append(st)
                continue

            # 7. Action clamping & path normalization
            if kw in POE2_VALID_ACTIONS:
                if kw == "SetFontSize":
                    if st.values and st.values[0].lstrip("-").isdigit():
                        raw_sz = int(st.values[0])
                        clamped_sz = max(18, min(45, raw_sz))
                        if clamped_sz != raw_sz:
                            st.values = [str(clamped_sz)]
                            fixes.append(
                                f"L{st.line}: Clamped `SetFontSize {raw_sz}` -> "
                                f"`SetFontSize {clamped_sz}`."
                            )
                    else:
                        st.values = ["32"]
                        fixes.append(f"L{st.line}: Reset invalid `SetFontSize` to `32`.")
                    new_stmts.append(st)
                    continue

                if kw in ("SetTextColor", "SetBorderColor", "SetBackgroundColor"):
                    if len(st.values) in (3, 4) and all(v.lstrip("-").isdigit() for v in st.values):
                        clamped_rgba = [str(max(0, min(255, int(v)))) for v in st.values]
                        if clamped_rgba != st.values:
                            fixes.append(f"L{st.line}: Clamped `{kw}` RGBA channels to `0..255`.")
                            st.values = clamped_rgba
                        new_stmts.append(st)
                    else:
                        fixes.append(f"L{st.line}: Removed malformed `{kw}` action.")
                    continue

                if kw in ("PlayAlertSound", "PlayAlertSoundPositional"):
                    if not st.values:
                        fixes.append(f"L{st.line}: Removed empty `{kw}` action.")
                        continue
                    sid = st.values[0]
                    if sid != "None":
                        if sid.lstrip("-").isdigit():
                            clamped_sid = max(1, min(16, int(sid)))
                            if str(clamped_sid) != sid:
                                st.values[0] = str(clamped_sid)
                                fixes.append(
                                    f"L{st.line}: Clamped `{kw}` ID `{sid}` -> `{clamped_sid}`."
                                )
                        elif sid not in VALID_NAMED_SOUND_SET:
                            st.values[0] = "1"
                            fixes.append(f"L{st.line}: Replaced unknown sound `{sid}` with `1`.")
                        if len(st.values) >= 2 and st.values[1].lstrip("-").isdigit():
                            vol = max(0, min(300, int(st.values[1])))
                            if str(vol) != st.values[1]:
                                st.values[1] = str(vol)
                                fixes.append(f"L{st.line}: Clamped `{kw}` volume to `{vol}`.")
                    new_stmts.append(st)
                    continue

                if kw in ("CustomAlertSound", "CustomAlertSoundOptional"):
                    if not st.values:
                        fixes.append(f"L{st.line}: Removed empty `{kw}` action.")
                        continue
                    if st.values[0] != "None":
                        raw_parts = [p.strip() for p in st.values[0].split(";") if p.strip()]
                        rel_parts: list[str] = []
                        rewrote_abs = False
                        for sp in raw_parts:
                            if _is_windows_or_unix_abs_path(sp):
                                rel_parts.append(Path(sp.replace("\\", "/")).name)
                                rewrote_abs = True
                            else:
                                rel_parts.append(sp)
                        new_snd = ";".join(rel_parts)
                        if rewrote_abs:
                            st.values[0] = new_snd
                            st.quoted_flags[0] = True
                            fixes.append(
                                f"L{st.line}: Converted absolute `{kw}` path "
                                f'to relative "{new_snd}".'
                            )
                        if kw == "CustomAlertSound" and (
                            rewrote_abs
                            or (
                                filter_dir is not None
                                and any(not (filter_dir / rp).is_file() for rp in rel_parts)
                            )
                        ):
                            st.keyword = "CustomAlertSoundOptional"
                            fixes.append(
                                f"L{st.line}: Upgraded `CustomAlertSound` -> "
                                "`CustomAlertSoundOptional` for missing-audio safety."
                            )
                        if len(st.values) >= 2 and st.values[1].lstrip("-").isdigit():
                            vol = max(0, min(300, int(st.values[1])))
                            st.values[1] = str(vol)
                    new_stmts.append(st)
                    continue

                if kw == "PlayEffect":
                    if st.values and (st.values[0] == "None" or st.values[0] in VALID_COLOR_SET):
                        if len(st.values) >= 2 and st.values[1] != "Temp":
                            st.values = [st.values[0]]
                        new_stmts.append(st)
                    else:
                        fixes.append(f"L{st.line}: Removed invalid `PlayEffect` action.")
                    continue

                if kw == "MinimapIcon":
                    if st.values and st.values[0].lstrip("-").isdigit():
                        sz = int(st.values[0])
                        if sz == -1:
                            st.values = ["-1"]
                            new_stmts.append(st)
                        elif (
                            sz in (0, 1, 2)
                            and len(st.values) >= 3
                            and st.values[1] in VALID_COLOR_SET
                            and st.values[2] in VALID_SHAPE_SET
                        ):
                            new_stmts.append(st)
                        else:
                            fixes.append(f"L{st.line}: Removed invalid `MinimapIcon` action.")
                    else:
                        fixes.append(f"L{st.line}: Removed invalid `MinimapIcon` action.")
                    continue

                new_stmts.append(st)
                continue

            # 8. Unknown keyword -> drop with fix note
            fixes.append(f"L{st.line}: Removed unknown filter keyword `{kw}`.")

        if drop_entire_block:
            pruned_blocks += 1
            continue

        remaining_conds = {st.keyword for st in new_stmts if st.keyword in POE2_VALID_CONDITIONS}
        if had_conditions_before and not remaining_conds:
            pruned_blocks += 1
            fixes.append(
                f"L{blk.line}: Pruned block #{blk.block_index} "
                "(only unsupported PoE1 conditions were present)."
            )
            continue

        if (
            removed_poe1_condition
            and remaining_conds
            and remaining_conds
            <= {
                "Class",
                "Rarity",
                "Width",
                "Height",
            }
        ):
            pruned_blocks += 1
            fixes.append(
                f"L{blk.line}: Pruned pure PoE1 link/socket-group block #{blk.block_index} "
                "to prevent broad catch-all shadowing."
            )
            continue

        # Reconcile Class + BaseType mismatch if both exist in the same healed block
        cls_stmts = [s for s in new_stmts if s.keyword == "Class"]
        bt_stmts = [s for s in new_stmts if s.keyword == "BaseType"]
        if cls_stmts and bt_stmts:
            cs = cls_stmts[0]
            bs = bt_stmts[0]
            c_exact = cs.operator in ("==", "=")
            b_exact = bs.operator in ("==", "=")
            allowed_fc = {
                fc
                for cv in cs.values
                for fc in index.valid_filter_classes
                if (fc == cv if c_exact else cv in fc)
            }
            needed_fc: list[str] = []
            for bv in bs.values:
                cand_fc = (
                    set(index.basetype_to_classes.get(bv, ()))
                    if b_exact
                    else {
                        fc
                        for bt_nm, fc_s in index.basetype_to_classes.items()
                        if bv in bt_nm
                        for fc in fc_s
                    }
                )
                if cand_fc and not (cand_fc & allowed_fc):
                    for extra_c in sorted(cand_fc):
                        if extra_c not in cs.values and extra_c not in needed_fc:
                            needed_fc.append(extra_c)
            if needed_fc:
                if not c_exact:
                    cs.operator = "=="
                    cs.quoted_flags = [True] * len(cs.values)
                for extra_c in needed_fc:
                    cs.values.append(extra_c)
                    cs.quoted_flags.append(True)
                fixes.append(
                    f"L{cs.line}: Aligned `Class` with healed `BaseType` classes {needed_fc}."
                )

        # Hoist `Continue` to the very end of the block
        if continue_stmt is not None:
            if had_stmt_after_continue or continue_count > 1:
                fixes.append(
                    f"L{continue_stmt.line}: Hoisted `Continue` to the end "
                    f"of block #{blk.block_index}."
                )
            new_stmts.append(continue_stmt)

        blk.statements = new_stmts
        blk.block_index = len(kept_blocks)
        kept_blocks.append(blk)

    parsed.blocks = kept_blocks
    return fixes, healed_items, pruned_blocks


def heal_filter_text(
    raw_text: str,
    *,
    filter_dir: Path | None = None,
    index: FilterGamedataIndex | None = None,
) -> FilterHealReport:
    """Doctor and auto-heal broken PoE1 / PoE2 0.1 `.filter` text into valid Patch 0.5.5 syntax."""
    idx = index or get_filter_gamedata_index()
    validator = FilterValidator(idx)
    pre_report = validator.validate_text(raw_text, filter_dir=filter_dir)

    initial_fixes: list[str] = []
    syntax_repaired_text = _repair_raw_syntax_lines(raw_text, initial_fixes)
    parsed = parse_filter_text(syntax_repaired_text)

    fixes_applied, healed_bases, pruned_blocks = _heal_parsed_filter_detailed(
        parsed,
        idx,
        filter_dir=filter_dir,
        initial_fixes=initial_fixes,
    )
    healed_text = parsed.render()
    post_report = validator.validate_text(healed_text, filter_dir=filter_dir)

    return FilterHealReport(
        valid=post_report.valid,
        patch=POE2_PATCH,
        healed_text=healed_text,
        fixes_applied=fixes_applied,
        healed_basetypes=healed_bases,
        pruned_blocks=pruned_blocks,
        original_error_count=pre_report.error_count,
        validation=post_report,
    )


class NeverSinkAdapter:
    """Heals, enriches, and restyles MIT `NeverSink-Filter-for-PoE2` filters for Patch 0.5.5."""

    def __init__(
        self,
        *,
        generator: FilterGenerator | None = None,
        http_client: Poe2HttpClient | None = None,
        index: FilterGamedataIndex | None = None,
    ) -> None:
        self.index = index or get_filter_gamedata_index()
        self.generator = generator or FilterGenerator(index=self.index)
        self.validator = FilterValidator(self.index)
        self._http = http_client

    def fetch_neversink_text(self, strictness: StrictnessTier = StrictnessTier.SEMI_STRICT) -> str:
        fname = STRICTNESS_TO_NEVERSINK_FILE[strictness]
        url = f"{NEVERSINK_RAW_BASE_URL}/{quote(fname)}"
        own_client = self._http is None
        client = self._http or Poe2HttpClient()
        try:
            return client.get_text(url, ttl_seconds=86400.0)
        finally:
            if own_client:
                client.close()

    def heal_parsed_filter(self, parsed: ParsedFilter) -> list[str]:
        """Prunes removed/dormant BaseTypes & Classes and fixes PoE2 0.5.5 syntax traps in-place."""
        _fixes, healed, _pruned = _heal_parsed_filter_detailed(parsed, self.index)
        return healed

    def restyle_and_heal(
        self,
        source_filter_text: str | None = None,
        *,
        source_filter_path: Path | None = None,
        strictness: StrictnessTier | str = StrictnessTier.SEMI_STRICT,
        theme: VisualThemeSpec | str | None = "default",
        style_prompt: str | None = None,
        theme_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        sound_pack: SoundPackManifest | None = None,
        require_custom_sounds: bool = False,
        inject_055_overrides: bool = True,
        base_only: bool = False,
        league: str = DEFAULT_LEAGUE,
        use_live_economy: bool = False,
        pob_code: str | None = None,
        pob_codes: Sequence[str] | None = None,
        pob_slot: str | None = None,
        pob_slots: Sequence[str] | None = None,
        build_context: BuildFilterContext | None = None,
        build_contexts: Sequence[BuildFilterContext] | None = None,
        filter_name: str = "poe2brain_neversink_healed",
        output_path: Path | None = None,
    ) -> FilterCompileResult:
        strict_tier = StrictnessTier(strictness) if isinstance(strictness, str) else strictness
        if source_filter_text is not None:
            raw_text = source_filter_text
        elif source_filter_path is not None:
            raw_text = source_filter_path.read_text(encoding="utf-8-sig")
        else:
            raw_text = self.fetch_neversink_text(strict_tier)

        if isinstance(theme, VisualThemeSpec):
            resolved_theme: VisualThemeSpec | None = theme
        elif style_prompt or theme_overrides:
            base_p = theme if isinstance(theme, str) and theme in BUILTIN_THEME_PRESETS else None
            resolved_theme = compose_theme(
                style_prompt, base_preset=base_p, overrides=theme_overrides
            )
        elif theme == "default":
            resolved_theme = None if base_only else get_visual_theme("forbidden_abyss")
        elif isinstance(theme, str):
            resolved_theme = get_visual_theme(theme)
        else:
            resolved_theme = None

        resolved_build_ctx = self.generator._resolve_combined_build_context(
            build_context=build_context,
            build_contexts=build_contexts,
            pob_code=pob_code,
            pob_codes=pob_codes,
            pob_slot=pob_slot,
            pob_slots=pob_slots,
        )

        supplied_base = source_filter_text is not None or source_filter_path is not None
        preserve_rules = base_only and supplied_base
        syntax_clean = raw_text if preserve_rules else _repair_raw_syntax_lines(raw_text, [])
        parsed = parse_filter_text(syntax_clean)
        if preserve_rules:
            source_dir = source_filter_path.parent if source_filter_path is not None else None
            source_report = self.validator.validate_parsed(parsed, filter_dir=source_dir)
            if not source_report.valid:
                error = next(d for d in source_report.diagnostics if d.severity == "error")
                raise ValueError(f"Base filter line {error.line}: {error.message}")
        healed_items = [] if preserve_rules else self.heal_parsed_filter(parsed)

        if resolved_theme is not None or (base_only and sound_pack is not None):
            style_theme = resolved_theme or get_visual_theme("forbidden_abyss")
            for blk in parsed.blocks:
                sem_tier = map_neversink_meta_to_semantic_tier(blk.meta_type, blk.meta_tier)
                tier_style = style_theme.tiers.get(sem_tier, style_theme.tiers["c"])
                _apply_style_to_neversink_block(
                    blk,
                    tier_style,
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                    change_visuals=resolved_theme is not None,
                    change_sounds=not base_only or sound_pack is not None,
                )

        injected_count = 0
        if inject_055_overrides and (not base_only or resolved_build_ctx is not None):
            active_theme = resolved_theme or get_visual_theme("forbidden_abyss")
            prices_ex = None
            if not base_only:
                prices_ex, _ = self.generator.collect_market_prices(
                    league=league, use_live_economy=use_live_economy,
                )
            ov_text_blocks = self.generator.build_poe2_override_blocks(
                active_theme,
                strictness=strict_tier,
                build_context=resolved_build_ctx,
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                prices_ex=prices_ex,
                include_economy=not base_only,
            )
            ov_parsed = parse_filter_text("\n\n".join(ov_text_blocks))
            injected_count = len(ov_parsed.blocks)
            parsed.blocks = ov_parsed.blocks + parsed.blocks
            for idx_b, b in enumerate(parsed.blocks):
                b.block_index = idx_b

        attribution = []
        notice = f"# Adapted for PoE2 Patch {POE2_PATCH} by poe2brain (GPL-3.0-only)."
        if notice not in raw_text.splitlines():
            attribution.append(notice)
        if not supplied_base:
            attribution.append(
                "# Base filter structure courtesy of NeverSink "
                "(https://github.com/NeverSinkDev/NeverSink-Filter-for-PoE2, MIT License)."
            )
            license_text = (BUNDLED_DATA_DIR / "filter" / "LICENSE.NeverSink").read_text(
                encoding="utf-8"
            )
            attribution.extend(f"# {line}".rstrip() for line in license_text.splitlines())
        parsed.preamble_comments = attribution + parsed.preamble_comments
        compiled_text = parsed.render(preserve_import_order=base_only)
        if preserve_rules and not injected_count and resolved_theme is None and sound_pack is None:
            compiled_text = raw_text

        out_dir: Path | None = None
        written_path_str: str | None = None
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(compiled_text, encoding="utf-8")
            out_dir = output_path.parent
            if sound_pack is not None and not (out_dir / sound_pack.pack_id).is_dir():
                out_dir = Path(sound_pack.pack_dir).parent
            written_path_str = str(output_path)
        elif sound_pack is not None:
            out_dir = Path(sound_pack.pack_dir).parent

        report = self.validator.validate_text(compiled_text, filter_dir=out_dir)

        return FilterCompileResult(
            filter_name=filter_name,
            patch=POE2_PATCH,
            league=league,
            strictness=strict_tier,
            theme_id=resolved_theme.id
            if resolved_theme
            else ("source_original" if supplied_base and base_only else "neversink_original"),
            sound_pack_id=sound_pack.pack_id if sound_pack else None,
            filter_text=compiled_text,
            filter_path=written_path_str,
            validation=report,
            build_context=resolved_build_ctx,
            healed_basetypes=healed_items,
            injected_override_blocks=injected_count,
        )
