# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from poe2brain.config import get_pob2_paths
from poe2brain.filters import (
    BUILTIN_THEME_PRESETS,
    FilterGenerator,
    FilterValidator,
    NeverSinkAdapter,
    SoundPackGenerator,
    compose_theme,
    compute_wcag_contrast,
    detect_poe2_filter_dir,
    extract_build_filter_context,
    get_filter_gamedata_index,
    install_filter_bundle,
    list_visual_themes,
    render_filter_preview_html,
    to_filter_class,
)
from poe2brain.models.filters import (
    BuildFilterContext,
    SoundPackStyle,
    SoundSynthesisParams,
    StrictnessTier,
)


def test_sqlite_item_class_translation_and_055_index() -> None:
    idx = get_filter_gamedata_index()
    assert to_filter_class("Body Armour", idx) == "Body Armours"
    assert to_filter_class("SoulCore", idx) == "Augment"
    assert to_filter_class("TowerAugmentation", idx) == "Tablet"
    assert to_filter_class("UtilityFlask", idx) == "Charms"
    assert to_filter_class("Map", idx) == "Waystones"
    assert to_filter_class("Warstaff", idx) == "Quarterstaves"
    assert to_filter_class("UncutReservationGemStackable", idx) == "Uncut Spirit Gems"
    assert to_filter_class("UncutSkillGemStackable", idx) == "Uncut Skill Gems"
    assert to_filter_class("Meta Skill Gem", idx) is None
    assert to_filter_class("PinnacleKey_OLD", idx) is None

    # Live 0.5.5 items including Gold & Reliquary Keys exist
    assert "Gold" in idx.basetype_to_classes
    assert "Zarokh's Reliquary Key: Temporalis" in idx.live_poe2_basetypes
    assert "Origin Core" in idx.live_poe2_basetypes

    # Stale 0.10.4a & dormant PoE1/0.4/0.5 items are excluded/blocked
    assert "Fist of Kalguur" not in idx.basetype_to_classes
    assert "Fragments of the Past" not in idx.basetype_to_classes
    assert "Orb of Alteration" in idx.dormant_or_removed_basetypes
    assert "Omen of Homogenising Exaltation" in idx.dormant_or_removed_basetypes
    assert "Omen of Recombination" in idx.dormant_or_removed_basetypes


def test_validator_catches_all_seven_poe2_crash_traps(tmp_path: Path) -> None:
    validator = FilterValidator()

    bad_filter = """
Show
\tClass == "Body Armour"
\tBaseType == "Fist of Kalguur" "Orb of Alteration" "Omen of Homogenising Exaltation"
\tSocketGroup "RGB"
\tMapTier >= 15
\tHasExplicitMod >= 2 "Merciless"
\tItemLevel >= 82
\tItemLevel <= 70
\tSetFontSize 12
\tCustomAlertSound "/Users/abs/divine.wav" 300
\tContinue
\tSetTextColor 255 255 255 255

Show
\tBaseType == "Divine Orb"
\tSetFontSize 45

Show
\tBaseType == "Divine Orb"
\tSetFontSize 40
"""
    rep = validator.validate_text(bad_filter, filter_dir=tmp_path)
    assert not rep.valid
    codes = {d.code for d in rep.diagnostics}
    assert "internal_item_class_id" in codes
    assert "unknown_basetype" in codes
    assert "dormant_or_removed_item" in codes
    assert "poe1_legacy_condition" in codes
    assert "has_explicit_mod_space" in codes
    assert "contradictory_condition" in codes
    assert "font_size_out_of_range" in codes
    assert "absolute_sound_path" in codes
    assert "statement_after_continue" in codes
    assert "shadowed_rule" in codes

    # Valid PoE2 0.5.5 syntax passes with 0 errors and 0 warnings
    good_filter = """
Show
\tClass == "Quarterstaves"
\tRarity Rare
\tSockets >= 2
\tUnidentifiedItemTier >= 4
\tHasExplicitMod >=2 "Merciless" "Flaring"
\tSetFontSize 42
\tSetTextColor 255 215 90 255
\tSetBorderColor 255 215 90 255
\tSetBackgroundColor 20 16 28 245
\tPlayAlertSound 2 300
\tPlayEffect Yellow Temp
\tMinimapIcon 1 Yellow Diamond
"""
    good_rep = validator.validate_text(good_filter)
    assert good_rep.valid
    assert good_rep.error_count == 0
    assert good_rep.warning_count == 0


def test_builtin_themes_and_agentic_composer_wcag_and_hierarchy() -> None:
    themes = list_visual_themes()
    assert len(themes) >= 8
    expected = {
        "forbidden_abyss",
        "solaris_gold",
        "vaal_blood",
        "cyberpunk_neon",
        "darkmode_obsidian",
        "cobalt_arcane",
        "zen_minimal",
        "colorblind_safe",
    }
    assert expected.issubset(set(BUILTIN_THEME_PRESETS))

    for theme in themes:
        for tier_key, st in theme.tiers.items():
            assert 18 <= st.font_size <= 45
            ratio = compute_wcag_contrast(st.text_color, st.background_color)
            assert ratio >= 4.5, f"{theme.id}.{tier_key} contrast {ratio} < 4.5"

    composed = compose_theme(
        "synthwave neon pink and cyan on pitch black, "
        "huge text on divines, subtle muted yellows on b-tier",
        overrides={
            "c": {
                "text_color": "#202026",
                "background_color": "#181820",
            },
        },
    )
    assert composed.base_preset == "cyberpunk_neon"
    assert composed.tiers["s_plus"].font_size == 45
    assert composed.tiers["b"].beam is None
    assert composed.tiers["c"].contrast_ratio >= 4.5
    # Monotonic hierarchy S+ >= S >= A >= B >= C >= D
    fonts = [composed.tiers[k].font_size for k in ("s_plus", "s", "a", "b", "c", "d")]
    assert fonts == sorted(fonts, reverse=True)

    # Regression: "obsidian" dark swatch must set background (#0e1017), NOT displace magenta/cyan!
    cyber_obs = compose_theme(
        "cyberpunk neon magenta and cyan on deep obsidian, huge divine labels"
    )
    assert cyber_obs.base_preset == "cyberpunk_neon"
    assert cyber_obs.tiers["s_plus"].font_size == 45
    assert cyber_obs.tiers["s_plus"].border_color.to_hex() == "#ff0099"
    assert cyber_obs.tiers["s"].border_color.to_hex() == "#00f5ff"
    assert cyber_obs.tiers["s"].beam is not None and cyber_obs.tiers["s"].beam.color == "Pink"
    assert cyber_obs.tiers["a"].beam is not None and cyber_obs.tiers["a"].beam.color == "Cyan"
    s_bg = cyber_obs.tiers["s"].background_color
    assert (s_bg.r, s_bg.g, s_bg.b) == (14, 16, 23)


def test_soundpack_generator_synth_and_voice_callout(tmp_path: Path) -> None:
    gen = SoundPackGenerator()

    # 1. Pure-Python polyphonic synth pack with custom musical scale/params
    synth_pack = gen.generate_pack(
        tmp_path,
        style=SoundPackStyle.CYBER_SYNTH,
        pack_id="cyber_phrygian",
        params=SoundSynthesisParams(
            musical_key="F#",
            scale="phrygian",
            brightness=1.35,
            punch=1.25,
            reverb_tail=0.9,
            export_mp3=False,
        ),
        tiers=("s_plus", "a", "b"),
    )
    assert synth_pack.pack_id == "poe2brain_cyber_phrygian"
    assert set(synth_pack.clips) == {"s_plus", "a", "b"}

    wav_file = Path(synth_pack.pack_dir) / synth_pack.clips["s_plus"].filename_wav
    assert wav_file.is_file()
    with wave.open(str(wav_file), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 44100
        frames = wf.readframes(wf.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    peak = max(abs(s) for s in samples)
    assert 20_000 <= peak <= 32_767

    # 2. Voice + Chime callout pack (local 18455, macOS say, or synth fallback)
    voice_pack = gen.generate_pack(
        tmp_path,
        style=SoundPackStyle.VOICE_CALLOUT,
        pack_id="callout_test",
        params=SoundSynthesisParams(
            prefer_local_ai_tts=False,
            custom_callouts={"s_plus": "Mirror Tier!"},
        ),
        tiers=("s_plus",),
    )
    clip = voice_pack.clips["s_plus"]
    assert clip.sample_rate == 44100
    assert clip.channels == 2
    assert clip.bits == 16
    assert (Path(voice_pack.pack_dir) / clip.filename_wav).is_file()


@pytest.mark.parametrize(
    "strictness",
    [
        StrictnessTier.SOFT,
        StrictnessTier.REGULAR,
        StrictnessTier.SEMI_STRICT,
        StrictnessTier.STRICT,
        StrictnessTier.VERY_STRICT,
        StrictnessTier.UBER_STRICT,
    ],
)
def test_native_filter_generator_all_strictnesses_and_unique_safety(
    strictness: StrictnessTier,
) -> None:
    fg = FilterGenerator()
    res = fg.generate(
        filter_name=f"test_{strictness.value}",
        strictness=strictness,
        theme="solaris_gold",
        use_live_economy=False,
    )
    assert res.validation.valid
    assert res.validation.error_count == 0
    assert "Zarokh's Reliquary Key: Temporalis" in res.filter_text
    # Shared multi-unique bases ("Silk Robe" / "Stellar Amulet") stay visible on uber_strict
    assert '"Silk Robe"' in res.filter_text
    assert '"Stellar Amulet"' in res.filter_text


def test_pob2_build_aware_filter_generation(pob_daemon_socket: Path) -> None:
    fg = FilterGenerator()
    if get_pob2_paths().is_ready:
        from poe2brain.pob import BuildService, Pob2Bridge

        svc = BuildService(bridge=Pob2Bridge(socket_path=pob_daemon_socket))
        try:
            svc.create_character("Monk", "Invoker", level=85, slot="filter_test")
            svc.equip_item(
                "Rarity: RARE\nStorm Spire\nSlicing Quarterstaff\n"
                "Implicits: 0\n+120% increased Physical Damage",
                slot="filter_test",
            )
            ctx = extract_build_filter_context(svc, slot="filter_test")
        finally:
            svc.close()
    else:
        ctx = BuildFilterContext(
            class_name="Monk",
            ascendancy_name="Invoker",
            level=85,
            main_skill="Tempest Flurry",
            weapon_filter_classes=["Quarterstaves"],
            offhand_filter_classes=[],
            preferred_armour_tags=["dex_int_armour", "dex_armour"],
            uncapped_resists=["fire", "chaos"],
            equipped_bases=["Slicing Quarterstaff"],
        )

    assert "Quarterstaves" in ctx.weapon_filter_classes
    res = fg.generate(
        strictness=StrictnessTier.STRICT,
        theme="cobalt_arcane",
        build_context=ctx,
        use_live_economy=False,
    )
    assert res.validation.valid
    assert res.validation.error_count == 0
    assert "pob_top_weapons_ilvl81" in res.filter_text
    assert '"Quarterstaves"' in res.filter_text
    assert '"Ruby Charm"' in res.filter_text


def test_neversink_healer_and_restyler_zero_errors(tmp_path: Path) -> None:
    stale_neversink_sample = """# NeverSink's Indepth Loot Filter - for Path of Exile 2
# VERSION: 0.10.4

# [[0100]] OVERRIDE AREA 1
Show # %D6 $type->gold $tier->stack3
\tStackSize >= 1000
\tBaseType == "Gold"
\tSetFontSize 40
\tSetTextColor 255 255 255 255
\tSetBorderColor 255 255 255 255
\tSetBackgroundColor 20 20 0 255

Show # %D9 $type->currency $tier->s
\tClass == "Stackable Currency"
\tBaseType == "Divine Orb" "Mirror of Kalandra" "Fist of Kalguur"
\tSetFontSize 45
\tSetTextColor 255 0 0 255
\tSetBorderColor 255 0 0 255
\tSetBackgroundColor 255 255 255 255
\tPlayAlertSound 6 300

Show # %D4 $type->fragments- $tier->b
\tBaseType == "Fragments of the Past"
\tSetFontSize 38

Show # $type->exoticmods $tier->cmranged
\tHasExplicitMod >= 1 "Merciless" "Flaring"
\tSetFontSize 40
"""
    # Confirm raw 0.10.4a snippet fails validation before healing
    raw_report = FilterValidator().validate_text(stale_neversink_sample)
    assert not raw_report.valid

    pack = SoundPackGenerator().generate_pack(
        tmp_path,
        style=SoundPackStyle.ZEN_MINIMAL,
        pack_id="zen_ns",
        params=SoundSynthesisParams(export_mp3=False),
        tiers=("s_plus", "s", "a", "b", "c"),
    )

    adapter = NeverSinkAdapter()
    healed = adapter.restyle_and_heal(
        stale_neversink_sample,
        theme="darkmode_obsidian",
        sound_pack=pack,
        require_custom_sounds=True,
        output_path=tmp_path / "healed_ns.filter",
    )
    assert healed.validation.valid
    assert healed.validation.error_count == 0
    assert "Fist of Kalguur" in healed.healed_basetypes
    assert "Fragments of the Past" in healed.healed_basetypes
    assert "Fist of Kalguur" not in healed.filter_text
    assert "Zarokh's Reliquary Key: Temporalis" in healed.filter_text
    assert "HasExplicitMod >=1" in healed.filter_text

    # Also test against the full 5,308-line NeverSink 0.10.4a file if cloned in /tmp
    full_ns_path = Path(
        "/tmp/poe2brain-filter/neversink-poe2/NeverSink's filter 2 - 2-SEMI-STRICT.filter"
    )
    if full_ns_path.is_file():
        full_healed = adapter.restyle_and_heal(
            source_filter_path=full_ns_path,
            theme="forbidden_abyss",
            inject_055_overrides=True,
        )
        assert full_healed.validation.valid, (
            f"Full NeverSink heal errors: {full_healed.validation.diagnostics[:5]}"
        )
        assert full_healed.validation.error_count == 0
        assert "Fist of Kalguur" in full_healed.healed_basetypes

    # Remote NeverSink strictness fetch via Poe2HttpClient.get_text (mocked + live GitHub)
    import os

    import httpx

    from poe2brain.http import Poe2HttpClient

    seen_ns_urls: list[str] = []

    def ns_mock_handler(request: httpx.Request) -> httpx.Response:
        seen_ns_urls.append(str(request.url))
        assert "3-STRICT.filter" in str(request.url)
        return httpx.Response(200, text=stale_neversink_sample)

    mock_http = Poe2HttpClient(sync_transport=httpx.MockTransport(ns_mock_handler))
    try:
        remote_mock_adapter = NeverSinkAdapter(http_client=mock_http)
        remote_healed = remote_mock_adapter.restyle_and_heal(
            strictness=StrictnessTier.STRICT,
            style_prompt="cyberpunk neon magenta and cyan on deep obsidian",
        )
        assert len(seen_ns_urls) == 1
        assert remote_healed.validation.valid
        assert remote_healed.validation.error_count == 0
        assert "Fist of Kalguur" in remote_healed.healed_basetypes
    finally:
        mock_http.close()

    if os.environ.get("POE2_LIVE_TESTS") == "1":
        live_healed = adapter.restyle_and_heal(
            strictness=StrictnessTier.SEMI_STRICT,
            style_prompt="cyberpunk neon magenta and cyan on deep obsidian, huge divine labels",
        )
        assert live_healed.validation.valid
        assert live_healed.validation.error_count == 0
        assert {"Fist of Kalguur", "Fragments of the Past"}.issubset(
            set(live_healed.healed_basetypes)
        )


def test_preview_html_and_bundle_installer(tmp_path: Path) -> None:
    assert detect_poe2_filter_dir(
        platform_override="darwin", home_dir=Path("/Users/alice")
    ) == Path("/Users/alice/Library/Application Support/Path of Exile 2")
    assert detect_poe2_filter_dir(platform_override="win32", home_dir=Path("/Users/alice")) == Path(
        "/Users/alice/Documents/My Games/Path of Exile 2"
    )

    pack = SoundPackGenerator().generate_pack(
        tmp_path / "staging",
        style=SoundPackStyle.ARCADE_RETRO,
        pack_id="arcade_pack",
        params=SoundSynthesisParams(export_mp3=False),
        tiers=("s_plus", "s", "a"),
    )
    theme = compose_theme("vaal crimson and gold")
    res = FilterGenerator().generate(
        filter_name="poe2brain_arcade",
        theme=theme,
        sound_pack=pack,
        require_custom_sounds=True,
        use_live_economy=False,
    )

    # Guard against installing into `onlinefilters/`
    with pytest.raises(ValueError, match="onlinefilters"):
        install_filter_bundle(res, target_dir=tmp_path / "onlinefilters", sound_pack=pack)

    poe2_dir = tmp_path / "Path of Exile 2"
    installed_filter = install_filter_bundle(res, target_dir=poe2_dir, sound_pack=pack)
    assert installed_filter.is_file()
    assert (poe2_dir / "poe2brain_arcade_pack" / "s_plus.wav").is_file()
    assert res.validation.valid

    html_out = tmp_path / "preview.html"
    html_doc = render_filter_preview_html(theme, result=res, sound_pack=pack, output_path=html_out)
    assert html_out.is_file()
    assert "data:audio/wav;base64," in html_doc
    assert "Divine Orb" in html_doc
    assert "Contrast" in html_doc


def test_chinese_vibe_filter_look_prompt_and_intent() -> None:
    from poe2brain.filters.themes import parse_look_intent

    intent = parse_look_intent("赛博霓虹 护耳 凌晨2点 电子音效")
    assert intent["warm_night_mode"] is True
    assert intent["sound_style"] == "cyber_synth"
    assert intent["cvd_safe"] is None

    cvd_intent = parse_look_intent("暗黑金色 红绿色盲 轻柔钢琴")
    assert cvd_intent["cvd_safe"] == "deuteranopia"
    assert cvd_intent["sound_style"] == "zen_minimal"

    theme = compose_theme("赛博霓虹，品红和青色，纯黑背景，神圣超大")
    assert theme.base_preset == "cyberpunk_neon"
    assert theme.id == "custom_cyberpunk_neon"
    assert theme.tiers["s_plus"].font_size == 45
    assert theme.tiers["s_plus"].border_color.to_hex() == "#ff0099"
    assert theme.tiers["s"].border_color.to_hex() == "#00f5ff"
    s_bg = theme.tiers["s"].background_color
    assert (s_bg.r, s_bg.g, s_bg.b) == (6, 7, 11)

    zh_voice = parse_look_intent("暗黑金色 中文语音播报")
    assert zh_voice["sound_style"] == "voice_callout"
    assert zh_voice["voice_language"] == "zh"


def test_endgame_filter_exceptional_bases_gems_jewellery_and_strictness_pruning(
    tmp_path: Path,
) -> None:
    fg = FilterGenerator()

    strict_res = fg.generate(
        filter_name="strict_055",
        strictness=StrictnessTier.STRICT,
        theme="darkmode_obsidian",
        use_live_economy=False,
    )
    assert strict_res.validation.valid
    assert strict_res.validation.error_count == 0
    # Exceptional over-quality (21%+) & over-socket (3+), Lv19 uncut gems, ilvl 81+ jewellery/meta
    assert "Quality >= 21" in strict_res.filter_text
    assert "Sockets >= 3" in strict_res.filter_text
    assert "ItemLevel == 19" in strict_res.filter_text
    assert "jewellery_ilvl81" in strict_res.filter_text
    assert "meta_weapons_ilvl82" in strict_res.filter_text
    # Routine salvage still present on Strict, endgame junk flasks/charms/jewellery hidden
    assert "$type->salvage $tier->multi_sockets" in strict_res.filter_text
    assert "flasks_charms_endgame" in strict_res.filter_text
    assert "jewellery_normal_magic_endgame" in strict_res.filter_text
    assert "$type->gems $tier->uncut_low" in strict_res.filter_text

    uber_res = fg.generate(
        filter_name="uber_055",
        strictness=StrictnessTier.UBER_STRICT,
        theme="solaris_gold",
        use_live_economy=False,
    )
    assert uber_res.validation.valid
    assert uber_res.validation.error_count == 0
    # Exceptional bases stay highlighted even on Uber Strict, routine salvage is pruned
    assert "Quality >= 21" in uber_res.filter_text
    assert "Sockets >= 3" in uber_res.filter_text
    assert "$type->salvage $tier->multi_sockets" not in uber_res.filter_text
    assert "uncut_support_low" in uber_res.filter_text

    # Chinese voice callout pack uses Mandarin callout strings cleanly
    zh_pack = SoundPackGenerator().generate_pack(
        tmp_path,
        style=SoundPackStyle.VOICE_CALLOUT,
        pack_id="zh_callout_test",
        params=SoundSynthesisParams(
            prefer_local_ai_tts=False,
            voice_language="zh",
            export_mp3=False,
        ),
        tiers=("s_plus", "build_upgrade"),
    )
    assert zh_pack.clips["s_plus"].callout_text == "神圣石，出大货！"
    assert zh_pack.clips["build_upgrade"].callout_text == "核心装备底材！"
    assert (Path(zh_pack.pack_dir) / "s_plus.wav").is_file()
