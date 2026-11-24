from __future__ import annotations

import colorsys
import re
from collections.abc import Mapping
from typing import Any

from poe2brain.filters.grammar import VALID_COLOR_SET, VALID_SHAPE_SET
from poe2brain.models.filters import (
    BeamSpec,
    MinimapIconSpec,
    RgbaColor,
    SemanticDropTier,
    TierVisualStyle,
    VisualThemeSpec,
)

WRAECLAST_GROUND_RGB: tuple[int, int, int] = (18, 20, 26)

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
                "#f0e442",
                "#e69f00",
                (12, 20, 36, 252),
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
                "#56b4e9",
                (10, 18, 30, 248),
                beam=("Cyan", False),
                icon=(1, "Cyan", "Triangle"),
                sound_id=2,
                vol=300,
            ),
            "b": _mk_tier(
                SemanticDropTier.B,
                37,
                "#ffffff",
                "#cc79a7",
                (24, 14, 28, 238),
                beam=("Pink", True),
                icon=(1, "Pink", "Circle"),
                sound_id=3,
                vol=240,
            ),
            "c": _mk_tier(
                SemanticDropTier.C,
                33,
                "#e5e7eb",
                "#6b7280",
                (16, 18, 22, 225),
                icon=(2, "White", "Hexagon"),
                sound_id=12,
                vol=170,
            ),
            "d": _mk_tier(SemanticDropTier.D, 28, "#b0b7c3", "#374151", (14, 16, 20, 200)),
            "build_upgrade": _mk_tier(
                SemanticDropTier.BUILD_UPGRADE,
                41,
                "#ffbe55",
                "#e69f00",
                (28, 18, 8, 248),
                beam=("Blue", False),
                icon=(0, "Blue", "Pentagon"),
                sound_id=5,
                vol=300,
            ),
            "waystone_high": _mk_tier(
                SemanticDropTier.WAYSTONE_HIGH,
                42,
                "#ffffff",
                "#56b4e9",
                (12, 24, 42, 248),
                beam=("White", False),
                icon=(0, "White", "Square"),
                sound_id=4,
                vol=300,
            ),
            "waystone_prog": _mk_tier(
                SemanticDropTier.WAYSTONE_PROG,
                36,
                "#dbeafe",
                "#0072b2",
                (12, 20, 34, 232),
                beam=("Blue", True),
                icon=(1, "Blue", "Square"),
                sound_id=4,
                vol=220,
            ),
            "chancing": _mk_tier(
                SemanticDropTier.CHANCING,
                38,
                "#f0e442",
                "#cc79a7",
                (24, 16, 26, 235),
                beam=("Pink", True),
                icon=(1, "Pink", "Kite"),
                sound_id=7,
                vol=250,
            ),
            "gold_high": _mk_tier(
                SemanticDropTier.GOLD_HIGH,
                36,
                "#f0e442",
                "#e69f00",
                (26, 22, 10, 232),
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
}

_PROMPT_PRESET_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("synthwave", "cyberpunk", "neon", "vaporwave", "laser"), "cyberpunk_neon"),
    (("colorblind", "deuteranopia", "protanopia", "okabe", "accessible"), "colorblind_safe"),
    (("zen", "minimal", "calm", "subtle", "clean", "low clutter"), "zen_minimal"),
    (("vaal", "blood", "crimson", "atziri", "sacrifice"), "vaal_blood"),
    (("solaris", "royal", "ivory", "sun", "golden"), "solaris_gold"),
    (("cobalt", "ice", "frost", "glacial", "sapphire", "storm"), "cobalt_arcane"),
    (("darkmode", "dark mode", "obsidian", "matte", "no white"), "darkmode_obsidian"),
)

_TIER_ALIASES: dict[str, str] = {
    "divine": "s_plus",
    "divines": "s_plus",
    "mirror": "s_plus",
    "jackpot": "s_plus",
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
    "waystone": "waystone_high",
    "waystones": "waystone_high",
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
    found: list[RgbaColor] = []
    for hex_m in re.finditer(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b", clause):
        found.append(RgbaColor.from_any(hex_m.group(0)))
    lower = clause.lower()
    for name in sorted(_NAMED_SWATCHES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", lower):
            found.append(RgbaColor.from_any(_NAMED_SWATCHES[name]))
    return found


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
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> VisualThemeSpec:
    """Compose a WCAG-verified PoE2 visual theme from natural-language prompts and overrides."""
    prompt_lower = (style_prompt or "").strip().lower()

    chosen_base = base_preset
    if not chosen_base and prompt_lower:
        for kw_group, preset_key in _PROMPT_PRESET_KEYWORDS:
            if any(kw in prompt_lower for kw in kw_group):
                chosen_base = preset_key
                break
    chosen_base = chosen_base or "forbidden_abyss"
    theme = get_visual_theme(chosen_base)

    if style_prompt:
        slug = re.sub(r"[^a-z0-9]+", "_", prompt_lower).strip("_")[:28] or "custom"
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
    if bg_override is None and ("pitch black" in prompt_lower or "pure black" in prompt_lower):
        bg_override = RgbaColor(r=6, g=7, b=11, a=250)

    if bg_override is not None:
        for t_key, style in theme.tiers.items():
            # Keep inverted S+ background unless pitch-black / darkmode explicitly requested
            if t_key == "s_plus" and "no white" not in prompt_lower and background_color is None:
                continue
            style.background_color = RgbaColor(
                r=bg_override.r,
                g=bg_override.g,
                b=bg_override.b,
                a=min(style.background_color.a, bg_override.a),
            )

    prim = RgbaColor.from_any(primary_color) if primary_color is not None else None
    acc = RgbaColor.from_any(accent_color) if accent_color is not None else None

    if prompt_lower and (prim is None or acc is None):
        first_clause = re.split(r"[,;]", prompt_lower)[0]
        swatches = [
            c
            for c in _extract_colors_from_clause(first_clause)
            if (c.r + c.g + c.b) > 45  # skip pitch-black swatch as foreground
        ]
        if prim is None and len(swatches) >= 1:
            prim = swatches[0]
        if acc is None and len(swatches) >= 2:
            acc = swatches[1]

    if prim is not None:
        theme.tiers["s"].text_color = prim
        theme.tiers["s_plus"].border_color = prim
        beam_col = _nearest_poe_color_name(prim)
        theme.tiers["s"].beam = BeamSpec(color=beam_col, temp=False)
        theme.tiers["s"].minimap_icon = MinimapIconSpec(size=0, color=beam_col, shape="Diamond")

    if acc is not None:
        theme.tiers["a"].text_color = acc
        theme.tiers["a"].border_color = acc
        theme.tiers["s"].border_color = acc
        beam_col = _nearest_poe_color_name(acc)
        theme.tiers["a"].beam = BeamSpec(color=beam_col, temp=False)
        theme.tiers["a"].minimap_icon = MinimapIconSpec(size=1, color=beam_col, shape="Circle")

    # Clause-level tier directives (e.g. "huge text on divines", "subtle muted yellows on b-tier")
    if prompt_lower:
        for clause in re.split(r"[,;]|(?:\band\b(?=[^,]*\bon\b))", prompt_lower):
            matched_tier: str | None = None
            for alias, canonical in _TIER_ALIASES.items():
                if re.search(rf"\b{re.escape(alias)}\b", clause):
                    matched_tier = canonical
                    break
            if matched_tier and matched_tier in theme.tiers:
                st = theme.tiers[matched_tier]
                clause_colors = [
                    c for c in _extract_colors_from_clause(clause) if (c.r + c.g + c.b) > 45
                ]
                if clause_colors:
                    col = clause_colors[0]
                    if "muted" in clause or "subtle" in clause:
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
                if any(w in clause for w in ("huge", "massive", "giant", "max")):
                    st.font_size = 45
                elif any(w in clause for w in ("small", "tiny", "subtle", "compact")):
                    st.font_size = max(20, st.font_size - 5)
                    if "subtle" in clause or "muted" in clause:
                        st.beam = None

        if "no beams" in prompt_lower or beam_mode == "none":
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

    # Final pass: enforce WCAG contrast on every single tier
    for tk, st in theme.tiers.items():
        target_ratio = max(min_contrast, 6.0 if tk in ("s_plus", "s") else min_contrast)
        adj_fg, actual_ratio = ensure_wcag_contrast(
            st.text_color, st.background_color, min_ratio=target_ratio
        )
        st.text_color = adj_fg
        st.contrast_ratio = actual_ratio

    return theme
