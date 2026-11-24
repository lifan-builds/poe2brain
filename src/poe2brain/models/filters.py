from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH
from poe2brain.models.common import Poe2Model


class StrictnessTier(StrEnum):
    SOFT = "soft"
    REGULAR = "regular"
    SEMI_STRICT = "semi_strict"
    STRICT = "strict"
    VERY_STRICT = "very_strict"
    UBER_STRICT = "uber_strict"


class SemanticDropTier(StrEnum):
    S_PLUS = "s_plus"
    S = "s"
    A = "a"
    B = "b"
    C = "c"
    D = "d"
    BUILD_UPGRADE = "build_upgrade"
    WAYSTONE_HIGH = "waystone_high"
    WAYSTONE_PROG = "waystone_prog"
    CHANCING = "chancing"
    GOLD_HIGH = "gold_high"
    GOLD_LOW = "gold_low"
    HIDE = "hide"


class SoundPackStyle(StrEnum):
    CELESTIAL_CHIME = "celestial_chime"
    RITUAL_GONG = "ritual_gong"
    CYBER_SYNTH = "cyber_synth"
    ARCADE_RETRO = "arcade_retro"
    ZEN_MINIMAL = "zen_minimal"
    VOICE_CALLOUT = "voice_callout"


class RgbaColor(Poe2Model):
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)
    a: int = Field(default=255, ge=0, le=255)

    def to_filter_tokens(self) -> str:
        return f"{self.r} {self.g} {self.b} {self.a}"

    def to_css_rgba(self) -> str:
        alpha = round(self.a / 255.0, 3)
        return f"rgba({self.r}, {self.g}, {self.b}, {alpha})"

    def to_hex(self) -> str:
        if self.a == 255:
            return f"#{self.r:02x}{self.g:02x}{self.b:02x}"
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}{self.a:02x}"

    @classmethod
    def from_any(
        cls, value: RgbaColor | str | tuple[int, ...] | list[int] | dict[str, Any]
    ) -> RgbaColor:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        if isinstance(value, (tuple, list)):
            if len(value) == 3:
                return cls(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=255)
            if len(value) == 4:
                return cls(r=int(value[0]), g=int(value[1]), b=int(value[2]), a=int(value[3]))
            raise ValueError(f"Expected 3 or 4 channel color sequence, got {value!r}")
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("#"):
                h = s[1:]
                if len(h) == 3:
                    h = "".join(ch * 2 for ch in h)
                if len(h) == 6:
                    return cls(r=int(h[0:2], 16), g=int(h[2:4], 16), b=int(h[4:6], 16), a=255)
                if len(h) == 8:
                    return cls(
                        r=int(h[0:2], 16),
                        g=int(h[2:4], 16),
                        b=int(h[4:6], 16),
                        a=int(h[6:8], 16),
                    )
                raise ValueError(f"Invalid hex color {value!r}")
            parts = [int(p) for p in s.replace(",", " ").split() if p]
            if len(parts) in (3, 4):
                return cls.from_any(parts)
        raise ValueError(f"Cannot parse color {value!r}")


class BeamSpec(Poe2Model):
    color: str
    temp: bool = False

    def to_filter_line(self) -> str:
        return f"PlayEffect {self.color} Temp" if self.temp else f"PlayEffect {self.color}"


class MinimapIconSpec(Poe2Model):
    size: int = Field(ge=-1, le=2)
    color: str
    shape: str

    def to_filter_line(self) -> str:
        if self.size == -1:
            return "MinimapIcon -1"
        return f"MinimapIcon {self.size} {self.color} {self.shape}"


class TierVisualStyle(Poe2Model):
    tier: SemanticDropTier
    font_size: int = Field(default=32, ge=18, le=45)
    text_color: RgbaColor
    border_color: RgbaColor
    background_color: RgbaColor
    beam: BeamSpec | None = None
    minimap_icon: MinimapIconSpec | None = None
    builtin_sound_id: int | str | None = None
    sound_volume: int = Field(default=300, ge=0, le=300)
    contrast_ratio: float = 0.0

    @field_validator("font_size")
    @classmethod
    def _clamp_poe2_font(cls, v: int) -> int:
        if not (18 <= v <= 45):
            raise ValueError(f"PoE2 SetFontSize must be in [18, 45], got {v}")
        return v


class VisualThemeSpec(Poe2Model):
    id: str
    name: str
    description: str
    base_preset: str | None = None
    style_prompt: str | None = None
    min_contrast_ratio: float = 4.5
    tiers: dict[str, TierVisualStyle] = Field(default_factory=dict)


class SoundSynthesisParams(Poe2Model):
    musical_key: str = "D"
    scale: Literal["lydian", "major", "minor", "pentatonic", "phrygian", "dorian"] = "lydian"
    brightness: float = Field(default=1.0, ge=0.25, le=2.5)
    punch: float = Field(default=1.0, ge=0.25, le=2.5)
    reverb_tail: float = Field(default=1.0, ge=0.25, le=2.5)
    tempo_scale: float = Field(default=1.0, ge=0.4, le=2.5)
    warm_night_mode: bool = False
    lowpass_hz: float | None = Field(default=None, ge=400.0, le=18000.0)
    peak_ceiling: float = Field(default=0.89, ge=0.15, le=0.98)
    voice_name: str | None = None
    prefer_local_ai_tts: bool = True
    export_mp3: bool = True
    custom_callouts: dict[str, str] = Field(default_factory=dict)


class GeneratedSoundClip(Poe2Model):
    tier: str
    filename_wav: str
    filename_mp3: str | None = None
    relative_filter_path: str
    duration_seconds: float
    bytes_wav: int
    bytes_mp3: int | None = None
    sample_rate: int = 44100
    channels: int = 2
    bits: int = 16
    backend_used: str
    callout_text: str | None = None
    lowpass_hz_applied: float | None = None
    hf_energy_ratio_above_2500hz: float | None = None


class SoundPackManifest(Poe2Model):
    pack_id: str
    style: SoundPackStyle
    pack_dir: str
    params: SoundSynthesisParams
    clips: dict[str, GeneratedSoundClip] = Field(default_factory=dict)


class FilterDiagnostic(Poe2Model):
    severity: Literal["error", "warning"]
    code: str
    message: str
    line: int
    block_index: int | None = None
    suggestion: str | None = None


class FilterValidationReport(Poe2Model):
    valid: bool
    patch: str = POE2_PATCH
    total_blocks: int = 0
    show_blocks: int = 0
    hide_blocks: int = 0
    error_count: int = 0
    warning_count: int = 0
    diagnostics: list[FilterDiagnostic] = Field(default_factory=list)


class ColorVisionDeficiency(StrEnum):
    DEUTERANOPIA = "deuteranopia"
    PROTANOPIA = "protanopia"
    TRITANOPIA = "tritanopia"


class CvdPairSeparation(Poe2Model):
    tier_a: str
    tier_b: str
    deficiency: ColorVisionDeficiency
    normal_rms_delta_e: float
    cvd_rms_delta_e: float
    cvd_max_channel_delta_e: float
    luminance_delta: float
    distinct_minimap_shapes: bool
    relies_on_red_green_hue_alone: bool = False


class CvdThemeAuditReport(Poe2Model):
    theme_id: str
    cvd_safe: bool
    min_deuteranopia_delta_e: float
    min_protanopia_delta_e: float
    min_tritanopia_delta_e: float
    distinct_minimap_shape_count: int
    duplicate_minimap_shapes: list[str] = Field(default_factory=list)
    red_green_conflicts: list[str] = Field(default_factory=list)
    worst_pairs: list[CvdPairSeparation] = Field(default_factory=list)


class BuildFilterContext(Poe2Model):
    class_name: str | None = None
    ascendancy_name: str | None = None
    level: int = 1
    main_skill: str | None = None
    weapon_filter_classes: list[str] = Field(default_factory=list)
    offhand_filter_classes: list[str] = Field(default_factory=list)
    preferred_armour_tags: list[str] = Field(default_factory=list)
    uncapped_resists: list[str] = Field(default_factory=list)
    equipped_bases: list[str] = Field(default_factory=list)
    party_members: list[str] = Field(default_factory=list)


class FilterHealReport(Poe2Model):
    valid: bool
    patch: str = POE2_PATCH
    healed_text: str
    fixes_applied: list[str] = Field(default_factory=list)
    healed_basetypes: list[str] = Field(default_factory=list)
    pruned_blocks: int = 0
    original_error_count: int = 0
    validation: FilterValidationReport


class FilterCompileResult(Poe2Model):
    filter_name: str
    patch: str = POE2_PATCH
    league: str = DEFAULT_LEAGUE
    strictness: StrictnessTier
    theme_id: str
    sound_pack_id: str | None = None
    filter_text: str
    filter_path: str | None = None
    preview_html_path: str | None = None
    installed_dir: str | None = None
    validation: FilterValidationReport
    build_context: BuildFilterContext | None = None
    economy_tiered_items: int = 0
    healed_basetypes: list[str] = Field(default_factory=list)
    injected_override_blocks: int = 0
