from __future__ import annotations

from typing import Any

from poe2brain.models.builds import SlotStatWeights, StatWeightEntry
from poe2brain.models.common import AffixSide, SaleType
from poe2brain.models.items import ItemRarity, ModDomain
from poe2brain.models.trade import CurrencyQuote
from poe2brain.trade.advisor import TradeAdvisor
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle
from poe2brain.trade.item_parser import (
    parse_clipboard_item,
    parsed_item_to_pob2_text,
    resolve_trade_category,
    strip_wiki_markup,
    trade2_fetch_to_pob2_text,
)
from poe2brain.trade.stat_matcher import (
    build_item_trade_query,
    build_natural_trade_query,
    match_mod_line,
    resolve_natural_stat,
)

# ---------------------------------------------------------------------------
# 16 Real PoE2 Golden Item Fixtures (Ctrl+C & Ctrl+Alt+C)
# ---------------------------------------------------------------------------

FIXTURE_01_CROSSBOW_ALL_MODS = """Item Class: Crossbows
Rarity: Rare
Storm Core
Gemini Crossbow
--------
Quality: +20% (augmented)
Physical Damage: 74-231 (augmented)
Lightning Damage: 10-273 (lightning)
Critical Hit Chance: 5.00%
Attacks per Second: 1.89 (augmented)
Reload Time: 0.93 (augmented)
--------
Requires: Level 78, 89 Str, 89 Dex
--------
Sockets: S S
--------
Item Level: 80
--------
45% increased Elemental Damage with Attacks (enchant)
--------
18% increased Physical Damage (rune)
Gain 30 Mana per enemy killed (rune)
--------
{ Implicit Modifier — Attack }
Loads an additional bolt
--------
Grants Skill: Level 18 Cackling Companions
--------
{ Fractured Prefix Modifier "Electrocuting" (Tier: 2) — Damage, Elemental, Lightning, Attack }
Adds 10(1-16) to 273(239-300) Lightning Damage
{ Prefix Modifier "Razor-sharp" (Tier: 3) — Damage, Physical, Attack }
Adds 24(23-35) to 51(39-59) Physical Damage
{ Prefix Modifier "Overpowering" (Tier: 2) — Damage, Elemental, Attack }
109(100-119)% increased Elemental Damage with Attacks
{ Suffix Modifier "of the Drought" (Tier: 1) — Mana, Physical, Attack }
Leeches 7.59(7-7.9)% of Physical Damage as Mana
{ Desecrated Suffix Modifier "of Siphoning" (Tier: 3) — Mana }
Gain 21(21-27) Mana per enemy killed
{ Suffix Modifier "of Acclaim" (Tier: 1) — Attack, Speed }
18(17-19)% increased Attack Speed
--------
Corrupted
--------
Fractured Item
"""

FIXTURE_02_PLAIN_CTRL_C_BONDED_BOW = """Item Class: Bows
Rarity: Rare
Honour Mark
Dualstring Bow
--------
Quality: +20% (augmented)
Physical Damage: 32-59 (augmented)
Elemental Damage: 10-18 (fire), 8-12 (cold), 1-10 (lightning)
Critical Hit Chance: 5.00%
Attacks per Second: 1.15
--------
Requires: Level 48, 86 Dex
--------
Sockets: S S
--------
Item Level: 82
--------
14% increased Physical Damage (rune)
Adds 1 to 10 Lightning Damage (rune)
Bonded: 20% increased effect of Fully Broken Armour (rune)
Bonded: 30% increased Magnitude of Shock you inflict (rune)
--------
+50% Surpassing chance to fire an additional Arrow (implicit)
--------
27% increased Physical Damage
Adds 10 to 18 Fire Damage
Adds 8 to 12 Cold Damage
+63 to Accuracy Rating
+13% to Critical Damage Bonus
Gain 13 Life per enemy killed
Leeches 5.62% of Physical Damage as Mana (desecrated)
"""

FIXTURE_03_SCEPTRE_SPIRIT = """Item Class: Sceptres
Rarity: Rare
Skull Song
Shrine Sceptre
--------
Spirit: 152 (augmented)
--------
Requires: Level 72, 39 Str, 98 Int
--------
Sockets: S
--------
Item Level: 79
--------
Grants Skill: Level 17 Purity of Lightning
--------
{ Prefix Modifier "Duke's" (Tier: 3) }
52(51-55)% increased Spirit
{ Prefix Modifier "Opalescent" (Tier: 5) — Mana }
+83(80-89) to maximum Mana
{ Suffix Modifier "of the Tutor" (Tier: 5) — Life, Minion }
Minions have 28(26-30)% increased maximum Life
{ Suffix Modifier "of the Sage" (Tier: 3) — Attribute }
+27(25-27) to Intelligence
"""

FIXTURE_04_AMULET_CATALYST_ANOINT = """Item Class: Amulets
Rarity: Rare
Brood Locket
Gold Amulet
--------
Quality (Caster Modifiers): +20% (augmented)
--------
Requires: Level 60
--------
Item Level: 81
--------
Allocates Cooked (enchant)
--------
{ Implicit Modifier }
18(12-20)% increased Rarity of Items found
--------
{ Prefix Modifier "Lady's" (Tier: 5) }
+30(30-33) to Spirit
{ Prefix Modifier "Gentian" (Tier: 6) — Mana }
+90(90-104) to maximum Mana
{ Prefix Modifier "Incanter's" (Tier: 1) — Damage, Caster }
29(27-30)% increased Spell Damage
{ Fractured Suffix Modifier "of the Ice" (Tier: 2) — Elemental, Cold, Resistance }
+37(36-40)% to Cold Resistance
{ Suffix Modifier "of the Sorcerer" (Tier: 1) — Caster, Gem }
+3 to Level of all Spell Skills
{ Desecrated Suffix Modifier "of Amanamu" (Tier: 1) — Elemental, Fire, Chaos, Resistance }
+17(13-17)% to Fire and Chaos Resistances
--------
Fractured Item
"""

FIXTURE_05_SANCTIFIED_RING = """Item Class: Rings
Rarity: Rare
Vortex Finger
Prismatic Ring
--------
Requires: Level 62
--------
Item Level: 82
--------
+10% to all Elemental Resistances (implicit)
--------
+145 to maximum Life
+38% to Fire Resistance
+35% to Lightning Resistance
+23% to Chaos Resistance
--------
Sanctified
"""

FIXTURE_06_TWICE_CORRUPTED_THREE_RUNES = """Item Class: Body Armours
Rarity: Rare
Hate Pelt
Soldier Cuirass
--------
Quality: +28% (augmented)
Armour: 3075 (augmented)
--------
Requires: Level 65, 121 (unmet) Str
--------
Sockets: S S S
--------
Item Level: 80
--------
54% increased Armour, Evasion and Energy Shield (rune)
--------
{ Prefix Modifier "Impenetrable" (Tier: 1) — Defences }
103(101-110)% increased Armour
{ Prefix Modifier "Unmoving" (Tier: 2) — Defences }
+256(226-256) to Armour (desecrated)
{ Suffix Modifier "of the Titan" (Tier: 1) — Attribute }
+32(31-33) to Strength
--------
Twice Corrupted
--------
Note: ~b/o 10 divine
"""

FIXTURE_07_UNIQUE_FOCUS = """Item Class: Foci
Rarity: Unique
The Eternal Spark
Crystal Focus
--------
Energy Shield: 44 (augmented)
--------
Requires: Level 26, 43 (augmented) Int
--------
Item Level: 81
--------
{ Unique Modifier — Defences }
56(50-70)% increased Energy Shield
{ Unique Modifier — Mana }
40% increased Mana Regeneration Rate while stationary
{ Unique Modifier — Elemental, Lightning, Resistance }
+26(20-30)% to Lightning Resistance
{ Unique Modifier — Elemental, Lightning, Resistance }
+5% to Maximum Lightning Resistance
{ Unique Modifier — Mana }
40% increased Mana Regeneration Rate
--------
A flash of blue, a stormcloud's kiss,
her motionless dance the pulse of bliss
"""

FIXTURE_08_UNIDENTIFIED_TIER_RING = """Item Class: Rings
Rarity: Magic
Sapphire Ring
--------
Item Level: 54
--------
{ Implicit Modifier — Elemental, Cold, Resistance }
+23(20-30)% to Cold Resistance
--------
Unidentified (Tier 4)
"""

FIXTURE_09_WAYSTONE_T16 = """Item Class: Waystones
Rarity: Rare
Blasted Control
Waystone (Tier 16)
--------
Revives Available: 0 (augmented)
Pack Size: +20% (augmented)
Magic Monsters: +30% (augmented)
Rare Monsters: +71% (augmented)
Waystone Drop Chance: +90% (augmented)
Item Rarity: +17% (augmented)
Monster Rarity: +32% (augmented)
Monster Effectiveness: +45% (augmented)
--------
Item Level: 79
--------
{ Prefix Modifier "Painful" (Tier: 1) }
30(26-30)% increased Monster Damage
{ Prefix Modifier "Slowing" (Tier: 1) }
Players are periodically Cursed with Temporal Chains — Unscalable Value
{ Suffix Modifier "of Drought" (Tier: 1) }
Players gain 33(35-30)% reduced Flask Charges
--------
Can be used in a Map Device, allowing you to enter a Map. Waystones can only be used once.
--------
Corrupted
"""

FIXTURE_10_PRECURSOR_TABLET = """Item Class: Tablet
Rarity: Rare
Planar Challenge
Delirium Precursor Tablet
--------
Item Level: 84
--------
{ Implicit Modifier }
Adds a Mirror of Delirium to a Map
17 uses remaining
--------
{ Prefix Modifier "Breeding" }
7(4-10)% increased Pack Size in Map
{ Suffix Modifier "of the Simulacrum" (Tier: 1) }
6(10-30)% increased Stack size of Simulacrum Splinters found in Map
--------
Can be used in a personal Map Device to add modifiers to a Map.
--------
Corrupted
--------
Note: ~b/o 1 exalted
"""

FIXTURE_11_INSCRIBED_ULTIMATUM = """Item Class: Inscribed Ultimatum
Rarity: Currency
Inscribed Ultimatum
--------
Area Level: 80
Number of Trials: 10
Deadly
--------
Item Level: 80
"""

FIXTURE_12_CHARM = """Item Class: Charms
Rarity: Magic
Sprouting Silver Charm of the Bountiful
--------
Quality: +14% (augmented)
Lasts 3.40 (augmented) Seconds
Consumes 20 of 60 (augmented) Charges on use
Currently has 0 Charges
Your speed is unaffected by Slows
--------
Requires: Level 37
--------
Item Level: 80
--------
{ Implicit Modifier }
Used when you are affected by a Slow — Unscalable Value
--------
{ Prefix Modifier "Sprouting" (Tier: 5) — Charm, Life }
Recover 106(96-130) Life when Used
{ Suffix Modifier "of the Bountiful" (Tier: 3) }
51(47-54)% increased Charges
--------
Used automatically when condition is met. Can only hold charges while in belt.
"""

FIXTURE_13_UNCUT_SKILL_GEM = """Item Class: Uncut Skill Gems
Rarity: Currency
Uncut Skill Gem (Level 19)
--------
Creates a Skill Gem or Level an existing gem to level 19
--------
Right Click to engrave a Skill Gem.
"""

FIXTURE_14_META_SKILL_GEM = """Rarity: Gem
Mirage Archer
--------
Buff, Persistent, Trigger, Duration, Meta
Level: 14
Reservation: 60 Spirit
--------
Requires: Level 58, 103 Dex
Requires: Spear, Bow, Crossbow
--------
Sockets: G G G G
--------
While active, dodge rolling will create a Mirage that uses socketed ranged Attacks.
--------
Place one or more Skill Gems into this Meta Gem's sockets in the Skills Panel.
"""

FIXTURE_15_ACTIVE_SKILL_GEM = """Item Class: Skill Gems
Rarity: Gem
Spark
--------
Spell, AoE, Duration, Lightning, Projectile
Level: 20 (Max)
Quality: +20% (augmented)
--------
Requires: Level 90, 205 Int
--------
Sockets: G G G G G
--------
Corrupted
"""

FIXTURE_16_JEWEL_UNREVEALED_DESECRATED = """Item Class: Jewels
Rarity: Rare
Hypnotic Sliver
Emerald
--------
Radius: Large
Limited to: 1
--------
Item Level: 81
--------
14% increased Projectile Damage
7% increased Attack Speed
Desecrated Prefix
Desecrated Suffix
"""


def test_ee2_bundle_load_and_attribution() -> None:
    bundle = get_default_bundle()
    assert isinstance(bundle, EE2Bundle)
    assert (bundle.data_dir / "LICENSE.Exiled-Exchange-2").is_file()
    assert bundle.manifest["license"] == "MIT"
    assert bundle.manifest["stat_count"] >= 2500
    assert bundle.manifest["item_count"] >= 4000
    assert bundle.manifest["remnant_recipe_count"] >= 150
    assert bundle.manifest["official_trade2_stat_count"] >= 7000
    assert bundle.trade_tag_by_name["Divine Orb"] == "divine"
    assert bundle.extract_magic_base_type("Crackling Temple Maul of the Brute") == "Temple Maul"


def test_parse_16_diverse_poe2_items() -> None:
    bundle = get_default_bundle()

    # 1. Crossbow with all modifier types
    xbow = parse_clipboard_item(FIXTURE_01_CROSSBOW_ALL_MODS, bundle)
    assert resolve_trade_category(xbow, bundle) == "weapon.crossbow"
    assert xbow.rarity == ItemRarity.RARE
    assert xbow.name == "Storm Core"
    assert xbow.base_type == "Gemini Crossbow"
    assert xbow.quality == 20
    assert xbow.reload_time == 0.93
    assert xbow.rune_sockets == 2
    assert xbow.item_level == 80
    assert xbow.requirements.level == 78
    assert xbow.requirements.strength == 89 and xbow.requirements.dexterity == 89
    assert xbow.corrupted is True and xbow.fractured is True
    assert xbow.physical_dps == round(152.5 * 1.89, 2)
    assert xbow.elemental_dps == round(141.5 * 1.89, 2)
    assert "Level 18 Cackling Companions" in xbow.granted_skills
    frac_mods = [m for m in xbow.mods if m.domain == ModDomain.FRACTURED]
    assert len(frac_mods) == 1
    assert frac_mods[0].raw_text == "Adds 10 to 273 Lightning Damage"
    assert frac_mods[0].affix_side == AffixSide.PREFIX
    assert frac_mods[0].display_tier == 2
    assert frac_mods[0].roll_ranges[0].min == 1.0 and frac_mods[0].roll_ranges[0].max == 16.0
    assert frac_mods[0].trade_stat_ids

    # 2. Plain Ctrl+C Bow with Bonded Shaman Runes
    bow = parse_clipboard_item(FIXTURE_02_PLAIN_CTRL_C_BONDED_BOW, bundle)
    assert resolve_trade_category(bow, bundle) == "weapon.bow"
    rune_mods = [m for m in bow.mods if m.domain == ModDomain.RUNE]
    assert len(rune_mods) == 4
    assert sum(1 for m in rune_mods if m.bonded) == 2
    assert any(m.domain == ModDomain.DESECRATED for m in bow.mods)

    # 3. Sceptre with Spirit & Granted Skill
    sceptre = parse_clipboard_item(FIXTURE_03_SCEPTRE_SPIRIT, bundle)
    assert sceptre.spirit == 152
    assert sceptre.rune_sockets == 1
    assert resolve_trade_category(sceptre, bundle) == "weapon.sceptre"
    assert "Level 17 Purity of Lightning" in sceptre.granted_skills

    # 4. Amulet with Catalyst Quality + Anoint Enchant + Fractured + Desecrated
    amulet = parse_clipboard_item(FIXTURE_04_AMULET_CATALYST_ANOINT, bundle)
    assert amulet.quality == 20
    assert amulet.catalyst_type == "Caster Modifiers"
    ench = [m for m in amulet.mods if m.domain == ModDomain.ENCHANT]
    assert ench[0].raw_text == "Allocates Cooked"
    assert "|" in ench[0].trade_stat_ids[0]
    assert amulet.fractured is True

    # 5. Sanctified Ring
    sanc = parse_clipboard_item(FIXTURE_05_SANCTIFIED_RING, bundle)
    assert sanc.sanctified is True
    assert sanc.corrupted is False

    # 6. Twice Corrupted 3-Socket Body Armour + Note
    cuirass = parse_clipboard_item(FIXTURE_06_TWICE_CORRUPTED_THREE_RUNES, bundle)
    assert cuirass.corrupted is True
    assert cuirass.twice_corrupted is True
    assert cuirass.rune_sockets == 3
    assert cuirass.armour == 3075
    assert cuirass.note == "~b/o 10 divine"

    # 7. Unique Focus (strips flavour prose cleanly)
    focus = parse_clipboard_item(FIXTURE_07_UNIQUE_FOCUS, bundle)
    assert focus.rarity == ItemRarity.UNIQUE
    assert focus.name == "The Eternal Spark"
    assert focus.base_type == "Crystal Focus"
    assert resolve_trade_category(focus, bundle) == "armour.focus"
    assert len(focus.mods) == 5

    # 8. Unidentified Tier 4 Magic Ring
    unid = parse_clipboard_item(FIXTURE_08_UNIDENTIFIED_TIER_RING, bundle)
    assert unid.identified is False
    assert unid.unidentified_tier == 4
    assert unid.base_type == "Sapphire Ring"

    # 9. Waystone Tier 16
    ws = parse_clipboard_item(FIXTURE_09_WAYSTONE_T16, bundle)
    assert resolve_trade_category(ws, bundle) == "map.waystone"
    assert ws.waystone_tier == 16
    assert ws.waystone_revives == 0
    assert ws.waystone_drop_chance_pct == 90
    assert any(m.unscalable for m in ws.mods)

    # 10. Precursor Tablet
    tab = parse_clipboard_item(FIXTURE_10_PRECURSOR_TABLET, bundle)
    assert resolve_trade_category(tab, bundle) == "map.tablet"
    assert len([m for m in tab.mods if m.domain == ModDomain.IMPLICIT]) == 2
    assert tab.corrupted is True

    # 11. Inscribed Ultimatum
    ult = parse_clipboard_item(FIXTURE_11_INSCRIBED_ULTIMATUM, bundle)
    assert ult.area_level == 80
    assert resolve_trade_category(ult, bundle) == "map.ultimatum"

    # 12. Charm
    charm = parse_clipboard_item(FIXTURE_12_CHARM, bundle)
    assert resolve_trade_category(charm, bundle) == "flask.charm"
    assert charm.base_type == "Silver Charm"
    assert charm.quality == 14
    assert len([m for m in charm.mods if m.domain == ModDomain.EXPLICIT]) == 2

    # 13. Uncut Skill Gem (Level 19)
    uncut = parse_clipboard_item(FIXTURE_13_UNCUT_SKILL_GEM, bundle)
    assert uncut.gem_level == 19
    assert uncut.base_type == "Uncut Skill Gem"
    assert len(uncut.mods) == 0

    # 14. Meta Skill Gem (missing Item Class:)
    meta = parse_clipboard_item(FIXTURE_14_META_SKILL_GEM, bundle)
    assert meta.rarity == ItemRarity.GEM
    assert resolve_trade_category(meta, bundle) == "gem.metagem"
    assert meta.gem_level == 14
    assert meta.spirit_reservation == 60
    assert meta.gem_sockets == 4

    # 15. Active 5-Socket Corrupted 20/20 Skill Gem
    spark = parse_clipboard_item(FIXTURE_15_ACTIVE_SKILL_GEM, bundle)
    assert resolve_trade_category(spark, bundle) == "gem.activegem"
    assert spark.gem_level == 20
    assert spark.quality == 20
    assert spark.gem_sockets == 5
    assert spark.corrupted is True

    # 16. Jewel with Unrevealed Desecrated affixes
    jwl = parse_clipboard_item(FIXTURE_16_JEWEL_UNREVEALED_DESECRATED, bundle)
    assert resolve_trade_category(jwl, bundle) == "jewel"
    assert jwl.veiled_desecrated is True
    assert len(jwl.mods) == 2


def test_trade2_fetch_to_pob2_text_conversion() -> None:
    assert (
        strip_wiki_markup("[ManaLeech|Leeches] 5.62% of [Physical] Damage")
        == "Leeches 5.62% of Physical Damage"
    )

    sample_fetch_entry = {
        "id": "abc123",
        "listing": {"price": {"type": "~b/o", "amount": 4, "currency": "exalted"}, "fee": 12000},
        "item": {
            "realm": "poe2",
            "name": "Honour Mark",
            "typeLine": "Dualstring Bow",
            "baseType": "Dualstring Bow",
            "rarity": "Rare",
            "ilvl": 82,
            "corrupted": True,
            "sockets": [{"group": 0, "type": "rune"}, {"group": 1, "type": "rune"}],
            "properties": [
                {"name": "[Quality]", "values": [["+20%", 1]], "type": 6},
                {"name": "[Physical] Damage", "values": [["32-59", 1]], "type": 9},
            ],
            "requirements": [{"name": "Level", "values": [["48", 0]], "type": 62}],
            "runeMods": [
                {"description": "14% increased [Physical] Damage", "domain": "rune"},
                {
                    "description": (
                        "[ShamanOnlyMods|Bonded]: 20% increased effect of "
                        "[ArmourBreak|Fully Broken Armour]"
                    ),
                    "domain": "rune",
                },
            ],
            "implicitMods": [
                {
                    "description": (
                        "+50% [SurpassChance|Surpassing] chance to fire an additional Arrow"
                    ),
                    "domain": "implicit",
                }
            ],
            "explicitMods": [
                {"description": "27% increased [Physical] Damage", "domain": "explicit"},
                {
                    "description": (
                        "[ManaLeech|Leeches] 5.62% of [Physical|Physical] Damage as Mana"
                    ),
                    "flags": {"desecrated": True},
                    "domain": "desecrated",
                },
            ],
        },
    }

    pob_raw = trade2_fetch_to_pob2_text(sample_fetch_entry)
    assert "Rarity: RARE\nHonour Mark\nDualstring Bow" in pob_raw
    assert "Quality: 20" in pob_raw
    assert "Sockets: S S" in pob_raw
    assert "LevelReq: 48" in pob_raw
    assert "Item Level: 82" in pob_raw
    assert "Implicits: 3" in pob_raw
    assert "{rune}14% increased Physical Damage" in pob_raw
    assert "{rune}Bonded: 20% increased effect of Fully Broken Armour" in pob_raw
    assert "{implicit}+50% Surpassing chance to fire an additional Arrow" in pob_raw
    assert "{desecrated}Leeches 5.62% of Physical Damage as Mana" in pob_raw
    assert pob_raw.strip().endswith("Corrupted")

    xbow = parse_clipboard_item(FIXTURE_01_CROSSBOW_ALL_MODS)
    xbow_pob = parsed_item_to_pob2_text(xbow)
    assert "{fractured}Adds 10 to 273 Lightning Damage" in xbow_pob
    assert "{desecrated}Gain 21 Mana per enemy killed" in xbow_pob


def test_stat_matcher_and_weighted_query_builder() -> None:
    bundle = get_default_bundle()

    m_life = match_mod_line("+142 to maximum Life", "explicit", bundle=bundle)
    assert m_life is not None
    assert m_life.trade_ids == ["explicit.stat_3299347043"]
    assert m_life.value == 142.0

    m_as = match_mod_line("18% increased Attack Speed", "explicit", bundle=bundle)
    assert m_as is not None
    assert m_as.ambiguous_local_global is True
    as_group = m_as.to_trade_stat_group()
    assert as_group["kind"] == "group"
    assert as_group["group"]["type"] == "count"
    assert as_group["group"]["value"] == {"min": 1}

    m_bonded = match_mod_line(
        "Bonded: 20% increased effect of Fully Broken Armour", "rune", bundle=bundle
    )
    assert m_bonded is not None
    assert m_bonded.trade_ids == ["rune.stat_1039491398"]

    nl_light = resolve_natural_stat("+level of all lightning spell skills", bundle=bundle)
    assert nl_light and "Lightning Spell Skills" in nl_light[0].ref

    nl_sp_sceptre = resolve_natural_stat("spirit", category="Sceptre", bundle=bundle)
    assert nl_sp_sceptre and nl_sp_sceptre[0].equipment_filter_key == "spirit"

    nl_sp_amulet = resolve_natural_stat("spirit", category="Amulet", bundle=bundle)
    assert nl_sp_amulet and nl_sp_amulet[0].equipment_filter_key is None

    weights = SlotStatWeights(
        equipment_slot="Amulet",
        objective="dps",
        weights=[
            StatWeightEntry(
                stat_id="",
                stat_label="+level of all lightning spell skills",
                weight=25.0,
            ),
            StatWeightEntry(
                stat_id="explicit.stat_3299347043",
                stat_label="maximum life",
                weight=0.5,
            ),
        ],
    )
    w_payload = build_natural_trade_query(
        category="Amulet",
        weighted_stats=weights,
        min_weight=20.0,
        max_price_exalted=100,
        bundle=bundle,
    )
    assert w_payload["sort"] == {"statgroup.0": "desc"}
    assert w_payload["query"]["stats"][0]["type"] == "weight"
    assert w_payload["query"]["stats"][0]["value"] == {"min": 20.0}

    amulet = parse_clipboard_item(FIXTURE_04_AMULET_CATALYST_ANOINT, bundle)
    amulet_q = build_item_trade_query(amulet, roll_tolerance=0.9, bundle=bundle)
    assert amulet_q["query"]["filters"]["type_filters"]["filters"]["category"] == {
        "option": "accessory.amulet"
    }


def test_trade_advisor_routing_and_deduplication() -> None:
    class StubEconomy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_price(
            self, query_or_id: str, league: str = "Forbidden Rites"
        ) -> CurrencyQuote | None:
            self.calls.append(query_or_id)
            if "Uncut Skill Gem" in query_or_id:
                return CurrencyQuote(
                    id="uncut-skill-gem-19",
                    name=query_or_id,
                    category="uncutgems",
                    league=league,
                    exalted=45.0,
                    divine=0.1,
                    source="poe2scout",
                )
            if query_or_id == "Sapphire Ring":
                return None
            return None

        def convert_to_exalted(
            self, amount: float, currency: str, league: str = "Forbidden Rites"
        ) -> float:
            rates = {"exalted": 1.0, "divine": 450.0, "chaos": 36.0}
            return amount * rates.get(currency, 1.0)

    class StubTrade2:
        def __init__(self) -> None:
            self.last_payload: dict[str, Any] | None = None

        def search(
            self, payload: dict[str, Any], league: str = "Forbidden Rites"
        ) -> dict[str, Any]:
            self.last_payload = payload
            return {"id": "q_stub_123", "total": 4, "result": ["id1", "id2", "id3", "id4"]}

        def fetch(self, ids: list[str], query_id: str = "") -> list[dict[str, Any]]:
            return [
                {
                    "id": "id1",
                    "listing": {
                        "price": {"amount": 1, "currency": "exalted"},
                        "account": {"name": "FixerGuy#0001", "online": None},
                        "whisper": "@Fixer hi",
                    },
                    "item": {
                        "name": "Brood Locket",
                        "baseType": "Gold Amulet",
                        "rarity": "Rare",
                        "ilvl": 81,
                    },
                },
                {
                    "id": "id2",
                    "listing": {
                        "price": {"amount": 1, "currency": "exalted"},
                        "account": {"name": "FixerGuy#0001", "online": None},
                        "whisper": "@Fixer hi",
                    },
                    "item": {
                        "name": "Brood Locket",
                        "baseType": "Gold Amulet",
                        "rarity": "Rare",
                        "ilvl": 81,
                    },
                },
                {
                    "id": "id3",
                    "listing": {
                        "price": {"amount": 1, "currency": "exalted"},
                        "account": {"name": "FixerGuy#0001", "online": None},
                        "whisper": "@Fixer hi",
                    },
                    "item": {
                        "name": "Brood Locket",
                        "baseType": "Gold Amulet",
                        "rarity": "Rare",
                        "ilvl": 81,
                    },
                },
                {
                    "id": "id4",
                    "listing": {
                        "price": {"amount": 25, "currency": "exalted"},
                        "fee": 8500,
                        "account": {"name": "HonestSeller#9999", "online": None},
                    },
                    "item": {
                        "name": "Honest Torc",
                        "baseType": "Gold Amulet",
                        "rarity": "Rare",
                        "ilvl": 82,
                        "explicitMods": [{"description": "+30 to [Spirit]", "domain": "explicit"}],
                    },
                },
            ]

    econ = StubEconomy()
    t2 = StubTrade2()
    advisor = TradeAdvisor(economy=econ, trade2=t2)

    # 1. Uncut Skill Gem fast-routes to EconomyService without touching Trade2
    gem_res = advisor.price_check_item(FIXTURE_13_UNCUT_SKILL_GEM)
    assert gem_res.estimated_exalted == 45.0
    assert t2.last_payload is None

    # 2. Rare Amulet routes to Trade2, deduplicating FixerGuy#0001 and flagging Instant Buyout fee
    rare_res = advisor.price_check_item(FIXTURE_04_AMULET_CATALYST_ANOINT, securable_only=True)
    assert t2.last_payload is not None
    assert t2.last_payload["query"]["status"] == {"option": "securable"}
    assert rare_res.distribution.count_total == 4
    assert rare_res.distribution.count_deduplicated == 2
    assert rare_res.distribution.count_securable == 1
    assert rare_res.distribution.securable_min_exalted == 25.0
    assert rare_res.fixer_warnings
    honest = next(lst for lst in rare_res.listings if lst.listing_id == "id4")
    assert honest.sale_type == SaleType.SECURABLE
    assert honest.is_instant_buyout is True
    assert honest.pob_item_text is not None
    assert "+30 to Spirit" in honest.pob_item_text
    assert getattr(honest, "pob2_item_text", None) == honest.pob_item_text


def test_trade_advisor_live_candidates_with_pob2_text() -> None:
    """Live round-trip: find real PoE2 Boots upgrades in Forbidden Rites with ready PoB2 text."""
    advisor = TradeAdvisor()
    res = advisor.find_trade_candidates(
        league="Forbidden Rites",
        slot_or_category="Boots",
        stat_filters_or_weights=[
            ("movement speed", 25, None),
            ("maximum life", 80, None),
        ],
        max_price_exalted=25,
        securable_only=True,
        limit=3,
    )
    assert res.total_matches > 0
    assert res.trade_url and "Forbidden%20Rites" in res.trade_url
    assert len(res.listings) > 0
    first = res.listings[0]
    assert first.is_instant_buyout is True
    assert first.fee is not None and first.fee > 0
    assert first.pob_item_text and first.pob_item_text.startswith("Rarity: ")
    assert "increased Movement Speed" in first.pob_item_text
