# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import difflib
import math
from pathlib import Path

from poe2brain.config import POE2_PATCH
from poe2brain.filters.grammar import (
    NUMERIC_OPERATORS,
    POE1_BLOCKED_CONDITIONS,
    POE2_VALID_ACTIONS,
    POE2_VALID_CONDITIONS,
    RARITY_ORDINAL,
    VALID_COLOR_SET,
    VALID_COLORS,
    VALID_NAMED_SOUND_SET,
    VALID_NAMED_SOUNDS,
    VALID_SHAPE_SET,
    VALID_SHAPES,
    FilterGamedataIndex,
    ParsedFilter,
    ParsedFilterBlock,
    ParsedStatement,
    get_filter_gamedata_index,
    parse_filter_text,
)
from poe2brain.models.filters import FilterDiagnostic, FilterValidationReport


def _closest_match(
    word: str, candidates: list[str] | tuple[str, ...] | frozenset[str]
) -> str | None:
    hits = difflib.get_close_matches(word, list(candidates), n=2, cutoff=0.65)
    return ", ".join(hits) if hits else None


def _is_windows_or_unix_abs_path(path_str: str) -> bool:
    if path_str.startswith(("/", "\\", "~")):
        return True
    return (
        len(path_str) >= 3
        and path_str[0].isalpha()
        and path_str[1] == ":"
        and path_str[2] in ("/", "\\")
    )


def _numeric_interval(op: str | None, val: float) -> tuple[float, bool, float, bool]:
    norm = op or "=="
    if norm in ("=", "=="):
        return (val, False, val, False)
    if norm == ">=":
        return (val, False, math.inf, False)
    if norm == ">":
        return (val, True, math.inf, False)
    if norm == "<=":
        return (-math.inf, False, val, False)
    if norm == "<":
        return (-math.inf, False, val, True)
    return (-math.inf, False, math.inf, False)


def _intervals_disjoint(
    a: tuple[float, bool, float, bool],
    b: tuple[float, bool, float, bool],
) -> bool:
    lo = max(a[0], b[0])
    lo_ex = a[1] if a[0] > b[0] else (b[1] if b[0] > a[0] else (a[1] or b[1]))
    hi = min(a[2], b[2])
    hi_ex = a[3] if a[2] < b[2] else (b[3] if b[2] < a[2] else (a[3] or b[3]))
    if lo > hi:
        return True
    if lo == hi and (lo_ex or hi_ex):
        return True
    return False


class FilterValidator:
    """Strict PoE2 Patch 0.5.5 `.filter` syntax + gamedata validator."""

    def __init__(self, index: FilterGamedataIndex | None = None) -> None:
        self.index = index or get_filter_gamedata_index()
        self._all_keywords = (
            frozenset(POE2_VALID_CONDITIONS)
            | POE2_VALID_ACTIONS
            | frozenset(POE1_BLOCKED_CONDITIONS)
        )

    def validate_file(self, path: Path) -> FilterValidationReport:
        text = path.read_text(encoding="utf-8")
        return self.validate_text(text, filter_dir=path.parent)

    def validate_text(
        self,
        text: str,
        *,
        filter_dir: Path | None = None,
    ) -> FilterValidationReport:
        parsed = parse_filter_text(text)
        return self.validate_parsed(parsed, filter_dir=filter_dir)

    def validate_parsed(
        self,
        parsed: ParsedFilter,
        *,
        filter_dir: Path | None = None,
    ) -> FilterValidationReport:
        diags: list[FilterDiagnostic] = []

        for err_line, msg in parsed.syntax_errors:
            diags.append(
                FilterDiagnostic(
                    severity="error",
                    code="syntax_error",
                    message=msg,
                    line=err_line,
                )
            )

        for imp in parsed.imports:
            if not imp.optional and filter_dir is not None:
                candidate = filter_dir / imp.path
                if not candidate.is_file():
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="missing_import_file",
                            message=f'Imported filter "{imp.path}" missing in {filter_dir}.',
                            line=imp.line,
                            suggestion=(
                                f'Use `Import "{imp.path}" Optional` or write the imported file.'
                            ),
                        )
                    )

        unconditional_signatures: list[
            tuple[int, int, frozenset[str] | None, frozenset[str] | None, frozenset[str] | None]
        ] = []

        for blk in parsed.blocks:
            self._validate_block(
                blk, diags, filter_dir=filter_dir, unconditional_signatures=unconditional_signatures
            )

        errors = sum(1 for d in diags if d.severity == "error")
        warnings = sum(1 for d in diags if d.severity == "warning")
        shows = sum(1 for b in parsed.blocks if b.visibility == "Show")
        hides = sum(1 for b in parsed.blocks if b.visibility == "Hide")

        return FilterValidationReport(
            valid=(errors == 0),
            patch=POE2_PATCH,
            total_blocks=len(parsed.blocks),
            show_blocks=shows,
            hide_blocks=hides,
            error_count=errors,
            warning_count=warnings,
            diagnostics=diags,
        )

    def _validate_block(
        self,
        blk: ParsedFilterBlock,
        diags: list[FilterDiagnostic],
        *,
        filter_dir: Path | None,
        unconditional_signatures: list[
            tuple[int, int, frozenset[str] | None, frozenset[str] | None, frozenset[str] | None]
        ],
    ) -> None:
        seen_continue = False
        has_continue = False
        numeric_bounds: dict[str, tuple[tuple[float, bool, float, bool], int]] = {}
        bool_states: dict[str, tuple[bool, int]] = {}
        exact_classes: set[str] | None = None
        exact_bases: set[str] | None = None
        exact_rarities: set[str] | None = None
        has_other_narrowing = False

        class_stmt: ParsedStatement | None = None
        base_stmt: ParsedStatement | None = None
        has_builtin_alert_sound = False
        custom_opt_stmt: ParsedStatement | None = None

        for st in blk.statements:
            if seen_continue:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="statement_after_continue",
                        message=(
                            f"'{st.keyword}' appears after `Continue`; "
                            "`Continue` must be the last statement in a block."
                        ),
                        line=st.line,
                        block_index=blk.block_index,
                        suggestion="Move `Continue` to the very end of this Show/Hide block.",
                    )
                )
                continue

            kw = st.keyword
            if kw in POE1_BLOCKED_CONDITIONS:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="poe1_legacy_condition",
                        message=(f"PoE1-only condition '{kw}' is invalid in PoE2 {POE2_PATCH}."),
                        line=st.line,
                        block_index=blk.block_index,
                        suggestion=POE1_BLOCKED_CONDITIONS[kw],
                    )
                )
                continue

            if kw in POE2_VALID_CONDITIONS:
                self._validate_condition(
                    st,
                    blk.block_index,
                    diags,
                    numeric_bounds=numeric_bounds,
                    bool_states=bool_states,
                )
                if kw == "Class":
                    if class_stmt is None:
                        class_stmt = st
                    if st.operator in ("==", "="):
                        exact_classes = (
                            set(st.values)
                            if exact_classes is None
                            else (exact_classes & set(st.values))
                        )
                    else:
                        has_other_narrowing = True
                elif kw == "BaseType":
                    if base_stmt is None:
                        base_stmt = st
                    if st.operator in ("==", "="):
                        exact_bases = (
                            set(st.values)
                            if exact_bases is None
                            else (exact_bases & set(st.values))
                        )
                    else:
                        has_other_narrowing = True
                elif kw == "Rarity" and st.operator in (None, "==", "="):
                    rset = set(st.values)
                    exact_rarities = rset if exact_rarities is None else (exact_rarities & rset)
                else:
                    has_other_narrowing = True
                continue

            if kw in POE2_VALID_ACTIONS:
                if kw == "Continue":
                    seen_continue = True
                    has_continue = True
                else:
                    if kw in ("PlayAlertSound", "PlayAlertSoundPositional") and st.values[:1] != [
                        "None"
                    ]:
                        has_builtin_alert_sound = True
                    if kw == "CustomAlertSoundOptional" and st.values[:1] != ["None"]:
                        custom_opt_stmt = st
                    self._validate_action(st, blk.block_index, diags, filter_dir=filter_dir)
                continue

            hint = _closest_match(kw, self._all_keywords)
            diags.append(
                FilterDiagnostic(
                    severity="error",
                    code="unknown_keyword",
                    message=f"Unknown PoE2 filter keyword '{kw}'.",
                    line=st.line,
                    block_index=blk.block_index,
                    suggestion=f"Did you mean: {hint}?" if hint else None,
                )
            )

        if class_stmt is not None and base_stmt is not None:
            self._validate_class_basetype_combo(class_stmt, base_stmt, blk.block_index, diags)

        if custom_opt_stmt is not None and not has_builtin_alert_sound and filter_dir is None:
            diags.append(
                FilterDiagnostic(
                    severity="warning",
                    code="optional_sound_without_fallback",
                    message=(
                        "`CustomAlertSoundOptional` has no `PlayAlertSound` fallback "
                        "if the custom audio folder is absent."
                    ),
                    line=custom_opt_stmt.line,
                    block_index=blk.block_index,
                    suggestion=(
                        "Include `PlayAlertSound <1..16> <vol>` above `CustomAlertSoundOptional`."
                    ),
                )
            )

        cur_c = frozenset(exact_classes) if exact_classes is not None else None
        cur_b = frozenset(exact_bases) if exact_bases is not None else None
        cur_r = frozenset(exact_rarities) if exact_rarities is not None else None

        for prev_blk_idx, prev_line, prev_c, prev_b, prev_r in unconditional_signatures:
            c_cov = prev_c is None or (cur_c is not None and cur_c.issubset(prev_c))
            b_cov = prev_b is None or (cur_b is not None and cur_b.issubset(prev_b))
            r_cov = prev_r is None or (cur_r is not None and cur_r.issubset(prev_r))
            if (
                c_cov
                and b_cov
                and r_cov
                and (cur_c is not None or cur_b is not None or cur_r is not None)
            ):
                diags.append(
                    FilterDiagnostic(
                        severity="warning",
                        code="shadowed_rule",
                        message=(
                            f"Block #{blk.block_index} is unconditionally shadowed by "
                            f"earlier terminal block #{prev_blk_idx} (line {prev_line})."
                        ),
                        line=blk.line,
                        block_index=blk.block_index,
                        suggestion=(
                            "Place narrower rules above broader catch-all rules, "
                            "or add `Continue` to the earlier decorator."
                        ),
                    )
                )
                break

        if not has_continue and not has_other_narrowing:
            unconditional_signatures.append((blk.block_index, blk.line, cur_c, cur_b, cur_r))

    def _validate_condition(
        self,
        st: ParsedStatement,
        block_index: int,
        diags: list[FilterDiagnostic],
        *,
        numeric_bounds: dict[str, tuple[tuple[float, bool, float, bool], int]],
        bool_states: dict[str, tuple[bool, int]],
    ) -> None:
        spec = POE2_VALID_CONDITIONS[st.keyword]

        if spec.kind == "explicit_mod":
            if st.operator is not None:
                if (
                    st.values
                    and st.values[0].isdigit()
                    and st.operator_end_col is not None
                    and st.first_value_col is not None
                    and st.first_value_col > st.operator_end_col
                ):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="has_explicit_mod_space",
                            message=(
                                "`HasExplicitMod` rejects whitespace between operator "
                                f"and count (`{st.operator} {st.values[0]}`)."
                            ),
                            line=st.line,
                            block_index=block_index,
                            suggestion=(
                                f"Write `HasExplicitMod {st.operator}{st.values[0]} ...` "
                                "with no space."
                            ),
                        )
                    )
            if not st.values:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="missing_condition_value",
                        message="`HasExplicitMod` requires at least one mod name.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
            return

        if not st.values and spec.kind != "boolean":
            diags.append(
                FilterDiagnostic(
                    severity="error",
                    code="missing_condition_value",
                    message=f"Condition '{st.keyword}' requires at least one argument.",
                    line=st.line,
                    block_index=block_index,
                )
            )
            return

        if spec.kind == "boolean":
            if st.operator not in (None, "=", "=="):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_boolean_operator",
                        message=(
                            f"Boolean condition '{st.keyword}' does not accept "
                            f"operator '{st.operator}'."
                        ),
                        line=st.line,
                        block_index=block_index,
                        suggestion=f"Use `{st.keyword} True` or `{st.keyword} False`.",
                    )
                )
                return
            raw_b = st.values[0] if st.values else "True"
            if raw_b not in ("True", "False"):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_boolean_value",
                        message=(
                            f"Condition '{st.keyword}' expects `True` or `False`, got '{raw_b}'."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            bval = raw_b == "True"
            if st.keyword in bool_states and bool_states[st.keyword][0] != bval:
                prev_ln = bool_states[st.keyword][1]
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="contradictory_condition",
                        message=(
                            f"Contradictory `{st.keyword}` condition "
                            f"(conflicts with line {prev_ln})."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
            bool_states[st.keyword] = (bval, st.line)
            return

        if spec.kind == "rarity":
            if st.operator is not None and st.operator not in NUMERIC_OPERATORS:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_operator",
                        message=f"Invalid operator '{st.operator}' for `Rarity`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
            for rv in st.values:
                if rv not in RARITY_ORDINAL:
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="invalid_rarity",
                            message=(
                                f"Invalid Rarity '{rv}'. Expected Normal, Magic, Rare, or Unique."
                            ),
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            return

        if spec.kind == "number":
            if st.operator is not None and st.operator not in NUMERIC_OPERATORS:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_operator",
                        message=f"Invalid numeric operator '{st.operator}' for `{st.keyword}`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            try:
                num = int(st.values[0])
            except ValueError:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_number",
                        message=(
                            f"Condition `{st.keyword}` expects an integer, got '{st.values[0]}'."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return

            if spec.min_val is not None and num < spec.min_val:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="value_out_of_range",
                        message=f"`{st.keyword} {num}` is below minimum `{spec.min_val}`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
            if spec.max_val is not None and num > spec.max_val:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="value_out_of_range",
                        message=f"`{st.keyword} {num}` exceeds maximum `{spec.max_val}`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )

            iv = _numeric_interval(st.operator, float(num))
            if st.keyword in numeric_bounds:
                prev_iv, prev_ln = numeric_bounds[st.keyword]
                if _intervals_disjoint(prev_iv, iv):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="contradictory_condition",
                            message=(
                                f"Contradictory `{st.keyword}` range "
                                f"(conflicts with line {prev_ln}); block can never match."
                            ),
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            else:
                numeric_bounds[st.keyword] = (iv, st.line)
            return

        if spec.kind == "string_list":
            is_exact = st.operator in ("==", "=")
            if st.operator not in (None, "==", "="):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_operator",
                        message=(
                            f"`{st.keyword}` only allows `==` (exact) or no operator "
                            f"(substring), got '{st.operator}'."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return

            for val in st.values:
                if not val.strip():
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="empty_class_or_basetype",
                            message=f"`{st.keyword}` contains an empty string literal.",
                            line=st.line,
                            block_index=block_index,
                        )
                    )
                    continue

                if st.keyword == "Class":
                    if val in self.index.internal_only_class_ids:
                        correct = self.index.internal_only_class_ids[val]
                        diags.append(
                            FilterDiagnostic(
                                severity="error",
                                code="internal_item_class_id",
                                message=(
                                    f'`Class "{val}"` uses internal `.datc64`/SQLite ID '
                                    "instead of PoE2 filter Class name."
                                ),
                                line=st.line,
                                block_index=block_index,
                                suggestion=f'Use `Class == "{correct}"`.',
                            )
                        )
                        continue
                    matched_cls = (
                        val in self.index.valid_filter_classes
                        if is_exact
                        else any(val in fc for fc in self.index.valid_filter_classes)
                    )
                    if not matched_cls:
                        hint = _closest_match(val, self.index.valid_filter_classes)
                        diags.append(
                            FilterDiagnostic(
                                severity="error",
                                code="unknown_class",
                                message=(
                                    f'Class "{val}" does not exist in PoE2 {POE2_PATCH}; '
                                    "PoE2 client will fail to load this filter."
                                ),
                                line=st.line,
                                block_index=block_index,
                                suggestion=f"Did you mean: {hint}?" if hint else None,
                            )
                        )
                elif st.keyword == "BaseType":
                    if val in self.index.dormant_or_removed_basetypes:
                        diags.append(
                            FilterDiagnostic(
                                severity="error",
                                code="dormant_or_removed_item",
                                message=(
                                    f'BaseType "{val}" is a dormant PoE1 or removed PoE2 '
                                    f"item and must not be emitted in Patch {POE2_PATCH}."
                                ),
                                line=st.line,
                                block_index=block_index,
                            )
                        )
                        continue
                    matched_bt = (
                        val in self.index.basetype_to_classes
                        if is_exact
                        else any(val in bt for bt in self.index.basetype_to_classes)
                    )
                    if not matched_bt:
                        hint = _closest_match(val, tuple(self.index.basetype_to_classes))
                        diags.append(
                            FilterDiagnostic(
                                severity="error",
                                code="unknown_basetype",
                                message=(
                                    f'BaseType "{val}" does not match any item in PoE2 '
                                    f"{POE2_PATCH}; PoE2 client will fail to load this filter."
                                ),
                                line=st.line,
                                block_index=block_index,
                                suggestion=(
                                    f"Did you mean: {hint}?" if hint else "Remove stale BaseType."
                                ),
                            )
                        )

    def _validate_class_basetype_combo(
        self,
        class_stmt: ParsedStatement,
        base_stmt: ParsedStatement,
        block_index: int,
        diags: list[FilterDiagnostic],
    ) -> None:
        c_exact = class_stmt.operator in ("==", "=")
        b_exact = base_stmt.operator in ("==", "=")
        allowed_classes = {
            fc
            for cv in class_stmt.values
            for fc in self.index.valid_filter_classes
            if (fc == cv if c_exact else cv in fc)
        }
        if not allowed_classes:
            return
        for bv in base_stmt.values:
            cand_classes: set[str] = set()
            if b_exact:
                cand_classes.update(self.index.basetype_to_classes.get(bv, ()))
            else:
                for bt_name, fc_set in self.index.basetype_to_classes.items():
                    if bv in bt_name:
                        cand_classes.update(fc_set)
            if cand_classes and not (cand_classes & allowed_classes):
                diags.append(
                    FilterDiagnostic(
                        severity="warning",
                        code="class_basetype_mismatch",
                        message=(
                            f'BaseType "{bv}" belongs to {sorted(cand_classes)}, '
                            f"which never matches Class {class_stmt.values}."
                        ),
                        line=base_stmt.line,
                        block_index=block_index,
                    )
                )

    def _validate_action(
        self,
        st: ParsedStatement,
        block_index: int,
        diags: list[FilterDiagnostic],
        *,
        filter_dir: Path | None,
    ) -> None:
        kw = st.keyword
        vals = st.values

        if kw == "SetFontSize":
            if len(vals) != 1 or not vals[0].lstrip("-").isdigit():
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_font_size",
                        message="`SetFontSize` requires a single integer in [18, 45].",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            sz = int(vals[0])
            if not (18 <= sz <= 45):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="font_size_out_of_range",
                        message=f"`SetFontSize {sz}` is outside PoE2's effective [18, 45] range.",
                        line=st.line,
                        block_index=block_index,
                        suggestion="Clamp `SetFontSize` to `18..45` (`32` default, `45` S+ drop).",
                    )
                )
            return

        if kw in ("SetTextColor", "SetBorderColor", "SetBackgroundColor"):
            if len(vals) not in (3, 4) or not all(v.lstrip("-").isdigit() for v in vals):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_rgba",
                        message=f"`{kw}` expects 3 or 4 integers `R G B [A]` in 0..255.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            for ch in vals:
                c_int = int(ch)
                if not (0 <= c_int <= 255):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="rgba_out_of_range",
                            message=f"`{kw}` channel `{c_int}` is outside 0..255.",
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            return

        if kw in ("PlayAlertSound", "PlayAlertSoundPositional"):
            if not vals:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_alert_sound",
                        message=f"`{kw}` requires a sound ID (`1..16`, named sound, or `None`).",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            if vals[0] == "None":
                return
            sid = vals[0]
            if sid.lstrip("-").isdigit():
                sid_n = int(sid)
                if not (1 <= sid_n <= 16):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="sound_id_out_of_range",
                            message=f"`{kw} {sid_n}` is outside valid built-in sound IDs `1..16`.",
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            elif sid not in VALID_NAMED_SOUND_SET:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_sound_name",
                        message=(
                            f"Invalid built-in sound '{sid}'. "
                            f"Valid named sounds: {', '.join(VALID_NAMED_SOUNDS)}."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
            if len(vals) >= 2:
                if not vals[1].lstrip("-").isdigit() or not (0 <= int(vals[1]) <= 300):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="volume_out_of_range",
                            message=f"`{kw}` volume '{vals[1]}' must be an integer in `0..300`.",
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            return

        if kw in ("CustomAlertSound", "CustomAlertSoundOptional"):
            if not vals:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_custom_sound",
                        message=f'`{kw}` requires a relative audio file path or `"None"`.',
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            if vals[0] == "None":
                return
            for snd_part in (p.strip() for p in vals[0].split(";") if p.strip()):
                if _is_windows_or_unix_abs_path(snd_part):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="absolute_sound_path",
                            message=(
                                f'`{kw}` path "{snd_part}" must be relative to the '
                                "PoE2 filter folder, not an absolute path."
                            ),
                            line=st.line,
                            block_index=block_index,
                        )
                    )
                elif filter_dir is not None and not (filter_dir / snd_part).is_file():
                    is_opt = kw == "CustomAlertSoundOptional"
                    diags.append(
                        FilterDiagnostic(
                            severity="warning" if is_opt else "error",
                            code="missing_custom_sound_file",
                            message=f'Custom sound file "{snd_part}" not found in {filter_dir}.',
                            line=st.line,
                            block_index=block_index,
                            suggestion=(
                                "Use `CustomAlertSoundOptional` + `PlayAlertSound` fallback "
                                "or install the sound pack."
                            ),
                        )
                    )
            if len(vals) >= 2:
                if not vals[1].lstrip("-").isdigit() or not (0 <= int(vals[1]) <= 300):
                    diags.append(
                        FilterDiagnostic(
                            severity="error",
                            code="volume_out_of_range",
                            message=f"`{kw}` volume '{vals[1]}' must be in `0..300`.",
                            line=st.line,
                            block_index=block_index,
                        )
                    )
            return

        if kw == "PlayEffect":
            if not vals:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_play_effect",
                        message="`PlayEffect` requires a beam color or `None`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            if vals[0] == "None":
                return
            if vals[0] not in VALID_COLOR_SET:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_beam_color",
                        message=(
                            f"Invalid `PlayEffect` color '{vals[0]}'. "
                            f"Valid colors: {', '.join(VALID_COLORS)}."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
            if len(vals) >= 2 and vals[1] != "Temp":
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_beam_modifier",
                        message=f"Invalid `PlayEffect` modifier '{vals[1]}'; only `Temp` is valid.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
            return

        if kw == "MinimapIcon":
            if not vals or not vals[0].lstrip("-").isdigit():
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_minimap_icon",
                        message="`MinimapIcon` requires `<size 0|1|2|-1> <Color> <Shape>`.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            sz = int(vals[0])
            if sz == -1:
                return
            if sz not in (0, 1, 2) or len(vals) < 3:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_minimap_icon",
                        message=(
                            "`MinimapIcon` size must be `0` (Large), `1` (Medium), "
                            "`2` (Small), or `-1` (Off) followed by Color and Shape."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
                return
            if vals[1] not in VALID_COLOR_SET:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_icon_color",
                        message=(
                            f"Invalid `MinimapIcon` color '{vals[1]}'. "
                            f"Valid colors: {', '.join(VALID_COLORS)}."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
            if vals[2] not in VALID_SHAPE_SET:
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_icon_shape",
                        message=(
                            f"Invalid `MinimapIcon` shape '{vals[2]}'. "
                            f"Valid shapes: {', '.join(VALID_SHAPES)}."
                        ),
                        line=st.line,
                        block_index=block_index,
                    )
                )
            return

        if kw in ("DisableDropSound", "DisableDropSoundIfAlertSound"):
            if vals and vals[0] not in ("True", "False"):
                diags.append(
                    FilterDiagnostic(
                        severity="error",
                        code="invalid_boolean_value",
                        message=f"`{kw}` only accepts optional `True` or `False`, got '{vals[0]}'.",
                        line=st.line,
                        block_index=block_index,
                    )
                )
