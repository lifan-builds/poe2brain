from __future__ import annotations

import base64
import html
import shutil
import sys
from pathlib import Path

from poe2brain.filters.validator import FilterValidator
from poe2brain.models.filters import (
    FilterCompileResult,
    SoundPackManifest,
    TierVisualStyle,
    VisualThemeSpec,
)

POE_COLOR_HEX: dict[str, str] = {
    "Red": "#ef4444",
    "Green": "#22c55e",
    "Blue": "#3b82f6",
    "Brown": "#a16207",
    "White": "#f8fafc",
    "Yellow": "#facc15",
    "Cyan": "#06b6d4",
    "Grey": "#94a3b8",
    "Orange": "#f97316",
    "Pink": "#ec4899",
    "Purple": "#a855f7",
}

SHAPE_GLYPHS: dict[str, str] = {
    "Star": "★",
    "Diamond": "◆",
    "Hexagon": "⬢",
    "Square": "■",
    "Circle": "●",
    "Triangle": "▲",
    "Cross": "✚",
    "Moon": "☾",
    "Raindrop": "💧",
    "Kite": "❖",
    "Pentagon": "⬟",
    "UpsideDownHouse": "⛨",
}

SAMPLE_ITEMS_FOR_TIER: tuple[tuple[str, str, str], ...] = (
    ("s_plus", "Divine Orb", "S+ Jackpot Currency"),
    ("s", "Zarokh's Reliquary Key: Against the Darkness", "S Tier Vault Key / Chase"),
    ("a", "Greater Jeweller's Orb", "A Tier High Currency"),
    ("build_upgrade", "Slicing Quarterstaff (Tier 4 Unid)", "PoB2 Build Priority Base (ilvl 82)"),
    ("waystone_high", "Waystone (Tier 15)", "Endgame Atlas Sustain"),
    ("b", "Chaos Orb", "B Tier Solid Drop"),
    ("chancing", "Stellar Amulet", "Normal Orb of Chance Base"),
    ("waystone_prog", "Waystone (Tier 10)", "Mid Waystone Progression"),
    ("c", "Exalted Orb", "C Tier Standard Currency"),
    ("gold_high", "1,850x Gold", "Large Gold Pile"),
    ("d", "Regal Orb", "D Tier Low Crafting Currency"),
    ("gold_low", "240x Gold", "Small Gold Pile"),
)


def detect_poe2_filter_dir(
    *,
    platform_override: str | None = None,
    home_dir: Path | None = None,
) -> Path:
    plat = (platform_override or sys.platform).lower()
    home = home_dir or Path.home()
    if plat.startswith("darwin") or plat == "macos":
        return home / "Library" / "Application Support" / "Path of Exile 2"
    if plat.startswith("win"):
        return home / "Documents" / "My Games" / "Path of Exile 2"
    return (
        home
        / ".local"
        / "share"
        / "Steam"
        / "steamapps"
        / "compatdata"
        / "2694490"
        / "pfx"
        / "drive_c"
        / "users"
        / "steamuser"
        / "My Documents"
        / "My Games"
        / "Path of Exile 2"
    )


def install_filter_bundle(
    result: FilterCompileResult,
    *,
    target_dir: Path | None = None,
    sound_pack: SoundPackManifest | None = None,
) -> Path:
    dest_dir = (target_dir or detect_poe2_filter_dir()).expanduser().resolve()
    if dest_dir.name.lower() == "onlinefilters":
        raise ValueError(
            f"Refusing to install local filter into '{dest_dir}': "
            "PoE2 reserves `onlinefilters/` for web-synced filters."
        )
    dest_dir.mkdir(parents=True, exist_ok=True)

    if sound_pack is not None:
        src_pack = Path(sound_pack.pack_dir).resolve()
        dst_pack = (dest_dir / sound_pack.pack_id).resolve()
        if src_pack != dst_pack:
            dst_pack.mkdir(parents=True, exist_ok=True)
            for item in src_pack.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst_pack / item.name)

    fname = (
        result.filter_name
        if result.filter_name.endswith(".filter")
        else f"{result.filter_name}.filter"
    )
    filter_file = dest_dir / fname
    filter_file.write_text(result.filter_text, encoding="utf-8")

    check = FilterValidator().validate_file(filter_file)
    if not check.valid:
        first_err = check.diagnostics[0]
        raise RuntimeError(
            f"Installed filter failed verification ({first_err.code}): {first_err.message}"
        )

    result.installed_dir = str(dest_dir)
    result.filter_path = str(filter_file)
    result.validation = check
    return filter_file


def _encode_clip_data_uri(sound_pack: SoundPackManifest | None, tier_key: str) -> str | None:
    if sound_pack is None or tier_key not in sound_pack.clips:
        return None
    clip = sound_pack.clips[tier_key]
    pdir = Path(sound_pack.pack_dir)
    if clip.filename_mp3 and (pdir / clip.filename_mp3).is_file():
        b64 = base64.b64encode((pdir / clip.filename_mp3).read_bytes()).decode("ascii")
        return f"data:audio/mpeg;base64,{b64}"
    wav_file = pdir / clip.filename_wav
    if wav_file.is_file():
        b64 = base64.b64encode(wav_file.read_bytes()).decode("ascii")
        return f"data:audio/wav;base64,{b64}"
    return None


def _render_card(
    tier_key: str,
    item_label: str,
    subtitle: str,
    style: TierVisualStyle,
    data_uri: str | None,
    clip_label: str,
) -> str:
    px = max(11, round(style.font_size * 0.50))
    fg = style.text_color.to_css_rgba()
    bd = style.border_color.to_css_rgba()
    bg = style.background_color.to_css_rgba()

    beam_html = ""
    if style.beam is not None:
        bhex = POE_COLOR_HEX.get(style.beam.color, "#ffffff")
        cls_extra = " beam-temp" if style.beam.temp else ""
        grad = f"linear-gradient(to top, {bhex}dd 0%, {bhex}44 55%, transparent 100%)"
        beam_html = f'<div class="loot-beam{cls_extra}" style="background: {grad};"></div>'

    icon_html = ""
    if style.minimap_icon is not None and style.minimap_icon.size >= 0:
        ihex = POE_COLOR_HEX.get(style.minimap_icon.color, "#ffffff")
        glyph = SHAPE_GLYPHS.get(style.minimap_icon.shape, "●")
        scale = {0: 16, 1: 13, 2: 10}.get(style.minimap_icon.size, 13)
        icon_html = f'<span class="mm-icon" style="color:{ihex};font-size:{scale}px">{glyph}</span>'

    freq_fallback = {
        "s_plus": 880,
        "s": 740,
        "a": 659,
        "build_upgrade": 698,
        "waystone_high": 587,
        "b": 523,
        "chancing": 554,
        "c": 440,
        "d": 349,
    }.get(tier_key, 440)

    onclick_js = "playEmbedded(this)" if data_uri else f"playFallbackTone({freq_fallback})"
    data_attr = f' data-audio="{data_uri}"' if data_uri else ""
    plate_style = f"font-size:{px}px;color:{fg};border-color:{bd};background:{bg};"
    metrics_txt = (
        f"Font {style.font_size} · Contrast {style.contrast_ratio}:1 · {html.escape(clip_label)}"
    )

    return f"""
    <div class="drop-card" onclick="{onclick_js}"{data_attr}>
      {beam_html}
      <div class="plate-wrap">
        <span class="nameplate" style="{plate_style}">
          {icon_html}{html.escape(item_label)}
        </span>
      </div>
      <div class="card-footer">
        <span class="tier-pill">{html.escape(subtitle)}</span>
        <span class="metrics">{metrics_txt}</span>
      </div>
    </div>"""


def render_filter_preview_html(
    theme: VisualThemeSpec,
    *,
    result: FilterCompileResult | None = None,
    sound_pack: SoundPackManifest | None = None,
    output_path: Path | None = None,
) -> str:
    cards: list[str] = []
    for tier_key, item_label, subtitle in SAMPLE_ITEMS_FOR_TIER:
        if tier_key not in theme.tiers:
            continue
        style = theme.tiers[tier_key]
        uri = _encode_clip_data_uri(sound_pack, tier_key)
        if sound_pack and tier_key in sound_pack.clips:
            c = sound_pack.clips[tier_key]
            clip_desc = f"🔊 {c.filename_wav}" + (
                f' ("{c.callout_text}")' if c.callout_text else ""
            )
        elif style.builtin_sound_id is not None:
            clip_desc = f"🔔 Builtin #{style.builtin_sound_id}"
        else:
            clip_desc = "Silent"
        cards.append(_render_card(tier_key, item_label, subtitle, style, uri, clip_desc))

    strict_txt = result.strictness.value if result else "semi_strict"
    league_txt = result.league if result else "Forbidden Rites"
    build_banner = ""
    if result and result.build_context:
        bc = result.build_context
        build_banner = (
            f'<div class="build-banner">PoB2 Build Context: '
            f"<strong>{html.escape(bc.ascendancy_name or bc.class_name or 'Custom')}</strong> "
            f"(Lv {bc.level}, {html.escape(bc.main_skill or 'Active Skill')}) · "
            f"Weapons: {html.escape(', '.join(bc.weapon_filter_classes))} · "
            f"Armour: {html.escape(', '.join(bc.preferred_armour_tags))}</div>"
        )

    css_lines = "\n  ".join(
        [
            ":root { color-scheme: dark; }",
            "body { margin:0; padding:32px; background:#090b10; color:#e5e7eb;"
            " font-family:Inter,system-ui,sans-serif; }",
            "header { max-width:1220px; margin:0 auto 24px; }",
            "h1 { margin:0 0 6px; font-size:24px; font-weight:700; letter-spacing:-0.01em; }",
            ".sub { color:#94a3b8; font-size:14px; margin-bottom:12px; }",
            ".build-banner { background:#111827; border:1px solid #1f2937;"
            " padding:10px 14px; border-radius:8px; font-size:13px; color:#a7f3d0; }",
            ".grid { max-width:1220px; margin:0 auto; display:grid;"
            " grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:20px; }",
            ".drop-card { position:relative; height:158px; border-radius:12px;"
            " border:1px solid #1e2433;"
            " background:radial-gradient(circle at 50% 68%, #1a1f2c 0%, #0d1017 78%);"
            " display:flex; flex-direction:column; justify-content:flex-end;"
            " align-items:center; padding:16px; cursor:pointer; overflow:hidden;"
            " transition:transform 0.12s ease, border-color 0.12s ease; }",
            ".drop-card:hover { transform:translateY(-2px); border-color:#475569; }",
            ".loot-beam { position:absolute; bottom:58px; left:50%; width:6px;"
            " height:96px; transform:translateX(-50%); filter:blur(1.5px);"
            " pointer-events:none; }",
            ".beam-temp { animation:pulseBeam 1.8s ease-in-out infinite; }",
            "@keyframes pulseBeam { 0%,100% { opacity:0.9; height:92px; }"
            " 50% { opacity:0.25; height:56px; } }",
            ".plate-wrap { position:relative; z-index:2; margin-bottom:18px; }",
            ".nameplate { display:inline-flex; align-items:center; gap:6px;"
            " padding:5px 14px; border:2px solid; font-weight:700;"
            " letter-spacing:0.025em; box-shadow:0 6px 20px rgba(0,0,0,0.72);"
            " user-select:none; white-space:nowrap; }",
            ".card-footer { width:100%; display:flex; flex-direction:column;"
            " gap:3px; text-align:center; z-index:2; }",
            ".tier-pill { font-size:12px; font-weight:600; color:#e2e8f0; }",
            ".metrics { font-size:11px; color:#94a3b8; }",
        ]
    )
    meta_span = (
        f'<span style="color:#64748b;font-weight:400">'
        f"· Patch 0.5.5 ({html.escape(league_txt)}) · {html.escape(strict_txt)}</span>"
    )
    sub_line = (
        f"{html.escape(theme.description)} — "
        "Click any ground item nameplate to audition its alert sound."
    )

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>poe2brain Filter Studio — {html.escape(theme.name)}</title>
<style>
  {css_lines}
</style>
</head>
<body>
<header>
  <h1>{html.escape(theme.name)} {meta_span}</h1>
  <div class="sub">{sub_line}</div>
  {build_banner}
</header>
<main class="grid">
  {"".join(cards)}
</main>
<script>
let audioCtx = null;
function playEmbedded(el) {{
  const src = el.getAttribute('data-audio');
  if (!src) return;
  const a = new Audio(src);
  a.play();
}}
function playFallbackTone(freq) {{
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  const now = audioCtx.currentTime;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = 'triangle';
  osc.frequency.setValueAtTime(freq, now);
  osc.frequency.exponentialRampToValueAtTime(freq * 1.5, now + 0.09);
  gain.gain.setValueAtTime(0.22, now);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.38);
  osc.connect(gain);
  gain.connect(audioCtx.destination);
  osc.start(now);
  osc.stop(now + 0.4);
}}
</script>
</body>
</html>
"""
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(doc, encoding="utf-8")
        if result is not None:
            result.preview_html_path = str(output_path)
    return doc
