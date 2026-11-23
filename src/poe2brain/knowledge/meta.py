from __future__ import annotations

import difflib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    POE2_PATCH,
    POENINJA_POE2_BASE_URL,
    get_pob2_paths,
)
from poe2brain.http import Poe2HttpClient, get_cached_json
from poe2brain.models.common import Poe2Model
from poe2brain.trade.economy import (
    POE2SCOUT_UNIQUE_CATEGORIES,
    EconomyService,
)

DEFAULT_META_TTL_SECONDS: float = 1800.0  # 30 minutes
GGG_LADDER_BASE_URL: str = "https://www.pathofexile.com/api/ladders"
GGG_OAUTH_API_BASE_URL: str = "https://api.pathofexile.com"
POENINJA_BUILD_INDEX_URL: str = f"{POENINJA_POE2_BASE_URL}/data/build-index-state"

_GRANTS_SKILL_RE = re.compile(r"^Grants(?:\s+Skill)?:\s*(.+)$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# PoE1-only classes/ascendancies (plus unreleased 1.0 Shadow/Duelist) that never belong in
# Patch 0.5.5 PoE2 ladders or class queries.
POE1_OR_UNRELEASED_TRAP_CLASSES: dict[str, str] = {
    "scion": "Scion does not exist in Path of Exile 2.",
    "ascendant": "Ascendant (Scion) does not exist in Path of Exile 2.",
    "marauder": (
        "Marauder is a PoE1 class; check Warrior "
        "(Titan / Warbringer / Smith of Kitava) or Druid in PoE2 0.5.5."
    ),
    "juggernaut": (
        "Juggernaut is a PoE1 Marauder ascendancy; check Warrior "
        "(Titan / Smith of Kitava / Warbringer) in PoE2 0.5.5."
    ),
    "berserker": (
        "Berserker is a PoE1 Marauder ascendancy; check Warrior or Shaman "
        "(Rage scaling) in PoE2 0.5.5."
    ),
    "chieftain": (
        "Chieftain is a PoE1 Marauder ascendancy; check Smith of Kitava "
        "or Warbringer in PoE2 0.5.5."
    ),
    "templar": (
        "Templar is not in PoE2 0.5.5; check Druid (Oracle / Shaman) "
        "or Monk (Invoker) instead."
    ),
    "inquisitor": (
        "Inquisitor is a PoE1 Templar ascendancy; check Monk Invoker "
        "('Sunder my Enemies...') or Oracle ('Forced Outcome') in PoE2 0.5.5."
    ),
    "hierophant": (
        "Hierophant is a PoE1 Templar ascendancy; check Sorceress "
        "(Stormweaver / Disciple of Varashta) or Druid (Oracle) in PoE2 0.5.5."
    ),
    "guardian": (
        "Guardian is a PoE1 Templar ascendancy; check Mercenary Tactician "
        "or Sorceress Disciple of Varashta in PoE2 0.5.5."
    ),
    "shadow": (
        "Shadow is not playable in PoE2 Patch 0.5.5 (scheduled for Version 1.0); "
        "check Monk (Martial Artist / Invoker / Acolyte of Chayula) or Huntress."
    ),
    "assassin": (
        "Assassin (Shadow) is not in PoE2 0.5.5; check Blood Mage "
        "('Sunder the Flesh'), Oracle ('Forced Outcome'), or Amazon ('Critical Strike')."
    ),
    "trickster": (
        "Trickster (Shadow) is not in PoE2 0.5.5; check Monk Invoker / "
        "Martial Artist or Sorceress Disciple of Varashta."
    ),
    "saboteur": "Saboteur (Shadow) is not in PoE2 0.5.5.",
    "duelist": (
        "Duelist is not playable in PoE2 Patch 0.5.5 (scheduled for Version 1.0); "
        "check Mercenary (Gemling Legionnaire / Witchhunter / Tactician) or Huntress."
    ),
    "slayer": (
        "Slayer (Duelist) is not in PoE2 0.5.5; check Amazon ('Mystic Harvest') "
        "or Witchhunter ('Pitiless Killer' / 'Judge, Jury, and Executioner')."
    ),
    "gladiator": (
        "Gladiator (Duelist) is not in PoE2 0.5.5; check Warbringer "
        "('Turtle Charm' / 'Renly's Training')."
    ),
    "champion": (
        "Champion (Duelist) is not in PoE2 0.5.5; check Mercenary Tactician "
        "('Polish That Gear' / 'Whoever Pays Best')."
    ),
    "necromancer": (
        "Necromancer is a PoE1 Witch ascendancy; check Witch "
        "(Infernalist / Lich / Abyssal Lich) or Sorceress (Disciple of Varashta) in PoE2 0.5.5."
    ),
    "elementalist": (
        "Elementalist is a PoE1 Witch ascendancy; check Sorceress Stormweaver "
        "or Druid Shaman in PoE2 0.5.5."
    ),
    "occultist": (
        "Occultist is a PoE1 Witch ascendancy; check Witch Lich / Abyssal Lich "
        "('Incessant Cacophony' / 'Rupture the Soul') in PoE2 0.5.5."
    ),
    "raider": (
        "Raider is a PoE1 Ranger ascendancy; check Ranger Deadeye / Pathfinder "
        "or Huntress Amazon in PoE2 0.5.5."
    ),
    "warden": (
        "Warden is a PoE1 Ranger ascendancy; check Ranger Deadeye / Pathfinder "
        "or Huntress Amazon / Spirit Walker in PoE2 0.5.5."
    ),
}

ASCENDANCY_SHORTHAND_ALIASES: dict[str, str] = {
    "gemling": "Gemling Legionnaire",
    "legionnaire": "Gemling Legionnaire",
    "varashta": "Disciple of Varashta",
    "disciple": "Disciple of Varashta",
    "djinn": "Disciple of Varashta",
    "martial": "Martial Artist",
    "martialartist": "Martial Artist",
    "stonefist": "Martial Artist",
    "spiritwalker": "Spirit Walker",
    "walker": "Spirit Walker",
    "smith": "Smith of Kitava",
    "kitava": "Smith of Kitava",
    "chayula": "Acolyte of Chayula",
    "acolyte": "Acolyte of Chayula",
    "abyssallich": "Abyssal Lich",
    "abysslich": "Abyssal Lich",
    "bloodmage": "Blood Mage",
    "sanguimancer": "Blood Mage",
    "chrono": "Chronomancer",
    "storm": "Stormweaver",
    "witchhunter": "Witchhunter",
}

# Curated mechanical identity & bossing/mapping profiles grounded in TreeData/0_5 notables.
_ASCENDANCY_PROFILES: dict[str, dict[str, Any]] = {
    "Gemling Legionnaire": {
        "summary": (
            "Universal attribute/gem-quality/max-resist hyper-scaler (Adaptive Capability, "
            "Thaumaturgical Infusion, Implanted Gems, Enhanced Effectiveness)."
        ),
        "archetypes": [
            "Elemental Crit Spear / Quarterstaff (Rakiata's Flow + Dialla's Desire)",
            "Twister / Spark Cast-on-Crit Projectile Stacker",
            "Attribute & Life/Mana Stacker (Enhanced Effectiveness)",
        ],
        "boss_bonus": 9.8,
        "map_bonus": 9.7,
    },
    "Martial Artist": {
        "summary": (
            "Combo & Hollow-Form Monk unlocking +5 tattooed Rune sockets (Runic Meridians) "
            "and Fists of Stone glove transformation (Way of the Stonefist)."
        ),
        "archetypes": [
            "Whirling Assault + Falling Thunder Combo Burst",
            "Tempest Flurry / Storm Wave Quarterstaff (Runic Meridians)",
            "Hollow Form Unarmed Technique",
        ],
        "boss_bonus": 9.5,
        "map_bonus": 9.4,
    },
    "Disciple of Varashta": {
        "summary": (
            "Invulnerable elemental Djinn commander (Ruzhan / Navira / Kelari) with elite "
            "ES-to-Armour, faster ES recharge, and 100% Elemental-from-Mana."
        ),
        "archetypes": [
            "Ruzhan / Navira / Kelari Elemental Djinn Caster",
            "High-ES Sacred Rituals + Varashta's Intuition Tank Caster",
            "Staff + Focus Instruments of Power Hybrid",
        ],
        "boss_bonus": 9.1,
        "map_bonus": 9.0,
    },
    "Oracle": {
        "summary": (
            "Foresight Druid with Inevitable Critical Hits (Forced Outcome), Unlucky enemy "
            "damage/crits (The Lesser Harm), and radius keystone allocation."
        ),
        "archetypes": [
            "Forced Outcome Cast-on-Crit Comet / Spark",
            "Entwined Realities Keystone-Jump Caster",
            "Unnamed Heartwood Spell/Attack Totems",
        ],
        "boss_bonus": 9.3,
        "map_bonus": 9.1,
    },
    "Spirit Walker": {
        "summary": (
            "Azmerian spirit-channeling Huntress (Vivid Stampede, Primal Bounty, Wild Protector) "
            "with Unique Beast capture and 60% weapon-damage companion scaling."
        ),
        "archetypes": [
            "Vivid Stampede / Primal Bounty Projectile Huntress",
            "The Natural Order Unique Tamed Beast + Catha's Balance",
            "Azmeri Idol Reservation & Companion Stacker",
        ],
        "boss_bonus": 8.8,
        "map_bonus": 9.5,
    },
    "Deadeye": {
        "summary": (
            "High-velocity projectile specialist with Tailwind (Gathering Winds), +1 Projectile "
            "(Endless Munitions), Frenzy doubling, and Mirage Deadeye."
        ),
        "archetypes": [
            "Lightning Arrow /Lightning Rod Bow Mapper",
            "Frenzy-Stacked Projectile Clearer",
            "Crossbow / Spear Far Shot Zoom",
        ],
        "boss_bonus": 8.2,
        "map_bonus": 9.6,
    },
    "Infernalist": {
        "summary": (
            "Demonflame, Hellhound DR pet, Beidat Life-reservation conversions "
            "(+Spirit/+ES/+Mana), and all-damage Ignite scaling."
        ),
        "archetypes": [
            "Beidat's Will High-Spirit Minion Army",
            "Demon Form Uncapped Demonflame Caster",
            "Pyromantic Pact / Grinning Immolation Crit Caster",
        ],
        "boss_bonus": 8.6,
        "map_bonus": 8.5,
    },
    "Stormweaver": {
        "summary": (
            "Arcane Surge / MoM elemental caster with double Shocks (Strike Twice), "
            "double Chills (Heavy Snows), and +2 Elemental Skill Limits."
        ),
        "archetypes": [
            "Archmage Spark / Orb of Storms MoM Caster",
            "Double-Shock / Double-Chill Elemental Storm Crit",
            "Elemental Infusion Remnant Caster",
        ],
        "boss_bonus": 8.5,
        "map_bonus": 8.9,
    },
    "Titan": {
        "summary": (
            "50% increased Small Passive effect (Hulking Form), +15% more Life, "
            "50% more Body Armour, Crushing Blows, and Slam Aftershocks."
        ),
        "archetypes": [
            "Hammer of the Gods / Stampede Aftershock Slammer",
            "Hulking Form Small-Passive Stacker",
            "Giant's Blood Dual-2H Mace / Armour Tank",
        ],
        "boss_bonus": 8.7,
        "map_bonus": 8.1,
    },
    "Lich": {
        "summary": (
            "Chaos/ES powerhouse with Eternal Life (Life cannot change while ES > 0), "
            "Crystalline Phylactery (100% jewel effect), +1 Curse, and 30% more spell damage."
        ),
        "archetypes": [
            "Eternal Life Low-Life / Damage-Bypass Mitigation Tank",
            "Hexblast / Essence Drain + Contagion Chaos Explosion",
            "Unholy Might Minion / Spell Hybrid",
        ],
        "boss_bonus": 8.5,
        "map_bonus": 8.6,
    },
    "Blood Mage": {
        "summary": (
            "15% base Spell Crit (Sunder the Flesh), Life-per-ES Body Armour (Crimson Power), "
            "Crit Bonus per Life (Gore Spike), and 20% Spell Life Leech."
        ),
        "archetypes": [
            "15% Base Spell Crit Rathpith Globe / Gore Spike Nuke",
            "Elemental Aggravated Bleed Caster (Blood Barbs)",
            "Progenesis-style Grasping Wounds Life Tank",
        ],
        "boss_bonus": 9.0,
        "map_bonus": 8.2,
    },
    "Smith of Kitava": {
        "summary": (
            "Customizes Normal Body Armour with bespoke notables (+Max All Res via Forged in "
            "Flame, Crit Immunity, Unaffected by Damaging Ailments, +Spirit%)."
        ),
        "archetypes": [
            "90% Max All-Res Forged in Flame Hardcore Tank",
            "Manifest Weapon / Temper Weapon Melee",
            "Fire Spell on Hit Melee Trigger",
        ],
        "boss_bonus": 8.3,
        "map_bonus": 7.9,
    },
    "Chronomancer": {
        "summary": (
            "Time Freeze boss lockdown, Temporal Rift instant rewind, 30% deferred hit "
            "mitigation (Phased Form), cooldown reset, and 20% AoE slow."
        ),
        "archetypes": [
            "Time Freeze + Hammer/Comet Cooldown Reset Bosser",
            "Phased Form + Recoup Immortal Tank",
            "Temporal Rift HC Support / Control Caster",
        ],
        "boss_bonus": 8.6,
        "map_bonus": 7.8,
    },
    "Invoker": {
        "summary": (
            "Elemental Crit Monk ignoring enemy elemental resistances on crit, Spirit from "
            "Body Armour ES/Evasion, and 35% more Meta-Gem Energy."
        ),
        "archetypes": [
            "Ice Strike / Tempest Flurry Crit Monk",
            "Cast-on-Crit / Cast-on-Freeze Meta-Gem Trigger",
            "Unbound Avatar Pinnacle Burst",
        ],
        "boss_bonus": 8.5,
        "map_bonus": 8.6,
    },
    "Amazon": {
        "summary": (
            "Converts excess Accuracy into base Crit Chance, adds 25% of weapon Accuracy as "
            "flat Phys, leeches Life from Elemental damage, and deals 50% more vs Open Weakness."
        ),
        "archetypes": [
            "Accuracy-Stacking Crit Spear / Bow",
            "Elemental Surge Charge Consumer",
            "Predatory Instinct Pinnacle Bosser",
        ],
        "boss_bonus": 8.6,
        "map_bonus": 8.5,
    },
    "Ritualist": {
        "summary": (
            "+1 Ring Slot (Unfurled Finger) and 25% increased Ring/Amulet bonuses "
            "(Mystic Attunement), +1 Charm Slot, and Ritual Sacrifice."
        ),
        "archetypes": [
            "3-Ring + Mystic Attunement Jewellery Attribute/Rarity Stacker",
            "4-Charm Mind Phylacteries Utility Build",
            "Blood Boil Clearer",
        ],
        "boss_bonus": 8.2,
        "map_bonus": 8.6,
    },
    "Shaman": {
        "summary": (
            "Unlocks Bonded modifiers on Runes/Idols (Wisdom of the Maji), +40 Spirit per empty "
            "Charm slot, Rage-to-Spell more damage, and elemental Adaptation."
        ),
        "archetypes": [
            "Wisdom of the Maji Bonded Rune/Idol Stacker",
            "Rage-Stacking Apocalypse Spell Druid",
            "Reactive Growth Elemental Tank",
        ],
        "boss_bonus": 8.2,
        "map_bonus": 8.2,
    },
    "Tactician": {
        "summary": (
            "50% less Spirit reservation on persistent buffs, +1 Attack-synced Totem with "
            "30% more speed, Projectile Pin, and unlimited Banners."
        ),
        "archetypes": [
            "50% Buff Reservation Aura/Herald Stacker",
            "Siege Ballista / Artillery Totem Mercenary",
            "Pin-Lock Projectile Control",
        ],
        "boss_bonus": 8.1,
        "map_bonus": 8.2,
    },
    "Witchhunter": {
        "summary": (
            "100 Weapon-Set Passive Points, Culling + Decimating Strike, Concentration up to "
            "40% more damage, Sorcery Ward, and 100% Life corpse explosions."
        ),
        "archetypes": [
            "Dual-Tree 100-Point Weapon-Swap Specialist",
            "Zealous Inquisition Mapping Blaster",
            "Concentration-Break Boss Hunter",
        ],
        "boss_bonus": 8.1,
        "map_bonus": 8.5,
    },
    "Pathfinder": {
        "summary": (
            "Doubles max Poison stacks, immune to Slows, 50% Evasion to Elemental DR, "
            "non-queuing Enduring Life Flasks, and Concoctions."
        ),
        "archetypes": [
            "Double-Stack Poison Bow / Concoction",
            "Sustainable Practices Evasion + Elemental DR Tank",
            "Traveller's Wisdom Attribute-Node Flex",
        ],
        "boss_bonus": 8.0,
        "map_bonus": 8.3,
    },
    "Warbringer": {
        "summary": (
            "75% max Block Turtle Charm, negative Armour Break (Imploding Impacts), "
            "zero-cooldown Warcry corpse explosions, and Jade Encase."
        ),
        "archetypes": [
            "75% Block Turtle Charm + Svalinn Tank",
            "Zero-Cooldown Corrupting Cry / Warcry Exploder",
            "Negative Armour-Break Totem/Melee Bosser",
        ],
        "boss_bonus": 8.1,
        "map_bonus": 8.0,
    },
    "Acolyte of Chayula": {
        "summary": (
            "Doubles Chaos Resistance (+10% max Chaos Res = 85%), Chaos Leech, Volatility, "
            "Into the Breach remnants, or Spirit-for-Darkness swap."
        ),
        "archetypes": [
            "85% Chaos Res + Chaos Leech Quarterstaff/Bow",
            "Into the Breach Purple Flame Stacker",
            "Archon of Chayula Chaos Hybrid",
        ],
        "boss_bonus": 7.7,
        "map_bonus": 7.8,
    },
    "Abyssal Lich": {
        "summary": (
            "Abyss-inflected Lich variant replacing Unholy Might/Power-Charge nodes with Umbral "
            "Souls, Steward of Kulemak, and self-targeted Culling Offerings."
        ),
        "archetypes": [
            "Umbral Well Soul-Stacking Caster",
            "Steward of Kulemak Power-Charge Apparitions",
            "Self-Offering Eternal Life Witch",
        ],
        "boss_bonus": 8.0,
        "map_bonus": 8.0,
    },
}

_DEMAND_ARCHETYPE_HINTS: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    (("rakiata",), ["Gemling Legionnaire", "Martial Artist", "Elemental Attack Hits"]),
    (("dialla",), ["Gemling Legionnaire", "Oracle", "Quality / +Level Stacking"]),
    (("rigwald",), ["Martial Artist", "Gemling Legionnaire", "Weapon-Set Attack Scaling"]),
    (("garukhan",), ["Gemling Legionnaire", "Amazon", "Deadeye", "Bifurcated Crit Attacks"]),
    (("uul-netol",), ["Blood Mage", "Martial Artist", "Titan", "Phys/Chaos Scaling"]),
    (("uhtred",), ["Gemling Legionnaire", "Oracle", "Disciple of Varashta", "+Gem Level Scaling"]),
    (
        ("atziri-s-communion", "atziris-communion", "seraph"),
        ["Blood Mage", "Infernalist", "Gemling Legionnaire", "Life/Spirit Reservation"],
    ),
    (
        ("tul-s-stillness", "breachlord"),
        ["Martial Artist", "Invoker", "Stormweaver", "Cold/Lightning Scaling"],
    ),
    (
        ("mageblood",),
        ["Gemling Legionnaire", "Martial Artist", "Oracle", "Universal High-Budget Belt"],
    ),
    (("headhunter",), ["Deadeye", "Gemling Legionnaire", "Spirit Walker", "Juiced Mapping"]),
    (("the-ordained",), ["Gemling Legionnaire", "Amazon", "Spirit Walker", "Crit Lightning Spear"]),
    (
        ("morior-invictus",),
        ["Gemling Legionnaire", "Martial Artist", "Spirit/Attribute Rune Socket Stacking"],
    ),
    (
        ("svalinn",),
        ["Gemling Legionnaire", "Warbringer", "Smith of Kitava", "Lucky Block + Cast on Block"],
    ),
    (("olroth",), ["Gemling Legionnaire", "Blood Mage", "Titan", "Instant Life Recovery / Guard"]),
    (("jiquani",), ["Disciple of Varashta", "Stormweaver", "Oracle", "ES Recharge / Mana-Armour"]),
    (
        ("aldur",),
        ["Martial Artist", "Shaman", "Gemling Legionnaire", "Kalguuran Unique Runeforging"],
    ),
    (
        ("raven-touched",),
        ["Gemling Legionnaire", "Oracle", "Stormweaver", "Endgame Helmet Soul Core"],
    ),
    (
        ("the-taming", "kalandra"),
        ["Gemling Legionnaire", "Martial Artist", "Deadeye", "Tri-Ailment Elemental Scaling"],
    ),
    (
        ("rite-of-passage",),
        ["Gemling Legionnaire", "Martial Artist", "Deadeye", "Spirit of the Cat Charm"],
    ),
    (
        ("the-adorned", "against-the-darkness", "controlled-metamorphosis", "voices"),
        ["Gemling Legionnaire", "Oracle", "Lich", "Jewel Socket Stacking"],
    ),
    (("runeseeker",), ["Martial Artist", "Shaman", "Gemling Legionnaire", "Rune Effect Stacking"]),
    (("temporalis",), ["Stormweaver", "Oracle", "Gemling Legionnaire", "Zero-Cooldown Blink/Cast"]),
    (("her-declaration",), ["Gemling Legionnaire", "Tactician", "Persistent Buff Scaling"]),
)


def _norm_key(text: str) -> str:
    return _NON_ALNUM_RE.sub("", text.strip().lower())


def _extract_granted_skills(stats: list[str]) -> list[str]:
    skills: list[str] = []
    for line in stats:
        m = _GRANTS_SKILL_RE.match(line.strip())
        if m:
            sk = m.group(1).strip()
            if sk and sk not in skills:
                skills.append(sk)
    return skills


def _infer_demand_archetypes(slug: str, name: str) -> list[str]:
    hay = f"{slug} {name.lower()}"
    for needles, tags in _DEMAND_ARCHETYPE_HINTS:
        if any(n in hay for n in needles):
            return list(tags)
    return ["Endgame Meta"]


# ---------------------------------------------------------------------------
# Pure-Python Lua Table Literal Parser (for standalone `tree.lua` support)
# ---------------------------------------------------------------------------

_LUA_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        --[^\n]*                              | # line comment
        (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*') |
        (?P<num>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)) |
        (?P<ident>[A-Za-z_][A-Za-z0-9_]*)     |
        (?P<sym>[{}\[\]=,])
    )
    """,
    re.VERBOSE,
)


def parse_lua_table(lua_text: str) -> Any:
    """Parse a deterministic PoB2 `return { ... }` Lua data table into Python dicts/lists."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    n = len(lua_text)
    while pos < n:
        m = _LUA_TOKEN_RE.match(lua_text, pos)
        if not m:
            if lua_text[pos:].strip() == "":
                break
            snippet = lua_text[pos : pos + 40]
            raise ValueError(f"Unexpected Lua token at offset {pos}: {snippet!r}")
        pos = m.end()
        if m.lastgroup:
            tokens.append((m.lastgroup, m.group(m.lastgroup)))

    idx = 0
    total = len(tokens)
    if idx < total and tokens[idx] == ("ident", "return"):
        idx += 1

    def _unescape_lua_str(raw: str) -> str:
        inner = raw[1:-1]
        if "\\" not in inner:
            return inner
        return (
            inner.replace('\\"', '"')
            .replace("\\'", "'")
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )

    def _parse_val() -> Any:
        nonlocal idx
        if idx >= total:
            raise ValueError("Unexpected EOF while parsing Lua table")
        kind, tok = tokens[idx]
        if kind == "sym" and tok == "{":
            return _parse_tbl()
        idx += 1
        if kind == "str":
            return _unescape_lua_str(tok)
        if kind == "num":
            if tok.startswith(("0x", "-0x")):
                return int(tok, 16)
            return float(tok) if ("." in tok or "e" in tok or "E" in tok) else int(tok)
        if kind == "ident":
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "nil":
                return None
            return tok
        raise ValueError(f"Unexpected Lua value token {(kind, tok)!r}")

    def _parse_tbl() -> Any:
        nonlocal idx
        idx += 1  # consume '{'
        entries: dict[Any, Any] = {}
        auto_i = 1
        while idx < total:
            kind, tok = tokens[idx]
            if kind == "sym" and tok == "}":
                idx += 1
                break
            if (
                kind == "sym"
                and tok == "["
                and idx + 3 < total
                and tokens[idx + 2] == ("sym", "]")
                and tokens[idx + 3] == ("sym", "=")
            ):
                idx += 1  # '['
                key = _parse_val()
                idx += 2  # ']' '='
                val = _parse_val()
                entries[key] = val
            elif kind == "ident" and idx + 1 < total and tokens[idx + 1] == ("sym", "="):
                key = tok
                idx += 2
                val = _parse_val()
                entries[key] = val
            else:
                val = _parse_val()
                entries[auto_i] = val
                auto_i += 1
            if idx < total and tokens[idx] == ("sym", ","):
                idx += 1

        if not entries:
            return {}
        if all(isinstance(k, int) for k in entries):
            int_keys = sorted(entries.keys())
            if int_keys == list(range(1, len(int_keys) + 1)):
                return [entries[k] for k in int_keys]
            return {str(k): v for k, v in entries.items()}
        return {str(k): v for k, v in entries.items()}

    return _parse_val()


# ---------------------------------------------------------------------------
# Pydantic v2 Schemas
# ---------------------------------------------------------------------------


class AscendancyChoiceOption(Poe2Model):
    node_id: int
    name: str
    stats: list[str] = Field(default_factory=list)
    granted_skills: list[str] = Field(default_factory=list)


class AscendancyNotable(Poe2Model):
    node_id: int
    name: str
    stats: list[str] = Field(default_factory=list)
    granted_skills: list[str] = Field(default_factory=list)
    is_multiple_choice: bool = False
    choice_options: list[AscendancyChoiceOption] = Field(default_factory=list)
    flavour_text: list[str] = Field(default_factory=list)


class AscendancySmallPassive(Poe2Model):
    node_id: int
    name: str
    stats: list[str] = Field(default_factory=list)


class AscendancySummary(Poe2Model):
    name: str
    class_name: str
    class_id: int
    internal_id: str | None = None
    replaces_ascendancy: str | None = None
    replaced_by_ascendancy: str | None = None
    notable_count: int = 0
    small_passive_count: int = 0
    choice_option_count: int = 0
    notable_names: list[str] = Field(default_factory=list)
    granted_skills: list[str] = Field(default_factory=list)
    archetype_summary: str = ""


class AscendancyDetail(AscendancySummary):
    base_str: int = 7
    base_dex: int = 7
    base_int: int = 7
    meta_archetypes: list[str] = Field(default_factory=list)
    notables: list[AscendancyNotable] = Field(default_factory=list)
    small_passives: list[AscendancySmallPassive] = Field(default_factory=list)


class ClassSummary(Poe2Model):
    class_id: int
    name: str
    base_str: int = 7
    base_dex: int = 7
    base_int: int = 7
    ascendancy_count: int = 0
    ascendancies: list[AscendancySummary] = Field(default_factory=list)


class AscendancyInspection(Poe2Model):
    query: str
    patch: str = POE2_PATCH
    matched_kind: Literal["ascendancy", "class"]
    class_name: str
    primary: AscendancyDetail | None = None
    ascendancies: list[AscendancyDetail] = Field(default_factory=list)


class LadderLeaderEntry(Poe2Model):
    rank: int
    character_name: str
    account_name: str | None = None
    class_name: str
    ascendancy_name: str | None = None
    level: int
    experience: int | None = None
    dead: bool = False
    delve_depth: int | None = None


class AscendancyShareEntry(Poe2Model):
    ascendancy_name: str
    class_name: str
    share_pct: float
    character_count: int
    trend: int = 0


class ClassShareEntry(Poe2Model):
    class_name: str
    share_pct: float
    character_count: int
    ascendancies: list[str] = Field(default_factory=list)


class LadderModeSnapshot(Poe2Model):
    league_name: str
    mode: Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"]
    hardcore: bool = False
    ssf: bool = False
    source: str
    total_characters: int = 0
    sampled_characters: int = 0
    level_100_count: int | None = None
    dead_count: int | None = None
    min_level: int | None = None
    max_level: int | None = None
    unlisted_share_pct: float = 0.0
    ascendancy_shares: list[AscendancyShareEntry] = Field(default_factory=list)
    class_shares: list[ClassShareEntry] = Field(default_factory=list)
    xp_leaders: list[LadderLeaderEntry] = Field(default_factory=list)
    delve_leaders: list[LadderLeaderEntry] = Field(default_factory=list)


class MetaDemandSignal(Poe2Model):
    id: str
    name: str
    category: str
    item_type: str | None = None
    exalted: float
    divine: float
    liquidity: float
    liquidity_unit: str
    change_7d_pct: float | None = None
    archetype_tags: list[str] = Field(default_factory=list)
    rationale: str


class AscendancyTierEntry(Poe2Model):
    rank: int
    ascendancy_name: str
    class_name: str
    tier: Literal["S", "A", "B", "C"]
    composite_score: float
    sc_share_pct: float = 0.0
    hc_share_pct: float = 0.0
    ssf_share_pct: float = 0.0
    hcssf_share_pct: float = 0.0
    bossing_tier: Literal["S", "A", "B", "C"] = "B"
    mapping_tier: Literal["S", "A", "B", "C"] = "B"
    hc_viability_tier: Literal["S", "A", "B", "C"] = "B"
    ssf_viability_tier: Literal["S", "A", "B", "C"] = "B"
    signature_notables: list[str] = Field(default_factory=list)
    meta_archetypes: list[str] = Field(default_factory=list)


class LadderMetaReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    patch: str = POE2_PATCH
    top_n: int = 200
    ttl_seconds: float = DEFAULT_META_TTL_SECONDS
    divine_exalted: float | None = None
    primary_mode: LadderModeSnapshot
    mode_breakdown: dict[str, LadderModeSnapshot] = Field(default_factory=dict)
    chase_demand_signals: list[MetaDemandSignal] = Field(default_factory=list)
    ascendancy_rankings: list[AscendancyTierEntry] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MetaService Implementation
# ---------------------------------------------------------------------------


def _score_to_tier(
    score: float, *, s_cut: float, a_cut: float, b_cut: float
) -> Literal["S", "A", "B", "C"]:
    if score >= s_cut:
        return "S"
    if score >= a_cut:
        return "A"
    if score >= b_cut:
        return "B"
    return "C"


def _league_family_names(
    league: str,
) -> dict[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], str]:
    clean = league.strip()
    upper = clean.upper()
    if upper.startswith("HC SSF "):
        base = clean[7:].strip()
    elif upper.startswith("HCSSF "):
        base = clean[6:].strip()
    elif upper.startswith("HC "):
        base = clean[3:].strip()
    elif upper.startswith("SSF "):
        base = clean[4:].strip()
    elif clean in ("Standard", "Hardcore", "Solo Self-Found", "Hardcore SSF"):
        return {
            "sc_trade": "Standard",
            "hc_trade": "Hardcore",
            "ssf": "Solo Self-Found",
            "hc_ssf": "Hardcore SSF",
        }
    else:
        base = clean

    return {
        "sc_trade": base,
        "hc_trade": f"HC {base}",
        "ssf": f"SSF {base}",
        "hc_ssf": f"HC SSF {base}",
    }


def _mode_for_league_name(
    league: str,
    family: Mapping[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], str],
) -> Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"]:
    low = league.strip().lower()
    for mode, name in family.items():
        if name.lower() == low:
            return mode
    if low.startswith(("hc ssf ", "hcssf ")):
        return "hc_ssf"
    if low.startswith("hc ") or low == "hardcore":
        return "hc_trade"
    if low.startswith("ssf "):
        return "ssf"
    return "sc_trade"


class MetaService:
    """PoE2 Patch 0.5.5 Class/Ascendancy Inspector & Live Ladder/Economy Meta Service."""

    def __init__(
        self,
        *,
        tree_path: Path | str | None = None,
        http_client: Poe2HttpClient | None = None,
        economy_service: EconomyService | None = None,
        cache_dir: Path | None = None,
        ttl_seconds: float = DEFAULT_META_TTL_SECONDS,
        ggg_oauth_token: str | None = None,
        ggg_ladder_base_url: str = GGG_LADDER_BASE_URL,
        poeninja_build_index_url: str = POENINJA_BUILD_INDEX_URL,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._tree_path = Path(tree_path).expanduser() if tree_path is not None else None
        self._owns_http = http_client is None
        self.http_client = http_client or Poe2HttpClient()
        self._owns_econ = economy_service is None
        self.economy_service = economy_service or EconomyService(
            http_client=self.http_client,
            cache_dir=cache_dir,
            now_fn=now_fn,
        )
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.ggg_oauth_token = ggg_oauth_token
        self.ggg_ladder_base_url = ggg_ladder_base_url.rstrip("/")
        self.poeninja_build_index_url = poeninja_build_index_url
        self._now_fn = now_fn

        self._loaded_tree: bool = False
        self._classes_by_name: dict[str, ClassSummary] = {}
        self._asc_details_by_name: dict[str, AscendancyDetail] = {}
        self._asc_to_class: dict[str, str] = {}
        self._norm_to_asc: dict[str, str] = {}
        self._norm_to_class: dict[str, str] = {}

    def close(self) -> None:
        if self._owns_econ:
            self.economy_service.close()
        if self._owns_http:
            self.http_client.close()

    def __enter__(self) -> MetaService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 1. TreeData/0_5 Class & Ascendancy Parsing
    # ------------------------------------------------------------------

    def _resolve_tree_data_dict(self) -> dict[str, Any]:
        target = self._tree_path
        if target is None:
            target = get_pob2_paths().src_dir / "TreeData" / "0_5"

        if target.is_dir():
            json_file = target / "tree.json"
            lua_file = target / "tree.lua"
            if json_file.is_file():
                return dict(json.loads(json_file.read_text(encoding="utf-8")))
            if lua_file.is_file():
                return dict(parse_lua_table(lua_file.read_text(encoding="utf-8")))
            raise FileNotFoundError(f"Neither tree.json nor tree.lua found in {target}")

        if not target.is_file():
            raise FileNotFoundError(f"PoB2 tree file does not exist: {target}")

        if target.suffix.lower() == ".json":
            return dict(json.loads(target.read_text(encoding="utf-8")))

        if target.suffix.lower() == ".lua":
            sibling_json = target.with_suffix(".json")
            if sibling_json.is_file() and target.name == "tree.lua":
                return dict(json.loads(sibling_json.read_text(encoding="utf-8")))
            return dict(parse_lua_table(target.read_text(encoding="utf-8")))

        raise ValueError(f"Unsupported PoB2 tree file format: {target}")

    def _ensure_tree_loaded(self) -> None:
        if self._loaded_tree:
            return

        raw_tree = self._resolve_tree_data_dict()
        raw_classes = raw_tree.get("classes") or []
        raw_nodes_obj = raw_tree.get("nodes") or {}
        nodes_by_id: dict[str, dict[str, Any]] = {
            str(k): v for k, v in raw_nodes_obj.items() if isinstance(v, dict)
        }

        # Pre-index multiple-choice option nodes by both forward and reverse connections
        mc_options_for_parent: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        for nid, node in nodes_by_id.items():
            if not node.get("isMultipleChoiceOption"):
                continue
            opt_skill = int(node.get("skill") or nid)
            for conn in node.get("connections") or []:
                parent_id = str(conn.get("id") if isinstance(conn, Mapping) else conn)
                mc_options_for_parent[parent_id][opt_skill] = node

        for nid, node in nodes_by_id.items():
            if node.get("isMultipleChoiceOption") or not node.get("ascendancyName"):
                continue
            for conn in node.get("connections") or []:
                cid = str(conn.get("id") if isinstance(conn, Mapping) else conn)
                cand = nodes_by_id.get(cid)
                if cand is not None and cand.get("isMultipleChoiceOption"):
                    opt_skill = int(cand.get("skill") or cid)
                    mc_options_for_parent[nid][opt_skill] = cand

        nodes_by_asc: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for nid, node in nodes_by_id.items():
            asc_name = node.get("ascendancyName")
            if isinstance(asc_name, str) and asc_name:
                nodes_by_asc[asc_name].append((nid, node))

        classes_out: dict[str, ClassSummary] = {}
        details_out: dict[str, AscendancyDetail] = {}
        asc_to_class: dict[str, str] = {}

        for idx, c_entry in enumerate(raw_classes):
            if not isinstance(c_entry, Mapping):
                continue
            cname = str(c_entry.get("name") or "").strip()
            if not cname:
                continue
            cid = int(c_entry.get("integerId", idx))
            b_str = int(c_entry.get("base_str", 7))
            b_dex = int(c_entry.get("base_dex", 7))
            b_int = int(c_entry.get("base_int", 7))

            asc_summaries: list[AscendancySummary] = []
            for a_entry in c_entry.get("ascendancies") or []:
                if not isinstance(a_entry, Mapping):
                    continue
                aname = str(a_entry.get("name") or "").strip()
                if not aname:
                    continue
                internal_id = str(a_entry.get("internalId") or a_entry.get("id") or "") or None
                replaces = str(a_entry["replace"]) if a_entry.get("replace") else None
                replaced_by = str(a_entry["replaceBy"]) if a_entry.get("replaceBy") else None

                # Resolve nodes directly or via `replace` (e.g. Abyssal Lich on Lich nodes)
                raw_asc_nodes = list(nodes_by_asc.get(aname, ()))
                use_override_from = replaces if (not raw_asc_nodes and replaces) else None
                if use_override_from:
                    raw_asc_nodes = list(nodes_by_asc.get(use_override_from, ()))

                notables: list[AscendancyNotable] = []
                smalls: list[AscendancySmallPassive] = []
                total_choice_opts = 0
                all_granted_skills: list[str] = []

                for nid, base_node in raw_asc_nodes:
                    if base_node.get("isAscendancyStart") or base_node.get(
                        "isMultipleChoiceOption"
                    ):
                        continue

                    override_map = (
                        base_node.get("options")
                        if isinstance(base_node.get("options"), Mapping)
                        else {}
                    )
                    ov = (
                        override_map.get(aname)
                        if (use_override_from and isinstance(override_map, Mapping))
                        else None
                    )
                    if not isinstance(ov, Mapping):
                        ov = {}

                    eff_id = int(ov.get("id") or base_node.get("skill") or nid)
                    eff_name = str(ov.get("name") or base_node.get("name") or "").strip()
                    raw_stats = ov["stats"] if "stats" in ov else (base_node.get("stats") or [])
                    eff_stats = [str(s) for s in raw_stats if str(s).strip()]

                    if base_node.get("isNotable"):
                        raw_opts = sorted(
                            mc_options_for_parent.get(nid, {}).values(),
                            key=lambda o: str(o.get("name") or ""),
                        )
                        choice_opts: list[AscendancyChoiceOption] = []
                        for opt in raw_opts:
                            opt_stats = [str(s) for s in (opt.get("stats") or []) if str(s).strip()]
                            opt_skills = _extract_granted_skills(opt_stats)
                            for sk in opt_skills:
                                if sk not in all_granted_skills:
                                    all_granted_skills.append(sk)
                            choice_opts.append(
                                AscendancyChoiceOption(
                                    node_id=int(opt.get("skill") or 0),
                                    name=str(opt.get("name") or ""),
                                    stats=opt_stats,
                                    granted_skills=opt_skills,
                                )
                            )
                        total_choice_opts += len(choice_opts)

                        if not eff_stats and choice_opts:
                            opt_labels = ", ".join(o.name for o in choice_opts)
                            eff_stats = [f"Choose one: {opt_labels}"]

                        g_skills = _extract_granted_skills(eff_stats)
                        for sk in g_skills:
                            if sk not in all_granted_skills:
                                all_granted_skills.append(sk)

                        flav = [
                            str(f) for f in (base_node.get("flavourText") or []) if str(f).strip()
                        ]
                        notables.append(
                            AscendancyNotable(
                                node_id=eff_id,
                                name=eff_name,
                                stats=eff_stats,
                                granted_skills=g_skills,
                                is_multiple_choice=bool(
                                    base_node.get("isMultipleChoice") or choice_opts
                                ),
                                choice_options=choice_opts,
                                flavour_text=flav,
                            )
                        )
                    else:
                        smalls.append(
                            AscendancySmallPassive(
                                node_id=eff_id,
                                name=eff_name,
                                stats=eff_stats,
                            )
                        )

                notables.sort(key=lambda n: n.name)
                smalls.sort(key=lambda s: (s.name, s.node_id))

                profile = _ASCENDANCY_PROFILES.get(aname, {})
                detail = AscendancyDetail(
                    name=aname,
                    class_name=cname,
                    class_id=cid,
                    internal_id=internal_id,
                    replaces_ascendancy=replaces,
                    replaced_by_ascendancy=replaced_by,
                    notable_count=len(notables),
                    small_passive_count=len(smalls),
                    choice_option_count=total_choice_opts,
                    notable_names=[n.name for n in notables],
                    granted_skills=all_granted_skills,
                    archetype_summary=str(profile.get("summary") or ""),
                    base_str=b_str,
                    base_dex=b_dex,
                    base_int=b_int,
                    meta_archetypes=list(profile.get("archetypes") or []),
                    notables=notables,
                    small_passives=smalls,
                )
                details_out[aname] = detail
                asc_to_class[aname] = cname
                asc_summaries.append(AscendancySummary.model_validate(detail.model_dump()))

            classes_out[cname] = ClassSummary(
                class_id=cid,
                name=cname,
                base_str=b_str,
                base_dex=b_dex,
                base_int=b_int,
                ascendancy_count=len(asc_summaries),
                ascendancies=asc_summaries,
            )

        self._classes_by_name = classes_out
        self._asc_details_by_name = details_out
        self._asc_to_class = asc_to_class
        self._norm_to_asc = {_norm_key(k): k for k in details_out}
        for alias, target_asc in ASCENDANCY_SHORTHAND_ALIASES.items():
            if target_asc in details_out:
                self._norm_to_asc[_norm_key(alias)] = target_asc
        self._norm_to_class = {_norm_key(k): k for k in classes_out}
        self._loaded_tree = True

    def list_classes(self) -> list[ClassSummary]:
        """List all 8 playable PoE2 Patch 0.5.5 classes and their 23 Ascendancies."""
        self._ensure_tree_loaded()
        return list(self._classes_by_name.values())

    def list_ascendancies(self, class_name: str | None = None) -> list[AscendancySummary]:
        """List all 23 PoE2 Patch 0.5.5 Ascendancies (or filter by base class name)."""
        self._ensure_tree_loaded()
        if class_name:
            norm = _norm_key(class_name)
            if norm in POE1_OR_UNRELEASED_TRAP_CLASSES:
                raise LookupError(POE1_OR_UNRELEASED_TRAP_CLASSES[norm])
            resolved_cls = self._norm_to_class.get(norm)
            if resolved_cls is None:
                raise LookupError(
                    f"Unknown PoE2 class {class_name!r}; valid classes: "
                    f"{', '.join(self._classes_by_name)}"
                )
            return list(self._classes_by_name[resolved_cls].ascendancies)

        out: list[AscendancySummary] = []
        for cls_summary in self._classes_by_name.values():
            out.extend(cls_summary.ascendancies)
        return out

    def inspect_ascendancy(self, name_or_class: str) -> AscendancyInspection:
        """Inspect a PoE2 Ascendancy (or all Ascendancies of a base Class) with full notables."""
        self._ensure_tree_loaded()
        q_clean = name_or_class.strip()
        if not q_clean:
            raise ValueError("Ascendancy or class query cannot be empty.")

        norm = _norm_key(q_clean)
        if norm in POE1_OR_UNRELEASED_TRAP_CLASSES:
            raise LookupError(POE1_OR_UNRELEASED_TRAP_CLASSES[norm])

        # 1. Exact or alias match on Ascendancy
        asc_match = self._norm_to_asc.get(norm)
        if asc_match is not None:
            detail = self._asc_details_by_name[asc_match]
            return AscendancyInspection(
                query=q_clean,
                matched_kind="ascendancy",
                class_name=detail.class_name,
                primary=detail,
                ascendancies=[detail],
            )

        # 2. Exact match on Base Class
        cls_match = self._norm_to_class.get(norm)
        if cls_match is not None:
            cls_summary = self._classes_by_name[cls_match]
            details = [self._asc_details_by_name[a.name] for a in cls_summary.ascendancies]
            return AscendancyInspection(
                query=q_clean,
                matched_kind="class",
                class_name=cls_match,
                primary=None,
                ascendancies=details,
            )

        # 3. Substring or fuzzy match across Ascendancies and Classes
        for k_norm, target_asc in self._norm_to_asc.items():
            if norm in k_norm or k_norm in norm:
                detail = self._asc_details_by_name[target_asc]
                return AscendancyInspection(
                    query=q_clean,
                    matched_kind="ascendancy",
                    class_name=detail.class_name,
                    primary=detail,
                    ascendancies=[detail],
                )

        candidates = list(self._norm_to_asc.keys()) + list(self._norm_to_class.keys())
        close = difflib.get_close_matches(norm, candidates, n=1, cutoff=0.72)
        if close:
            hit = close[0]
            if hit in self._norm_to_asc:
                detail = self._asc_details_by_name[self._norm_to_asc[hit]]
                return AscendancyInspection(
                    query=q_clean,
                    matched_kind="ascendancy",
                    class_name=detail.class_name,
                    primary=detail,
                    ascendancies=[detail],
                )
            cls_name = self._norm_to_class[hit]
            details = [
                self._asc_details_by_name[a.name]
                for a in self._classes_by_name[cls_name].ascendancies
            ]
            return AscendancyInspection(
                query=q_clean,
                matched_kind="class",
                class_name=cls_name,
                primary=None,
                ascendancies=details,
            )

        raise LookupError(
            f"No PoE2 {POE2_PATCH} Ascendancy or Class matching {q_clean!r}. "
            f"Valid classes: {', '.join(self._classes_by_name)}."
        )

    # ------------------------------------------------------------------
    # 2. Live Ladder & Economy Meta Telemetry
    # ------------------------------------------------------------------

    def _cached_get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        force_refresh: bool = False,
    ) -> Any:
        def _fetch() -> Any:
            return self.http_client.get_json(url, params=params, headers=headers)

        return get_cached_json(
            url,
            params=params,
            ttl_seconds=self.ttl_seconds,
            fetcher=_fetch,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )

    def _resolve_character_class_and_ascendancy(
        self, raw_class: str
    ) -> tuple[str, str | None] | None:
        """Map ladder `class` string to `(base_class, ascendancy | None)`, rejecting PoE1."""
        self._ensure_tree_loaded()
        norm = _norm_key(raw_class)
        if not norm or norm in POE1_OR_UNRELEASED_TRAP_CLASSES:
            return None
        if norm in self._norm_to_asc:
            asc = self._norm_to_asc[norm]
            return self._asc_to_class[asc], asc
        if norm in self._norm_to_class:
            return self._norm_to_class[norm], None
        return None

    def _try_fetch_ggg_ladder_snapshot(
        self,
        league_name: str,
        mode: Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"],
        *,
        top_n: int,
        force_refresh: bool,
        notes: list[str],
    ) -> LadderModeSnapshot | None:
        enc_league = quote(league_name, safe="")
        limit = max(1, min(top_n, 200))

        if self.ggg_oauth_token:
            url = f"{GGG_OAUTH_API_BASE_URL}/league/{enc_league}/ladder"
            params: dict[str, Any] = {"realm": "poe2", "limit": limit}
            headers: dict[str, str] | None = {"Authorization": f"Bearer {self.ggg_oauth_token}"}
            source_label = "ggg_oauth_ladder"
        else:
            url = f"{self.ggg_ladder_base_url}/{enc_league}"
            params = {"realm": "poe2", "limit": limit}
            headers = None
            source_label = "ggg_public_ladder"

        def _fetch_ggg() -> Any:
            try:
                return self.http_client.get_json(url, params=params, headers=headers)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404):
                    return {"_unauthenticated_status": exc.response.status_code}
                raise

        try:
            raw = get_cached_json(
                url,
                params=params,
                ttl_seconds=self.ttl_seconds,
                fetcher=_fetch_ggg,
                cache_dir=self.cache_dir,
                client=self.http_client,
                force_refresh=force_refresh,
                allow_stale_on_error=True,
                now_fn=self._now_fn,
            )
        except httpx.HTTPError as exc:
            notes.append(f"GGG ladder API ({league_name}) unreachable ({type(exc).__name__}).")
            return None

        if isinstance(raw, Mapping) and raw.get("_unauthenticated_status") in (401, 403, 404):
            code = int(raw["_unauthenticated_status"])
            msg = (
                f"GGG ladder API ({league_name}) returned HTTP {code} without OAuth 2.1 "
                "`service:leagues:ladder` token; using live poe.ninja build-index-state."
            )
            if msg not in notes:
                notes.append(msg)
            return None

        if not isinstance(raw, Mapping) or not isinstance(raw.get("entries"), list):
            return None

        entries = [e for e in raw["entries"] if isinstance(e, Mapping)][:top_n]
        if not entries:
            return None

        parsed_rows: list[LadderLeaderEntry] = []
        poe1_contaminated = 0
        asc_counts: dict[str, int] = defaultdict(int)
        cls_counts: dict[str, int] = defaultdict(int)
        lv100 = 0
        dead_cnt = 0
        levels: list[int] = []

        for idx, entry in enumerate(entries, start=1):
            char = entry.get("character") if isinstance(entry.get("character"), Mapping) else {}
            acct = entry.get("account") if isinstance(entry.get("account"), Mapping) else {}
            raw_cls = str(char.get("class") or "").strip()
            resolved = self._resolve_character_class_and_ascendancy(raw_cls)
            if resolved is None:
                poe1_contaminated += 1
                continue

            base_cls, asc_name = resolved
            lvl = int(char.get("level") or 1)
            exp_val = char.get("experience")
            exp_int = int(exp_val) if isinstance(exp_val, (int, float)) else None
            is_dead = bool(entry.get("dead", False))
            depth_obj = char.get("depth") if isinstance(char.get("depth"), Mapping) else {}
            depth_val = depth_obj.get("solo") or depth_obj.get("default")
            depth_int = int(depth_val) if isinstance(depth_val, (int, float)) else None

            levels.append(lvl)
            if lvl >= 100:
                lv100 += 1
            if is_dead:
                dead_cnt += 1
            cls_counts[base_cls] += 1
            if asc_name:
                asc_counts[asc_name] += 1

            parsed_rows.append(
                LadderLeaderEntry(
                    rank=int(entry.get("rank") or idx),
                    character_name=str(char.get("name") or f"Rank_{idx}"),
                    account_name=str(acct.get("name")) if acct.get("name") else None,
                    class_name=base_cls,
                    ascendancy_name=asc_name,
                    level=lvl,
                    experience=exp_int,
                    dead=is_dead,
                    delve_depth=depth_int,
                )
            )

        if not parsed_rows or poe1_contaminated > len(parsed_rows):
            notes.append(
                f"Rejected legacy PoE1 ladder response on {league_name!r} "
                f"({poe1_contaminated} non-PoE2 entries; legacy /api/ladders ignores realm=poe2)."
            )
            return None

        sampled = len(parsed_rows)
        total_chars = int(raw.get("total") or sampled)

        asc_shares = [
            AscendancyShareEntry(
                ascendancy_name=asc,
                class_name=self._asc_to_class[asc],
                share_pct=round((cnt / sampled) * 100.0, 2),
                character_count=cnt,
                trend=0,
            )
            for asc, cnt in sorted(asc_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        cls_shares = [
            ClassShareEntry(
                class_name=cls_nm,
                share_pct=round((cnt / sampled) * 100.0, 2),
                character_count=cnt,
                ascendancies=[a.ascendancy_name for a in asc_shares if a.class_name == cls_nm],
            )
            for cls_nm, cnt in sorted(cls_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        xp_leaders = sorted(
            parsed_rows,
            key=lambda r: (r.rank, -(r.experience or 0), -r.level),
        )[:10]
        delve_leaders = sorted(
            [r for r in parsed_rows if r.delve_depth is not None and r.delve_depth > 0],
            key=lambda r: -(r.delve_depth or 0),
        )[:10]

        return LadderModeSnapshot(
            league_name=league_name,
            mode=mode,
            hardcore=mode in ("hc_trade", "hc_ssf"),
            ssf=mode in ("ssf", "hc_ssf"),
            source=source_label,
            total_characters=total_chars,
            sampled_characters=sampled,
            level_100_count=lv100,
            dead_count=dead_cnt,
            min_level=min(levels) if levels else None,
            max_level=max(levels) if levels else None,
            ascendancy_shares=asc_shares,
            class_shares=cls_shares,
            xp_leaders=xp_leaders,
            delve_leaders=delve_leaders,
        )

    def _fetch_poeninja_mode_snapshots(
        self,
        family: Mapping[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], str],
        *,
        force_refresh: bool,
        notes: list[str],
    ) -> dict[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], LadderModeSnapshot]:
        self._ensure_tree_loaded()
        try:
            raw = self._cached_get_json(
                self.poeninja_build_index_url,
                force_refresh=force_refresh,
            )
        except httpx.HTTPError as exc:
            notes.append(f"poe.ninja build-index-state unreachable ({type(exc).__name__}).")
            return {}

        league_builds = raw.get("leagueBuilds") if isinstance(raw, Mapping) else None
        if not isinstance(league_builds, list):
            return {}

        by_lower_name: dict[str, Mapping[str, Any]] = {}
        for row in league_builds:
            if isinstance(row, Mapping) and row.get("leagueName"):
                by_lower_name[str(row["leagueName"]).strip().lower()] = row

        snapshots: dict[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], LadderModeSnapshot] = {}
        for mode, target_name in family.items():
            row = by_lower_name.get(target_name.lower())
            if row is None:
                continue
            total = int(row.get("total") or 0)
            stats_list = row.get("statistics") if isinstance(row.get("statistics"), list) else []

            asc_shares: list[AscendancyShareEntry] = []
            cls_pct_totals: dict[str, float] = defaultdict(float)
            cls_char_totals: dict[str, int] = defaultdict(int)
            cls_asc_names: dict[str, list[str]] = defaultdict(list)

            for st in stats_list:
                if not isinstance(st, Mapping):
                    continue
                raw_cls = str(st.get("class") or "").strip()
                pct = float(st.get("percentage") or 0.0)
                trend = int(st.get("trend") or 0)
                est_count = int(round(total * (pct / 100.0)))
                resolved = self._resolve_character_class_and_ascendancy(raw_cls)
                if resolved is None:
                    continue
                base_cls, asc_name = resolved
                label_asc = asc_name or base_cls
                asc_shares.append(
                    AscendancyShareEntry(
                        ascendancy_name=label_asc,
                        class_name=base_cls,
                        share_pct=round(pct, 2),
                        character_count=est_count,
                        trend=trend,
                    )
                )
                cls_pct_totals[base_cls] += pct
                cls_char_totals[base_cls] += est_count
                if label_asc not in cls_asc_names[base_cls]:
                    cls_asc_names[base_cls].append(label_asc)

            asc_shares.sort(key=lambda x: (-x.share_pct, x.ascendancy_name))
            cls_shares = [
                ClassShareEntry(
                    class_name=cls_nm,
                    share_pct=round(pct_sum, 2),
                    character_count=cls_char_totals[cls_nm],
                    ascendancies=cls_asc_names[cls_nm],
                )
                for cls_nm, pct_sum in sorted(
                    cls_pct_totals.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ]

            snapshots[mode] = LadderModeSnapshot(
                league_name=str(row.get("leagueName") or target_name),
                mode=mode,
                hardcore=bool(row.get("hardcore", mode in ("hc_trade", "hc_ssf"))),
                ssf=mode in ("ssf", "hc_ssf"),
                source="poeninja_build_index",
                total_characters=total,
                sampled_characters=total,
                unlisted_share_pct=round(max(0.0, 100.0 - sum(a.share_pct for a in asc_shares)), 2),
                ascendancy_shares=asc_shares,
                class_shares=cls_shares,
            )

        return snapshots

    def _collect_chase_demand_signals(
        self,
        league: str,
        *,
        divine_exalted: float,
        force_refresh: bool = False,
        limit: int = 20,
    ) -> list[MetaDemandSignal]:
        signals: list[MetaDemandSignal] = []

        # 1. Chase Uniques across poe2scout categories (filtered for depth >= 5 listings)
        for ucat in POE2SCOUT_UNIQUE_CATEGORIES:
            if ucat == "map":
                continue
            try:
                uniques = self.economy_service.get_uniques_by_category(
                    ucat,
                    league=league,
                    page=1,
                    per_page=100,
                    force_refresh=force_refresh,
                )
            except httpx.HTTPError:
                continue

            for uq in uniques:
                if uq.name.strip().upper() == "INCOMPLETE":
                    continue
                qty = int(uq.quantity or 0)
                div_val = (
                    uq.divine
                    if (uq.divine is not None and uq.divine > 0)
                    else (uq.exalted / divine_exalted)
                )
                if qty < 5 or div_val < 3.0:
                    continue
                tags = _infer_demand_archetypes(uq.id, uq.name)
                signals.append(
                    MetaDemandSignal(
                        id=uq.id,
                        name=uq.name,
                        category=f"unique_{ucat}",
                        item_type=uq.base_type,
                        exalted=round(uq.exalted, 2),
                        divine=round(div_val, 2),
                        liquidity=float(qty),
                        liquidity_unit="active_listings",
                        change_7d_pct=uq.change_7d_pct,
                        archetype_tags=tags,
                        rationale=(
                            f"{div_val:.1f} Divine chase {ucat} ({qty} active listings) "
                            f"anchoring {', '.join(tags[:2])}."
                        ),
                    )
                )

        # 2. Endgame Lineage Support Gems, Soul Cores & Runes via EconomyService
        for cat_name in ("lineagesupportgems", "ultimatum", "runes"):
            try:
                quotes = self.economy_service.search_prices(
                    "a",
                    league=league,
                    category=cat_name,
                    limit=50,
                    force_refresh=False,
                )
            except httpx.HTTPError:
                quotes = []

            # Also pull full snapshot entries for the category if EconomyService already loaded it
            snap = self.economy_service._snapshots.get(league)
            cand_quotes = (
                [
                    e.quote
                    for e in snap.entries_by_id.values()
                    if e.category == cat_name and e.quote is not None
                ]
                if snap is not None
                else quotes
            )
            for q in cand_quotes:
                vol = float(q.volume_primary or 0.0)
                div_val = (
                    q.divine
                    if (q.divine is not None and q.divine > 0)
                    else (q.exalted / divine_exalted)
                )
                if vol < 25.0 or div_val < 15.0:
                    continue
                tags = _infer_demand_archetypes(q.id, q.name)
                signals.append(
                    MetaDemandSignal(
                        id=q.id,
                        name=q.name,
                        category=q.category,
                        item_type="Lineage Support Gem"
                        if cat_name == "lineagesupportgems"
                        else "Socketable Augment",
                        exalted=round(q.exalted, 2),
                        divine=round(div_val, 2),
                        liquidity=round(vol, 1),
                        liquidity_unit="divine_volume",
                        change_7d_pct=q.change_7d_pct,
                        archetype_tags=tags,
                        rationale=(
                            f"{div_val:.1f} Divine ({vol:.0f} Div exchange volume) "
                            f"driving {', '.join(tags[:2])}."
                        ),
                    )
                )

        signals.sort(
            key=lambda s: min(s.divine, 600.0) * (1.0 + min(s.liquidity, 2000.0) / 1000.0),
            reverse=True,
        )
        return signals[: max(1, limit)]

    def _compute_ascendancy_rankings(
        self,
        mode_map: Mapping[str, LadderModeSnapshot],
    ) -> list[AscendancyTierEntry]:
        self._ensure_tree_loaded()

        def _share_lookup(mode_key: str) -> dict[str, float]:
            snap = mode_map.get(mode_key)
            if snap is None:
                return {}
            return {e.ascendancy_name: e.share_pct for e in snap.ascendancy_shares}

        sc_map = _share_lookup("sc_trade")
        hc_map = _share_lookup("hc_trade")
        ssf_map = _share_lookup("ssf")
        hcssf_map = _share_lookup("hc_ssf")

        scored_entries: list[tuple[float, str, dict[str, Any]]] = []
        for asc_name, detail in self._asc_details_by_name.items():
            sc_pct = sc_map.get(asc_name, 0.0)
            hc_pct = hc_map.get(asc_name, 0.0)
            ssf_pct = ssf_map.get(asc_name, 0.0)
            hcssf_pct = hcssf_map.get(asc_name, 0.0)

            prof = _ASCENDANCY_PROFILES.get(asc_name, {})
            boss_base = float(prof.get("boss_bonus", 7.8))
            map_base = float(prof.get("map_bonus", 7.8))

            # Log-damped ladder share components + mechanical breadth across SC, HC, and SSF
            sc_pts = min(38.0, sc_pct * 2.1 if sc_pct <= 12.0 else 25.2 + (sc_pct - 12.0) * 0.64)
            hc_pts = min(24.0, hc_pct * 1.45)
            ssf_pts = min(18.0, (ssf_pct * 0.95) + (hcssf_pct * 0.45))
            mech_pts = (boss_base + map_base) * 1.05
            composite = round(sc_pts + hc_pts + ssf_pts + mech_pts, 2)

            hc_score = hc_pct * 1.5 + hcssf_pct * 0.8 + (boss_base - 7.5) * 3.0
            ssf_score = ssf_pct * 1.6 + hcssf_pct * 0.7 + (map_base - 7.5) * 2.5
            boss_score = boss_base * 10.0 + min(12.0, sc_pct * 0.5 + hc_pct * 0.4)
            map_score = map_base * 10.0 + min(12.0, sc_pct * 0.6 + ssf_pct * 0.3)

            scored_entries.append(
                (
                    composite,
                    asc_name,
                    {
                        "detail": detail,
                        "sc_pct": sc_pct,
                        "hc_pct": hc_pct,
                        "ssf_pct": ssf_pct,
                        "hcssf_pct": hcssf_pct,
                        "boss_tier": _score_to_tier(boss_score, s_cut=99.0, a_cut=90.0, b_cut=83.0),
                        "map_tier": _score_to_tier(map_score, s_cut=99.0, a_cut=90.0, b_cut=83.0),
                        "hc_tier": _score_to_tier(hc_score, s_cut=20.0, a_cut=8.0, b_cut=3.2),
                        "ssf_tier": _score_to_tier(ssf_score, s_cut=18.0, a_cut=7.5, b_cut=3.0),
                        "archetypes": list(prof.get("archetypes") or []),
                    },
                )
            )

        scored_entries.sort(key=lambda row: (-row[0], row[1]))
        rankings: list[AscendancyTierEntry] = []
        for rank_idx, (comp_score, asc_name, meta) in enumerate(scored_entries, start=1):
            detail = meta["detail"]
            overall_tier = _score_to_tier(comp_score, s_cut=48.0, a_cut=26.0, b_cut=18.5)
            rankings.append(
                AscendancyTierEntry(
                    rank=rank_idx,
                    ascendancy_name=asc_name,
                    class_name=detail.class_name,
                    tier=overall_tier,
                    composite_score=comp_score,
                    sc_share_pct=round(meta["sc_pct"], 2),
                    hc_share_pct=round(meta["hc_pct"], 2),
                    ssf_share_pct=round(meta["ssf_pct"], 2),
                    hcssf_share_pct=round(meta["hcssf_pct"], 2),
                    bossing_tier=meta["boss_tier"],
                    mapping_tier=meta["map_tier"],
                    hc_viability_tier=meta["hc_tier"],
                    ssf_viability_tier=meta["ssf_tier"],
                    signature_notables=detail.notable_names[:4],
                    meta_archetypes=meta["archetypes"],
                )
            )
        return rankings

    def rank_ascendancies(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        force_refresh: bool = False,
    ) -> list[AscendancyTierEntry]:
        report = self.get_ladder_meta(
            league=league,
            top_n=200,
            include_economy_signals=False,
            force_refresh=force_refresh,
        )
        return report.ascendancy_rankings

    def get_ladder_meta(
        self,
        league: str = DEFAULT_LEAGUE,
        top_n: int = 200,
        *,
        include_economy_signals: bool = True,
        force_refresh: bool = False,
    ) -> LadderMetaReport:
        """Aggregate live PoE2 SC/HC/SSF ladder share %, leaderboards, and chase demand signals."""
        self._ensure_tree_loaded()
        family = _league_family_names(league)
        requested_mode = _mode_for_league_name(league, family)
        notes: list[str] = []

        # 1. Fetch multi-mode playrates across SC, HC, SSF, and HCSSF from poe.ninja
        ninja_modes = self._fetch_poeninja_mode_snapshots(
            family,
            force_refresh=force_refresh,
            notes=notes,
        )

        # 2. Probe official GGG ladder endpoint for requested league; merge leader details
        ggg_primary = self._try_fetch_ggg_ladder_snapshot(
            family[requested_mode],
            requested_mode,
            top_n=top_n,
            force_refresh=force_refresh,
            notes=notes,
        )

        mode_breakdown: dict[str, LadderModeSnapshot] = dict(ninja_modes)
        if ggg_primary is not None:
            existing_ninja = mode_breakdown.get(requested_mode)
            if existing_ninja is not None and not ggg_primary.ascendancy_shares:
                ggg_primary = ggg_primary.model_copy(
                    update={
                        "ascendancy_shares": existing_ninja.ascendancy_shares,
                        "class_shares": existing_ninja.class_shares,
                    }
                )
            mode_breakdown[requested_mode] = ggg_primary

        primary_snapshot = mode_breakdown.get(requested_mode)
        if primary_snapshot is None:
            primary_snapshot = LadderModeSnapshot(
                league_name=league,
                mode=requested_mode,
                hardcore=requested_mode in ("hc_trade", "hc_ssf"),
                ssf=requested_mode in ("ssf", "hc_ssf"),
                source="offline_empty",
            )
            mode_breakdown[requested_mode] = primary_snapshot

        # 3. Enrich with live EconomyService reference rates & chase unique / lineage gem demand
        div_ex: float | None = None
        demand_signals: list[MetaDemandSignal] = []
        econ_league = (
            family["hc_trade"] if requested_mode in ("hc_trade", "hc_ssf") else family["sc_trade"]
        )
        if include_economy_signals:
            try:
                rates = self.economy_service.get_reference_rates(
                    econ_league, force_refresh=force_refresh
                )
                div_ex = round(rates.divine_exalted, 2)
                demand_signals = self._collect_chase_demand_signals(
                    econ_league,
                    divine_exalted=rates.divine_exalted,
                    force_refresh=force_refresh,
                )
            except Exception as exc:
                notes.append(f"Economy demand enrichment skipped ({type(exc).__name__}).")

        rankings = self._compute_ascendancy_rankings(mode_breakdown)

        return LadderMetaReport(
            league=league,
            patch=POE2_PATCH,
            top_n=top_n,
            ttl_seconds=self.ttl_seconds,
            divine_exalted=div_ex,
            primary_mode=primary_snapshot,
            mode_breakdown=mode_breakdown,
            chase_demand_signals=demand_signals,
            ascendancy_rankings=rankings,
            source_notes=notes,
        )
