# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import os
from pathlib import Path

from poe2brain.config import get_pob2_paths
from poe2brain.filters import (
    ColorVisionDeficiency,
    FilterGenerator,
    FilterHealReport,
    FilterValidator,
    NeverSinkAdapter,
    SoundPackGenerator,
    audit_theme_cvd,
    compose_theme,
    detect_rhythmic_pulses,
    extract_build_filter_context,
    get_filter_gamedata_index,
    get_visual_theme,
    heal_filter_text,
    install_filter_bundle,
    measure_high_frequency_energy_ratio,
    merge_build_filter_contexts,
    parse_filter_text,
    render_filter_preview_html,
    simulate_cvd_color,
)
from poe2brain.filters.generator import _query_top_bases_for_context
from poe2brain.filters.soundpacks import read_stereo_wav
from poe2brain.models.filters import (
    BuildFilterContext,
    RgbaColor,
    SoundPackStyle,
    SoundSynthesisParams,
    StrictnessTier,
)

ALL_POE2_CLASSES_AND_ASCENDANCIES: tuple[tuple[str, str, set[str], set[str]], ...] = (
    ("Ranger", "Deadeye", {"Bows"}, {"Quivers"}),
    ("Ranger", "Pathfinder", {"Bows"}, {"Quivers"}),
    ("Huntress", "Amazon", {"Spears"}, {"Bucklers"}),
    ("Huntress", "Spirit Walker", {"Spears"}, {"Bucklers"}),
    ("Huntress", "Ritualist", {"Spears"}, {"Bucklers"}),
    ("Warrior", "Titan", {"Two Hand Maces", "One Hand Maces"}, {"Shields"}),
    ("Warrior", "Warbringer", {"Two Hand Maces", "One Hand Maces"}, {"Shields"}),
    ("Warrior", "Smith of Kitava", {"Two Hand Maces", "One Hand Maces"}, {"Shields"}),
    ("Mercenary", "Tactician", {"Crossbows"}, set()),
    ("Mercenary", "Witchhunter", {"Crossbows"}, set()),
    ("Mercenary", "Gemling Legionnaire", {"Crossbows"}, set()),
    ("Druid", "Oracle", {"Talismans", "Staves"}, set()),
    ("Druid", "Shaman", {"Talismans", "Staves"}, set()),
    ("Witch", "Infernalist", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Witch", "Blood Mage", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Witch", "Lich", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Witch", "Abyssal Lich", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Sorceress", "Stormweaver", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Sorceress", "Chronomancer", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Sorceress", "Disciple of Varashta", {"Wands", "Sceptres", "Staves"}, {"Foci"}),
    ("Monk", "Martial Artist", {"Quarterstaves"}, set()),
    ("Monk", "Invoker", {"Quarterstaves"}, set()),
    ("Monk", "Acolyte of Chayula", {"Quarterstaves"}, set()),
)


def test_journey8_all_8_classes_23_ascendancies_weapon_and_offhand_filter_mapping() -> None:
    """Verify all 8 PoE2 classes & 23 Ascendancies map cleanly to valid 0.5.5 .filter Classes."""
    idx = get_filter_gamedata_index()
    assert len({cls for cls, _, _, _ in ALL_POE2_CLASSES_AND_ASCENDANCIES}) == 8
    assert len(ALL_POE2_CLASSES_AND_ASCENDANCIES) == 23

    svc = None
    if get_pob2_paths().is_ready:
        from poe2brain.pob.service import BuildService

        svc = BuildService()

    try:
        for (
            cls_name,
            asc_name,
            expected_weapons,
            expected_offhands,
        ) in ALL_POE2_CLASSES_AND_ASCENDANCIES:
            if svc is not None:
                svc.create_character(cls_name, asc_name, level=86, slot="j8_matrix")
                ctx = extract_build_filter_context(svc, slot="j8_matrix", index=idx)
            else:
                ctx = BuildFilterContext(
                    class_name=cls_name,
                    ascendancy_name=asc_name,
                    level=86,
                    weapon_filter_classes=sorted(expected_weapons),
                    offhand_filter_classes=sorted(expected_offhands),
                    preferred_armour_tags=["dex_armour"],
                )

            assert expected_weapons.issubset(set(ctx.weapon_filter_classes)), (
                f"{cls_name}/{asc_name} missing weapons: {ctx.weapon_filter_classes}"
            )
            assert expected_offhands.issubset(set(ctx.offhand_filter_classes)), (
                f"{cls_name}/{asc_name} missing offhands: {ctx.offhand_filter_classes}"
            )

            for fc in [*ctx.weapon_filter_classes, *ctx.offhand_filter_classes]:
                assert fc in idx.valid_filter_classes, f"Invalid filter Class {fc!r}"
                assert fc not in idx.internal_only_class_ids

            top_weapons, top_armours = _query_top_bases_for_context(ctx, idx)
            assert top_weapons, f"No top weapons found for {cls_name}/{asc_name}"
            assert top_armours, f"No top armours found for {cls_name}/{asc_name}"
            for wb in top_weapons:
                assert wb in idx.live_poe2_basetypes
                assert not wb.startswith(("Runeforged ", "Runemastered "))

            # Verify every requested weapon and offhand filter class has live base representation
            covered_fcs = {
                fc for wb in top_weapons for fc in idx.basetype_to_classes.get(wb, frozenset())
            }
            for req_fc in [*ctx.weapon_filter_classes, *ctx.offhand_filter_classes]:
                assert req_fc in covered_fcs, (
                    f"{cls_name}/{asc_name} filter class {req_fc!r} crowded out of {top_weapons}"
                )
    finally:
        if svc is not None:
            svc.close()


def test_journey8_deuteranopia_cvd_and_2am_tinnitus_safe_spirit_walker_e2e(
    tmp_path: Path,
) -> None:
    """Journey 8: HC SSF Spirit Walker + Deuteranopia/Protanopia CVD theme + 2 AM warm soundpack."""
    idx = get_filter_gamedata_index()

    # 1. Import or construct HC SSF Spirit Walker (Spears + Bucklers + Companion/Dex gear)
    if get_pob2_paths().is_ready:
        from poe2brain.pob.service import BuildService

        svc = BuildService()
        try:
            if os.environ.get("POE2_LIVE_TESTS") == "1":
                from poe2brain.ninja.service import NinjaBuildsService

                ninja = NinjaBuildsService(build_service=svc)
                sr = ninja.search_builds(ascendancy="Spirit Walker", limit=1)
                assert sr.builds
                ninja.fetch_character(
                    sr.builds[0].account,
                    character=sr.builds[0].character,
                    load_into_pob=True,
                    slot="j8_sw",
                )
            else:
                svc.create_character("Huntress", "Spirit Walker", level=90, slot="j8_sw")
                svc.equip_item(
                    "Rarity: RARE\nGale Fang\nGrand Spear\n"
                    "Implicits: 0\n140% increased Physical Damage",
                    target_slot="Weapon 1",
                    slot="j8_sw",
                )
                svc.equip_item(
                    "Rarity: RARE\nThorn Guard\nDesert Buckler\nImplicits: 0\n+95 to maximum Life",
                    target_slot="Weapon 2",
                    slot="j8_sw",
                )
            sw_ctx = extract_build_filter_context(svc, slot="j8_sw", index=idx)
        finally:
            svc.close()
    else:
        sw_ctx = BuildFilterContext(
            class_name="Huntress",
            ascendancy_name="Spirit Walker",
            level=90,
            main_skill="Twister",
            weapon_filter_classes=["Spears"],
            offhand_filter_classes=["Bucklers"],
            preferred_armour_tags=["dex_armour", "dex_int_armour"],
            uncapped_resists=["chaos"],
            equipped_bases=["Grand Spear", "Desert Buckler"],
        )

    assert sw_ctx.class_name == "Huntress"
    assert sw_ctx.ascendancy_name == "Spirit Walker"
    assert "Spears" in sw_ctx.weapon_filter_classes
    assert "Bucklers" in sw_ctx.offhand_filter_classes

    # 2. Verify CVD colorblind separation & distinct minimap geometry (preset + composed dark look)
    preset_cb = get_visual_theme("colorblind_safe")
    preset_audit = audit_theme_cvd(preset_cb, min_cvd_delta_e=14.0, require_unique_shapes=True)
    assert preset_audit.cvd_safe, f"Preset CVD audit failed: {preset_audit}"
    assert preset_audit.min_deuteranopia_delta_e >= 14.0
    assert preset_audit.min_protanopia_delta_e >= 14.0
    assert preset_audit.duplicate_minimap_shapes == []
    assert preset_audit.red_green_conflicts == []
    assert preset_audit.distinct_minimap_shape_count == 10

    night_cb = compose_theme(
        "deuteranopia red-green colorblind safe obsidian dark look, huge text on divines"
    )
    night_audit = audit_theme_cvd(night_cb, min_cvd_delta_e=12.0, require_unique_shapes=True)
    assert night_audit.cvd_safe, f"Composed night CVD audit failed: {night_audit}"
    assert night_audit.duplicate_minimap_shapes == []
    assert night_audit.red_green_conflicts == []

    # Sanity-check that simulate_cvd_color & audit_theme_cvd catch actual red-vs-green hue traps
    bad_rg_theme = get_visual_theme("forbidden_abyss")
    bad_rg_theme.tiers["s"].text_color = RgbaColor(r=220, g=55, b=45)
    bad_rg_theme.tiers["s"].border_color = RgbaColor(r=220, g=55, b=45)
    bad_rg_theme.tiers["s"].background_color = RgbaColor(r=20, g=20, b=20, a=255)
    bad_rg_theme.tiers["s"].beam.color = "Red"  # type: ignore[union-attr]
    bad_rg_theme.tiers["a"].text_color = RgbaColor(r=55, g=175, b=50)
    bad_rg_theme.tiers["a"].border_color = RgbaColor(r=55, g=175, b=50)
    bad_rg_theme.tiers["a"].background_color = RgbaColor(r=20, g=20, b=20, a=255)
    bad_rg_theme.tiers["a"].beam.color = "Green"  # type: ignore[union-attr]
    bad_audit = audit_theme_cvd(bad_rg_theme, min_cvd_delta_e=12.0)
    assert not bad_audit.cvd_safe
    assert bad_audit.red_green_conflicts
    sim_red = simulate_cvd_color(RgbaColor(r=220, g=55, b=45), ColorVisionDeficiency.DEUTERANOPIA)
    assert abs(sim_red.r - sim_red.g) < 35  # dichromat R/G channels converge

    # 3. Synthesize 2 AM tinnitus-safe warm soundpack vs bright reference pack
    sp_gen = SoundPackGenerator()
    bright_pack = sp_gen.generate_pack(
        tmp_path / "bright",
        style=SoundPackStyle.CELESTIAL_CHIME,
        pack_id="bright_ref",
        params=SoundSynthesisParams(brightness=1.6, punch=1.5, export_mp3=False),
    )
    warm_pack = sp_gen.generate_pack(
        tmp_path / "warm",
        style=SoundPackStyle.CELESTIAL_CHIME,
        pack_id="night_tinnitus_safe",
        params=SoundSynthesisParams(
            musical_key="D",
            scale="pentatonic",
            brightness=0.75,
            punch=0.70,
            reverb_tail=0.80,
            warm_night_mode=True,
            lowpass_hz=2000.0,
            export_mp3=False,
        ),
    )

    pulse_counts: dict[str, int] = {}
    for tier_key in ("s_plus", "s", "a", "b", "c", "build_upgrade", "waystone_high", "chancing"):
        warm_clip = warm_pack.clips[tier_key]
        bright_clip = bright_pack.clips[tier_key]
        assert warm_clip.lowpass_hz_applied == 2000.0

        wl, _wr = read_stereo_wav(Path(warm_pack.pack_dir) / warm_clip.filename_wav)
        bl, _br = read_stereo_wav(Path(bright_pack.pack_dir) / bright_clip.filename_wav)
        warm_hf = measure_high_frequency_energy_ratio(wl, cutoff_hz=2500.0)
        bright_hf = measure_high_frequency_energy_ratio(bl, cutoff_hz=2500.0)
        assert warm_hf < 0.015, f"{tier_key} warm HF ratio {warm_hf} >= 1.5% above 2.5 kHz"
        assert warm_hf < bright_hf * 0.15

        pulses = detect_rhythmic_pulses(wl)
        pulse_counts[tier_key] = len(pulses)

    # Distinct rhythmic cadence across tiers even with >2.5 kHz rolled off
    assert pulse_counts["c"] == 1
    assert pulse_counts["b"] == 2
    assert pulse_counts["a"] == 3
    assert pulse_counts["s"] == 4
    assert pulse_counts["s_plus"] >= 5
    bu_pulses = detect_rhythmic_pulses(
        read_stereo_wav(Path(warm_pack.pack_dir) / "build_upgrade.wav")[0]
    )
    ws_pulses = detect_rhythmic_pulses(
        read_stereo_wav(Path(warm_pack.pack_dir) / "waystone_high.wav")[0]
    )
    assert len(bu_pulses) >= 3 and len(ws_pulses) >= 3
    assert abs((bu_pulses[1] - bu_pulses[0]) - (ws_pulses[1] - ws_pulses[0])) > 0.03

    # 4. Compile HC SSF very_strict filter, install into temp dir, and verify HTML preview
    fg = FilterGenerator()
    res = fg.generate(
        filter_name="hc_ssf_spirit_walker_2am",
        strictness=StrictnessTier.VERY_STRICT,
        theme=night_cb,
        sound_pack=warm_pack,
        require_custom_sounds=True,
        league="HC SSF Forbidden Rites",
        use_live_economy=False,
        build_context=sw_ctx,
        output_path=tmp_path / "staging" / "hc_ssf_spirit_walker_2am.filter",
    )
    assert res.validation.valid
    assert res.validation.error_count == 0
    assert '"Spears"' in res.filter_text
    assert '"Bucklers"' in res.filter_text
    assert '"Grand Spear"' in res.filter_text
    assert '"Desert Buckler"' in res.filter_text
    # Azmeri Companion Idols and HC SSF Exalted Orbs must remain shown on very_strict!
    assert "pob_azmeri_idols" in res.filter_text
    assert '"Bear Idol"' in res.filter_text
    parsed_sw = parse_filter_text(res.filter_text)
    hide_basetypes = {
        v
        for b in parsed_sw.blocks
        if b.visibility == "Hide"
        for st in b.statements
        if st.keyword == "BaseType"
        for v in st.values
    }
    assert "Exalted Orb" not in hide_basetypes

    install_dir = tmp_path / "Simulated PoE2 Folder"
    installed = install_filter_bundle(res, target_dir=install_dir, sound_pack=warm_pack)
    assert installed.is_file()
    assert res.validation.valid and res.validation.error_count == 0


def test_journey9_duo_coop_shared_filter_and_custom_voice_callouts(tmp_path: Path) -> None:
    """Journey 9: Duo Co-op (Martial Artist Carry + Lich/Chronomancer Support) + Voice Callouts."""
    idx = get_filter_gamedata_index()

    if get_pob2_paths().is_ready:
        from poe2brain.pob.service import BuildService

        svc = BuildService()
        try:
            svc.create_character("Monk", "Martial Artist", level=93, slot="coop_carry")
            svc.equip_item(
                "Rarity: RARE\nTempest Rod\nSlicing Quarterstaff\n"
                "Implicits: 0\n165% increased Physical Damage",
                target_slot="Weapon 1",
                slot="coop_carry",
            )
            svc.equip_item(
                "Rarity: RARE\nShadow Garb\nFlowing Raiment\n"
                "Implicits: 0\n+420 to maximum Energy Shield\n+780 to Evasion Rating",
                target_slot="Body Armour",
                slot="coop_carry",
            )

            svc.create_character("Witch", "Lich", level=92, slot="coop_support")
            svc.equip_item(
                "Rarity: RARE\nDread Chant\nOmen Sceptre\nImplicits: 0\n55% increased Spirit",
                target_slot="Weapon 1",
                slot="coop_support",
            )
            svc.equip_item(
                "Rarity: RARE\nVoid Ward\nTasalian Focus\n"
                "Implicits: 0\n+280 to maximum Energy Shield",
                target_slot="Weapon 2",
                slot="coop_support",
            )
            svc.equip_item(
                "Rarity: RARE\nHex Crown\nAncestral Tiara\n"
                "Implicits: 0\n+360 to maximum Energy Shield",
                target_slot="Helmet",
                slot="coop_support",
            )
            carry_ctx = extract_build_filter_context(svc, slot="coop_carry", index=idx)
            support_ctx = extract_build_filter_context(svc, slot="coop_support", index=idx)
        finally:
            svc.close()
    else:
        carry_ctx = BuildFilterContext(
            class_name="Monk",
            ascendancy_name="Martial Artist",
            level=93,
            main_skill="Tempest Flurry",
            weapon_filter_classes=["Quarterstaves"],
            offhand_filter_classes=[],
            preferred_armour_tags=["dex_int_armour", "int_armour", "dex_armour"],
            uncapped_resists=["fire"],
            equipped_bases=["Slicing Quarterstaff"],
        )
        support_ctx = BuildFilterContext(
            class_name="Witch",
            ascendancy_name="Lich",
            level=92,
            main_skill="Hexblast",
            weapon_filter_classes=["Sceptres"],
            offhand_filter_classes=["Foci"],
            preferred_armour_tags=["int_armour"],
            uncapped_resists=["chaos"],
            equipped_bases=["Omen Sceptre", "Tasalian Focus", "Ancestral Tiara"],
        )

    party_ctx = merge_build_filter_contexts([carry_ctx, support_ctx])
    assert "Quarterstaves" in party_ctx.weapon_filter_classes
    assert "Sceptres" in party_ctx.weapon_filter_classes
    assert "Foci" in party_ctx.offhand_filter_classes
    assert "int_armour" in party_ctx.preferred_armour_tags
    assert any(t in party_ctx.preferred_armour_tags for t in ("dex_int_armour", "dex_armour"))
    assert len(party_ctx.party_members) == 2

    # Custom spoken voice callout pack
    custom_lines = {
        "s_plus": "Split this Divine!",
        "build_upgrade": "Party gear upgrade!",
        "waystone_high": "Tier sixteen waystone!",
    }
    coop_pack = SoundPackGenerator().generate_pack(
        tmp_path / "audio",
        style=SoundPackStyle.VOICE_CALLOUT,
        pack_id="duo_party_callouts",
        params=SoundSynthesisParams(prefer_local_ai_tts=False, export_mp3=False),
        custom_callouts=custom_lines,
    )
    for tk, expected_line in custom_lines.items():
        clip = coop_pack.clips[tk]
        assert clip.callout_text == expected_line
        assert (Path(coop_pack.pack_dir) / clip.filename_wav).is_file()
        assert clip.duration_seconds >= 0.35

    # 1. Native FilterGenerator with multi-build `build_contexts=[carry_ctx, support_ctx]`
    fg = FilterGenerator()
    native_coop = fg.generate(
        filter_name="duo_coop_native",
        strictness=StrictnessTier.VERY_STRICT,
        theme="cyberpunk_neon",
        sound_pack=coop_pack,
        require_custom_sounds=True,
        use_live_economy=False,
        build_contexts=[carry_ctx, support_ctx],
        output_path=tmp_path / "compiled" / "duo_coop_native.filter",
    )
    assert native_coop.validation.valid
    assert native_coop.validation.error_count == 0
    for expected_token in (
        '"Quarterstaves"',
        '"Sceptres"',
        '"Foci"',
        '"Wrath Sceptre"',
        '"Omen Sceptre"',
        '"Tasalian Focus"',
        '"Ancestral Tiara"',
        "poe2brain_duo_party_callouts/s_plus.wav",
        "poe2brain_duo_party_callouts/build_upgrade.wav",
        "poe2brain_duo_party_callouts/waystone_high.wav",
    ):
        assert expected_token in native_coop.filter_text, (
            f"Missing {expected_token} in native co-op"
        )

    # 2. Restyled MIT NeverSink 4-VERY-STRICT with multi-build `build_contexts`
    ns_fixture = """# NeverSink's Indepth Loot Filter - for Path of Exile 2
# VERSION: 0.10.4 - 4-VERY-STRICT
Show # $type->currency $tier->s
\tClass == "Stackable Currency"
\tBaseType == "Divine Orb" "Mirror of Kalandra" "Fist of Kalguur"
\tSetFontSize 45
\tPlayAlertSound 6 300

Show # $type->waystones $tier->t16
\tClass == "Waystones"
\tWaystoneTier >= 15
\tSetFontSize 42
"""
    adapter = NeverSinkAdapter(generator=fg)
    ns_coop = adapter.restyle_and_heal(
        source_filter_text=None if os.environ.get("POE2_LIVE_TESTS") == "1" else ns_fixture,
        strictness=StrictnessTier.VERY_STRICT,
        theme="cyberpunk_neon",
        sound_pack=coop_pack,
        require_custom_sounds=True,
        build_contexts=[carry_ctx, support_ctx],
        filter_name="duo_coop_neversink_4_very_strict",
        output_path=tmp_path / "compiled" / "duo_coop_neversink.filter",
    )
    assert ns_coop.validation.valid, f"NS co-op diags: {ns_coop.validation.diagnostics[:5]}"
    assert ns_coop.validation.error_count == 0
    assert '"Quarterstaves"' in ns_coop.filter_text
    assert '"Sceptres"' in ns_coop.filter_text
    assert '"Foci"' in ns_coop.filter_text
    assert '"Omen Sceptre"' in ns_coop.filter_text
    assert '"Tasalian Focus"' in ns_coop.filter_text

    html_doc = render_filter_preview_html(
        get_visual_theme("cyberpunk_neon"),
        result=native_coop,
        sound_pack=coop_pack,
        output_path=tmp_path / "compiled" / "duo_preview.html",
    )
    assert "Split this Divine!" in html_doc
    assert "Party gear upgrade!" in html_doc
    assert "Tier sixteen waystone!" in html_doc
    assert "Martial Artist" in html_doc and "Lich" in html_doc


def test_journey10_broken_legacy_80_line_filter_doctor_and_auto_healer() -> None:
    """Journey 10: Auto-heal an 80+ line broken PoE1 / PoE2 0.1 .filter to 0 errors."""
    broken_legacy_filter = "\n".join(
        [
            "# ===============================================================",
            "# Migrated Custom Filter from PoE1 3.25 + PoE2 0.1 Early Access",
            "# ===============================================================",
            "",
            "# Block 1: Maps migrated badly from PoE1 with Continue before actions & FontSize 52",
            "Show",
            '\tClass "Map"',
            "\tMapTier >= 15",
            "\tContinue",
            "\tSetFontSize 52",
            "\tSetTextColor 255 240 150 255",
            "\tSetBorderColor 245 158 11 255",
            "\tSetBackgroundColor 35 20 10 245",
            "\tPlayAlertSound 4 300",
            "\tPlayEffect Orange",
            "\tMinimapIcon 0 Orange Square",
            "",
            "# Block 2: Pure PoE1 5-Link / Chromatic RGB Vendor Recipe (must be pruned!)",
            "Show",
            '\tSocketGroup "RGB"',
            "\tLinkedSockets >= 5",
            "\tSetFontSize 42",
            "\tSetTextColor 255 255 255 255",
            "\tPlayAlertSound 2 300",
            "",
            "# Block 3: Patch 0.1 Caster ES Body Armour with singular Class, Expert prefix & links",
            "Show",
            '\tClass "Body Armour"',
            '\tBaseType == "Expert Hexer\'s Robe"',
            '\tSocketGroup "RGB"',
            "\tLinkedSockets >= 5",
            "\tRarity Rare",
            "\tItemLevel >= 82",
            '\tHasExplicitMod >= 2 "Merciless"',
            "\tSetFontSize 52",
            "\tSetTextColor 120 255 190 255",
            "\tSetBorderColor 0 245 212 255",
            "\tSetBackgroundColor 12 34 30 248",
            "\tPlayAlertSound 5 300",
            "",
            "# Block 4: Patch 0.1 Caster Wand with singular Class, glued AdvancedAttuned Wand",
            "Show",
            '\tClass "Wand"',
            '\tBaseType == "AdvancedAttuned Wand"',
            "\tRarity Magic Rare",
            '\tHasExplicitMod >= 2 "Merciless"',
            "\tContinue",
            "\tSetFontSize 48",
            "\tSetTextColor 125 211 252 255",
            "\tPlayEffect Cyan Temp",
            "\tMinimapIcon 1 Cyan Diamond",
            "",
            "# Block 5: Renamed Patch 0.1 Expert Dualstring Bow -> Gemini Bow",
            "Show",
            '\tClass == "Bow"',
            '\tBaseType == "Expert Dualstring Bow"',
            "\tItemLevel >= 78",
            "\tSetFontSize 12",
            "\tSetTextColor 255 215 90 255",
            "",
            "# Block 6: Pure dead PoE1 / 0.4 currency & scarabs (entire block must be pruned!)",
            "Show",
            '\tBaseType == "Orb of Alteration" "Omen of Homogenising Exaltation" "Chaos Scarab"',
            "\tSetFontSize 45",
            "\tSetTextColor 255 0 0 255",
            "\tPlayAlertSound 6 300",
            "",
            "# Block 7: Mixed currency block with live Divine/Exalted + dead PoE1/0.4 currencies",
            "Show",
            '\tClass == "StackableCurrency"',
            (
                '\tBaseType == "Divine Orb" "Exalted Orb" "Orb of Alteration" '
                '"Omen of Homogenising Exaltation" "Chaos Scarab"'
            ),
            "\tSetFontSize 52",
            "\tSetTextColor 255 215 0 255",
            "\tSetBorderColor 255 70 120 255",
            "\tSetBackgroundColor 25 8 31 255",
            "\tPlayAlertSound 6 300",
            "\tPlayEffect Red",
            "\tMinimapIcon 0 Red Star",
            "",
            "# Block 8: Gold pile fallback",
            "Show",
            '\tBaseType == "Gold"',
            "\tStackSize >= 500",
            "\tSetFontSize 34",
            "",
        ]
    )
    assert len(broken_legacy_filter.splitlines()) >= 80

    # Confirm validator rejects all legacy traps on the raw filter first
    validator = FilterValidator()
    raw_val = validator.validate_text(broken_legacy_filter)
    assert not raw_val.valid
    assert raw_val.error_count >= 10
    raw_codes = {d.code for d in raw_val.diagnostics}
    assert {
        "internal_item_class_id",
        "poe1_legacy_condition",
        "statement_after_continue",
        "unknown_basetype",
        "dormant_or_removed_item",
        "has_explicit_mod_space",
        "font_size_out_of_range",
    }.issubset(raw_codes)

    # Auto-heal the broken legacy filter
    heal_rep = heal_filter_text(broken_legacy_filter)
    assert isinstance(heal_rep, FilterHealReport)
    assert heal_rep.valid
    assert heal_rep.validation.valid, f"Remaining diags: {heal_rep.validation.diagnostics}"
    assert heal_rep.validation.error_count == 0
    assert heal_rep.pruned_blocks == 2
    assert len(heal_rep.fixes_applied) >= 12

    healed = heal_rep.healed_text
    healed_code = "\n".join(ln for ln in healed.splitlines() if not ln.lstrip().startswith("#"))
    # MapTier -> WaystoneTier, PoE1 SocketGroup & LinkedSockets removed
    assert "WaystoneTier >= 15" in healed_code
    assert "MapTier" not in healed_code
    assert "SocketGroup" not in healed_code
    assert "LinkedSockets" not in healed_code

    # Singular Class -> plural .filter Class
    assert '"Body Armours"' in healed_code
    assert '"Wands"' in healed_code
    assert '"Waystones"' in healed_code
    assert '"Bows"' in healed_code
    assert '"Stackable Currency"' in healed_code

    # 0.1 Expert/Advanced stripped + Dualstring Bow -> Gemini Bow
    assert '"Hexer\'s Robe"' in healed_code
    assert '"Attuned Wand"' in healed_code
    assert '"Gemini Bow"' in healed_code
    assert "Expert Hexer's Robe" not in healed_code
    assert "AdvancedAttuned Wand" not in healed_code
    assert "Expert Dualstring Bow" not in healed_code

    # Dead PoE1 / 0.4 items pruned while live currencies survive
    assert "Orb of Alteration" not in healed_code
    assert "Omen of Homogenising Exaltation" not in healed_code
    assert "Chaos Scarab" not in healed_code
    assert '"Divine Orb"' in healed_code
    assert '"Exalted Orb"' in healed_code

    # HasExplicitMod whitespace glued & SetFontSize clamped to [18, 45]
    assert 'HasExplicitMod >=2 "Merciless"' in healed_code
    assert "HasExplicitMod >= 2" not in healed_code
    assert "SetFontSize 45" in healed_code
    assert "SetFontSize 18" in healed_code
    assert "SetFontSize 52" not in healed_code
    assert "SetFontSize 48" not in healed_code
    assert "SetFontSize 12" not in healed_code

    # Continue hoisted to the very end of blocks WITHOUT losing actions that were after Continue
    parsed_healed = parse_filter_text(healed)
    continue_blocks = [
        blk for blk in parsed_healed.blocks if any(s.keyword == "Continue" for s in blk.statements)
    ]
    assert len(continue_blocks) == 2
    for cblk in continue_blocks:
        assert cblk.statements[-1].keyword == "Continue"
        action_kws = {s.keyword for s in cblk.statements[:-1]}
        assert "SetFontSize" in action_kws
        assert "SetTextColor" in action_kws
        assert "PlayEffect" in action_kws
        assert "MinimapIcon" in action_kws
