from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH
from poe2brain.filters.generator import FilterGenerator
from poe2brain.filters.grammar import (
    FilterGamedataIndex,
    ParsedFilter,
    ParsedFilterBlock,
    ParsedStatement,
    get_filter_gamedata_index,
    parse_filter_text,
)
from poe2brain.filters.themes import BUILTIN_THEME_PRESETS, compose_theme, get_visual_theme
from poe2brain.filters.validator import FilterValidator
from poe2brain.http import Poe2HttpClient
from poe2brain.models.filters import (
    BuildFilterContext,
    FilterCompileResult,
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
            if st.keyword == "SetFontSize":
                st.values = [str(style.font_size)]
        return

    _set_or_append_action(blk.statements, blk.line, "SetFontSize", [str(style.font_size)])
    _set_or_append_action(
        blk.statements,
        blk.line,
        "SetTextColor",
        style.text_color.to_filter_tokens().split(),
    )
    _set_or_append_action(
        blk.statements,
        blk.line,
        "SetBorderColor",
        style.border_color.to_filter_tokens().split(),
    )
    _set_or_append_action(
        blk.statements,
        blk.line,
        "SetBackgroundColor",
        style.background_color.to_filter_tokens().split(),
    )

    # Remove prior sound/beam/icon actions so the new theme + soundpack apply cleanly
    blk.statements = [
        st
        for st in blk.statements
        if st.keyword
        not in (
            "PlayAlertSound",
            "PlayAlertSoundPositional",
            "CustomAlertSound",
            "CustomAlertSoundOptional",
            "PlayEffect",
            "MinimapIcon",
        )
    ]

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
        healed: list[str] = []
        kept_blocks: list[ParsedFilterBlock] = []

        for blk in parsed.blocks:
            drop_entire_block = False
            new_stmts: list[ParsedStatement] = []
            seen_continue = False

            for st in blk.statements:
                if seen_continue:
                    continue
                if st.keyword == "Continue":
                    seen_continue = True
                    new_stmts.append(st)
                    continue

                if st.keyword == "BaseType":
                    is_exact = st.operator in ("==", "=")
                    valid_vals: list[str] = []
                    valid_qflags: list[bool] = []
                    for val, qf in zip(st.values, st.quoted_flags, strict=False):
                        if val in self.index.dormant_or_removed_basetypes:
                            if val not in healed:
                                healed.append(val)
                            continue
                        ok = (
                            val in self.index.basetype_to_classes
                            if is_exact
                            else any(val in bt for bt in self.index.basetype_to_classes)
                        )
                        if ok:
                            valid_vals.append(val)
                            valid_qflags.append(qf)
                        else:
                            if val not in healed:
                                healed.append(val)
                    if not valid_vals:
                        drop_entire_block = True
                        break
                    st.values = valid_vals
                    st.quoted_flags = valid_qflags
                    new_stmts.append(st)
                    continue

                if st.keyword == "Class":
                    is_exact = st.operator in ("==", "=")
                    valid_c: list[str] = []
                    valid_cq: list[bool] = []
                    for val, qf in zip(st.values, st.quoted_flags, strict=False):
                        mapped = (
                            self.index.internal_only_class_ids.get(val, val) if is_exact else val
                        )
                        ok = (
                            mapped in self.index.valid_filter_classes
                            if is_exact
                            else any(mapped in fc for fc in self.index.valid_filter_classes)
                        )
                        if ok:
                            valid_c.append(mapped)
                            valid_cq.append(qf)
                        else:
                            if val not in healed:
                                healed.append(val)
                    if not valid_c:
                        drop_entire_block = True
                        break
                    st.values = valid_c
                    st.quoted_flags = valid_cq
                    new_stmts.append(st)
                    continue

                if st.keyword == "SetFontSize" and st.values and st.values[0].lstrip("-").isdigit():
                    st.values = [str(max(18, min(45, int(st.values[0]))))]

                if (
                    st.keyword == "HasExplicitMod"
                    and st.operator
                    and st.values
                    and st.values[0].isdigit()
                ):
                    st.first_value_col = st.operator_end_col

                new_stmts.append(st)

            if not drop_entire_block:
                blk.statements = new_stmts
                blk.block_index = len(kept_blocks)
                kept_blocks.append(blk)

        parsed.blocks = kept_blocks
        return healed

    def restyle_and_heal(
        self,
        source_filter_text: str | None = None,
        *,
        source_filter_path: Path | None = None,
        strictness: StrictnessTier | str = StrictnessTier.SEMI_STRICT,
        theme: VisualThemeSpec | str | None = "forbidden_abyss",
        style_prompt: str | None = None,
        theme_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        sound_pack: SoundPackManifest | None = None,
        require_custom_sounds: bool = False,
        inject_055_overrides: bool = True,
        league: str = DEFAULT_LEAGUE,
        use_live_economy: bool = False,
        build_context: BuildFilterContext | None = None,
        filter_name: str = "poe2brain_neversink_healed",
        output_path: Path | None = None,
    ) -> FilterCompileResult:
        strict_tier = StrictnessTier(strictness) if isinstance(strictness, str) else strictness
        if source_filter_text is not None:
            raw_text = source_filter_text
        elif source_filter_path is not None:
            raw_text = source_filter_path.read_text(encoding="utf-8")
        else:
            raw_text = self.fetch_neversink_text(strict_tier)

        if isinstance(theme, VisualThemeSpec):
            resolved_theme: VisualThemeSpec | None = theme
        elif style_prompt or theme_overrides:
            base_p = theme if isinstance(theme, str) and theme in BUILTIN_THEME_PRESETS else None
            resolved_theme = compose_theme(
                style_prompt, base_preset=base_p, overrides=theme_overrides
            )
        elif isinstance(theme, str):
            resolved_theme = get_visual_theme(theme)
        else:
            resolved_theme = None

        parsed = parse_filter_text(raw_text)
        healed_items = self.heal_parsed_filter(parsed)

        if resolved_theme is not None:
            for blk in parsed.blocks:
                sem_tier = map_neversink_meta_to_semantic_tier(blk.meta_type, blk.meta_tier)
                tier_style = resolved_theme.tiers.get(sem_tier, resolved_theme.tiers["c"])
                _apply_style_to_neversink_block(
                    blk,
                    tier_style,
                    sound_pack=sound_pack,
                    require_custom_sounds=require_custom_sounds,
                )

        injected_count = 0
        if inject_055_overrides:
            active_theme = resolved_theme or get_visual_theme("forbidden_abyss")
            prices_ex, _ = self.generator.collect_market_prices(
                league=league,
                use_live_economy=use_live_economy,
            )
            ov_text_blocks = self.generator.build_poe2_override_blocks(
                active_theme,
                strictness=strict_tier,
                build_context=build_context,
                sound_pack=sound_pack,
                require_custom_sounds=require_custom_sounds,
                prices_ex=prices_ex,
            )
            ov_parsed = parse_filter_text("\n\n".join(ov_text_blocks))
            injected_count = len(ov_parsed.blocks)
            parsed.blocks = ov_parsed.blocks + parsed.blocks
            for idx_b, b in enumerate(parsed.blocks):
                b.block_index = idx_b

        attribution = [
            f"# Adapted & healed for PoE2 Patch {POE2_PATCH} by poe2brain (MIT License).",
            (
                "# Base filter structure courtesy of NeverSink "
                "(https://github.com/NeverSinkDev/NeverSink-Filter-for-PoE2, MIT License)."
            ),
        ]
        parsed.preamble_comments = attribution + parsed.preamble_comments
        compiled_text = parsed.render()

        out_dir: Path | None = None
        written_path_str: str | None = None
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(compiled_text, encoding="utf-8")
            out_dir = output_path.parent
            written_path_str = str(output_path)
        elif sound_pack is not None:
            out_dir = Path(sound_pack.pack_dir).parent

        report = self.validator.validate_text(compiled_text, filter_dir=out_dir)

        return FilterCompileResult(
            filter_name=filter_name,
            patch=POE2_PATCH,
            league=league,
            strictness=strict_tier,
            theme_id=resolved_theme.id if resolved_theme else "neversink_original",
            sound_pack_id=sound_pack.pack_id if sound_pack else None,
            filter_text=compiled_text,
            filter_path=written_path_str,
            validation=report,
            build_context=build_context,
            healed_basetypes=healed_items,
            injected_override_blocks=injected_count,
        )
