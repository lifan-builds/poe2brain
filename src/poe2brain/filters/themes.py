# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import colorsys
import math
import re
from collections.abc import Mapping
from typing import Any

from poe2brain.filters.grammar import VALID_COLOR_SET, VALID_SHAPE_SET, VALID_SHAPES
from poe2brain.models.filters import (
    BeamSpec,
    ColorVisionDeficiency,
    CvdPairSeparation,
    CvdThemeAuditReport,
    MinimapIconSpec,
    RgbaColor,
    SemanticDropTier,
    TierVisualStyle,
    VisualThemeSpec,
)

WRAECLAST_GROUND_RGB: tuple[int, int, int] = (18, 20, 26)

# Viénot et al. (1999) / Machado et al. (2009) linear-sRGB dichromat projection matrices
_CVD_LINEAR_MATRICES: dict[
    ColorVisionDeficiency,
    tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
] = {
    ColorVisionDeficiency.DEUTERANOPIA: (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    ColorVisionDeficiency.PROTANOPIA: (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    ColorVisionDeficiency.TRITANOPIA: (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}

_CVD_AUDIT_TIERS: tuple[str, ...] = (
    "s_plus",
    "s",
    "a",
    "b",
    "c",
    "d",
    "build_upgrade",
    "waystone_high",
    "waystone_prog",
    "chancing",
    "gold_high",
)

_CVD_SAFE_COLOR_REMAP: dict[str, str] = {
    "Red": "Orange",
    "Green": "Cyan",
}

TIER_HIERARCHY: tuple[SemanticDropTier, ...] = (
    SemanticDropTier.S_PLUS,
    SemanticDropTier.S,
    SemanticDropTier.A,
    SemanticDropTier.B,
    SemanticDropTier.C,
    SemanticDropTier.D,
)


def _srgb_channel_lum(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def composite_over_ground(
    bg: RgbaColor, ground: tuple[int, int, int] = WRAECLAST_GROUND_RGB
) -> tuple[int, int, int]:
    a = bg.a / 255.0
    r = int(round(bg.r * a + ground[0] * (1.0 - a)))
    g = int(round(bg.g * a + ground[1] * (1.0 - a)))
    b = int(round(bg.b * a + ground[2] * (1.0 - a)))
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    return (
        0.2126 * _srgb_channel_lum(rgb[0])
        + 0.7152 * _srgb_channel_lum(rgb[1])
        + 0.0722 * _srgb_channel_lum(rgb[2])
    )


def _linear_to_srgb_byte(val: float) -> int:
    clamped = max(0.0, min(1.0, val))
    srgb = clamped * 12.92 if clamped <= 0.0031308 else 1.055 * (clamped ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(round(srgb * 255.0))))


def _as_rgb_tuple(color: RgbaColor | tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(color, RgbaColor):
        return (color.r, color.g, color.b)
    return (int(color[0]), int(color[1]), int(color[2]))


def simulate_cvd_color(
    color: RgbaColor | tuple[int, int, int],
    deficiency: ColorVisionDeficiency | str = ColorVisionDeficiency.DEUTERANOPIA,
) -> RgbaColor:
    """Simulate full dichromacy (deuteranopia, protanopia, or tritanopia) in linear sRGB."""
    mode = (
        deficiency
        if isinstance(deficiency, ColorVisionDeficiency)
        else ColorVisionDeficiency(deficiency.strip().lower())
    )
    r_i, g_i, b_i = _as_rgb_tuple(color)
    alpha = color.a if isinstance(color, RgbaColor) else 255
    rl, gl, bl = _srgb_channel_lum(r_i), _srgb_channel_lum(g_i), _srgb_channel_lum(b_i)
    mat = _CVD_LINEAR_MATRICES[mode]
    r2 = mat[0][0] * rl + mat[0][1] * gl + mat[0][2] * bl
    g2 = mat[1][0] * rl + mat[1][1] * gl + mat[1][2] * bl
    b2 = mat[2][0] * rl + mat[2][1] * gl + mat[2][2] * bl
    return RgbaColor(
        r=_linear_to_srgb_byte(r2),
        g=_linear_to_srgb_byte(g2),
        b=_linear_to_srgb_byte(b2),
        a=alpha,
    )


def rgb_to_cielab(color: RgbaColor | tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert sRGB (D65) into CIELAB (L*, a*, b*)."""
    r_i, g_i, b_i = _as_rgb_tuple(color)
    rl, gl, bl = _srgb_channel_lum(r_i), _srgb_channel_lum(g_i), _srgb_channel_lum(b_i)
    x = (rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375) / 0.95047
    y = (rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750) / 1.00000
    z = (rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041) / 1.08883

    def _f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t) + (16.0 / 116.0)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def compute_cie_delta_e(
    color_a: RgbaColor | tuple[int, int, int],
    color_b: RgbaColor | tuple[int, int, int],
) -> float:
    """Compute perceptual CIELAB Euclidean distance (Delta-E*ab)."""
    return round(math.dist(rgb_to_cielab(color_a), rgb_to_cielab(color_b)), 2)


def _plate_rms_delta_e(
    style_a: TierVisualStyle,
    style_b: TierVisualStyle,
    deficiency: ColorVisionDeficiency | None = None,
) -> tuple[float, float]:
    bg_a = composite_over_ground(style_a.background_color)
    bg_b = composite_over_ground(style_b.background_color)
    if deficiency is None:
        fg_de = compute_cie_delta_e(style_a.text_color, style_b.text_color)
        bd_de = compute_cie_delta_e(style_a.border_color, style_b.border_color)
        bg_de = compute_cie_delta_e(bg_a, bg_b)
    else:
        fg_de = compute_cie_delta_e(
            simulate_cvd_color(style_a.text_color, deficiency),
            simulate_cvd_color(style_b.text_color, deficiency),
        )
        bd_de = compute_cie_delta_e(
            simulate_cvd_color(style_a.border_color, deficiency),
            simulate_cvd_color(style_b.border_color, deficiency),
        )
        bg_de = compute_cie_delta_e(
            simulate_cvd_color(bg_a, deficiency),
            simulate_cvd_color(bg_b, deficiency),
        )
    rms = round(math.sqrt((fg_de**2 + bd_de**2 + bg_de**2) / 3.0), 2)
    return rms, round(max(fg_de, bd_de, bg_de), 2)


def audit_theme_cvd(
    theme: VisualThemeSpec,
    *,
    min_cvd_delta_e: float = 12.0,
    require_unique_shapes: bool = False,
) -> CvdThemeAuditReport:
    """Audit VisualThemeSpec for Deuteranopia/Protanopia/Tritanopia separation and geometry."""
    present_tiers = [tk for tk in _CVD_AUDIT_TIERS if tk in theme.tiers]
    shape_owners: dict[str, list[str]] = {}
    rg_beam_tiers: dict[str, list[str]] = {"Red": [], "Green": []}

    for tk in present_tiers:
        st = theme.tiers[tk]
        if st.minimap_icon is not None and st.minimap_icon.size >= 0:
            shape_owners.setdefault(st.minimap_icon.shape, []).append(tk)
            if st.minimap_icon.color in rg_beam_tiers:
                rg_beam_tiers[st.minimap_icon.color].append(f"{tk}:icon")
        if st.beam is not None and st.beam.color in rg_beam_tiers:
            rg_beam_tiers[st.beam.color].append(f"{tk}:beam")

    duplicate_shapes = [
        f"{shape} ({', '.join(owners)})"
        for shape, owners in sorted(shape_owners.items())
        if len(owners) > 1
    ]

    red_green_conflicts: list[str] = []
    if rg_beam_tiers["Red"] and rg_beam_tiers["Green"]:
        red_green_conflicts.append(
            f"Red vs Green beam/icon mix: Red={rg_beam_tiers['Red']} "
            f"vs Green={rg_beam_tiers['Green']}"
        )

    all_pairs: list[CvdPairSeparation] = []
    min_by_mode: dict[ColorVisionDeficiency, float] = {
        ColorVisionDeficiency.DEUTERANOPIA: 999.0,
        ColorVisionDeficiency.PROTANOPIA: 999.0,
        ColorVisionDeficiency.TRITANOPIA: 999.0,
    }

    for i in range(len(present_tiers)):
        for j in range(i + 1, len(present_tiers)):
            ta, tb = present_tiers[i], present_tiers[j]
            sa, sb = theme.tiers[ta], theme.tiers[tb]
            norm_rms, _ = _plate_rms_delta_e(sa, sb, None)
            lum_a = relative_luminance((sa.text_color.r, sa.text_color.g, sa.text_color.b))
            lum_b = relative_luminance((sb.text_color.r, sb.text_color.g, sb.text_color.b))
            bg_lum_a = relative_luminance(composite_over_ground(sa.background_color))
            bg_lum_b = relative_luminance(composite_over_ground(sb.background_color))
            lum_delta = round(max(abs(lum_a - lum_b), abs(bg_lum_a - bg_lum_b)), 4)

            shape_a = (
                sa.minimap_icon.shape if (sa.minimap_icon and sa.minimap_icon.size >= 0) else None
            )
            shape_b = (
                sb.minimap_icon.shape if (sb.minimap_icon and sb.minimap_icon.size >= 0) else None
            )
            distinct_shapes = shape_a != shape_b

            for mode in (
                ColorVisionDeficiency.DEUTERANOPIA,
                ColorVisionDeficiency.PROTANOPIA,
                ColorVisionDeficiency.TRITANOPIA,
            ):
                cvd_rms, cvd_max = _plate_rms_delta_e(sa, sb, mode)
                min_by_mode[mode] = min(min_by_mode[mode], cvd_rms)
                rg_collapse = (
                    mode in (ColorVisionDeficiency.DEUTERANOPIA, ColorVisionDeficiency.PROTANOPIA)
                    and norm_rms >= 20.0
                    and cvd_rms < min_cvd_delta_e
                    and lum_delta < 0.12
                )
                if rg_collapse:
                    red_green_conflicts.append(
                        f"{ta} vs {tb} collapses under {mode.value} "
                        f"(normal dE={norm_rms} -> {cvd_rms})"
                    )
                all_pairs.append(
                    CvdPairSeparation(
                        tier_a=ta,
                        tier_b=tb,
                        deficiency=mode,
                        normal_rms_delta_e=norm_rms,
                        cvd_rms_delta_e=cvd_rms,
                        cvd_max_channel_delta_e=cvd_max,
                        luminance_delta=lum_delta,
                        distinct_minimap_shapes=distinct_shapes,
                        relies_on_red_green_hue_alone=rg_collapse,
                    )
                )

    all_pairs.sort(key=lambda p: p.cvd_rms_delta_e)
    min_deut = round(min_by_mode[ColorVisionDeficiency.DEUTERANOPIA], 2)
    min_prot = round(min_by_mode[ColorVisionDeficiency.PROTANOPIA], 2)
    min_trit = round(min_by_mode[ColorVisionDeficiency.TRITANOPIA], 2)
    shapes_ok = (len(duplicate_shapes) == 0) if require_unique_shapes else True
    cvd_safe = (
        min_deut >= min_cvd_delta_e
        and min_prot >= min_cvd_delta_e
        and not red_green_conflicts
        and shapes_ok
    )

    return CvdThemeAuditReport(
        theme_id=theme.id,
        cvd_safe=cvd_safe,
        min_deuteranopia_delta_e=min_deut,
        min_protanopia_delta_e=min_prot,
        min_tritanopia_delta_e=min_trit,
        distinct_minimap_shape_count=len(shape_owners),
        duplicate_minimap_shapes=duplicate_shapes,
        red_green_conflicts=red_green_conflicts,
        worst_pairs=all_pairs[:10],
    )


def compute_wcag_contrast(fg: RgbaColor, bg: RgbaColor) -> float:
    eff_bg = composite_over_ground(bg)
    l1 = relative_luminance((fg.r, fg.g, fg.b))
    l2 = relative_luminance(eff_bg)
    bright, dark = (l1, l2) if l1 >= l2 else (l2, l1)
    return round((bright + 0.05) / (dark + 0.05), 2)


def ensure_wcag_contrast(
    fg: RgbaColor, bg: RgbaColor, min_ratio: float = 4.5
) -> tuple[RgbaColor, float]:
    current = compute_wcag_contrast(fg, bg)
    if current >= min_ratio:
        return fg, current

    eff_bg = composite_over_ground(bg)
    bg_lum = relative_luminance(eff_bg)
    lighten = bg_lum < 0.28

    h, l_val, s = colorsys.rgb_to_hls(fg.r / 255.0, fg.g / 255.0, fg.b / 255.0)
    best_color = fg
    best_ratio = current

    for step in range(1, 45):
        delta = step * 0.022
        cand_l = min(0.98, l_val + delta) if lighten else max(0.03, l_val - delta)
        r_f, g_f, b_f = colorsys.hls_to_rgb(h, cand_l, s)
        cand = RgbaColor(
            r=int(round(r_f * 255)),
            g=int(round(g_f * 255)),
            b=int(round(b_f * 255)),
            a=fg.a,
        )
        ratio = compute_wcag_contrast(cand, bg)
        if ratio > best_ratio:
            best_color = cand
            best_ratio = ratio
        if ratio >= min_ratio:
            return cand, ratio

    fallback = (
        RgbaColor(r=255, g=255, b=255, a=fg.a) if lighten else RgbaColor(r=12, g=12, b=16, a=fg.a)
    )
    fb_ratio = compute_wcag_contrast(fallback, bg)
    return (best_color, best_ratio) if best_ratio >= fb_ratio else (fallback, fb_ratio)


def _nearest_poe_color_name(c: RgbaColor) -> str:
    palette: dict[str, tuple[int, int, int]] = {
        "Red": (235, 45, 55),
        "Green": (60, 220, 95),
        "Blue": (65, 130, 250),
        "Brown": (165, 105, 55),
        "White": (245, 245, 248),
        "Yellow": (245, 220, 65),
        "Cyan": (65, 235, 235),
        "Grey": (150, 155, 165),
        "Orange": (245, 145, 40),
        "Pink": (245, 95, 185),
        "Purple": (175, 80, 245),
    }
    best_name = "White"
    best_dist = 10**9
    for name, (pr, pg, pb) in palette.items():
        d = (c.r - pr) ** 2 + (c.g - pg) ** 2 + (c.b - pb) ** 2
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name


def _mk_tier(
    tier: SemanticDropTier,
    font_size: int,
    fg: str | tuple[int, ...],
    border: str | tuple[int, ...],
    bg: str | tuple[int, ...],
    *,
    beam: tuple[str, bool] | None = None,
    icon: tuple[int, str, str] | None = None,
    sound_id: int | str | None = None,
    vol: int = 300,
    min_contrast: float = 4.5,
) -> TierVisualStyle:
    fg_c = RgbaColor.from_any(fg)
    bd_c = RgbaColor.from_any(border)
    bg_c = RgbaColor.from_any(bg)
    adj_fg, ratio = ensure_wcag_contrast(fg_c, bg_c, min_ratio=min_contrast)
    return TierVisualStyle(
        tier=tier,
        font_size=max(18, min(45, font_size)),
        text_color=adj_fg,
        border_color=bd_c,
        background_color=bg_c,
        beam=BeamSpec(color=beam[0], temp=beam[1]) if beam else None,
        minimap_icon=MinimapIconSpec(size=icon[0], color=icon[1], shape=icon[2]) if icon else None,
        builtin_sound_id=sound_id,
        sound_volume=vol,
        contrast_ratio=ratio,
    )


def _build_builtin_presets() -> dict[str, VisualThemeSpec]:
    presets: dict[str, VisualThemeSpec] = {}

    presets["forbidden_abyss"] = VisualThemeSpec(
        id="forbidden_abyss",
        name="Forbidden Abyss",
        description=(
            "Patch 0.5.5 Forbidden Rites signature look: "
            "abyssal emerald & royal gold over obsidian-violet."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#19081f",
                "#ff2e63",
                (255, 245, 225, 255),
                beam=("Red", False),
                icon=(0, "Red", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#ffd86b",
                "#ff477e",
                (36, 12, 52, 252),
                beam=("Purple", False),
                icon=(0, "Purple", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#52ffb8",
                "#34e89e",
                (12, 34, 30, 246),
                beam=("Green", False),
                icon=(1, "Green", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#7ce8ff",
                "#38bdf8",
                (14, 24, 38, 238),
                beam=("Cyan", True),
                icon=(1, "Cyan", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e2d9f3",
                "#7c6f9f",
                (20, 18, 30, 225),
                beam=None,
                icon=(2, "Grey", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(
                SemanticDropTier.D,
                28,
                "#b8b5c8",
                "#423d58",
                (16, 15, 24, 200),
                beam=None,
                icon=None,
                sound_id=None,
                vol=100,
            ),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#7effa7",
                "#00f5d4",
                (10, 38, 32, 248),
                beam=("Cyan", False),
                icon=(0, "Cyan", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#fff099",
                "#f59e0b",
                (38, 22, 10, 248),
                beam=("Orange", False),
                icon=(0, "Orange", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#fde68a",
                "#b45309",
                (28, 20, 12, 232),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#6ee7b7",
                "#10b981",
                (12, 30, 24, 235),
                beam=("Green", True),
                icon=(1, "Green", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#ffe57f",
                "#fbbf24",
                (32, 26, 8, 230),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
                sound_id=None,
                vol=0,
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW,
                24,
                "#cfc7a8",
                "#3a3420",
                (18, 18, 12, 175),
                beam=None,
                icon=None,
                sound_id=None,
                vol=0,
            ),
        },
    )

    presets["solaris_gold"] = VisualThemeSpec(
        id="solaris_gold",
        name="Solaris Gold",
        description=(
            "Radiant Temple of Solaris ivory, sun-forged gold, and imperial crimson highlights."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#b91c1c",
                "#f59e0b",
                (255, 251, 235, 255),
                beam=("Red", False),
                icon=(0, "Red", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#fef08a",
                "#fbbf24",
                (58, 22, 10, 252),
                beam=("Yellow", False),
                icon=(0, "Yellow", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#fed7aa",
                "#fb923c",
                (44, 20, 8, 245),
                beam=("Orange", False),
                icon=(1, "Orange", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#fde68a",
                "#d97706",
                (32, 22, 10, 235),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e7e5e4",
                "#78716c",
                (26, 22, 18, 222),
                beam=None,
                icon=(2, "Brown", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(
                SemanticDropTier.D,
                28,
                "#bdb6ac",
                "#44403c",
                (20, 18, 15, 195),
                beam=None,
                icon=None,
            ),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#93c5fd",
                "#3b82f6",
                (12, 24, 48, 248),
                beam=("Blue", False),
                icon=(0, "Blue", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#ffffff",
                "#f59e0b",
                (48, 28, 8, 248),
                beam=("White", False),
                icon=(0, "White", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#fef3c7",
                "#b45309",
                (32, 22, 12, 230),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#86efac",
                "#22c55e",
                (14, 32, 20, 235),
                beam=("Green", True),
                icon=(1, "Green", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#fde047",
                "#eab308",
                (36, 28, 8, 230),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#d6cfc2", "#44403c", (20, 18, 14, 175)
            ),
        },
    )

    presets["vaal_blood"] = VisualThemeSpec(
        id="vaal_blood",
        name="Vaal Sacrificial Blood",
        description="Corrupted Atziri crimson, sacrificial jade, and gilded Vaal stone.",
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#7f1d1d",
                "#dc2626",
                (255, 240, 240, 255),
                beam=("Red", False),
                icon=(0, "Red", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#fca5a5",
                "#ef4444",
                (52, 8, 12, 252),
                beam=("Red", False),
                icon=(0, "Red", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#fde047",
                "#dc2626",
                (40, 12, 12, 246),
                beam=("Orange", False),
                icon=(1, "Orange", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#fdba74",
                "#b91c1c",
                (32, 14, 14, 236),
                beam=("Red", True),
                icon=(1, "Red", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e5d5d5",
                "#7f1d1d",
                (24, 14, 14, 222),
                icon=(2, "Brown", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#b8a8a8", "#451a1a", (18, 12, 12, 195)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#6ee7b7",
                "#10b981",
                (10, 36, 26, 248),
                beam=("Green", False),
                icon=(0, "Green", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#fecdd3",
                "#f43f5e",
                (46, 10, 20, 248),
                beam=("Pink", False),
                icon=(0, "Pink", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#fda4af",
                "#be123c",
                (30, 12, 18, 232),
                beam=("Red", True),
                icon=(1, "Red", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#bbf7d0",
                "#16a34a",
                (12, 30, 18, 235),
                beam=("Green", True),
                icon=(1, "Green", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#fde047",
                "#ca8a04",
                (34, 24, 8, 230),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#c9baba", "#3f2222", (18, 14, 14, 175)
            ),
        },
    )

    presets["cyberpunk_neon"] = VisualThemeSpec(
        id="cyberpunk_neon",
        name="Cyberpunk Synthwave Neon",
        description=("Hot magenta, laser cyan, and electric lime over pitch-black stealth plates."),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#090a10",
                "#ff007f",
                (0, 245, 255, 255),
                beam=("Pink", False),
                icon=(0, "Pink", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#ff66c4",
                "#00f5ff",
                (10, 8, 22, 252),
                beam=("Pink", False),
                icon=(0, "Cyan", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#00f5ff",
                "#ff007f",
                (8, 14, 26, 248),
                beam=("Cyan", False),
                icon=(1, "Cyan", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#b8ff66",
                "#39ff14",
                (8, 18, 14, 238),
                beam=("Green", True),
                icon=(1, "Green", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#cbd5e1",
                "#6366f1",
                (10, 12, 22, 225),
                icon=(2, "Purple", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#94a3b8", "#334155", (8, 10, 18, 200)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#facc15",
                "#00f5ff",
                (12, 16, 28, 250),
                beam=("Yellow", False),
                icon=(0, "Yellow", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#e879f9",
                "#d946ef",
                (22, 8, 32, 248),
                beam=("Purple", False),
                icon=(0, "Purple", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#c084fc",
                "#9333ea",
                (16, 10, 26, 232),
                beam=("Purple", True),
                icon=(1, "Purple", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#67e8f9",
                "#06b6d4",
                (8, 22, 28, 235),
                beam=("Cyan", True),
                icon=(1, "Cyan", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#fde047",
                "#facc15",
                (20, 18, 8, 232),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#94a3b8", "#1e293b", (8, 10, 16, 180)
            ),
        },
    )

    presets["darkmode_obsidian"] = VisualThemeSpec(
        id="darkmode_obsidian",
        name="Darkmode Obsidian",
        description=(
            "Zero-glare opaque charcoal nameplates (no white backgrounds) with pastel borders."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#ff4d6d",
                "#ffd166",
                (16, 14, 22, 255),
                beam=("Red", False),
                icon=(0, "Red", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=5.5,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#ffd166",
                "#ff9f1c",
                (18, 16, 20, 252),
                beam=("Orange", False),
                icon=(0, "Orange", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#72efdd",
                "#4ea8de",
                (14, 18, 24, 248),
                beam=("Cyan", False),
                icon=(1, "Cyan", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#a8dadc",
                "#457b9d",
                (14, 18, 22, 240),
                beam=("Blue", True),
                icon=(1, "Blue", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#d8dee9",
                "#4c566a",
                (14, 16, 20, 228),
                icon=(2, "Grey", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#aeb6c4", "#2e3440", (12, 14, 18, 205)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#80ed99",
                "#38b000",
                (12, 22, 16, 248),
                beam=("Green", False),
                icon=(0, "Green", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#f8f9fa",
                "#adb5bd",
                (18, 20, 24, 250),
                beam=("White", False),
                icon=(0, "White", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#dee2e6",
                "#6c757d",
                (16, 18, 22, 235),
                beam=("Grey", True),
                icon=(1, "White", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#9ef01a",
                "#70e000",
                (14, 22, 12, 238),
                beam=("Green", True),
                icon=(1, "Green", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#ffe169",
                "#dbb42c",
                (22, 20, 12, 235),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#adb5bd", "#343a40", (12, 14, 16, 180)
            ),
        },
    )

    presets["cobalt_arcane"] = VisualThemeSpec(
        id="cobalt_arcane",
        name="Cobalt Arcane",
        description=(
            "Chronomancer & Stormweaver glacial sapphire, astral silver, and lightning violet."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#0f172a",
                "#2563eb",
                (236, 254, 255, 255),
                beam=("Cyan", False),
                icon=(0, "Cyan", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#7dd3fc",
                "#38bdf8",
                (10, 22, 48, 252),
                beam=("Blue", False),
                icon=(0, "Blue", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#c4b5fd",
                "#8b5cf6",
                (18, 16, 42, 246),
                beam=("Purple", False),
                icon=(1, "Purple", "Circle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#bae6fd",
                "#0284c7",
                (12, 22, 38, 238),
                beam=("Cyan", True),
                icon=(1, "Cyan", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e2e8f0",
                "#475569",
                (14, 20, 32, 225),
                icon=(2, "Blue", "Circle"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#94a3b8", "#334155", (12, 16, 26, 200)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#5eead4",
                "#14b8a6",
                (10, 32, 34, 248),
                beam=("Green", False),
                icon=(0, "Green", "Hexagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#f0f9ff",
                "#38bdf8",
                (14, 28, 52, 248),
                beam=("White", False),
                icon=(0, "White", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#cbd5e1",
                "#64748b",
                (14, 22, 36, 232),
                beam=("Blue", True),
                icon=(1, "Blue", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#6ee7b7",
                "#10b981",
                (10, 28, 26, 235),
                beam=("Green", True),
                icon=(1, "Green", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#fde68a",
                "#eab308",
                (28, 24, 12, 230),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#94a3b8", "#1e293b", (12, 16, 24, 175)
            ),
        },
    )

    presets["zen_minimal"] = VisualThemeSpec(
        id="zen_minimal",
        name="Zen Minimalist",
        description=(
            "Compact typography, translucent routine plates, and distraction-free mapping."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                42,
                "#18181b",
                "#f43f5e",
                (250, 250, 249, 250),
                beam=("Red", False),
                icon=(0, "Red", "Star"),
                sound_id=6,
                vol=280,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                40,
                "#fde68a",
                "#f59e0b",
                (24, 22, 20, 235),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Diamond"),
                sound_id=1,
                vol=250,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                36,
                "#a7f3d0",
                "#34d399",
                (18, 24, 22, 215),
                beam=("Green", True),
                icon=(1, "Green", "Circle"),
                sound_id=2,
                vol=210,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                32,
                "#e4e4e7",
                "#71717a",
                (18, 18, 22, 190),
                beam=None,
                icon=(2, "White", "Circle"),
                sound_id=12,
                vol=150,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                28,
                "#d4d4d8",
                "#3f3f46",
                (16, 16, 18, 165),
                beam=None,
                icon=None,
                sound_id=None,
                vol=0,
            ),
            "d": _mk_tier(
                SemanticDropTier.D,
                22,
                "#a1a1aa",
                "#27272a",
                (14, 14, 16, 140),
                beam=None,
                icon=None,
                sound_id=None,
                vol=0,
            ),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                38,
                "#67e8f9",
                "#22d3ee",
                (14, 26, 30, 225),
                beam=("Cyan", True),
                icon=(1, "Cyan", "Hexagon"),
                sound_id=5,
                vol=230,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                38,
                "#fef3c7",
                "#d97706",
                (26, 22, 16, 225),
                beam=("Orange", True),
                icon=(1, "Orange", "Square"),
                sound_id=4,
                vol=230,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                32,
                "#e5e7eb",
                "#6b7280",
                (18, 18, 20, 190),
                beam=None,
                icon=(2, "White", "Square"),
                sound_id=4,
                vol=160,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                34,
                "#bbf7d0",
                "#22c55e",
                (16, 24, 18, 200),
                beam=None,
                icon=(2, "Green", "Kite"),
                sound_id=7,
                vol=180,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH, 30, "#fde68a", "#a16207", (22, 20, 12, 180)
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 20, "#a1a1aa", "#27272a", (14, 14, 16, 120)
            ),
        },
    )

    presets["colorblind_safe"] = VisualThemeSpec(
        id="colorblind_safe",
        name="Okabe-Ito Colorblind Safe",
        description=(
            "Deuteranopia/protanopia/tritanopia-safe luminance + distinct minimap geometry."
        ),
        tiers={
            "s_plus": _mk_tier(
                SemanticDropTier.S_PLUS,
                45,
                "#000000",
                "#0072b2",
                (240, 228, 66, 255),
                beam=("Yellow", False),
                icon=(0, "Yellow", "Star"),
                sound_id=6,
                vol=300,
                min_contrast=7.0,
            ),
            "s": _mk_tier(
                SemanticDropTier.S,
                44,
                "#ffb703",
                "#fb8500",
                (10, 24, 56, 252),
                beam=("Orange", False),
                icon=(0, "Orange", "Diamond"),
                sound_id=1,
                vol=300,
                min_contrast=7.0,
            ),
            "a": _mk_tier(
                SemanticDropTier.A,
                41,
                "#56b4e9",
                "#00b4d8",
                (8, 18, 30, 248),
                beam=("Cyan", False),
                icon=(1, "Cyan", "Triangle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#f5d0fe",
                "#a855f7",
                (48, 16, 96, 246),
                beam=("Pink", True),
                icon=(1, "Pink", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e2e8f0",
                "#64748b",
                (16, 18, 22, 225),
                icon=(2, "Grey", "Hexagon"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#94a3b8", "#334155", (10, 12, 16, 195)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#7dd3fc",
                "#38bdf8",
                (6, 48, 108, 252),
                beam=("Blue", False),
                icon=(0, "Blue", "Pentagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#ffffff",
                "#ffffff",
                (34, 44, 64, 250),
                beam=("White", False),
                icon=(0, "White", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#fed7aa",
                "#d97706",
                (36, 20, 10, 234),
                beam=("Orange", True),
                icon=(1, "Orange", "Moon"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#fef08a",
                "#c084fc",
                (36, 12, 68, 240),
                beam=("Pink", True),
                icon=(1, "Pink", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#fde047",
                "#a16207",
                (46, 38, 6, 234),
                beam=("Yellow", True),
                icon=(1, "Yellow", "Cross"),
            ),
            "gold_low": _mk_tier(
                SemanticDropTier.GOLD_LOW, 24, "#cbd5e1", "#334155", (14, 16, 20, 175)
            ),
        },
    )

    return presets


BUILTIN_THEME_PRESETS: dict[str, VisualThemeSpec] = _build_builtin_presets()

_NAMED_SWATCHES: dict[str, str] = {
    "pink": "#ff4da6",
    "hot pink": "#ff007f",
    "magenta": "#ff0099",
    "cyan": "#00f5ff",
    "teal": "#14b8a6",
    "emerald": "#34d399",
    "green": "#4ade80",
    "lime": "#a3e635",
    "gold": "#fbbf24",
    "yellow": "#fde047",
    "amber": "#f59e0b",
    "orange": "#fb923c",
    "crimson": "#ef4444",
    "red": "#f87171",
    "purple": "#c084fc",
    "violet": "#a78bfa",
    "blue": "#60a5fa",
    "sapphire": "#38bdf8",
    "silver": "#e2e8f0",
    "white": "#f8fafc",
    "ivory": "#fffbeb",
    "pitch black": "#06070b",
    "obsidian": "#0e1017",
    "粉红": "#ff4da6",
    "品红": "#ff0099",
    "洋红": "#ff0099",
    "青色": "#00f5ff",
    "电青": "#00f5ff",
    "蓝绿": "#14b8a6",
    "翠绿": "#34d399",
    "翡翠": "#34d399",
    "荧光绿": "#a3e635",
    "金色": "#fbbf24",
    "明黄": "#fde047",
    "琥珀": "#f59e0b",
    "橙色": "#fb923c",
    "猩红": "#ef4444",
    "紫色": "#c084fc",
    "紫罗兰": "#a78bfa",
    "冰蓝": "#38bdf8",
    "宝石蓝": "#38bdf8",
    "银白": "#e2e8f0",
    "纯黑": "#06070b",
    "黑曜石": "#0e1017",
}

_CVD_PROMPT_KEYWORDS: tuple[str, ...] = (
    "colorblind",
    "colourblind",
    "deuteranopia",
    "protanopia",
    "tritanopia",
    "red-green",
    "red green",
    "cvd",
    "okabe",
    "accessible",
    "红绿色盲",
    "绿色盲",
    "红色盲",
    "蓝黄色盲",
    "色盲",
    "色弱",
    "无障碍",
)

_PROMPT_PRESET_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (_CVD_PROMPT_KEYWORDS, "colorblind_safe"),
    (
        ("synthwave", "cyberpunk", "neon", "vaporwave", "laser", "赛博", "霓虹", "蒸汽波", "激光"),
        "cyberpunk_neon",
    ),
    (
        (
            "zen",
            "minimal",
            "calm",
            "subtle",
            "clean",
            "low clutter",
            "极简",
            "禅意",
            "清爽",
            "克制",
            "低调",
        ),
        "zen_minimal",
    ),
    (
        ("vaal", "blood", "crimson", "atziri", "sacrifice", "瓦尔", "猩红", "血红", "鲜血", "祭祀"),
        "vaal_blood",
    ),
    (
        (
            "solaris",
            "royal",
            "ivory",
            "sun",
            "golden",
            "太阳神",
            "圣金",
            "皇家金",
            "耀金",
            "象牙白",
        ),
        "solaris_gold",
    ),
    (
        (
            "cobalt",
            "ice",
            "frost",
            "glacial",
            "sapphire",
            "storm",
            "钴蓝",
            "奥术",
            "冰蓝",
            "寒霜",
            "星界",
        ),
        "cobalt_arcane",
    ),
    (
        (
            "darkmode",
            "dark mode",
            "obsidian",
            "matte",
            "no white",
            "暗黑",
            "黑曜石",
            "护眼暗黑",
            "哑光黑",
            "不要白底",
            "去白底",
        ),
        "darkmode_obsidian",
    ),
)

_TIER_ALIASES: dict[str, str] = {
    "divine": "s_plus",
    "divines": "s_plus",
    "mirror": "s_plus",
    "jackpot": "s_plus",
    "神圣": "s_plus",
    "镜子": "s_plus",
    "大奖": "s_plus",
    "s+": "s_plus",
    "s-plus": "s_plus",
    "s_plus": "s_plus",
    "s-tier": "s",
    "s tier": "s",
    "a-tier": "a",
    "a tier": "a",
    "b-tier": "b",
    "b tier": "b",
    "c-tier": "c",
    "c tier": "c",
    "d-tier": "d",
    "d tier": "d",
    "build": "build_upgrade",
    "pob": "build_upgrade",
    "流派": "build_upgrade",
    "构筑": "build_upgrade",
    "waystone": "waystone_high",
    "waystones": "waystone_high",
    "引路石": "waystone_high",
    "异界地图": "waystone_high",
}


def list_visual_themes() -> list[VisualThemeSpec]:
    return list(BUILTIN_THEME_PRESETS.values())


def get_visual_theme(theme_id: str) -> VisualThemeSpec:
    key = theme_id.strip().lower().replace("-", "_")
    if key not in BUILTIN_THEME_PRESETS:
        valid = ", ".join(sorted(BUILTIN_THEME_PRESETS))
        raise ValueError(f"Unknown visual theme {theme_id!r}. Available presets: {valid}")
    return BUILTIN_THEME_PRESETS[key].model_copy(deep=True)


def _extract_colors_from_clause(clause: str) -> list[RgbaColor]:
    positioned: list[tuple[int, int, RgbaColor]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= ps or start >= pe) for ps, pe, _ in positioned)

    for hex_m in re.finditer(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b", clause):
        s, e = hex_m.span()
        positioned.append((s, e, RgbaColor.from_any(hex_m.group(0))))

    lower = clause.lower()
    for name in sorted(_NAMED_SWATCHES, key=len, reverse=True):
        pattern = rf"\b{re.escape(name)}\b" if name.isascii() else re.escape(name)
        for m in re.finditer(pattern, lower):
            s, e = m.span()
            if not _overlaps(s, e):
                positioned.append((s, e, RgbaColor.from_any(_NAMED_SWATCHES[name])))

    positioned.sort(key=lambda item: item[0])
    return [col for _, _, col in positioned]


def _enforce_cvd_theme_hygiene(theme: VisualThemeSpec) -> None:
    """Eliminate Red/Green beam-icon pairs and ensure unique minimap geometry across tiers."""
    used_shapes: set[str] = set()
    fallback_shapes = [sh for sh in VALID_SHAPES]

    for tk in _CVD_AUDIT_TIERS:
        if tk not in theme.tiers:
            continue
        st = theme.tiers[tk]
        if st.beam is not None and st.beam.color in _CVD_SAFE_COLOR_REMAP:
            st.beam = BeamSpec(color=_CVD_SAFE_COLOR_REMAP[st.beam.color], temp=st.beam.temp)
        if st.minimap_icon is not None and st.minimap_icon.size >= 0:
            safe_col = _CVD_SAFE_COLOR_REMAP.get(st.minimap_icon.color, st.minimap_icon.color)
            shape = st.minimap_icon.shape
            if shape in used_shapes:
                shape = next((cand for cand in fallback_shapes if cand not in used_shapes), shape)
            used_shapes.add(shape)
            st.minimap_icon = MinimapIconSpec(
                size=st.minimap_icon.size,
                color=safe_col,
                shape=shape,
            )


def compose_theme(
    style_prompt: str | None = None,
    *,
    base_preset: str | None = None,
    theme_id: str | None = None,
    primary_color: str | tuple[int, ...] | RgbaColor | None = None,
    accent_color: str | tuple[int, ...] | RgbaColor | None = None,
    background_color: str | tuple[int, ...] | RgbaColor | None = None,
    font_scale: float = 1.0,
    min_contrast: float = 4.5,
    beam_mode: str | None = None,
    cvd_safe: bool | ColorVisionDeficiency | str | None = None,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> VisualThemeSpec:
    """Compose a WCAG-verified PoE2 visual theme from natural-language prompts and overrides."""
    prompt_lower = (style_prompt or "").strip().lower()
    wants_cvd = bool(cvd_safe) or any(kw in prompt_lower for kw in _CVD_PROMPT_KEYWORDS)

    chosen_base = base_preset
    if not chosen_base and wants_cvd:
        chosen_base = "colorblind_safe"
    if not chosen_base and prompt_lower:
        for kw_group, preset_key in _PROMPT_PRESET_KEYWORDS:
            if any(kw in prompt_lower for kw in kw_group):
                chosen_base = preset_key
                break
    chosen_base = chosen_base or "forbidden_abyss"
    if chosen_base == "colorblind_safe":
        wants_cvd = True
    theme = get_visual_theme(chosen_base)

    if style_prompt:
        ascii_slug = re.sub(r"[^a-z0-9]+", "_", prompt_lower).strip("_")[:28]
        ascii_letters = sum(ch.isalpha() for ch in ascii_slug)
        slug = ascii_slug if ascii_letters >= 3 else chosen_base
        theme.id = theme_id or f"custom_{slug}"
        theme.name = f"Custom ({style_prompt.strip()[:48]})"
        theme.description = f"Agent-composed theme from '{chosen_base}': {style_prompt.strip()}"
        theme.base_preset = chosen_base
        theme.style_prompt = style_prompt.strip()
    elif theme_id:
        theme.id = theme_id

    theme.min_contrast_ratio = min_contrast

    # Parse global background & palette cues from prompt
    bg_override: RgbaColor | None = (
        RgbaColor.from_any(background_color) if background_color is not None else None
    )
    if bg_override is None:
        if any(k in prompt_lower for k in ("pitch black", "pure black", "纯黑", "深黑")):
            bg_override = RgbaColor(r=6, g=7, b=11, a=250)
        elif any(k in prompt_lower for k in ("obsidian", "黑曜石", "暗黑")):
            bg_override = RgbaColor(r=14, g=16, b=23, a=250)

    if bg_override is not None:
        no_white = any(k in prompt_lower for k in ("no white", "不要白底", "去白底", "无白底"))
        for t_key, style in theme.tiers.items():
            # Keep inverted S+ background unless pitch-black / darkmode explicitly requested
            if t_key == "s_plus" and not no_white and background_color is None:
                continue
            if wants_cvd and background_color is None:
                # Blend 45% toward obsidian/pitch-black to keep dark comfort + CVD plate separation
                style.background_color = RgbaColor(
                    r=int(round(style.background_color.r * 0.55 + bg_override.r * 0.45)),
                    g=int(round(style.background_color.g * 0.55 + bg_override.g * 0.45)),
                    b=int(round(style.background_color.b * 0.55 + bg_override.b * 0.45)),
                    a=min(style.background_color.a, bg_override.a),
                )
            else:
                style.background_color = RgbaColor(
                    r=bg_override.r,
                    g=bg_override.g,
                    b=bg_override.b,
                    a=min(style.background_color.a, bg_override.a),
                )

    prim = RgbaColor.from_any(primary_color) if primary_color is not None else None
    acc = RgbaColor.from_any(accent_color) if accent_color is not None else None

    if prompt_lower and (prim is None or acc is None):
        # Extract global swatches from non-tier-specific clauses (ignoring "red-green" / "红绿色盲")
        global_clauses: list[str] = []
        for clause in re.split(r"[,;，；]", prompt_lower):
            has_tier = any(
                re.search(rf"\b{re.escape(al)}\b" if al.isascii() else re.escape(al), clause)
                for al in _TIER_ALIASES
            )
            if not has_tier:
                global_clauses.append(clause)
        joined_global = " ".join(global_clauses)
        sanitized_clause = re.sub(r"\bred[\s-]+green\b|红绿色盲|蓝黄色盲", "", joined_global)
        swatches = [
            c
            for c in _extract_colors_from_clause(sanitized_clause)
            if (c.r + c.g + c.b) > 70  # skip pitch-black / obsidian swatches as foreground
        ]
        if wants_cvd:
            # Filter out red/green confusion swatches unless primary_color was explicit
            swatches = [c for c in swatches if _nearest_poe_color_name(c) not in ("Red", "Green")]
        if prim is None and len(swatches) >= 1:
            prim = swatches[0]
        if acc is None and len(swatches) >= 2:
            acc = swatches[1]

    if prim is not None:
        theme.tiers["s"].text_color = prim
        theme.tiers["s_plus"].border_color = prim
        beam_col = _nearest_poe_color_name(prim)
        theme.tiers["s"].beam = BeamSpec(color=beam_col, temp=False)
        s_shape = (
            theme.tiers["s"].minimap_icon.shape if theme.tiers["s"].minimap_icon else "Diamond"
        )
        theme.tiers["s"].minimap_icon = MinimapIconSpec(size=0, color=beam_col, shape=s_shape)

    if acc is not None:
        theme.tiers["a"].text_color = acc
        theme.tiers["a"].border_color = acc
        theme.tiers["s"].border_color = acc
        beam_col = _nearest_poe_color_name(acc)
        theme.tiers["a"].beam = BeamSpec(color=beam_col, temp=False)
        a_shape = theme.tiers["a"].minimap_icon.shape if theme.tiers["a"].minimap_icon else "Circle"
        theme.tiers["a"].minimap_icon = MinimapIconSpec(size=1, color=beam_col, shape=a_shape)

    # Clause-level tier directives (e.g. "huge text on divines", "subtle muted yellows on b-tier")
    if prompt_lower:
        for clause in re.split(r"[,;，；、]|(?:\band\b(?=[^,]*\bon\b))", prompt_lower):
            matched_tier: str | None = None
            for alias, canonical in _TIER_ALIASES.items():
                pat = rf"\b{re.escape(alias)}\b" if alias.isascii() else re.escape(alias)
                if re.search(pat, clause):
                    matched_tier = canonical
                    break
            if matched_tier and matched_tier in theme.tiers:
                st = theme.tiers[matched_tier]
                clause_colors = [
                    c for c in _extract_colors_from_clause(clause) if (c.r + c.g + c.b) > 70
                ]
                if clause_colors:
                    col = clause_colors[0]
                    if any(w in clause for w in ("muted", "subtle", "柔和", "淡化", "低调")):
                        h, l_v, s_v = colorsys.rgb_to_hls(
                            col.r / 255.0, col.g / 255.0, col.b / 255.0
                        )
                        rf, gf, bf = colorsys.hls_to_rgb(h, min(0.76, l_v), s_v * 0.55)
                        col = RgbaColor(r=int(rf * 255), g=int(gf * 255), b=int(bf * 255), a=255)
                    st.text_color = col
                    st.border_color = col
                    b_name = _nearest_poe_color_name(col)
                    if st.beam is not None:
                        st.beam = BeamSpec(color=b_name, temp=st.beam.temp)
                    if st.minimap_icon is not None:
                        st.minimap_icon = MinimapIconSpec(
                            size=st.minimap_icon.size,
                            color=b_name,
                            shape=st.minimap_icon.shape,
                        )
                if any(w in clause for w in ("huge", "massive", "giant", "max", "超大", "大字")):
                    st.font_size = 45
                elif any(
                    w in clause
                    for w in ("small", "tiny", "subtle", "compact", "小字", "低调", "淡化")
                ):
                    st.font_size = max(20, st.font_size - 5)
                    if any(w in clause for w in ("subtle", "muted", "低调", "淡化")):
                        st.beam = None

        no_beams = any(k in prompt_lower for k in ("no beams", "无光柱", "不要光柱"))
        if no_beams or beam_mode == "none":
            for st in theme.tiers.values():
                st.beam = None
        elif beam_mode == "all_temp":
            for st in theme.tiers.values():
                if st.beam is not None:
                    st.beam = BeamSpec(color=st.beam.color, temp=True)

    # Apply font_scale if != 1.0
    if abs(font_scale - 1.0) > 1e-6:
        for st in theme.tiers.values():
            st.font_size = max(18, min(45, int(round(st.font_size * font_scale))))

    # Apply explicit structured per-tier overrides
    if overrides:
        for tier_key, patch in overrides.items():
            norm_key = _TIER_ALIASES.get(tier_key.lower(), tier_key.lower())
            if norm_key not in theme.tiers:
                continue
            st = theme.tiers[norm_key]
            if "font_size" in patch:
                st.font_size = max(18, min(45, int(patch["font_size"])))
            if "text_color" in patch:
                st.text_color = RgbaColor.from_any(patch["text_color"])
            if "border_color" in patch:
                st.border_color = RgbaColor.from_any(patch["border_color"])
            if "background_color" in patch:
                st.background_color = RgbaColor.from_any(patch["background_color"])
            if "sound_volume" in patch:
                st.sound_volume = max(0, min(300, int(patch["sound_volume"])))
            if "beam" in patch:
                b_raw = patch["beam"]
                if b_raw is None:
                    st.beam = None
                elif isinstance(b_raw, Mapping):
                    c_name = str(b_raw["color"])
                    if c_name not in VALID_COLOR_SET:
                        raise ValueError(f"Invalid beam color {c_name!r}")
                    st.beam = BeamSpec(color=c_name, temp=bool(b_raw.get("temp", False)))
            if "minimap_icon" in patch:
                i_raw = patch["minimap_icon"]
                if i_raw is None:
                    st.minimap_icon = None
                elif isinstance(i_raw, Mapping):
                    c_name = str(i_raw["color"])
                    sh_name = str(i_raw["shape"])
                    if c_name not in VALID_COLOR_SET or sh_name not in VALID_SHAPE_SET:
                        raise ValueError(f"Invalid minimap_icon spec {i_raw!r}")
                    st.minimap_icon = MinimapIconSpec(
                        size=int(i_raw.get("size", 1)),
                        color=c_name,
                        shape=sh_name,
                    )

    # Enforce monotonic font hierarchy S+ >= S >= A >= B >= C >= D (unless explicitly overridden)
    overridden_fonts = {
        _TIER_ALIASES.get(k.lower(), k.lower())
        for k, v in (overrides or {}).items()
        if isinstance(v, Mapping) and "font_size" in v
    }
    ceil_font = 45
    for tier_enum in TIER_HIERARCHY:
        tk = tier_enum.value
        if tk not in theme.tiers:
            continue
        st = theme.tiers[tk]
        if tk not in overridden_fonts and st.font_size > ceil_font:
            st.font_size = ceil_font
        ceil_font = st.font_size

    if wants_cvd:
        _enforce_cvd_theme_hygiene(theme)

    # Final pass: enforce WCAG contrast on every single tier
    for tk, st in theme.tiers.items():
        target_ratio = max(min_contrast, 6.0 if tk in ("s_plus", "s") else min_contrast)
        adj_fg, actual_ratio = ensure_wcag_contrast(
            st.text_color, st.background_color, min_ratio=target_ratio
        )
        st.text_color = adj_fg
        st.contrast_ratio = actual_ratio

    return theme


def parse_look_intent(look: str | None) -> dict[str, Any]:
    """Extract optional accessibility (`cvd_safe`) and audio (`warm_night_mode`, `sound_style`,
    `voice_language`) directives embedded inside a natural-language `--look` prompt.
    """
    if not look or not look.strip():
        return {
            "cvd_safe": None,
            "warm_night_mode": False,
            "sound_style": None,
            "voice_language": "en",
        }

    low = look.strip().lower()

    cvd_safe: str | bool | None = None
    if any(
        k in low
        for k in ("deuteranopia", "deutan", "red-green", "red green", "红绿色盲", "绿色盲")
    ):
        cvd_safe = "deuteranopia"
    elif any(k in low for k in ("protanopia", "protan", "红色盲")):
        cvd_safe = "protanopia"
    elif any(k in low for k in ("tritanopia", "tritan", "blue-yellow", "blue yellow", "蓝黄色盲")):
        cvd_safe = "tritanopia"
    elif any(k in low for k in ("colorblind", "colourblind", "color-blind", "cvd", "色盲", "色弱")):
        cvd_safe = True

    warm_night_mode = any(
        k in low
        for k in (
            "2am",
            "2 am",
            "late night",
            "late-night",
            "warm night",
            "warm-night",
            "tinnitus",
            "soft audio",
            "no harsh highs",
            "ear safe",
            "ear-safe",
            "护耳",
            "凌晨",
            "深夜",
            "半夜",
            "不刺耳",
            "防耳鸣",
            "柔和音效",
            "低频",
        )
    )

    sound_style: str | None = None
    if any(
        k in low
        for k in (
            "voice",
            "callout",
            "spoken",
            "tts",
            "语音",
            "播报",
            "人声",
            "好运来",
            "大货播报",
        )
    ):
        sound_style = "voice_callout"
    elif any(k in low for k in ("gong", "ritual gong", "bronze bell", "铜锣", "古锣", "祭坛")):
        sound_style = "ritual_gong"
    elif any(
        k in low
        for k in (
            "synthwave",
            "cyber synth",
            "cyber_synth",
            "synth audio",
            "电子音效",
            "电音",
            "合成器",
            "赛博音效",
        )
    ):
        sound_style = "cyber_synth"
    elif any(
        k in low for k in ("arcade", "chiptune", "8-bit", "8bit", "retro audio", "像素", "街机")
    ):
        sound_style = "arcade_retro"
    elif any(
        k in low
        for k in (
            "zen minimal",
            "zen_minimal",
            "rhodes",
            "piano",
            "soft chime",
            "钢琴",
            "电钢",
            "轻柔",
        )
    ):
        sound_style = "zen_minimal"
    elif any(k in low for k in ("chime", "celestial", "bell", "风铃", "清脆", "星铃", "水晶")):
        sound_style = "celestial_chime"

    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in look)
    voice_language = (
        "zh"
        if any(k in low for k in ("中文语音", "中文播报", "国语", "汉语", "好运来", "大货播报"))
        or (sound_style == "voice_callout" and has_cjk)
        else "en"
    )

    return {
        "cvd_safe": cvd_safe,
        "warm_night_mode": warm_night_mode,
        "sound_style": sound_style,
        "voice_language": voice_language,
    }
