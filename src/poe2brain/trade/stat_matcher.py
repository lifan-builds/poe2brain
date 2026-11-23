from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from poe2brain.models.builds import SlotStatWeights
from poe2brain.models.items import ItemRarity, ModDomain, ParsedItem
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle
from poe2brain.trade.item_parser import resolve_slot_or_category, resolve_trade_category

PLACEHOLDER_MAP: tuple[tuple[tuple[int, ...], ...], ...] = (
    ((),),
    ((0,), ()),
    ((0, 1), (0,), (1,), ()),
    ((0, 1, 2), (1, 2), (0, 2), (0, 1), (2,), (1,), (0,), ()),
    (
        (0, 1, 2, 3),
        (1, 2, 3),
        (0, 2, 3),
        (0, 1, 3),
        (0, 1, 2),
        (2, 3),
        (1, 3),
        (1, 2),
        (0, 3),
        (0, 2),
        (0, 1),
        (3,),
        (2,),
        (1,),
        (0,),
        (),
    ),
)

NUMBER_TOKEN_RE = re.compile(r"(?<!\d|\))(?P<val>[+-]?\d+(?:\.\d+)?)")
WORD_TOKEN_RE = re.compile(r"[a-z0-9+]+")
STAT_ID_RE = re.compile(
    r"^(?:explicit|implicit|fractured|desecrated|rune|enchant|crafted|sanctum|skill|pseudo)\."
)

BUILTIN_IGNORED_SKILLS = frozenset(
    {
        "Grants Skill: Parry",
        "Grants Skill: Raise Shield",
        "Grants Skill: Spear Throw",
    }
)

NATURAL_QUERY_ALIASES: dict[str, list[tuple[str, str]]] = {
    "ms": [("explicit", "#% increased Movement Speed")],
    "move speed": [("explicit", "#% increased Movement Speed")],
    "movement speed": [("explicit", "#% increased Movement Speed")],
    "spirit": [
        ("explicit", "# to Spirit"),
        ("implicit", "# to Spirit"),
    ],
    "life": [
        ("pseudo", "+#% total Maximum Life"),
        ("explicit", "# to maximum Life"),
    ],
    "max life": [
        ("pseudo", "+#% total Maximum Life"),
        ("explicit", "# to maximum Life"),
    ],
    "maximum life": [
        ("explicit", "# to maximum Life"),
        ("pseudo", "+#% total Maximum Life"),
    ],
    "mana": [
        ("explicit", "# to maximum Mana"),
        ("pseudo", "+#% total Maximum Mana"),
    ],
    "maximum mana": [("explicit", "# to maximum Mana")],
    "es": [("explicit", "# to maximum Energy Shield")],
    "energy shield": [("explicit", "# to maximum Energy Shield")],
    "chaos res": [
        ("pseudo", "+#% total to Chaos Resistance"),
        ("explicit", "+#% to Chaos Resistance"),
    ],
    "chaos resistance": [
        ("explicit", "+#% to Chaos Resistance"),
        ("pseudo", "+#% total to Chaos Resistance"),
    ],
    "fire res": [("explicit", "+#% to Fire Resistance")],
    "fire resistance": [("explicit", "+#% to Fire Resistance")],
    "cold res": [("explicit", "+#% to Cold Resistance")],
    "cold resistance": [("explicit", "+#% to Cold Resistance")],
    "lightning res": [("explicit", "+#% to Lightning Resistance")],
    "lightning resistance": [("explicit", "+#% to Lightning Resistance")],
    "ele res": [("pseudo", "+#% total Elemental Resistance")],
    "elemental resistance": [
        ("pseudo", "+#% total Elemental Resistance"),
        ("explicit", "+#% to all Elemental Resistances"),
    ],
    "total elemental resistance": [("pseudo", "+#% total Elemental Resistance")],
    "all elemental resistances": [("explicit", "+#% to all Elemental Resistances")],
    "all res": [
        ("pseudo", "+#% total Elemental Resistance"),
        ("explicit", "+#% to all Elemental Resistances"),
    ],
    "iir": [("explicit", "#% increased Rarity of Items found")],
    "magic find": [("explicit", "#% increased Rarity of Items found")],
    "rarity": [("explicit", "#% increased Rarity of Items found")],
    "rarity of items found": [("explicit", "#% increased Rarity of Items found")],
    "attack speed": [("explicit", "#% increased Attack Speed")],
    "cast speed": [("explicit", "#% increased Cast Speed")],
    "spell crit": [("explicit", "#% increased Critical Hit Chance for Spells")],
    "crit chance": [("explicit", "+#% to Critical Hit Chance")],
    "crit bonus": [("explicit", "+#% to Critical Damage Bonus")],
    "crit multi": [("explicit", "+#% to Critical Damage Bonus")],
    "empty prefix": [("pseudo", "# Empty Prefix Modifiers")],
    "empty suffix": [("pseudo", "# Empty Suffix Modifiers")],
    "open prefix": [("pseudo", "# Empty Prefix Modifiers")],
    "open suffix": [("pseudo", "# Empty Suffix Modifiers")],
}


@dataclass(slots=True)
class MatchedStat:
    ref: str
    domain: str
    trade_ids: list[str]
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    option_value: int | str | None = None
    is_decimal: bool = False
    is_negated: bool = False
    trade_inverted: bool = False
    better: int = 1
    ambiguous_local_global: bool = False
    matched_template: str = ""

    def to_trade_stat_group(
        self,
        *,
        min_override: float | None = None,
        max_override: float | None = None,
        fuzzy_pct: float = 0.0,
    ) -> dict[str, Any]:
        """Convert to a `trade2` filter or `count min:1` group when local/global collide."""
        f_min = min_override
        f_max = max_override
        if f_min is None and f_max is None and self.value is not None and self.option_value is None:
            raw_v = self.value
            if fuzzy_pct > 0:
                delta = abs(raw_v) * (fuzzy_pct / 100.0)
                lo = raw_v - delta
                hi = raw_v + delta
                f_min = round(lo, 2) if self.is_decimal else math.floor(lo)
                f_max = round(hi, 2) if self.is_decimal else math.ceil(hi)
            else:
                f_min = round(raw_v, 2) if self.is_decimal else math.floor(raw_v)

            if self.better == -1 and min_override is None:
                f_max = (
                    math.ceil(raw_v + (abs(raw_v) * fuzzy_pct / 100.0))
                    if not self.is_decimal
                    else round(raw_v, 2)
                )
                f_min = None
            elif self.better == 1 and max_override is None:
                f_max = None

        if self.trade_inverted:
            a = -f_min if f_min is not None else None
            b = -f_max if f_max is not None else None
            f_min, f_max = b, a

        def _make_single(tid: str) -> dict[str, Any]:
            full_id = tid
            if self.option_value is not None and "|" not in full_id:
                full_id = f"{full_id}|{self.option_value}"
            val_obj: dict[str, Any] = {}
            if f_min is not None:
                val_obj["min"] = f_min
            if f_max is not None:
                val_obj["max"] = f_max
            entry: dict[str, Any] = {"id": full_id}
            if val_obj:
                entry["value"] = val_obj
            return entry

        if len(self.trade_ids) == 1:
            return {"kind": "single", "filter": _make_single(self.trade_ids[0])}
        return {
            "kind": "group",
            "group": {
                "type": "count",
                "value": {"min": 1},
                "filters": [_make_single(tid) for tid in self.trade_ids],
            },
        }


def _avg_roll_values(values: list[float]) -> float:
    if len(values) in (2, 4):
        return sum(values) / len(values)
    return values[0]


def _generate_placeholder_candidates(stat_line: str):
    matches: list[tuple[str, float, bool]] = []
    sentinel = "\x00"

    def _sub(m: re.Match[str]) -> str:
        s = m.group("val")
        matches.append((s, float(s), "." in s))
        return sentinel

    templated = NUMBER_TOKEN_RE.sub(_sub, stat_line)
    if len(matches) < len(PLACEHOLDER_MAP):
        for keep_literal_idxs in PLACEHOLDER_MAP[len(matches)]:
            keep_set = set(keep_literal_idxs)
            slot_iter = iter(range(len(matches)))

            def _restore(_m: re.Match[str], ks=keep_set, it=slot_iter) -> str:
                idx = next(it)
                return matches[idx][0] if idx in ks else "#"

            cand_str = re.sub(sentinel, _restore, templated)
            active_vals = [
                (matches[i][1], matches[i][2]) for i in range(len(matches)) if i not in keep_set
            ]
            yield cand_str, active_vals
            if cand_str.startswith("+#"):
                yield cand_str[1:], active_vals
            if any(
                matches[i][0].startswith("+") and i not in keep_set for i in range(len(matches))
            ):
                slot_iter2 = iter(range(len(matches)))

                def _restore_plus(_m: re.Match[str], ks=keep_set, it2=slot_iter2) -> str:
                    idx = next(it2)
                    if idx in ks:
                        return matches[idx][0]
                    return "+#" if matches[idx][0].startswith("+") else "#"

                plus_str = re.sub(sentinel, _restore_plus, templated)
                if plus_str != cand_str:
                    yield plus_str, active_vals

    yield stat_line, []
    if stat_line.startswith("+#"):
        yield stat_line[1:], []


def _resolve_domain_ids(
    trade_ids_map: dict[str, list[str]], domain: str, collapse_to_explicit: bool
) -> tuple[str, list[str]]:
    if (
        collapse_to_explicit
        and domain in ("fractured", "desecrated", "crafted")
        and "explicit" in trade_ids_map
    ):
        return "explicit", list(trade_ids_map["explicit"])
    if domain in trade_ids_map and trade_ids_map[domain]:
        return domain, list(trade_ids_map[domain])
    if domain == "explicit" and "sanctum" in trade_ids_map:
        return "sanctum", list(trade_ids_map["sanctum"])
    for fallback_dom in (
        "explicit",
        "implicit",
        "rune",
        "enchant",
        "skill",
        "pseudo",
        "fractured",
        "desecrated",
        "crafted",
        "sanctum",
    ):
        if fallback_dom in trade_ids_map and trade_ids_map[fallback_dom]:
            return fallback_dom, list(trade_ids_map[fallback_dom])
    return domain, []


def match_mod_line(
    text: str,
    domain: str | ModDomain = ModDomain.EXPLICIT,
    *,
    bundle: EE2Bundle | None = None,
    collapse_to_explicit: bool = False,
) -> MatchedStat | None:
    """Map a single PoE2 item mod line to official `trade2` stat IDs."""
    if bundle is None:
        bundle = get_default_bundle()
    dom_str = domain.value if isinstance(domain, ModDomain) else domain
    if dom_str == "corrupted":
        dom_str = "enchant"

    clean = text.strip()
    for unscal in (" \u2014 Unscalable Value", " - Unscalable Value"):
        if clean.endswith(unscal):
            clean = clean[: -len(unscal)].rstrip()
    clean = re.sub(
        r"(?P<val>(?<!\d|\))[+-]?\d+(?:\.\d+)?)\([+-]?\d+(?:\.\d+)?(?:-[+-]?\d+(?:\.\d+)?)?\)",
        r"\g<val>",
        clean,
    )
    if clean in BUILTIN_IGNORED_SKILLS:
        return None

    for cand_str, active_vals in _generate_placeholder_candidates(clean):
        ee2_hits = bundle.stat_by_match_str.get(cand_str)
        if ee2_hits:
            for stat_obj, matcher in ee2_hits:
                trade_meta = stat_obj.get("trade") or {}
                ids_by_dom = trade_meta.get("ids") or {}
                if not ids_by_dom:
                    continue
                if not collapse_to_explicit and dom_str in ids_by_dom and ids_by_dom[dom_str]:
                    chosen_dom, chosen_ids = dom_str, list(ids_by_dom[dom_str])
                else:
                    chosen_dom, chosen_ids = _resolve_domain_ids(
                        ids_by_dom, dom_str, collapse_to_explicit
                    )
                if not chosen_ids:
                    continue

                negate = bool(matcher.get("negate"))
                opt_val = (
                    matcher.get("value")
                    if trade_meta.get("option") or ("Allocates" in stat_obj["ref"])
                    else None
                )
                val: float | None = None
                is_dp = bool(stat_obj.get("dp")) or any(dec for _, dec in active_vals)

                if active_vals:
                    nums = [(-v if negate else v) for v, _ in active_vals]
                    val = _avg_roll_values(nums)
                elif matcher.get("value") is not None and opt_val is None:
                    val = float(matcher["value"])

                trade_inv = bool(trade_meta.get("inverted"))
                if negate:
                    trade_inv = not trade_inv

                final_ids = [
                    f"{tid}|{opt_val}" if (opt_val is not None and "|" not in tid) else tid
                    for tid in chosen_ids
                ]
                return MatchedStat(
                    ref=stat_obj["ref"],
                    domain=chosen_dom,
                    trade_ids=final_ids,
                    value=val,
                    option_value=opt_val,
                    is_decimal=is_dp,
                    is_negated=negate,
                    trade_inverted=trade_inv,
                    better=int(stat_obj.get("better", 1)),
                    ambiguous_local_global=len(final_ids) > 1,
                    matched_template=cand_str,
                )

        # Secondary fallback in official GGG `/api/trade2/data/stats`
        search_doms = [dom_str] + [
            d
            for d in (
                "explicit",
                "rune",
                "implicit",
                "enchant",
                "skill",
                "desecrated",
                "fractured",
                "crafted",
                "sanctum",
                "pseudo",
            )
            if d != dom_str
        ]
        for sdom in search_doms:
            dom_dict = bundle.official_stats_by_domain.get(sdom)
            if not dom_dict:
                continue
            off_ids = dom_dict.get(cand_str)
            if off_ids:
                val = _avg_roll_values([v for v, _ in active_vals]) if active_vals else None
                is_dp = any(dec for _, dec in active_vals)
                return MatchedStat(
                    ref=cand_str,
                    domain=sdom,
                    trade_ids=list(off_ids),
                    value=val,
                    is_decimal=is_dp,
                    ambiguous_local_global=len(off_ids) > 1,
                    matched_template=cand_str,
                )

    return None


@dataclass(slots=True)
class NaturalStatCandidate:
    query: str
    ref: str
    domain: str
    trade_ids: list[str]
    score: float
    equipment_filter_key: str | None = None
    ambiguous_local_global: bool = False


def _tokenize_query(text: str) -> list[str]:
    cleaned = text.lower().replace("%", " ").replace("#", " ")
    cleaned = re.sub(r"\blevel of all\b", "level all", cleaned)
    stops = {
        "to",
        "of",
        "the",
        "a",
        "an",
        "in",
        "on",
        "with",
        "for",
        "your",
        "you",
        "have",
        "has",
        "is",
        "are",
        "increased",
        "added",
    }
    tokens = [t.lstrip("+") for t in WORD_TOKEN_RE.findall(cleaned)]
    return [t for t in tokens if t and t not in stops and not t.isdigit()]


def resolve_natural_stat(
    query: str,
    *,
    domain: str = "explicit",
    category: str | None = None,
    limit: int = 5,
    bundle: EE2Bundle | None = None,
) -> list[NaturalStatCandidate]:
    """
    Resolve natural-language stat queries (e.g. 'movement speed', 'spirit',
    '+level of all lightning spell skills') to ranked official `trade2` stat IDs.
    """
    if bundle is None:
        bundle = get_default_bundle()

    q_clean = query.strip()
    if STAT_ID_RE.match(q_clean):
        dom = q_clean.split(".", 1)[0]
        return [
            NaturalStatCandidate(
                query=query,
                ref=q_clean,
                domain=dom,
                trade_ids=[q_clean],
                score=120.0,
            )
        ]

    q_norm = re.sub(r"\s+", " ", q_clean.lower()).lstrip("+")
    results: list[NaturalStatCandidate] = []
    seen_key: set[tuple[str, tuple[str, ...]]] = set()

    trade_cat = resolve_slot_or_category(category)
    is_spirit_base = trade_cat == "weapon.sceptre"
    if q_norm == "spirit" and is_spirit_base:
        results.append(
            NaturalStatCandidate(
                query=query,
                ref="Equipment Spirit Property",
                domain="property",
                trade_ids=["equipment_filters.filters.spirit"],
                score=110.0,
                equipment_filter_key="spirit",
            )
        )
    elif q_norm == "spirit" and trade_cat is not None:
        sp_obj = bundle.stat_by_ref.get("# to Spirit")
        sp_ids_map = (sp_obj.get("trade") or {}).get("ids") or {} if sp_obj else {}
        combined_sp_ids: list[str] = []
        for dom_k in ("explicit", "implicit", "fractured", "desecrated"):
            for tid in sp_ids_map.get(dom_k) or []:
                if tid not in combined_sp_ids:
                    combined_sp_ids.append(tid)
        if combined_sp_ids:
            k = ("explicit", tuple(combined_sp_ids))
            seen_key.add(k)
            results.append(
                NaturalStatCandidate(
                    query=query,
                    ref="# to Spirit",
                    domain="explicit",
                    trade_ids=combined_sp_ids,
                    score=109.5,
                    ambiguous_local_global=len(combined_sp_ids) > 1,
                )
            )

    alias_targets = NATURAL_QUERY_ALIASES.get(q_norm, [])
    for idx_a, (pref_dom, ref_str) in enumerate(alias_targets):
        st_obj = bundle.stat_by_ref.get(ref_str)
        if not st_obj:
            continue
        ids_map = (st_obj.get("trade") or {}).get("ids") or {}
        use_dom = pref_dom if pref_dom in ids_map else domain
        tids = ids_map.get(use_dom) or ids_map.get("explicit") or ids_map.get("pseudo") or []
        if tids:
            k = (use_dom, tuple(tids))
            if k not in seen_key:
                seen_key.add(k)
                results.append(
                    NaturalStatCandidate(
                        query=query,
                        ref=st_obj["ref"],
                        domain=use_dom,
                        trade_ids=list(tids),
                        score=round(108.0 - idx_a * 0.5, 2),
                        ambiguous_local_global=len(tids) > 1,
                    )
                )

    direct = match_mod_line(query, domain=domain, bundle=bundle)
    if direct:
        k = (direct.domain, tuple(direct.trade_ids))
        if k not in seen_key:
            seen_key.add(k)
            results.insert(
                0,
                NaturalStatCandidate(
                    query=query,
                    ref=direct.ref,
                    domain=direct.domain,
                    trade_ids=list(direct.trade_ids),
                    score=109.0,
                    ambiguous_local_global=direct.ambiguous_local_global,
                ),
            )

    q_tokens = _tokenize_query(query)
    if not q_tokens:
        return results[:limit]
    q_set = set(q_tokens)

    scored: list[NaturalStatCandidate] = []
    want_pseudo = "total" in q_set or "empty" in q_set or "open" in q_set

    for st_obj in bundle.stats:
        ids_map = (st_obj.get("trade") or {}).get("ids") or {}
        if not ids_map:
            continue
        if want_pseudo and "pseudo" in ids_map:
            cand_dom = "pseudo"
        elif domain in ids_map:
            cand_dom = domain
        elif "explicit" in ids_map:
            cand_dom = "explicit"
        elif "pseudo" in ids_map:
            cand_dom = "pseudo"
        elif "skill" in ids_map:
            cand_dom = "skill"
        else:
            cand_dom = next(iter(ids_map))

        tids = ids_map.get(cand_dom) or []
        if not tids:
            continue

        ref_text = st_obj["ref"]
        cand_tokens = _tokenize_query(ref_text)
        if not cand_tokens:
            continue
        cand_set = set(cand_tokens)
        overlap = len(q_set & cand_set)
        if overlap == 0:
            continue

        coverage = overlap / len(q_set)
        precision = overlap / len(cand_set)
        if coverage < 0.6 and overlap < 2:
            continue

        score = coverage * 60.0 + precision * 30.0
        if q_norm in ref_text.lower():
            score += 8.0
        if cand_dom == domain or (want_pseudo and cand_dom == "pseudo"):
            score += 4.0

        if (st_obj.get("trade") or {}).get("option") or ref_text == "Allocates #":
            for m in st_obj.get("matchers", []):
                m_str = m.get("string", "")
                if q_norm == m_str.lower():
                    opt_v = m.get("value")
                    opt_ids = [f"{tid}|{opt_v}" if opt_v is not None else tid for tid in tids]
                    k = (cand_dom, tuple(opt_ids))
                    if k not in seen_key:
                        seen_key.add(k)
                        scored.append(
                            NaturalStatCandidate(
                                query=query,
                                ref=m_str,
                                domain=cand_dom,
                                trade_ids=opt_ids,
                                score=98.0,
                                ambiguous_local_global=len(opt_ids) > 1,
                            )
                        )
            continue

        k = (cand_dom, tuple(tids))
        if k not in seen_key:
            seen_key.add(k)
            scored.append(
                NaturalStatCandidate(
                    query=query,
                    ref=ref_text,
                    domain=cand_dom,
                    trade_ids=list(tids),
                    score=round(score, 2),
                    ambiguous_local_global=len(tids) > 1,
                )
            )

    if len(scored) < limit:
        for o_dom, o_id, o_text in bundle.official_stats_flat:
            if domain != "any" and o_dom not in (
                domain,
                "explicit",
                "pseudo",
                "rune",
                "skill",
                "enchant",
            ):
                continue
            cand_tokens = _tokenize_query(o_text)
            if not cand_tokens:
                continue
            cand_set = set(cand_tokens)
            overlap = len(q_set & cand_set)
            if overlap == len(q_set):
                precision = overlap / len(cand_set)
                score = 60.0 + precision * 28.0 + (3.0 if o_dom == domain else 0.0)
                all_ids = bundle.official_stats_by_domain[o_dom].get(o_text, [o_id])
                k = (o_dom, tuple(all_ids))
                if k not in seen_key:
                    seen_key.add(k)
                    scored.append(
                        NaturalStatCandidate(
                            query=query,
                            ref=o_text,
                            domain=o_dom,
                            trade_ids=list(all_ids),
                            score=round(score, 2),
                            ambiguous_local_global=len(all_ids) > 1,
                        )
                    )

    combined = results + scored
    combined.sort(key=lambda c: c.score, reverse=True)
    return combined[:limit]


def build_item_trade_query(
    item: ParsedItem,
    *,
    status: str = "available",
    roll_tolerance: float = 0.9,
    collapse_to_explicit: bool = True,
    max_price_exalted: float | None = None,
    bundle: EE2Bundle | None = None,
) -> dict[str, Any]:
    """
    Build a verified official `POST /api/trade2/search/poe2/<League>` JSON payload
    for price-checking a `ParsedItem`. `roll_tolerance=0.9` searches >=90% of each roll.
    """
    if bundle is None:
        bundle = get_default_bundle()

    fuzzy_pct = max(0.0, (1.0 - roll_tolerance) * 100.0)
    trade_category = resolve_trade_category(item, bundle)

    and_filters: list[dict[str, Any]] = []
    extra_stat_groups: list[dict[str, Any]] = []
    filters_block: dict[str, Any] = {}

    def _set_filter(group: str, key: str, val: Any) -> None:
        filters_block.setdefault(group, {"filters": {}})["filters"][key] = val

    query: dict[str, Any] = {
        "status": {"option": status},
        "stats": [{"type": "and", "filters": and_filters}],
    }

    if item.rarity == ItemRarity.UNIQUE and item.identified and item.name:
        query["name"] = item.name
        if item.base_type and item.base_type != item.name:
            query["type"] = item.base_type
        is_foil = "Foil Unique" in item.raw_text
        _set_filter("type_filters", "rarity", {"option": "uniquefoil" if is_foil else "unique"})
    elif item.rarity == ItemRarity.GEM:
        if item.name:
            query["type"] = item.name
        if trade_category:
            _set_filter("type_filters", "category", {"option": trade_category})
        if item.gem_level:
            _set_filter("misc_filters", "gem_level", {"min": item.gem_level})
        if item.quality >= 10:
            _set_filter("type_filters", "quality", {"min": item.quality})
        if item.gem_sockets and item.gem_sockets >= 4:
            _set_filter("misc_filters", "gem_sockets", {"min": item.gem_sockets})
    else:
        if trade_category:
            _set_filter("type_filters", "category", {"option": trade_category})
        elif item.base_type:
            query["type"] = item.base_type
        if item.rarity in (ItemRarity.NORMAL, ItemRarity.MAGIC, ItemRarity.RARE):
            _set_filter("type_filters", "rarity", {"option": "nonunique"})
        if item.waystone_tier:
            _set_filter("map_filters", "map_tier", {"min": item.waystone_tier})
        if item.waystone_revives is not None:
            _set_filter("map_filters", "map_revives", {"min": item.waystone_revives})
        if item.area_level:
            _set_filter("misc_filters", "area_level", {"min": item.area_level})

    if not item.identified:
        _set_filter("misc_filters", "identified", {"option": "false"})
        if item.unidentified_tier:
            _set_filter("misc_filters", "unidentified_tier", {"min": item.unidentified_tier})
    if not item.corrupted and item.rarity in (ItemRarity.NORMAL, ItemRarity.MAGIC, ItemRarity.RARE):
        _set_filter("misc_filters", "corrupted", {"option": "false"})
    if not item.mirrored and item.rarity in (ItemRarity.NORMAL, ItemRarity.MAGIC, ItemRarity.RARE):
        _set_filter("misc_filters", "mirrored", {"option": "false"})
    if item.sanctified:
        _set_filter("misc_filters", "sanctified", {"option": "true"})
    if max_price_exalted is not None:
        _set_filter("trade_filters", "price", {"max": max_price_exalted})

    for mod in item.mods:
        if mod.domain == ModDomain.RUNE and item.rarity != ItemRarity.UNIQUE:
            continue
        # On Uniques, skip fixed non-variable mods unless roll_ranges indicate variability
        if (
            item.rarity == ItemRarity.UNIQUE
            and mod.roll_ranges
            and all(r.min == r.max for r in mod.roll_ranges)
            and mod.domain != ModDomain.ENCHANT
        ):
            continue

        match_dom = (
            "sanctum"
            if (trade_category == "sanctum.relic" and mod.domain == ModDomain.EXPLICIT)
            else mod.domain.value
        )
        matched = match_mod_line(
            mod.raw_text,
            domain=match_dom,
            bundle=bundle,
            collapse_to_explicit=collapse_to_explicit and item.rarity != ItemRarity.UNIQUE,
        )
        if not matched:
            continue
        built = matched.to_trade_stat_group(fuzzy_pct=fuzzy_pct)
        if built["kind"] == "single":
            and_filters.append(built["filter"])
        else:
            extra_stat_groups.append(built["group"])

    cap_unauth_stat_groups(and_filters, extra_stat_groups)
    query["stats"].extend(extra_stat_groups)
    if filters_block:
        query["filters"] = filters_block

    return {"query": query, "sort": {"price": "asc"}}


def cap_unauth_stat_groups(
    and_filters: list[dict[str, Any]],
    extra_groups: list[dict[str, Any]],
    *,
    max_non_empty_groups: int = 3,
    reserved_groups: int = 0,
) -> None:
    """Collapse overflow `count` stat groups into `and_filters` so total non-empty
    `query.stats` groups never exceed GGG's unauthenticated <=3 stat-group ceiling.

    Preserves index 0 (fracture-capable opener) and index -1 (Desecrated finisher) first.
    """
    seen_and_ids = {
        str(f.get("id")) for f in and_filters if isinstance(f, Mapping) and f.get("id")
    }
    while (1 if and_filters else 0) + len(extra_groups) + reserved_groups > max_non_empty_groups:
        if not extra_groups:
            break
        pop_idx = 1 if len(extra_groups) >= 3 else 0
        grp = extra_groups.pop(pop_idx)
        grp_filters = grp.get("filters") or []
        if not grp_filters:
            continue
        chosen = next(
            (
                f
                for f in grp_filters
                if isinstance(f, Mapping) and str(f.get("id", "")).startswith("explicit.")
            ),
            grp_filters[0],
        )
        cid = str(chosen.get("id") or "") if isinstance(chosen, Mapping) else ""
        if cid and cid not in seen_and_ids:
            and_filters.append(dict(chosen))
            seen_and_ids.add(cid)
        elif not cid and isinstance(chosen, Mapping):
            and_filters.append(dict(chosen))


def _normalize_stat_constraints(
    raw: Sequence[Any] | Mapping[str, Any] | None,
) -> list[tuple[str, float | None, float | None]]:
    if not raw:
        return []
    out: list[tuple[str, float | None, float | None]] = []
    if isinstance(raw, Mapping):
        for k, v in raw.items():
            if isinstance(v, tuple) and len(v) == 2:
                out.append((str(k), v[0], v[1]))
            elif isinstance(v, Mapping):
                out.append((str(k), v.get("min"), v.get("max")))
            elif isinstance(v, (int, float)):
                out.append((str(k), float(v), None))
            else:
                out.append((str(k), None, None))
        return out

    for item in raw:
        if isinstance(item, tuple):
            if len(item) == 3:
                out.append((str(item[0]), item[1], item[2]))
            elif len(item) == 2:
                out.append((str(item[0]), item[1], None))
        elif isinstance(item, Mapping):
            q = str(item.get("stat") or item.get("id") or item.get("query") or "")
            out.append((q, item.get("min"), item.get("max")))
        elif isinstance(item, str):
            out.append((item, None, None))
    return out


def build_natural_trade_query(
    *,
    category: str | None = None,
    base_type: str | None = None,
    unique_name: str | None = None,
    stat_constraints: Sequence[Any] | Mapping[str, Any] | None = None,
    weighted_stats: SlotStatWeights
    | Mapping[str, float]
    | Sequence[tuple[str, float]]
    | None = None,
    min_weight: float | None = None,
    min_spirit_prop: int | None = None,
    min_rune_sockets: int | None = None,
    min_pdps: float | None = None,
    min_dps: float | None = None,
    min_es: int | None = None,
    min_ev: int | None = None,
    min_ar: int | None = None,
    max_price_exalted: float | None = None,
    corrupted: bool | None = False,
    status: str = "available",
    bundle: EE2Bundle | None = None,
) -> dict[str, Any]:
    """
    Build a ready-to-POST `/api/trade2/search/poe2/<League>` payload from natural-language
    constraints or PoB2 `SlotStatWeights`. Emits `count min:1` when local/global IDs collide.
    """
    if bundle is None:
        bundle = get_default_bundle()

    and_filters: list[dict[str, Any]] = []
    extra_groups: list[dict[str, Any]] = []
    weight_filters: list[dict[str, Any]] = []
    filters_block: dict[str, Any] = {}

    def _set_filter(group: str, key: str, val: Any) -> None:
        filters_block.setdefault(group, {"filters": {}})["filters"][key] = val

    trade_cat = resolve_slot_or_category(category)
    if trade_cat:
        _set_filter("type_filters", "category", {"option": trade_cat})
    if not unique_name and trade_cat and not trade_cat.startswith(("gem", "currency", "card")):
        _set_filter("type_filters", "rarity", {"option": "nonunique"})
    if unique_name:
        _set_filter("type_filters", "rarity", {"option": "unique"})

    if min_spirit_prop is not None and min_spirit_prop > 0:
        if trade_cat in (None, "weapon.sceptre"):
            _set_filter("equipment_filters", "spirit", {"min": min_spirit_prop})
        else:
            sp_cands = resolve_natural_stat("spirit", category=trade_cat, limit=1, bundle=bundle)
            if sp_cands and sp_cands[0].trade_ids:
                extra_groups.append(
                    {
                        "type": "count",
                        "value": {"min": 1},
                        "filters": [
                            {"id": tid, "value": {"min": min_spirit_prop}}
                            for tid in sp_cands[0].trade_ids
                        ],
                    }
                )
            else:
                _set_filter("equipment_filters", "spirit", {"min": min_spirit_prop})
    elif min_spirit_prop == 0 and trade_cat in (None, "weapon.sceptre"):
        _set_filter("equipment_filters", "spirit", {"min": 0})
    if min_rune_sockets is not None:
        _set_filter("equipment_filters", "rune_sockets", {"min": min_rune_sockets})
    if min_pdps is not None:
        _set_filter("equipment_filters", "pdps", {"min": min_pdps})
    if min_dps is not None:
        _set_filter("equipment_filters", "dps", {"min": min_dps})
    if min_es is not None:
        _set_filter("equipment_filters", "es", {"min": min_es})
    if min_ev is not None:
        _set_filter("equipment_filters", "ev", {"min": min_ev})
    if min_ar is not None:
        _set_filter("equipment_filters", "ar", {"min": min_ar})
    if corrupted is not None:
        _set_filter("misc_filters", "corrupted", {"option": "true" if corrupted else "false"})
    if max_price_exalted is not None:
        _set_filter("trade_filters", "price", {"max": max_price_exalted})

    for stat_q, min_v, max_v in _normalize_stat_constraints(stat_constraints):
        cands = resolve_natural_stat(stat_q, category=trade_cat, limit=1, bundle=bundle)
        if not cands:
            raise ValueError(f"Unrecognized PoE2 stat query: {stat_q!r}")
        best = cands[0]
        if best.equipment_filter_key:
            val_range: dict[str, Any] = {}
            if min_v is not None:
                val_range["min"] = min_v
            if max_v is not None:
                val_range["max"] = max_v
            _set_filter("equipment_filters", best.equipment_filter_key, val_range)
            continue

        ms = MatchedStat(
            ref=best.ref,
            domain=best.domain,
            trade_ids=best.trade_ids,
            ambiguous_local_global=best.ambiguous_local_global,
        )
        built = ms.to_trade_stat_group(min_override=min_v, max_override=max_v)
        if built["kind"] == "single":
            and_filters.append(built["filter"])
        else:
            extra_groups.append(built["group"])

    if weighted_stats is not None:
        raw_weights: list[tuple[str, float]] = []
        if isinstance(weighted_stats, SlotStatWeights):
            for w_ent in weighted_stats.weights:
                raw_weights.append((w_ent.stat_id or w_ent.stat_label, w_ent.weight))
        elif isinstance(weighted_stats, Mapping):
            raw_weights = [(str(k), float(v)) for k, v in weighted_stats.items()]
        else:
            raw_weights = [(str(k), float(v)) for k, v in weighted_stats]

        seen_w_ids: set[str] = set()
        for w_key, w_val in sorted(raw_weights, key=lambda x: abs(x[1]), reverse=True):
            if w_val == 0:
                continue
            cands = resolve_natural_stat(w_key, category=trade_cat, limit=1, bundle=bundle)
            if not cands or cands[0].equipment_filter_key:
                continue
            for tid in cands[0].trade_ids:
                if tid not in seen_w_ids and len(weight_filters) < 35:
                    seen_w_ids.add(tid)
                    weight_filters.append({"id": tid, "value": {"weight": round(w_val, 4)}})

    stats_list: list[dict[str, Any]] = []
    if weight_filters:
        w_group: dict[str, Any] = {"type": "weight", "filters": weight_filters}
        if min_weight is not None:
            w_group["value"] = {"min": min_weight}
        stats_list.append(w_group)
    cap_unauth_stat_groups(
        and_filters,
        extra_groups,
        reserved_groups=(1 if weight_filters else 0),
    )
    stats_list.append({"type": "and", "filters": and_filters})
    stats_list.extend(extra_groups)

    query: dict[str, Any] = {
        "status": {"option": status},
        "stats": stats_list,
    }
    if unique_name:
        query["name"] = unique_name
    if base_type:
        query["type"] = base_type
    if filters_block:
        query["filters"] = filters_block

    sort_spec = {"statgroup.0": "desc"} if weight_filters else {"price": "asc"}
    return {"query": query, "sort": sort_spec}
