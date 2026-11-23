from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from poe2brain.models.common import AffixSide, StatRollRange
from poe2brain.models.items import (
    ItemRarity,
    ItemRequirements,
    ModDomain,
    ParsedItem,
    ParsedMod,
)
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle

ITEM_CLASS_TO_CATEGORY: dict[str, tuple[str, str | None]] = {
    "Amulets": ("Amulet", "accessory.amulet"),
    "Rings": ("Ring", "accessory.ring"),
    "Belts": ("Belt", "accessory.belt"),
    "Trinkets": ("Trinket", "accessory.trinket"),
    "Body Armours": ("Body Armour", "armour.chest"),
    "Helmets": ("Helmet", "armour.helmet"),
    "Gloves": ("Gloves", "armour.gloves"),
    "Boots": ("Boots", "armour.boots"),
    "Shields": ("Shield", "armour.shield"),
    "Bucklers": ("Buckler", "armour.buckler"),
    "Foci": ("Focus", "armour.focus"),
    "Quivers": ("Quiver", "armour.quiver"),
    "Bows": ("Bow", "weapon.bow"),
    "Crossbows": ("Crossbow", "weapon.crossbow"),
    "Wands": ("Wand", "weapon.wand"),
    "Sceptres": ("Sceptre", "weapon.sceptre"),
    "Staves": ("Staff", "weapon.staff"),
    "Quarterstaves": ("Warstaff", "weapon.warstaff"),
    "Warstaves": ("Warstaff", "weapon.warstaff"),
    "One Hand Swords": ("One-Handed Sword", "weapon.onesword"),
    "Two Hand Swords": ("Two-Handed Sword", "weapon.twosword"),
    "One Hand Axes": ("One-Handed Axe", "weapon.oneaxe"),
    "Two Hand Axes": ("Two-Handed Axe", "weapon.twoaxe"),
    "One Hand Maces": ("One-Handed Mace", "weapon.onemace"),
    "Two Hand Maces": ("Two-Handed Mace", "weapon.twomace"),
    "Claws": ("Claw", "weapon.claw"),
    "Daggers": ("Dagger", "weapon.dagger"),
    "Rune Daggers": ("Rune Dagger", "weapon.runedagger"),
    "Spears": ("Spear", "weapon.spear"),
    "Flails": ("Flail", "weapon.flail"),
    "Talismans": ("Talisman", "weapon.talisman"),
    "Fishing Rods": ("Fishing Rod", "weapon.rod"),
    "Life Flasks": ("Life Flask", "flask.life"),
    "Mana Flasks": ("Mana Flask", "flask.mana"),
    "Charms": ("Charm", "flask.charm"),
    "Jewels": ("Jewel", "jewel"),
    "Abyss Jewels": ("Abyss Jewel", "jewel.abyss"),
    "Waystones": ("Waystone", "map.waystone"),
    "Maps": ("Map", "map"),
    "Tablet": ("Tablet", "map.tablet"),
    "Tablets": ("Tablet", "map.tablet"),
    "Map Fragments": ("Map Fragment", "map.fragment"),
    "Breachstones": ("Breachstone", "map.breachstone"),
    "Expedition Logbooks": ("Logbook", "map.logbook"),
    "Trial Coins": ("Trial Coin", "map.barya"),
    "Inscribed Ultimatum": ("Ultimatum", "map.ultimatum"),
    "Pinnacle Keys": ("Pinnacle Key", "map.bosskey"),
    "Relics": ("Relic", "sanctum.relic"),
    "Skill Gems": ("Skill Gem", "gem.activegem"),
    "Active Skill Gems": ("Skill Gem", "gem.activegem"),
    "Support Gems": ("Support Gem", "gem.supportgem"),
    "Meta Skill Gems": ("Meta Gem", "gem.metagem"),
    "Uncut Skill Gems": ("Uncut Skill Gem", "currency"),
    "Uncut Spirit Gems": ("Uncut Spirit Gem", "currency"),
    "Uncut Support Gems": ("Uncut Support Gem", "currency"),
    "Socketable": ("Socketable", "currency.socketable"),
    "Omen": ("Omen", "currency.omen"),
    "Stackable Currency": ("Currency", "currency"),
    "Currency": ("Currency", "currency"),
    "Divination Cards": ("Divination Card", "card"),
}

EE2_CATEGORY_TO_TRADE: dict[str, str] = {
    "Amulet": "accessory.amulet",
    "Ring": "accessory.ring",
    "Belt": "accessory.belt",
    "Body Armour": "armour.chest",
    "Helmet": "armour.helmet",
    "Gloves": "armour.gloves",
    "Boots": "armour.boots",
    "Shield": "armour.shield",
    "Buckler": "armour.buckler",
    "Focus": "armour.focus",
    "Quiver": "armour.quiver",
    "Bow": "weapon.bow",
    "Crossbow": "weapon.crossbow",
    "Wand": "weapon.wand",
    "Sceptre": "weapon.sceptre",
    "Staff": "weapon.staff",
    "Warstaff": "weapon.warstaff",
    "Quarterstaff": "weapon.warstaff",
    "One Hand Sword": "weapon.onesword",
    "One-Handed Sword": "weapon.onesword",
    "Two Hand Sword": "weapon.twosword",
    "Two-Handed Sword": "weapon.twosword",
    "One Hand Axe": "weapon.oneaxe",
    "One-Handed Axe": "weapon.oneaxe",
    "Two Hand Axe": "weapon.twoaxe",
    "Two-Handed Axe": "weapon.twoaxe",
    "One Hand Mace": "weapon.onemace",
    "One-Handed Mace": "weapon.onemace",
    "Two Hand Mace": "weapon.twomace",
    "Two-Handed Mace": "weapon.twomace",
    "Claw": "weapon.claw",
    "Dagger": "weapon.dagger",
    "Spear": "weapon.spear",
    "Flail": "weapon.flail",
    "Talisman": "weapon.talisman",
    "Flask": "flask",
    "Life Flask": "flask.life",
    "Mana Flask": "flask.mana",
    "Charm": "flask.charm",
    "Jewel": "jewel",
    "Waystone": "map.waystone",
    "Map": "map",
    "Tablet": "map.tablet",
    "Relic": "sanctum.relic",
    "Sanctum Relic": "sanctum.relic",
    "Skill Gem": "gem.activegem",
    "Support Gem": "gem.supportgem",
    "Meta Gem": "gem.metagem",
    "Gem": "gem",
    "SoulCore": "currency.socketable",
    "Currency": "currency",
}

SLOT_ALIASES_TO_TRADE: dict[str, str] = {
    "weapon 1": "weapon",
    "weapon 2": "armour",
    "weapon": "weapon",
    "offhand": "armour.focus",
    "body armour": "armour.chest",
    "chest": "armour.chest",
    "helmet": "armour.helmet",
    "helm": "armour.helmet",
    "gloves": "armour.gloves",
    "boots": "armour.boots",
    "amulet": "accessory.amulet",
    "ring": "accessory.ring",
    "ring 1": "accessory.ring",
    "ring 2": "accessory.ring",
    "belt": "accessory.belt",
    "quiver": "armour.quiver",
    "shield": "armour.shield",
    "buckler": "armour.buckler",
    "focus": "armour.focus",
    "bow": "weapon.bow",
    "crossbow": "weapon.crossbow",
    "wand": "weapon.wand",
    "sceptre": "weapon.sceptre",
    "staff": "weapon.staff",
    "quarterstaff": "weapon.warstaff",
    "warstaff": "weapon.warstaff",
    "spear": "weapon.spear",
    "flail": "weapon.flail",
    "talisman": "weapon.talisman",
    "mace": "weapon.onemace",
    "two hand mace": "weapon.twomace",
    "jewel": "jewel",
    "charm": "flask.charm",
    "charm 1": "flask.charm",
    "charm 2": "flask.charm",
    "charm 3": "flask.charm",
    "flask": "flask",
    "flask 1": "flask.life",
    "flask 2": "flask.mana",
    "waystone": "map.waystone",
    "tablet": "map.tablet",
    "relic": "sanctum.relic",
}

WIKI_MARKUP_RE = re.compile(r"\[([^\]|]+)\|?([^\]]*)\]")
QUALITY_HEADER_RE = re.compile(r"^Quality(?:\s*\((?P<qtype>[^)]+)\))?:\s*[+-]?(?P<val>\d+)%")
REQUIRES_RE = re.compile(
    r"^Requires:\s*(?:Level[^\d,]*(?P<level>\d+))?"
    r"(?:.*?\b(?P<str>\d+)\s*(?:\([^)]+\)\s*)?(?:Strength|Str)\b)?"
    r"(?:.*?\b(?P<dex>\d+)\s*(?:\([^)]+\)\s*)?(?:Dexterity|Dex)\b)?"
    r"(?:.*?\b(?P<int>\d+)\s*(?:\([^)]+\)\s*)?(?:Intelligence|Int)\b)?",
)
UNIDENTIFIED_RE = re.compile(r"^Unidentified(?:\s*\(Tier\s*(?P<tier>\d+)\))?$")
UNCUT_LEVEL_RE = re.compile(r"\(Level\s+(?P<lvl>\d+)\)")
WAYSTONE_TIER_NAME_RE = re.compile(r"Waystone\s*\(Tier\s*(?P<tier>\d+)\)")
MOD_HEADER_RE = re.compile(
    r'^(?P<kind>[^"]+)(?:\s+"(?P<name>[^"]*)")?'
    r"(?:\s*\(Tier:\s*(?P<tier>\d+)\))?"
    r"(?:\s*\(Rank:\s*(?P<rank>\d+)\))?$"
)
MOD_INCREASED_RE = re.compile(r"^(\d+)%\s+Increased$")
INLINE_ROLL_RE = re.compile(
    r"(?P<val>(?<!\d|\))[+-]?\d+(?:\.\d+)?)"
    r"\((?P<min>[+-]?\d+(?:\.\d+)?)(?:-(?P<max>[+-]?\d+(?:\.\d+)?))?\)"
)
PLAIN_NUMBER_RE = re.compile(r"(?<!\d)[+-]?\d+(?:\.\d+)?")
DAMAGE_PAIR_RE = re.compile(r"([\d,]+)\s*-\s*([\d,]+)")

SUFFIX_DOMAIN_MARKERS: tuple[tuple[str, ModDomain], ...] = (
    (" (enchant)", ModDomain.ENCHANT),
    (" (rune)", ModDomain.RUNE),
    (" (added rune)", ModDomain.RUNE),
    (" (implicit)", ModDomain.IMPLICIT),
    (" (fractured)", ModDomain.FRACTURED),
    (" (desecrated)", ModDomain.DESECRATED),
    (" (crafted)", ModDomain.CRAFTED),
)

FLASK_CHARM_SKIP_PATTERNS = (
    re.compile(r"^Lasts\s+[\d.]+\s*(?:\([^)]+\)\s*)?Seconds?$"),
    re.compile(r"^Recovers\s+.+\s+over\s+.+\s+Seconds?$"),
    re.compile(r"^Consumes\s+\d+.*\s+of\s+\d+.*\s+Charges\s+on\s+use$"),
    re.compile(r"^Currently\s+has\s+\d+\s+Charges$"),
)

HELP_TEXT_PREFIXES = (
    "Right Click to ",
    "Right-click to ",
    "Can be used in a ",
    "Place into an allocated ",
    "Place one or more Skill Gems ",
    "Place into a Skill's Support ",
    "Used automatically when condition is met.",
    "Socket this into ",
    "Combine this with ",
    "Click to ",
)


def strip_wiki_markup(text: str) -> str:
    """Convert PoE2 trade API '[LinkTarget|Display]' or '[Display]' markup to plain text."""
    return WIKI_MARKUP_RE.sub(lambda m: m.group(2) or m.group(1), text)


def _strip_ggg_condition_markup(text: str) -> str:
    text = re.sub(r"<<set:.+?>>", "", text)
    return re.sub(
        r"<(if:.+?|elif:.+?|else)>\{(.+?)\}",
        lambda m: m.group(2) if m.group(1).startswith("if:") else "",
        text,
    )


def resolve_slot_or_category(slot_or_category: str | None) -> str | None:
    if not slot_or_category:
        return None
    raw = slot_or_category.strip()
    if "." in raw:
        return raw
    if raw in EE2_CATEGORY_TO_TRADE:
        return EE2_CATEGORY_TO_TRADE[raw]
    if raw in ITEM_CLASS_TO_CATEGORY:
        return ITEM_CLASS_TO_CATEGORY[raw][1]
    low = raw.lower()
    if low in SLOT_ALIASES_TO_TRADE:
        return SLOT_ALIASES_TO_TRADE[low]
    return raw


def resolve_trade_category(item: ParsedItem, bundle: EE2Bundle | None = None) -> str | None:
    if bundle is None:
        bundle = get_default_bundle()
    if item.item_class and item.item_class in ITEM_CLASS_TO_CATEGORY:
        return ITEM_CLASS_TO_CATEGORY[item.item_class][1]
    if item.rarity == ItemRarity.GEM and not item.item_class:
        return "gem.metagem" if item.spirit_reservation is not None else "gem.activegem"
    lookup = (
        item.name
        if (item.rarity == ItemRarity.UNIQUE and item.identified and item.name)
        else item.base_type
    )
    ns = (
        "UNIQUE"
        if (item.rarity == ItemRarity.UNIQUE and item.identified)
        else ("GEM" if item.rarity == ItemRarity.GEM else "ITEM")
    )
    hits = bundle.find_item(lookup, namespace=ns) or bundle.find_item(lookup)
    if hits:
        hit0 = hits[0]
        ee2_cat = (hit0.get("craftable") or {}).get("category")
        if not ee2_cat and hit0.get("unique"):
            base_hits = bundle.find_item(hit0["unique"]["base"], namespace="ITEM")
            if base_hits:
                ee2_cat = (base_hits[0].get("craftable") or {}).get("category")
        if ee2_cat:
            return EE2_CATEGORY_TO_TRADE.get(ee2_cat)
    return None


def _split_sections(clipboard: str) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for raw_line in clipboard.strip().splitlines():
        line = raw_line.strip()
        if line == "--------":
            if sections[-1]:
                sections.append([])
        elif line:
            sections[-1].append(line)
    return [s for s in sections if s]


def _parse_damage_avg(val_str: str) -> float:
    total = 0.0
    for m in DAMAGE_PAIR_RE.finditer(val_str):
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        total += (lo + hi) / 2.0
    return total


def _extract_inline_rolls(line: str) -> tuple[str, list[float], list[StatRollRange]]:
    ranges: list[StatRollRange] = []

    def _repl(m: re.Match[str]) -> str:
        lo = float(m.group("min"))
        hi = float(m.group("max")) if m.group("max") is not None else lo
        if lo > hi:
            lo, hi = hi, lo
        ranges.append(StatRollRange(min=lo, max=hi))
        return m.group("val")

    cleaned = INLINE_ROLL_RE.sub(_repl, line).replace("()", "")
    values = [float(m.group(0)) for m in PLAIN_NUMBER_RE.finditer(cleaned)]
    return cleaned, values, ranges


def _parse_mod_header(
    header_line: str,
) -> tuple[ModDomain, AffixSide | None, str | None, int | None, list[str]]:
    inner = header_line[1:-1].strip()
    parts = [p.strip() for p in inner.split("\u2014")]
    mod_part = parts[0]
    tags: list[str] = []
    for extra in parts[1:]:
        if not MOD_INCREASED_RE.match(extra):
            tags.extend(t.strip() for t in extra.split(",") if t.strip())

    m = MOD_HEADER_RE.match(mod_part)
    if not m:
        return ModDomain.EXPLICIT, None, None, None, tags

    kind = m.group("kind").strip()
    affix_name = m.group("name") or None
    tier = int(m.group("tier")) if m.group("tier") else None

    domain = ModDomain.EXPLICIT
    side: AffixSide | None = None

    if kind.startswith("Fractured"):
        domain = ModDomain.FRACTURED
        kind = kind[len("Fractured") :].strip()
    elif kind.startswith("Desecrated"):
        domain = ModDomain.DESECRATED
        kind = kind[len("Desecrated") :].strip()
    elif kind.startswith("Crafted"):
        domain = ModDomain.CRAFTED
        kind = kind[len("Crafted") :].strip()

    if "Prefix" in kind:
        side = AffixSide.PREFIX
    elif "Suffix" in kind:
        side = AffixSide.SUFFIX
    elif "Implicit" in kind:
        side = AffixSide.IMPLICIT
        if domain == ModDomain.EXPLICIT:
            domain = ModDomain.IMPLICIT
    elif "Corruption Enhancement" in kind:
        domain = ModDomain.CORRUPTED
        side = AffixSide.CORRUPTED
    elif "Enhancement" in kind:
        domain = ModDomain.ENCHANT
        side = AffixSide.ENCHANT
    elif "Unique" in kind:
        side = AffixSide.NONE

    return domain, side, affix_name, tier, tags


def _strip_stat_line_suffix(
    line: str,
    fallback_domain: ModDomain,
) -> tuple[str, ModDomain, bool]:
    domain = fallback_domain
    for suffix, dom in SUFFIX_DOMAIN_MARKERS:
        if line.endswith(suffix):
            line = line[: -len(suffix)].rstrip()
            if fallback_domain != ModDomain.FRACTURED or dom == ModDomain.FRACTURED:
                domain = dom
            break

    unscalable = False
    for unscal_marker in (" \u2014 Unscalable Value", " - Unscalable Value"):
        if line.endswith(unscal_marker):
            line = line[: -len(unscal_marker)].rstrip()
            unscalable = True
            break

    return line, domain, unscalable


def _parse_rarity(r_str: str) -> ItemRarity:
    low = r_str.strip().lower()
    if low == "divination card":
        return ItemRarity.CURRENCY
    try:
        return ItemRarity(low)
    except ValueError:
        return ItemRarity.NORMAL


def parse_clipboard_item(raw_text: str, bundle: EE2Bundle | None = None) -> ParsedItem:
    """Parse PoE2 Ctrl+C or Ctrl+Alt+C clipboard text into a Pydantic `ParsedItem`."""
    if bundle is None:
        bundle = get_default_bundle()
    # Local import avoids circular module import at load time
    from poe2brain.trade.stat_matcher import match_mod_line

    sections = _split_sections(raw_text)
    if not sections:
        raise ValueError("Empty clipboard item text")

    if len(sections[0]) >= 3 and sections[0][2].startswith("You cannot use this item"):
        head = sections[0][:2]
        if len(sections) > 1:
            sections[1] = head + sections[1]
            sections.pop(0)

    sec0 = list(sections[0])
    item_class: str | None = None
    if sec0 and sec0[0].startswith("Item Class:"):
        item_class = sec0.pop(0).split(":", 1)[1].strip()

    rarity = ItemRarity.NORMAL
    if sec0 and sec0[0].startswith("Rarity:"):
        rarity = _parse_rarity(sec0.pop(0).split(":", 1)[1])

    if not sec0:
        raise ValueError("Missing item name/base type in nameplate section")

    line_a = _strip_ggg_condition_markup(sec0[0])
    line_b = _strip_ggg_condition_markup(sec0[1]) if len(sec0) > 1 else None

    identified = True
    unidentified_tier: int | None = None
    for s in sections[1:]:
        if len(s) == 1 and (um := UNIDENTIFIED_RE.match(s[0])):
            identified = False
            if um.group("tier"):
                unidentified_tier = int(um.group("tier"))

    name: str | None = None
    base_type: str
    if line_b is not None:
        name = line_a
        base_type = line_b
    else:
        cleaned_single = line_a
        if rarity in (ItemRarity.NORMAL, ItemRarity.MAGIC, ItemRarity.RARE, ItemRarity.UNIQUE):
            for qual_prefix in ("Superior ", "Exceptional "):
                if cleaned_single.startswith(qual_prefix) and (
                    rarity == ItemRarity.NORMAL or not identified
                ):
                    cleaned_single = cleaned_single[len(qual_prefix) :]
                    break

        if rarity == ItemRarity.MAGIC and identified:
            name = line_a
            base_type = bundle.extract_magic_base_type(cleaned_single) or cleaned_single
        elif rarity in (ItemRarity.GEM, ItemRarity.CURRENCY):
            if item_class and item_class.startswith("Uncut "):
                base_type = re.sub(r"\s*\(Level\s+\d+\)$", "", cleaned_single)
                name = cleaned_single
            else:
                name = cleaned_single
                base_type = cleaned_single
        else:
            base_type = cleaned_single
            name = cleaned_single if (rarity == ItemRarity.UNIQUE and identified) else None

    category: str | None = None
    if item_class and item_class in ITEM_CLASS_TO_CATEGORY:
        category = ITEM_CLASS_TO_CATEGORY[item_class][0]
    elif rarity == ItemRarity.GEM and item_class is None:
        category = "Meta Gem"

    waystone_tier: int | None = None
    if wm := WAYSTONE_TIER_NAME_RE.search(base_type):
        waystone_tier = int(wm.group("tier"))

    gem_level: int | None = None
    if name and item_class and item_class.startswith("Uncut "):
        if um_lvl := UNCUT_LEVEL_RE.search(name):
            gem_level = int(um_lvl.group("lvl"))

    item_level: int | None = None
    area_level: int | None = None
    quality = 0
    catalyst_type: str | None = None
    spirit: int | None = None
    armour: int | None = None
    evasion: int | None = None
    energy_shield: int | None = None
    ward: int | None = None
    block_chance: float | None = None
    phys_avg_hit: float | None = None
    ele_avg_hit: float | None = None
    chaos_avg_hit: float | None = None
    attacks_per_second: float | None = None
    crit_chance: float | None = None
    reload_time: float | None = None
    rune_sockets = 0
    socketed_runes: list[str] = []
    gem_sockets: int | None = None
    spirit_reservation: int | None = None
    waystone_revives: int | None = None
    waystone_drop_chance_pct: int | None = None
    requirements = ItemRequirements()
    corrupted = False
    twice_corrupted = False
    sanctified = False
    fractured = False
    mirrored = False
    veiled_desecrated = False
    granted_skills: list[str] = []
    mods: list[ParsedMod] = []
    note: str | None = None

    is_gem_like = rarity == ItemRarity.GEM or (item_class or "").endswith("Gems")
    is_charm_or_flask = category in ("Charm", "Life Flask", "Mana Flask", "Flask")
    seen_item_level = False
    gem_tags: list[str] = []

    def _append_mod(
        raw_line: str,
        eff_domain: ModDomain,
        side: AffixSide | None = None,
        affix_name: str | None = None,
        tier: int | None = None,
        tags: list[str] | None = None,
        unscalable: bool = False,
    ) -> None:
        nonlocal fractured
        clean_text, vals, ranges = _extract_inline_rolls(raw_line)
        if eff_domain == ModDomain.FRACTURED:
            fractured = True
        if clean_text.startswith("Grants Skill:"):
            skill_label = clean_text.split(":", 1)[1].strip()
            if skill_label and skill_label not in granted_skills:
                granted_skills.append(skill_label)
            if eff_domain == ModDomain.EXPLICIT:
                eff_domain = ModDomain.SKILL
                side = AffixSide.SKILL

        is_bonded = clean_text.startswith("Bonded:")
        if eff_domain == ModDomain.RUNE and not is_bonded:
            socketed_runes.append(clean_text)

        match_dom = (
            "sanctum"
            if category == "Relic" and eff_domain == ModDomain.EXPLICIT
            else eff_domain.value
        )
        matched = match_mod_line(clean_text, domain=match_dom, bundle=bundle)
        tids = list(matched.trade_ids) if matched else []

        mods.append(
            ParsedMod(
                raw_text=clean_text,
                domain=eff_domain,
                affix_side=side,
                affix_name=affix_name,
                display_tier=tier,
                values=vals,
                roll_ranges=ranges,
                tags=tags or [],
                unscalable=unscalable,
                bonded=is_bonded,
                trade_stat_ids=tids,
            )
        )

    for sec_idx, sec in enumerate(sections[1:], start=1):
        first = sec[0]

        if len(sec) == 1:
            if first == "Corrupted":
                corrupted = True
                continue
            if first == "Twice Corrupted":
                corrupted = True
                twice_corrupted = True
                continue
            if first == "Sanctified":
                sanctified = True
                continue
            if first == "Mirrored":
                mirrored = True
                continue
            if first == "Fractured Item":
                fractured = True
                continue
            if first == "Foil Unique" or UNIDENTIFIED_RE.match(first):
                continue
            if first.startswith("Note:"):
                note = first.split(":", 1)[1].strip()
                continue
            if first.startswith("Item Level:"):
                item_level = int(re.sub(r"[^\d]", "", first.split(":", 1)[1]))
                seen_item_level = True
                continue
            if first.startswith("Stack Size:"):
                continue
            if first.startswith("Sockets:"):
                sock_spec = first.split(":", 1)[1].strip()
                if is_gem_like:
                    gem_sockets = len([tok for tok in sock_spec.split() if tok])
                else:
                    rune_sockets = sock_spec.count("S")
                continue

        if first.startswith("Requires:"):
            if rm := REQUIRES_RE.match(first):
                requirements = ItemRequirements(
                    level=int(rm.group("level") or 0),
                    strength=int(rm.group("str") or 0),
                    dexterity=int(rm.group("dex") or 0),
                    intelligence=int(rm.group("int") or 0),
                )
            continue

        if is_gem_like:
            for idx_l, line in enumerate(sec):
                if idx_l == 0 and "," in line and ":" not in line and not gem_tags:
                    gem_tags = [t.strip() for t in line.split(",") if t.strip()]
                elif line.startswith("Level:"):
                    if m_num := re.search(r"\d+", line):
                        gem_level = int(m_num.group(0))
                elif line.startswith("Reservation:"):
                    if m_sp := re.search(r"(\d+)\s+Spirit", line):
                        spirit_reservation = int(m_sp.group(1))
                elif qm := QUALITY_HEADER_RE.match(line):
                    quality = int(qm.group("val"))
                    catalyst_type = qm.group("qtype")
            continue

        if any(first.startswith(p) for p in HELP_TEXT_PREFIXES):
            continue

        is_prop_section = False
        for line in sec:
            if qm := QUALITY_HEADER_RE.match(line):
                quality = int(qm.group("val"))
                catalyst_type = qm.group("qtype")
                is_prop_section = True
            elif line.startswith("Item Level:"):
                item_level = int(re.sub(r"[^\d]", "", line.split(":", 1)[1]))
                seen_item_level = True
                is_prop_section = True
            elif line.startswith("Area Level:"):
                area_level = int(re.sub(r"[^\d]", "", line.split(":", 1)[1]))
                is_prop_section = True
            elif line.startswith("Number of Trials:"):
                is_prop_section = True
            elif line in ("Victorious", "Cowardly", "Deadly") and area_level is not None:
                is_prop_section = True
            elif line.startswith("Spirit:"):
                if m_d := re.search(r"\d+", line):
                    spirit = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Charm Slots:"):
                is_prop_section = True
            elif line.startswith("Armour:"):
                if m_d := re.search(r"\d+", line):
                    armour = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Evasion Rating:"):
                if m_d := re.search(r"\d+", line):
                    evasion = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Energy Shield:"):
                if m_d := re.search(r"\d+", line):
                    energy_shield = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Runic Ward:"):
                if m_d := re.search(r"\d+", line):
                    ward = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Block chance:"):
                if m_fl := re.search(r"\d+(?:\.\d+)?", line):
                    block_chance = float(m_fl.group(0))
                is_prop_section = True
            elif line.startswith("Physical Damage:"):
                phys_avg_hit = _parse_damage_avg(line.split(":", 1)[1])
                is_prop_section = True
            elif line.startswith(
                ("Elemental Damage:", "Fire Damage:", "Cold Damage:", "Lightning Damage:")
            ):
                ele_avg_hit = (ele_avg_hit or 0.0) + _parse_damage_avg(line.split(":", 1)[1])
                is_prop_section = True
            elif line.startswith("Chaos Damage:"):
                chaos_avg_hit = _parse_damage_avg(line.split(":", 1)[1])
                is_prop_section = True
            elif line.startswith("Critical Hit Chance:"):
                if m_fl := re.search(r"\d+(?:\.\d+)?", line):
                    crit_chance = float(m_fl.group(0))
                is_prop_section = True
            elif line.startswith("Attacks per Second:"):
                if m_fl := re.search(r"\d+(?:\.\d+)?", line):
                    attacks_per_second = float(m_fl.group(0))
                is_prop_section = True
            elif line.startswith("Reload Time:"):
                if m_fl := re.search(r"\d+(?:\.\d+)?", line):
                    reload_time = float(m_fl.group(0))
                is_prop_section = True
            elif line.startswith(("Waystone Tier:", "Map Tier:")):
                if m_d := re.search(r"\d+", line):
                    waystone_tier = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Revives Available:"):
                if m_d := re.search(r"\d+", line):
                    waystone_revives = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith("Waystone Drop Chance:"):
                if m_d := re.search(r"[+-]?\d+", line):
                    waystone_drop_chance_pct = int(m_d.group(0))
                is_prop_section = True
            elif line.startswith(
                (
                    "Pack Size:",
                    "Magic Monsters:",
                    "Rare Monsters:",
                    "Item Rarity:",
                    "Monster Rarity:",
                    "Monster Effectiveness:",
                )
            ):
                is_prop_section = True
            elif line.startswith(("Radius:", "Limited to:")) and category in (
                "Jewel",
                "Abyss Jewel",
            ):
                is_prop_section = True
            elif is_charm_or_flask and any(pat.match(line) for pat in FLASK_CHARM_SKIP_PATTERNS):
                is_prop_section = True

        if is_prop_section:
            continue

        if rarity == ItemRarity.CURRENCY:
            continue

        if (
            not seen_item_level
            and area_level is None
            and category not in ("Jewel", "Abyss Jewel", "Relic")
            and not any(
                ln.startswith(("{", "Grants Skill:"))
                or any(ln.endswith(sfx) for sfx, _ in SUFFIX_DOMAIN_MARKERS)
                for ln in sec
            )
        ):
            continue

        if (
            rarity == ItemRarity.UNIQUE
            and sec_idx == len(sections) - 1
            and not any(
                ln.startswith("{") or any(ln.endswith(sfx) for sfx, _ in SUFFIX_DOMAIN_MARKERS)
                for ln in sec
            )
        ):
            probe_clean, _, _ = _extract_inline_rolls(sec[0])
            probe_hash = re.sub(r"[+-]?\d+(?:\.\d+)?", "#", probe_clean)
            if (
                probe_hash not in bundle.stat_by_match_str
                and sec[0] not in bundle.stat_by_match_str
                and not sec[0].startswith("Grants Skill:")
            ):
                continue

        if sec[0].startswith("{") and sec[0].endswith("}"):
            cur_domain = ModDomain.EXPLICIT
            cur_side: AffixSide | None = None
            cur_name: str | None = None
            cur_tier: int | None = None
            cur_tags: list[str] = []
            in_reminder = False

            for line in sec:
                if line.startswith("{") and line.endswith("}"):
                    cur_domain, cur_side, cur_name, cur_tier, cur_tags = _parse_mod_header(line)
                    continue
                if line.startswith("("):
                    if not line.endswith(")"):
                        in_reminder = True
                    continue
                if in_reminder:
                    if line.endswith(")"):
                        in_reminder = False
                    continue
                if line in ("Desecrated Prefix", "Desecrated Suffix"):
                    veiled_desecrated = True
                    continue

                stripped, eff_domain, unscalable = _strip_stat_line_suffix(line, cur_domain)
                _append_mod(
                    stripped,
                    eff_domain,
                    side=cur_side,
                    affix_name=cur_name,
                    tier=cur_tier,
                    tags=list(cur_tags),
                    unscalable=unscalable,
                )
        else:
            in_reminder = False
            for line in sec:
                if line.startswith("("):
                    if not line.endswith(")"):
                        in_reminder = True
                    continue
                if in_reminder:
                    if line.endswith(")"):
                        in_reminder = False
                    continue
                if line in ("Desecrated Prefix", "Desecrated Suffix"):
                    veiled_desecrated = True
                    continue

                default_dom = (
                    ModDomain.SKILL if line.startswith("Grants Skill:") else ModDomain.EXPLICIT
                )
                stripped, eff_domain, unscalable = _strip_stat_line_suffix(line, default_dom)
                _append_mod(stripped, eff_domain, unscalable=unscalable)

    physical_dps = (
        round(phys_avg_hit * attacks_per_second, 2)
        if (phys_avg_hit is not None and attacks_per_second)
        else None
    )
    elemental_dps = (
        round(ele_avg_hit * attacks_per_second, 2)
        if (ele_avg_hit is not None and attacks_per_second)
        else None
    )
    total_hit = (phys_avg_hit or 0.0) + (ele_avg_hit or 0.0) + (chaos_avg_hit or 0.0)
    total_dps = (
        round(total_hit * attacks_per_second, 2) if (total_hit > 0 and attacks_per_second) else None
    )

    return ParsedItem(
        raw_text=raw_text,
        item_class=item_class,
        rarity=rarity,
        name=name,
        base_type=base_type,
        item_level=item_level,
        area_level=area_level,
        identified=identified,
        unidentified_tier=unidentified_tier,
        quality=quality,
        catalyst_type=catalyst_type,
        spirit=spirit,
        armour=armour,
        evasion=evasion,
        energy_shield=energy_shield,
        ward=ward,
        block_chance=block_chance,
        physical_dps=physical_dps,
        elemental_dps=elemental_dps,
        total_dps=total_dps,
        attacks_per_second=attacks_per_second,
        critical_hit_chance=crit_chance,
        reload_time=reload_time,
        rune_sockets=rune_sockets,
        socketed_runes=socketed_runes,
        gem_level=gem_level,
        gem_sockets=gem_sockets,
        spirit_reservation=spirit_reservation,
        waystone_tier=waystone_tier,
        waystone_revives=waystone_revives,
        waystone_drop_chance_pct=waystone_drop_chance_pct,
        requirements=requirements,
        corrupted=corrupted,
        twice_corrupted=twice_corrupted,
        sanctified=sanctified,
        fractured=fractured,
        mirrored=mirrored,
        veiled_desecrated=veiled_desecrated,
        granted_skills=granted_skills,
        mods=mods,
        note=note,
    )


def _extract_mod_descriptions(
    mod_array: list[Any] | None,
    default_domain: str,
) -> list[tuple[str, str]]:
    """Return (domain, clean_description) pairs from trade2/fetch mod arrays."""
    out: list[tuple[str, str]] = []
    if not mod_array:
        return out
    for entry in mod_array:
        if isinstance(entry, str):
            for sub in entry.splitlines():
                sub_clean = strip_wiki_markup(sub.strip())
                if sub_clean:
                    out.append((default_domain, sub_clean))
        elif isinstance(entry, dict):
            dom = entry.get("domain") or default_domain
            flags = entry.get("flags") or {}
            if flags.get("desecrated"):
                dom = "desecrated"
            elif flags.get("fractured"):
                dom = "fractured"
            elif flags.get("mutated"):
                dom = "mutated"
            desc = entry.get("description") or ""
            for sub in desc.splitlines():
                sub_clean = strip_wiki_markup(sub.strip())
                if sub_clean:
                    out.append((dom, sub_clean))
    return out


_JEWEL_SOCKET_BASES = frozenset({"Diamond", "Emerald", "Ruby", "Sapphire"})


def extract_socketed_rune_names(
    item_obj: Mapping[str, Any],
    rune_sock_count: int | None = None,
) -> tuple[int, list[str]]:
    """Extract socket count and ordered `Rune: <Name>` entries from `item.socketedItems`.

    Returns `(resolved_rune_sock_count, rune_lines)` where `rune_lines` is non-empty only
    when `item.socketedItems` provides at least one non-jewel augment name (falling back
    to `[]` when `socketedItems` is absent so PoB2's `{rune}` inference still runs).
    """
    sockets = item_obj.get("sockets") or []
    raw_to_rune_pos: dict[int, int] = {}
    if isinstance(sockets, Sequence):
        for raw_idx, sock in enumerate(sockets):
            if isinstance(sock, Mapping) and sock.get("type") in ("rune", None):
                raw_to_rune_pos[raw_idx] = len(raw_to_rune_pos)

    base_count = rune_sock_count if rune_sock_count is not None else len(raw_to_rune_pos)
    socketed_items = item_obj.get("socketedItems")
    if not isinstance(socketed_items, Sequence) or not socketed_items:
        return base_count, []

    indexed_entries: list[tuple[int | None, str]] = []
    max_pos = base_count - 1
    for si in socketed_items:
        if not isinstance(si, Mapping):
            continue
        raw_name = strip_wiki_markup(
            str(si.get("baseType") or si.get("typeLine") or si.get("name") or "").strip()
        )
        if not raw_name or raw_name in _JEWEL_SOCKET_BASES:
            continue
        sock_idx = si.get("socket")
        if isinstance(sock_idx, int) and not isinstance(sock_idx, bool) and sock_idx >= 0:
            pos = raw_to_rune_pos.get(sock_idx, sock_idx)
            max_pos = max(max_pos, pos)
            indexed_entries.append((pos, raw_name))
        else:
            indexed_entries.append((None, raw_name))

    if not indexed_entries:
        return base_count, []

    resolved_count = max(base_count, max_pos + 1, len(indexed_entries))
    slot_names: list[str] = ["None"] * resolved_count
    for pos, name in indexed_entries:
        if pos is not None and 0 <= pos < resolved_count and slot_names[pos] == "None":
            slot_names[pos] = name
    next_free = 0
    for pos, name in indexed_entries:
        if pos is not None and 0 <= pos < resolved_count:
            continue
        while next_free < resolved_count and slot_names[next_free] != "None":
            next_free += 1
        if next_free < resolved_count:
            slot_names[next_free] = name
            next_free += 1

    return resolved_count, slot_names


def trade2_fetch_to_pob2_text(fetch_entry: dict[str, Any]) -> str:
    """
    Convert an official PoE2 `/api/trade2/fetch` result entry (or inner `item` dict)
    into PathOfBuilding-PoE2 `Item.lua`-compatible raw text for `new("Item"):Item(raw)`
    and `GetMiscCalculator()`.
    """
    item = fetch_entry.get("item", fetch_entry)
    rarity_raw = (item.get("rarity") or item.get("frameTypeId") or "Rare").upper()
    if item.get("isRelic") or item.get("frameType") == 9:
        rarity_pob = "RELIC"
    elif rarity_raw in ("NORMAL", "MAGIC", "RARE", "UNIQUE", "RELIC"):
        rarity_pob = rarity_raw
    else:
        rarity_pob = "RARE"

    name = strip_wiki_markup((item.get("name") or "").strip())
    type_line = strip_wiki_markup((item.get("typeLine") or item.get("baseType") or "").strip())
    base_type = strip_wiki_markup((item.get("baseType") or type_line).strip())

    lines: list[str] = [f"Rarity: {rarity_pob}"]
    if rarity_pob in ("RARE", "UNIQUE", "RELIC"):
        lines.append(name if name else (type_line or base_type))
        lines.append(base_type if base_type else type_line)
    else:
        lines.append(type_line or base_type or name)

    # Emit plain Weapon/Armour/Charm/Flask Quality; skip Jewelry Catalyst quality
    # because trade2/fetch descriptions already bake Catalyst scalars into values.
    for prop in item.get("properties") or []:
        prop_name = strip_wiki_markup(prop.get("name") or "")
        prop_type = prop.get("type")
        vals = prop.get("values") or []
        if vals and (prop_type == 6 or prop_name == "Quality") and "(" not in prop_name:
            if m_q := re.search(r"\d+", str(vals[0][0])):
                lines.append(f"Quality: {int(m_q.group(0))}")

    sockets = item.get("sockets") or []
    raw_rune_sock_count = sum(
        1 for s in sockets if isinstance(s, dict) and s.get("type") in ("rune", None)
    )
    rune_sock_count, socketed_rune_names = extract_socketed_rune_names(item, raw_rune_sock_count)
    if rune_sock_count > 0:
        lines.append("Sockets: " + " ".join(["S"] * rune_sock_count))
        for rune_name in socketed_rune_names:
            lines.append(f"Rune: {rune_name}")

    for req in item.get("requirements") or []:
        req_name = strip_wiki_markup(req.get("name") or "")
        vals = req.get("values") or []
        if req_name == "Level" and vals:
            if m_lvl := re.search(r"\d+", str(vals[0][0])):
                lines.append(f"LevelReq: {int(m_lvl.group(0))}")

    if item.get("ilvl") is not None:
        lines.append(f"Item Level: {int(item['ilvl'])}")

    header_mods: list[str] = []
    for _, txt in _extract_mod_descriptions(item.get("enchantMods"), "enchant"):
        header_mods.append(f"{{enchant}}{txt}")
    for _, txt in _extract_mod_descriptions(item.get("runeMods"), "rune"):
        header_mods.append(f"{{rune}}{txt}")
    for _, txt in _extract_mod_descriptions(item.get("implicitMods"), "implicit"):
        header_mods.append(f"{{implicit}}{txt}")

    for gs in item.get("grantedSkills") or []:
        gs_name = strip_wiki_markup(gs.get("name") or "Grants Skill")
        gs_vals = gs.get("values") or []
        if gs_vals and gs_vals[0]:
            gs_val = strip_wiki_markup(str(gs_vals[0][0]))
            skill_line = (
                f"{gs_name}: {gs_val}" if not gs_name.endswith(":") else f"{gs_name} {gs_val}"
            )
            if skill_line not in (
                "Grants Skill: Parry",
                "Grants Skill: Raise Shield",
                "Grants Skill: Spear Throw",
            ):
                header_mods.append(f"{{implicit}}{skill_line}")

    explicit_lines: list[str] = []
    domain_prefix_map = {
        "fractured": "{fractured}",
        "desecrated": "{desecrated}",
        "crafted": "{crafted}",
        "mutated": "{mutated}",
    }
    for arr_key, def_dom in (
        ("fracturedMods", "fractured"),
        ("explicitMods", "explicit"),
        ("desecratedMods", "desecrated"),
        ("craftedMods", "crafted"),
        ("mutatedMods", "mutated"),
    ):
        for dom, txt in _extract_mod_descriptions(item.get(arr_key), def_dom):
            prefix = domain_prefix_map.get(dom, "")
            line_out = f"{prefix}{txt}"
            if line_out not in explicit_lines:
                explicit_lines.append(line_out)

    lines.append(f"Implicits: {len(header_mods)}")
    lines.extend(header_mods)
    lines.extend(explicit_lines)

    if item.get("doubleCorrupted"):
        lines.append("Twice Corrupted")
    elif item.get("corrupted"):
        lines.append("Corrupted")
    if item.get("sanctified"):
        lines.append("Sanctified")
    if item.get("duplicated"):
        lines.append("Mirrored")

    return "\n".join(lines) + "\n"


def parsed_item_to_pob2_text(item: ParsedItem) -> str:
    """Convert a `ParsedItem` back into clean PoB2 `Item.lua` raw text."""
    rarity_pob = item.rarity.value.upper()
    lines: list[str] = [f"Rarity: {rarity_pob}"]
    if rarity_pob in ("RARE", "UNIQUE"):
        lines.append(item.name or item.base_type)
        lines.append(item.base_type)
    else:
        lines.append(item.name or item.base_type)

    if item.quality and not item.catalyst_type:
        lines.append(f"Quality: {item.quality}")
    if item.rune_sockets > 0:
        lines.append("Sockets: " + " ".join(["S"] * item.rune_sockets))
    if item.requirements.level > 0:
        lines.append(f"LevelReq: {item.requirements.level}")
    if item.item_level is not None:
        lines.append(f"Item Level: {item.item_level}")

    header_mods: list[str] = []
    explicit_mods: list[str] = []
    for m in item.mods:
        if m.domain in (ModDomain.ENCHANT, ModDomain.CORRUPTED):
            header_mods.append(f"{{enchant}}{m.raw_text}")
        elif m.domain == ModDomain.RUNE:
            header_mods.append(f"{{rune}}{m.raw_text}")
        elif m.domain in (ModDomain.IMPLICIT, ModDomain.SKILL):
            if m.raw_text not in (
                "Grants Skill: Parry",
                "Grants Skill: Raise Shield",
                "Grants Skill: Spear Throw",
            ):
                header_mods.append(f"{{implicit}}{m.raw_text}")
        elif m.domain == ModDomain.FRACTURED:
            explicit_mods.append(f"{{fractured}}{m.raw_text}")
        elif m.domain == ModDomain.DESECRATED:
            explicit_mods.append(f"{{desecrated}}{m.raw_text}")
        elif m.domain == ModDomain.CRAFTED:
            explicit_mods.append(f"{{crafted}}{m.raw_text}")
        else:
            explicit_mods.append(m.raw_text)

    lines.append(f"Implicits: {len(header_mods)}")
    lines.extend(header_mods)
    lines.extend(explicit_mods)
    if item.twice_corrupted:
        lines.append("Twice Corrupted")
    elif item.corrupted:
        lines.append("Corrupted")
    if item.sanctified:
        lines.append("Sanctified")
    if item.mirrored:
        lines.append("Mirrored")
    return "\n".join(lines) + "\n"
