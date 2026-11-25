from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from poe2brain.config import get_gamedata_db_path
from poe2brain.knowledge.db import GamedataDB
from poe2brain.models.builds import BuildStats, DewateredAudit
from poe2brain.models.common import Poe2Model
from poe2brain.models.items import ModDomain, ParsedItem
from poe2brain.trade.item_parser import parse_clipboard_item

POE2_STASH_REGEX_MAX_CHARS: int = 50

# Official PoE2 0.5.5 Waystone revive scaling (`min(5, max(0, 6 - mod_count))`).
WAYSTONE_REVIVES_BY_MOD_COUNT: dict[int, int] = {
    0: 5,
    1: 5,
    2: 4,
    3: 3,
    4: 2,
    5: 1,
    6: 0,
}

_INTERNAL_STAT_LINE_RE = re.compile(r"^[a-z0-9 +%_.-]+\[-?\d+\]$")
_ROLL_RANGE_RE = re.compile(r"\((?P<lo>[+-]?\d+(?:\.\d+)?)\s*-\s*(?P<hi>[+-]?\d+(?:\.\d+)?)\)")
_ALPHA_SPACE_SPAN_RE = re.compile(r"[a-z]+(?:\s+[a-z]+)*")

_STANDARD_TOOLTIP_BOILERPLATE: tuple[str, ...] = (
    "Item Class: Waystones",
    "Item Class: Tablet",
    "Rarity: Rare",
    "Rarity: Magic",
    "Rarity: Normal",
    "Waystone (Tier 15)",
    "Waystone (Tier 16)",
    "Waystone Tier: 15",
    "Revives Available: 2 (augmented)",
    "Pack Size: +24% (augmented)",
    "Magic Monsters: +35% (augmented)",
    "Rare Monsters: +70% (augmented)",
    "Waystone Drop Chance: +185% (augmented)",
    "Item Rarity: +42% (augmented)",
    "Monster Rarity: +32% (augmented)",
    "Monster Effectiveness: +45% (augmented)",
    "Can be used in a Map Device, allowing you to enter a Map. Waystones can only be used once.",
    "Can be used in a personal Map Device to add modifiers to a Map.",
)


HazardSeverity = Literal["brick", "dangerous", "annoying"]


class WaystoneReviveRule(Poe2Model):
    mod_count: int
    revives_available: int
    rarity_hint: str
    warning: str | None = None


class DetectedWaystoneHazard(Poe2Model):
    hazard_id: str
    severity: HazardSeverity = "brick"
    reason: str
    mod_types: list[str] = Field(default_factory=list)
    sample_stat_texts: list[str] = Field(default_factory=list)
    regex_token: str = ""


class CompiledStashRegex(Poe2Model):
    purpose: str
    regex: str
    char_length: int
    max_chars: int = POE2_STASH_REGEX_MAX_CHARS
    within_client_limit: bool
    tokens: list[str] = Field(default_factory=list)
    matched_target_count: int = 0
    total_targets: int = 0
    false_negatives: int = 0
    false_positives: int = 0
    usage_note: str = ""


class WaystoneItemCheckResult(Poe2Model):
    waystone_name: str
    waystone_tier: int | None = None
    mod_count: int
    revives_available: int
    waystone_drop_chance_pct: int | None = None
    is_brick: bool
    brick_mods_hit: list[str] = Field(default_factory=list)
    dangerous_mods_hit: list[str] = Field(default_factory=list)
    matches_brick_regex: bool
    matches_juicing_regex: bool
    revive_warning: str | None = None


class WaystoneRegexPlan(Poe2Model):
    detected_archetypes: list[str] = Field(default_factory=list)
    hazards: list[DetectedWaystoneHazard] = Field(default_factory=list)
    brick_regex: CompiledStashRegex
    safe_runnable_regex: CompiledStashRegex
    juicing_regex: CompiledStashRegex
    tablet_regex: CompiledStashRegex
    revive_schedule: list[WaystoneReviveRule] = Field(default_factory=list)
    atlas_warnings: list[str] = Field(default_factory=list)
    item_check: WaystoneItemCheckResult | None = None


@dataclass(frozen=True, slots=True)
class _ModFamilyRecord:
    domain_kind: Literal["waystone", "tablet"]
    mod_type: str
    source_pool: str
    affix_side: str
    affix_names: tuple[str, ...]
    clean_templates: tuple[str, ...]
    rendered_samples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HazardDefinition:
    hazard_id: str
    mod_types: tuple[str, ...]
    synthetic_samples: tuple[str, ...]
    default_severity: HazardSeverity
    reason: str


_KNOWN_HAZARDS: dict[str, _HazardDefinition] = {
    "less_recovery_rate": _HazardDefinition(
        hazard_id="less_recovery_rate",
        mod_types=("MapPlayerRecoveryRate",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Slashes Life, Energy Shield, and Mana recovery/regen/leech rate by up to 40%.",
    ),
    "cannot_leech": _HazardDefinition(
        hazard_id="cannot_leech",
        mod_types=(),
        synthetic_samples=(
            "Monsters cannot be Leeched from",
            "Cannot Leech Life or Mana from Monsters",
        ),
        default_severity="brick",
        reason="Disables Life/Mana/ES Leech sustain completely.",
    ),
    "mana_siphon_ground": _HazardDefinition(
        hazard_id="mana_siphon_ground",
        mod_types=("MapSpreadManaSiphonGround", "MapAbyssModManaSiphonGround"),
        synthetic_samples=("Area has patches of Mana Siphoning Ground",),
        default_severity="brick",
        reason="Drains 1,200 Mana/min + Lightning DoT; drains Archmage/MoM EHP pool to zero.",
    ),
    "elemental_penetration": _HazardDefinition(
        hazard_id="elemental_penetration",
        mod_types=("MapMonstersElementalPenetration",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Monster Damage Penetrates 6–14% Elemental Resistances, bypassing overcap.",
    ),
    "minus_max_resists": _HazardDefinition(
        hazard_id="minus_max_resists",
        mod_types=("MapPlayerMaximumResists",),
        synthetic_samples=(),
        default_severity="brick",
        reason="(-8 to -3)% maximum Player Resistances (~24–32% more elemental damage taken).",
    ),
    "elemental_weakness": _HazardDefinition(
        hazard_id="elemental_weakness",
        mod_types=("MapPlayerElementalWeakness",),
        synthetic_samples=(),
        default_severity="brick",
        reason=(
            "Curses player with Elemental Weakness, uncapping elemental resists unless overcapped."
        ),
    ),
    "monster_crit": _HazardDefinition(
        hazard_id="monster_crit",
        mod_types=("MapMonsterCritIncrease",),
        synthetic_samples=(),
        default_severity="brick",
        reason=(
            "Monsters gain up to +240% Crit Chance and +25% Crit Damage Bonus (one-shot spikes)."
        ),
    ),
    "monster_stun_buildup": _HazardDefinition(
        hazard_id="monster_stun_buildup",
        mod_types=("MapMonsterStunBuildup",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Monsters gain +50–100% Stun Buildup; deadly chain-stuns on CI (Life=1) & casters.",
    ),
    "abyss_no_damage_cycle": _HazardDefinition(
        hazard_id="abyss_no_damage_cycle",
        mod_types=("MapAbyssDamageCycle",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Players and Minions deal NO damage for 3 out of every 10 seconds.",
    ),
    "crit_damage_reduction": _HazardDefinition(
        hazard_id="crit_damage_reduction",
        mod_types=("MapMonstersBaseSelfCriticalMultiplier",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Monsters take up to 30% reduced Extra Damage from Critical Hits.",
    ),
    "enfeeble_curse": _HazardDefinition(
        hazard_id="enfeeble_curse",
        mod_types=("MapPlayerEnfeeble",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Periodic Enfeeble curse reduces Hit Damage and Critical Hit Multiplier.",
    ),
    "extra_chaos_damage": _HazardDefinition(
        hazard_id="extra_chaos_damage",
        mod_types=("MapMonsterDamageAsChaos",),
        synthetic_samples=(),
        default_severity="brick",
        reason=("Monsters deal Extra Chaos Damage (Chaos deals 2x damage to non-CI ES in 0.5.5)."),
    ),
    "bleed_on_hit": _HazardDefinition(
        hazard_id="bleed_on_hit",
        mod_types=("MapMonsterBleeding",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Monster Bleeding bypasses Energy Shield directly to Life in PoE2 0.5.5.",
    ),
    "poison_on_hit": _HazardDefinition(
        hazard_id="poison_on_hit",
        mod_types=("MapMonsterPoisoning",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Monster Poison bypasses Energy Shield directly to Life on non-CI ES builds.",
    ),
    "ignited_ground": _HazardDefinition(
        hazard_id="ignited_ground",
        mod_types=("MapSpreadBurningGround",),
        synthetic_samples=(),
        default_severity="annoying",
        reason="Ignited Ground continuously interrupts Energy Shield Recharge.",
    ),
    "monster_accuracy": _HazardDefinition(
        hazard_id="monster_accuracy",
        mod_types=("MapMonsterAccuracy",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Monsters have +10–40% Accuracy Rating, cutting Evasion mitigation.",
    ),
    "less_cooldown_recovery": _HazardDefinition(
        hazard_id="less_cooldown_recovery",
        mod_types=("MapPlayerCooldownRecovery",),
        synthetic_samples=(),
        default_severity="brick",
        reason="Players have 15–30% less Cooldown Recovery Rate.",
    ),
    "temporal_chains": _HazardDefinition(
        hazard_id="temporal_chains",
        mod_types=("MapPlayerTemporalChains",),
        synthetic_samples=(),
        default_severity="annoying",
        reason="Periodic Temporal Chains slows Action Speed.",
    ),
    "reduced_flask_charges": _HazardDefinition(
        hazard_id="reduced_flask_charges",
        mod_types=("MapPlayerFlaskChargeGain",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Players gain 20–35% reduced Flask Charges.",
    ),
    "ailment_stun_threshold": _HazardDefinition(
        hazard_id="ailment_stun_threshold",
        mod_types=("MapMonsterStunAilmentThreshold",),
        synthetic_samples=(),
        default_severity="dangerous",
        reason="Monsters gain up to +79% Ailment and Stun Threshold.",
    ),
}


def waystone_revives_for_mod_count(mod_count: int) -> int:
    """Return PoE2 0.5.5 revives available for a Waystone with `mod_count` explicit mods."""
    if mod_count <= 0:
        return 5
    return min(5, max(0, 6 - mod_count))


def build_revive_schedule() -> list[WaystoneReviveRule]:
    """Return the full 0..6 modifier -> revive schedule with Patch 0.5.5 portal warnings."""
    rules: list[WaystoneReviveRule] = []
    labels = {
        0: ("Normal (0 mods)", None),
        1: ("Magic (1 mod)", None),
        2: ("Magic (2 mods)", None),
        3: ("Rare (3 mods)", None),
        4: ("Rare (4 mods)", "2 revives left — sweet spot for safe XP / progression mapping."),
        5: ("Rare (5 mods)", "Only 1 revive — avoid rippy monster crit/pen combos."),
        6: (
            "Rare / Corrupted (6+ mods)",
            "0 REVIVES (1-portal mode): a single death bricks the map and forfeits Tablet uses!",
        ),
    }
    for mc in range(7):
        lbl, warn = labels[mc]
        rules.append(
            WaystoneReviveRule(
                mod_count=mc,
                revives_available=waystone_revives_for_mod_count(mc),
                rarity_hint=lbl,
                warning=warn,
            )
        )
    return rules


def _sanitize_stat_text(stat_text: str) -> str:
    kept: list[str] = []
    for raw_line in stat_text.splitlines():
        ln = raw_line.strip()
        if not ln or _INTERNAL_STAT_LINE_RE.match(ln):
            continue
        kept.append(ln)
    return "\n".join(kept)


def _render_roll_endpoints(template_text: str) -> tuple[str, str]:
    def _sub_lo(m: re.Match[str]) -> str:
        val = float(m.group("lo"))
        return str(int(val)) if val.is_integer() else f"{val:g}"

    def _sub_hi(m: re.Match[str]) -> str:
        val = float(m.group("hi"))
        return str(int(val)) if val.is_integer() else f"{val:g}"

    return _ROLL_RANGE_RE.sub(_sub_lo, template_text), _ROLL_RANGE_RE.sub(_sub_hi, template_text)


def _strip_waystone_bonus_lines(text: str) -> str:
    """Keep only the primary hazard/mechanic lines of a Waystone mod, stripping generic
    `15% more Waystones found in Area` / `9% more Pack size` lines shared by every Waystone mod.
    """
    primary: list[str] = []
    for ln in text.splitlines():
        low = ln.lower()
        if (
            "waystones found in area" in low
            or "more pack size" in low
            or "rarity of items found in this area" in low
            or "more magic and rare monsters" in low
            or "more chance of monster modifiers" in low
            or low == "monsters have 13% more effectiveness"
            or low == "monsters have 15% more effectiveness"
            or low == "monsters have 16% more effectiveness"
        ):
            continue
        primary.append(ln)
    return "\n".join(primary) if primary else text


def _candidate_tokens_for_samples(samples: Sequence[str]) -> list[str]:
    if not samples:
        return []
    anchor = _strip_waystone_bonus_lines(samples[0]).lower()
    spans = _ALPHA_SPACE_SPAN_RE.findall(anchor)

    cands: dict[str, bool] = {}  # token -> starts_at_word_boundary
    for span in spans:
        norm_span = re.sub(r"\s+", " ", span).strip()
        n = len(norm_span)
        for length in range(2, min(13, n + 1)):
            for start in range(0, n - length + 1):
                sub = norm_span[start : start + length]
                if sub[0] == " " or sub[-1] == " ":
                    continue
                if len(sub.replace(" ", "")) < 2:
                    continue
                is_word_start = start == 0 or norm_span[start - 1] == " "
                if is_word_start or sub not in cands:
                    cands[sub] = is_word_start

    verified: list[str] = []
    for tok in cands:
        if all(re.search(tok, s, re.I) is not None for s in samples):
            verified.append(tok)

    verified.sort(
        key=lambda t: (
            0 if cands[t] else 1,
            len(t),
            1 if " " in t else 0,
            t,
        )
    )
    return verified


def compile_minimal_stash_regex(
    purpose: str,
    target_groups: Sequence[tuple[str, Sequence[str]]],
    negative_texts: Sequence[str],
    *,
    negate: bool = False,
    max_chars: int = POE2_STASH_REGEX_MAX_CHARS,
    usage_note: str = "",
) -> CompiledStashRegex:
    """Compile a minimal PoE2 Stash Search regex covering 100% of `target_groups` (`0` false
    negatives) while rejecting `negative_texts` and strictly honoring `len(regex) <= max_chars`.
    """
    wrapper_overhead = 3 if negate else 2  # `!"..."` vs `"..."`
    # Reserve 3 chars even on non-negated brick regexes when possible so `!"..."` ALSO fits <=50!
    preferred_inner_budget = max(8, max_chars - 3)
    hard_inner_budget = max(8, max_chars - wrapper_overhead)

    active_groups = [(gid, list(samps)) for gid, samps in target_groups if samps]
    if not active_groups:
        empty_rx = '!".*"' if negate else '""'
        return CompiledStashRegex(
            purpose=purpose,
            regex=empty_rx,
            char_length=len(empty_rx),
            max_chars=max_chars,
            within_client_limit=len(empty_rx) <= max_chars,
            tokens=[],
            matched_target_count=0,
            total_targets=0,
            false_negatives=0,
            false_positives=0,
            usage_note=usage_note,
        )

    neg_lower = [n.lower() for n in negative_texts if n]
    group_Low_samples = [[s.lower() for s in samps] for _, samps in active_groups]
    n_groups = len(active_groups)

    # Map candidate token -> (covered_group_set, false_positive_count, is_word_prefix)
    word_prefix_re_cache: dict[str, re.Pattern[str]] = {}
    token_Pool: dict[str, tuple[set[int], int, bool]] = {}
    for _, samps in active_groups:
        for tok in _candidate_tokens_for_samples(samps):
            if tok in token_Pool:
                continue
            fp = sum(1 for neg in neg_lower if tok in neg)
            cov = {
                j for j, g_samps in enumerate(group_Low_samples) if all(tok in gs for gs in g_samps)
            }
            if cov:
                wp_pat = word_prefix_re_cache.setdefault(
                    tok, re.compile(rf"(?:^|\s){re.escape(tok)}", re.I)
                )
                is_wp = any(
                    all(wp_pat.search(gs) is not None for gs in group_Low_samples[j]) for j in cov
                )
                token_Pool[tok] = (cov, fp, is_wp)

    def _solve_cover(
        min_tok_len: int, allow_fp: bool, require_word_prefix: bool = False
    ) -> list[str]:
        uncovered = set(range(n_groups))
        chosen_tokens: list[str] = []
        while uncovered:
            best_tok: str | None = None
            best_score: tuple[int, int, float, int, str] | None = None
            for tok, (cov, fp, is_wp) in token_Pool.items():
                if len(tok) < min_tok_len:
                    continue
                if not allow_fp and fp > 0:
                    continue
                if require_word_prefix and not is_wp:
                    continue
                newly = cov & uncovered
                if not newly:
                    continue
                added_cost = len(tok) + (1 if chosen_tokens else 0)
                cand_score = (fp, 0 if is_wp else 1, added_cost / len(newly), len(tok), tok)
                if best_score is None or cand_score < best_score:
                    best_score = cand_score
                    best_tok = tok

            if best_tok is None:
                if require_word_prefix:
                    return _solve_cover(
                        min_tok_len=min_tok_len, allow_fp=allow_fp, require_word_prefix=False
                    )
                g_pick = min(uncovered)
                fallback_cands = [
                    (fp, 0 if is_wp else 1, len(tok), tok)
                    for tok, (cov, fp, is_wp) in token_Pool.items()
                    if g_pick in cov
                ]
                if fallback_cands:
                    fallback_cands.sort()
                    best_tok = fallback_cands[0][3]
                else:
                    raw_word = re.sub(r"[^a-z]+", "", group_Low_samples[g_pick][0])[:4] or "mod"
                    best_tok = raw_word
                    token_Pool[best_tok] = ({g_pick}, 0, True)

            chosen_tokens.append(best_tok)
            uncovered -= token_Pool[best_tok][0]

        # Prune redundant tokens
        pruned: list[str] = list(chosen_tokens)
        i = 0
        while i < len(pruned):
            others = pruned[:i] + pruned[i + 1 :]
            union_others = set()
            for ot in others:
                union_others |= token_Pool[ot][0]
            if len(union_others) == n_groups:
                pruned.pop(i)
            else:
                i += 1
        return pruned

    # Prefer clean >=4-char word-boundary prefixes when they fit within <=47 inner chars!
    selected = _solve_cover(min_tok_len=4, allow_fp=False, require_word_prefix=True)
    if len("|".join(selected)) > preferred_inner_budget:
        selected = _solve_cover(min_tok_len=3, allow_fp=False, require_word_prefix=True)
    if len("|".join(selected)) > preferred_inner_budget:
        selected = _solve_cover(min_tok_len=3, allow_fp=False, require_word_prefix=False)
    if len("|".join(selected)) > preferred_inner_budget:
        selected = _solve_cover(min_tok_len=2, allow_fp=False, require_word_prefix=False)
    if len("|".join(selected)) > hard_inner_budget:
        selected = _solve_cover(min_tok_len=2, allow_fp=True, require_word_prefix=False)

    # Final hard-cap safety guard if caller passed dozens of custom keywords
    while len(selected) > 1 and len("|".join(selected)) > hard_inner_budget:
        selected.pop()

    inner = "|".join(selected)
    if len(inner) > hard_inner_budget:
        inner = inner[:hard_inner_budget].rstrip("| ")

    compiled_re = re.compile(inner, re.I)
    matched_groups = sum(
        1
        for g_samps in group_Low_samples
        if all(compiled_re.search(gs) is not None for gs in g_samps)
    )
    fn_count = n_groups - matched_groups
    fp_count = sum(1 for neg in neg_lower if compiled_re.search(neg) is not None)

    regex_str = f'!"{inner}"' if negate else f'"{inner}"'
    return CompiledStashRegex(
        purpose=purpose,
        regex=regex_str,
        char_length=len(regex_str),
        max_chars=max_chars,
        within_client_limit=len(regex_str) <= max_chars,
        tokens=selected,
        matched_target_count=matched_groups,
        total_targets=n_groups,
        false_negatives=fn_count,
        false_positives=fp_count,
        usage_note=usage_note,
    )


class WaystoneRegexEngine:
    """SQLite-backed PoE2 0.5.5 Waystone & Precursor Tablet Stash Regex Compiler."""

    def __init__(self, db: GamedataDB | Path | str | None = None) -> None:
        self._owns_db = not isinstance(db, GamedataDB)
        self.db = db if isinstance(db, GamedataDB) else GamedataDB(db or get_gamedata_db_path())
        self._waystone_families: dict[str, _ModFamilyRecord] = {}
        self._tablet_families: dict[str, _ModFamilyRecord] = {}
        self._load_families()

    def close(self) -> None:
        if self._owns_db:
            self.db.close()

    def __enter__(self) -> WaystoneRegexEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _load_families(self) -> None:
        rows = self.db.query_all(
            """
            SELECT DISTINCT
                base_group_slug, source_pool, affix_side, affix_name, mod_type, stat_text
            FROM mod_tiers
            WHERE (base_group_slug LIKE 'Waystones%' OR base_group_slug LIKE '%Tablet%')
              AND stat_text != ''
            ORDER BY base_group_slug, source_pool, mod_type
            """
        )
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            slug = str(r["base_group_slug"])
            dkind: Literal["waystone", "tablet"] = (
                "waystone" if slug.startswith("Waystones") else "tablet"
            )
            mtype = str(r["mod_type"])
            clean_txt = _sanitize_stat_text(str(r["stat_text"]))
            if not clean_txt:
                continue
            bucket = grouped.setdefault(
                (dkind, mtype),
                {
                    "source_pool": str(r["source_pool"]),
                    "affix_side": str(r["affix_side"]),
                    "affix_names": [],
                    "templates": [],
                    "samples": [],
                },
            )
            aname = str(r["affix_name"] or "").strip()
            if aname and aname not in bucket["affix_names"]:
                bucket["affix_names"].append(aname)
            if clean_txt not in bucket["templates"]:
                bucket["templates"].append(clean_txt)
                lo_s, hi_s = _render_roll_endpoints(clean_txt)
                for s in (lo_s, hi_s):
                    if s not in bucket["samples"]:
                        bucket["samples"].append(s)

        for (dkind, mtype), data in grouped.items():
            rec = _ModFamilyRecord(
                domain_kind=dkind,
                mod_type=mtype,
                source_pool=data["source_pool"],
                affix_side=data["affix_side"],
                affix_names=tuple(data["affix_names"]),
                clean_templates=tuple(data["templates"]),
                rendered_samples=tuple(data["samples"]),
            )
            if dkind == "waystone":
                self._waystone_families[mtype] = rec
            else:
                self._tablet_families[mtype] = rec

    def detect_build_hazards(
        self,
        build: BuildStats | DewateredAudit | Mapping[str, Any] | None = None,
        *,
        archetypes: Sequence[str] | None = None,
        avoid_keywords: Sequence[str] | None = None,
        include_dangerous: bool = True,
    ) -> tuple[list[str], list[DetectedWaystoneHazard]]:
        """Auto-detect deadly Waystone hazards from a PoB2 `BuildStats`, `DewateredAudit`,
        `inspect_build` dict, explicit `archetypes`, and/or `avoid_keywords`.
        """
        stats_dict: dict[str, Any] = {}
        if isinstance(build, DewateredAudit):
            stats_dict = build.sustainable_pinnacle_stats.model_dump()
        elif isinstance(build, BuildStats):
            stats_dict = build.model_dump()
        elif isinstance(build, Mapping):
            if "sustainable_pinnacle_stats" in build and isinstance(
                build["sustainable_pinnacle_stats"], Mapping
            ):
                stats_dict = dict(build["sustainable_pinnacle_stats"])
            elif "stats" in build and isinstance(build["stats"], Mapping):
                stats_dict = {**dict(build), **dict(build["stats"])}
            else:
                stats_dict = dict(build)

        blob_parts: list[str] = [str(a).lower() for a in (archetypes or [])]
        for k in (
            "class_name",
            "ascendancy_name",
            "main_skill",
            "active_skills",
            "skills",
            "keystones",
            "archetypes",
            "notes",
        ):
            val = stats_dict.get(k)
            if isinstance(val, str):
                blob_parts.append(val.lower())
            elif isinstance(val, Sequence):
                blob_parts.extend(str(x).lower() for x in val)
        blob = " | ".join(blob_parts)

        life = float(stats_dict.get("life") or stats_dict.get("Life") or 0.0)
        es = float(stats_dict.get("energy_shield") or stats_dict.get("ES") or 0.0)
        mana = float(stats_dict.get("mana") or stats_dict.get("Mana") or 0.0)
        evasion = float(stats_dict.get("evasion") or stats_dict.get("Evasion") or 0.0)
        block = float(stats_dict.get("block_chance") or stats_dict.get("Block") or 0.0)
        crit_ch = float(stats_dict.get("crit_chance") or stats_dict.get("CritChance") or 0.0)

        fire_res = stats_dict.get("fire_resist")
        cold_res = stats_dict.get("cold_resist")
        light_res = stats_dict.get("lightning_resist")

        detected_arch: list[str] = []
        selected_hazard_ids: list[str] = []

        def _add_arch(tag: str) -> None:
            if tag not in detected_arch:
                detected_arch.append(tag)

        def _add_hazard(hid: str) -> None:
            if hid in _KNOWN_HAZARDS and hid not in selected_hazard_ids:
                defn = _KNOWN_HAZARDS[hid]
                if defn.default_severity == "brick" or include_dangerous:
                    selected_hazard_ids.append(hid)

        is_ci = bool(
            stats_dict.get("is_ci")
            or (0 < life <= 1.5 and es >= 100.0)
            or re.search(r"\b(ci|chaos inoculation)\b", blob)
        )
        is_archmage = bool(
            stats_dict.get("is_archmage")
            or stats_dict.get("is_mom")
            or mana >= 2000.0
            or re.search(r"\b(archmage|mind over matter|mom|mana stack|stormweaver)\b", blob)
        )
        is_leech = bool(
            stats_dict.get("is_leech")
            or re.search(r"\b(leech|vaal pact|blood mage|chayula)\b", blob)
        )
        is_crit = bool(
            stats_dict.get("is_crit")
            or crit_ch >= 25.0
            or re.search(r"\b(crit|critical|cast on critical|invoker|deadeye|blood mage)\b", blob)
        )
        is_evasion_block = bool(
            evasion >= 3000.0
            or block >= 30.0
            or re.search(r"\b(evasion|block|acrobatics|deflect|pathfinder|stalker)\b", blob)
        )
        is_hybrid_es = bool(not is_ci and es >= 400.0 and life > 1.5)
        is_cooldown = bool(re.search(r"\b(cooldown|comet|hammer of the gods|chronomancer)\b", blob))

        # Core universal endgame mapper hazards when a build or default profile is evaluated
        if build is not None or archetypes:
            _add_hazard("minus_max_resists")
            _add_hazard("elemental_penetration")
            _add_hazard("abyss_no_damage_cycle")

        if is_ci:
            _add_arch("ci")
            _add_hazard("monster_stun_buildup")
            _add_hazard("less_recovery_rate")
            _add_hazard("elemental_penetration")
            _add_hazard("minus_max_resists")

        if is_archmage:
            _add_arch("archmage_mom")
            _add_hazard("mana_siphon_ground")
            _add_hazard("less_recovery_rate")

        if is_leech:
            _add_arch("leech")
            _add_hazard("cannot_leech")
            _add_hazard("less_recovery_rate")

        if is_crit:
            _add_arch("crit")
            _add_hazard("monster_crit")
            if include_dangerous:
                _add_hazard("crit_damage_reduction")
                _add_hazard("enfeeble_curse")

        if is_hybrid_es:
            _add_arch("hybrid_es_life")
            _add_hazard("poison_on_hit")
            _add_hazard("bleed_on_hit")
            _add_hazard("extra_chaos_damage")

        if is_evasion_block:
            _add_arch("evasion_block")
            _add_hazard("monster_accuracy")

        if is_cooldown:
            _add_arch("cooldown")
            _add_hazard("less_cooldown_recovery")

        # Elemental Weakness is deadly unless all 3 elemental resistances are overcapped >= 105%
        res_known = all(
            isinstance(r, (int, float)) and r > 0 for r in (fire_res, cold_res, light_res)
        )
        if (build is not None or archetypes) and (
            not res_known or min(float(fire_res), float(cold_res), float(light_res)) < 105.0  # type: ignore[arg-type]
        ):
            _add_hazard("elemental_weakness")

        hazards: list[DetectedWaystoneHazard] = []
        covered_mod_types: set[str] = set()

        for hid in selected_hazard_ids:
            defn = _KNOWN_HAZARDS[hid]
            samples: list[str] = list(defn.synthetic_samples)
            present_mtypes: list[str] = []
            for mt in defn.mod_types:
                rec = self._waystone_families.get(mt)
                if rec is not None:
                    present_mtypes.append(mt)
                    covered_mod_types.add(mt)
                    for smp in rec.rendered_samples:
                        if smp not in samples:
                            samples.append(smp)
            if samples:
                hazards.append(
                    DetectedWaystoneHazard(
                        hazard_id=hid,
                        severity=defn.default_severity,
                        reason=defn.reason,
                        mod_types=present_mtypes,
                        sample_stat_texts=samples[:2],
                    )
                )

        # Match explicit `avoid_keywords` against known hazards + all SQLite Waystone families
        for raw_kw in avoid_keywords or []:
            kw = raw_kw.strip()
            if not kw:
                continue
            kw_low = kw.lower()

            if "leech" in kw_low and "cannot_leech" not in selected_hazard_ids:
                selected_hazard_ids.append("cannot_leech")
                defn_l = _KNOWN_HAZARDS["cannot_leech"]
                hazards.append(
                    DetectedWaystoneHazard(
                        hazard_id="cannot_leech",
                        severity="brick",
                        reason=defn_l.reason,
                        mod_types=[],
                        sample_stat_texts=list(defn_l.synthetic_samples),
                    )
                )
                continue

            matched_any_sql = False
            for mt, rec in self._waystone_families.items():
                haystack = " ".join(
                    [mt, *rec.affix_names, *rec.clean_templates, *rec.rendered_samples]
                ).lower()
                if kw_low in haystack:
                    matched_any_sql = True
                    if mt in covered_mod_types:
                        continue
                    covered_mod_types.add(mt)
                    hazards.append(
                        DetectedWaystoneHazard(
                            hazard_id=f"avoid:{re.sub(r'[^a-z0-9]+', '_', kw_low).strip('_')}:{mt}",
                            severity="brick",
                            reason=f"Matched explicit avoid keyword {kw!r}.",
                            mod_types=[mt],
                            sample_stat_texts=list(rec.rendered_samples[:2]),
                        )
                    )

            if not matched_any_sql and not any(
                kw_low in " ".join(h.sample_stat_texts).lower() for h in hazards
            ):
                hazards.append(
                    DetectedWaystoneHazard(
                        hazard_id=f"custom:{re.sub(r'[^a-z0-9]+', '_', kw_low).strip('_')}",
                        severity="brick",
                        reason=f"Custom explicit avoid keyword {kw!r}.",
                        mod_types=[],
                        sample_stat_texts=[kw],
                    )
                )

        # Fallback sensible mapper brick defaults if neither build nor avoid_keywords were supplied
        if not hazards:
            for default_hid in (
                "less_recovery_rate",
                "minus_max_resists",
                "elemental_penetration",
                "elemental_weakness",
                "monster_crit",
                "abyss_no_damage_cycle",
            ):
                defn = _KNOWN_HAZARDS[default_hid]
                samps = list(defn.synthetic_samples)
                mtypes = []
                for mt in defn.mod_types:
                    if mt in self._waystone_families:
                        mtypes.append(mt)
                        samps.extend(self._waystone_families[mt].rendered_samples)
                if samps:
                    hazards.append(
                        DetectedWaystoneHazard(
                            hazard_id=default_hid,
                            severity=defn.default_severity,
                            reason=defn.reason,
                            mod_types=mtypes,
                            sample_stat_texts=samps[:2],
                        )
                    )

        return detected_arch, hazards

    def compile_waystone_juicing_regex(
        self,
        *,
        min_waystone_drop_pct: int = 100,
        min_pack_size_pct: int | None = None,
        want_keywords: Sequence[str] | None = None,
        max_chars: int = POE2_STASH_REGEX_MAX_CHARS,
    ) -> CompiledStashRegex:
        """Compile a `<= 50` char PoE2 Stash Regex highlighting high-Waystone-Drop-Chance /
        high-Pack-Size / juiced Waystones.
        """
        clauses: list[str] = []

        if min_waystone_drop_pct >= 200:
            drop_pat = r"nce: \+[2-9]\d\d%"
        elif min_waystone_drop_pct >= 150:
            drop_pat = r"nce: \+(1[5-9]|[2-9]\d)\d%"
        elif min_waystone_drop_pct >= 120:
            drop_pat = r"nce: \+(1[2-9]|[2-9]\d)\d%"
        elif min_waystone_drop_pct >= 100:
            drop_pat = r"nce: \+[1-9]\d\d%"
        else:
            tens = max(1, min_waystone_drop_pct // 10)
            drop_pat = rf"nce: \+([{tens}-9]\d|[1-9]\d\d)%"
        clauses.append(drop_pat)

        if min_pack_size_pct is not None and min_pack_size_pct >= 20:
            tens_p = min(9, max(2, min_pack_size_pct // 10))
            p_cand = rf"size: \+[{tens_p}-9]\d%"
            if len("|".join([*clauses, p_cand])) + 2 <= max_chars:
                clauses.append(p_cand)

        if want_keywords:
            target_groups: list[tuple[str, list[str]]] = []
            hit_mtypes: set[str] = set()
            for kw in want_keywords:
                kw_l = kw.strip().lower()
                if not kw_l or "waystone drop" in kw_l or "pack size" in kw_l:
                    continue
                matched_m = False
                for mt, rec in self._waystone_families.items():
                    if kw_l in " ".join([mt, *rec.clean_templates]).lower():
                        matched_m = True
                        if mt not in hit_mtypes:
                            hit_mtypes.add(mt)
                            target_groups.append((mt, list(rec.rendered_samples)))
                if not matched_m:
                    target_groups.append((kw_l, [kw]))

            if target_groups:
                neg = [
                    smp
                    for mt, rec in self._waystone_families.items()
                    if mt not in hit_mtypes
                    for smp in rec.rendered_samples
                ]
                sub = compile_minimal_stash_regex(
                    "waystone_juicing_extra",
                    target_groups,
                    neg,
                    max_chars=max(12, max_chars - len("|".join(clauses)) - 1),
                )
                for tok in sub.tokens:
                    if len("|".join([*clauses, tok])) + 2 <= max_chars:
                        clauses.append(tok)

        inner = "|".join(clauses)
        regex_str = f'"{inner}"'
        pos_probe = f"Waystone Drop Chance: +{max(min_waystone_drop_pct, 100)}% (augmented)"
        low_probe = f"Waystone Drop Chance: +{max(0, min_waystone_drop_pct - 25)}% (augmented)"
        cre = re.compile(inner, re.I)
        fn = 0 if cre.search(pos_probe) else 1
        fp = 1 if (min_waystone_drop_pct >= 20 and cre.search(low_probe)) else 0

        return CompiledStashRegex(
            purpose="waystone_juicing",
            regex=regex_str,
            char_length=len(regex_str),
            max_chars=max_chars,
            within_client_limit=len(regex_str) <= max_chars,
            tokens=clauses,
            matched_target_count=len(clauses) - fn,
            total_targets=len(clauses),
            false_negatives=fn,
            false_positives=fp,
            usage_note=(
                f"Paste into Stash Search to highlight Waystones with +{min_waystone_drop_pct}%+ "
                "Waystone Drop Chance"
                + (f" or +{min_pack_size_pct}%+ Pack Size." if min_pack_size_pct else ".")
            ),
        )

    def compile_tablet_rolling_regex(
        self,
        *,
        want_keywords: Sequence[str] | None = None,
        max_chars: int = POE2_STASH_REGEX_MAX_CHARS,
    ) -> CompiledStashRegex:
        """Compile a `<= 50` char PoE2 Stash Regex for rolling Precursor Tablets against SQLite."""
        effective_kws = list(
            want_keywords
            or (
                "Quantity of Waystones",
                "Pack Size in Map",
                "Simulacrum Splinters",
                "Wombgifts",
                "Omen",
                "Logbooks",
            )
        )
        hit_mtypes: set[str] = set()
        target_groups: list[tuple[str, list[str]]] = []

        for kw in effective_kws:
            kw_low = kw.strip().lower()
            if not kw_low:
                continue
            group_samples: list[str] = []
            for mt, rec in self._tablet_families.items():
                if rec.source_pool == "corruption_upgrade":
                    continue
                hay = " ".join([mt, *rec.affix_names, *rec.clean_templates]).lower()
                if kw_low in hay:
                    hit_mtypes.add(mt)
                    for smp in rec.rendered_samples:
                        if smp not in group_samples:
                            group_samples.append(smp)
            if group_samples:
                target_groups.append((kw, group_samples))
            else:
                target_groups.append((kw, [kw]))

        negative_texts = [
            *_STANDARD_TOOLTIP_BOILERPLATE,
            *[
                smp
                for mt, rec in self._tablet_families.items()
                if mt not in hit_mtypes
                for smp in rec.rendered_samples
            ],
        ]
        return compile_minimal_stash_regex(
            "tablet_rolling",
            target_groups,
            negative_texts,
            negate=False,
            max_chars=max_chars,
            usage_note=(
                "Paste into Stash Search while rolling Precursor Tablets (slot finished Tablets "
                "directly into the Map Device in PoE2 0.5.5, NOT into Atlas Towers!)."
            ),
        )

    def build_plan(
        self,
        build: BuildStats | DewateredAudit | Mapping[str, Any] | None = None,
        *,
        archetypes: Sequence[str] | None = None,
        avoid_keywords: Sequence[str] | None = None,
        want_keywords: Sequence[str] | None = None,
        tablet_want_keywords: Sequence[str] | None = None,
        min_waystone_drop_pct: int = 120,
        min_pack_size_pct: int | None = 20,
        waystone_item: ParsedItem | str | None = None,
        include_dangerous: bool = False,
        max_chars: int = POE2_STASH_REGEX_MAX_CHARS,
    ) -> WaystoneRegexPlan:
        """Build a complete PoB2-aware Waystone brick regex, safe-map regex, juicing regex,
        Precursor Tablet rolling regex, and 0..6 mod revive warning plan.
        """
        detected_arch, hazards = self.detect_build_hazards(
            build,
            archetypes=archetypes,
            avoid_keywords=avoid_keywords,
            include_dangerous=include_dangerous,
        )

        deadly_mtypes = {mt for h in hazards for mt in h.mod_types}
        target_groups: list[tuple[str, list[str]]] = []
        for h in hazards:
            samps: list[str] = []
            for mt in h.mod_types:
                rec = self._waystone_families.get(mt)
                if rec:
                    for s in rec.rendered_samples:
                        if s not in samps:
                            samps.append(s)
            for syn in h.sample_stat_texts:
                if syn not in samps:
                    samps.append(syn)
            if samps:
                target_groups.append((h.hazard_id, samps))

        negative_texts: list[str] = list(_STANDARD_TOOLTIP_BOILERPLATE)
        for mt, rec in self._waystone_families.items():
            if mt not in deadly_mtypes:
                negative_texts.extend(_strip_waystone_bonus_lines(s) for s in rec.rendered_samples)

        brick_rx = compile_minimal_stash_regex(
            "brick_highlight",
            target_groups,
            negative_texts,
            negate=False,
            max_chars=max_chars,
            usage_note=(
                "Highlights DEADLY / BRICKED Waystones in your stash tab so you can pull them "
                "out to Chaos-reroll or 3-to-1 at the Reforging Bench."
            ),
        )
        safe_rx = compile_minimal_stash_regex(
            "safe_runnable",
            target_groups,
            negative_texts,
            negate=True,
            max_chars=max_chars,
            usage_note=(
                'Negated PoE2 Stash Search (`!"..."`) highlighting SAFE Waystones with zero '
                "build-bricking modifiers."
            ),
        )

        # Back-populate which compact token covers each hazard
        for h in hazards:
            probe_txt = " ".join(h.sample_stat_texts)
            matched_tok = next(
                (t for t in brick_rx.tokens if re.search(t, probe_txt, re.I)),
                brick_rx.tokens[0] if brick_rx.tokens else "",
            )
            h.regex_token = matched_tok

        juicing_rx = self.compile_waystone_juicing_regex(
            min_waystone_drop_pct=min_waystone_drop_pct,
            min_pack_size_pct=min_pack_size_pct,
            want_keywords=want_keywords,
            max_chars=max_chars,
        )
        tablet_rx = self.compile_tablet_rolling_regex(
            want_keywords=tablet_want_keywords,
            max_chars=max_chars,
        )
        revive_sched = build_revive_schedule()

        atlas_warnings = [
            "6-mod Rare Waystones have 0 Revives Available (1 death bricks the map and consumes "
            "all Atlas mechanics): stick to 4–5 mod Waystones (2–1 revives) when pushing XP or "
            "running expensive Boss / Delirium setups.",
            "In PoE2 0.5.5, slot Precursor Tablets DIRECTLY into the Map Device alongside your "
            "Waystone (never look for obsolete 0.1–0.3 Lost Tower tablet slots).",
        ]
        if "ci" in detected_arch:
            atlas_warnings.append(
                "CI Alert: Base Stun Threshold scales with Life=1 unless converted via passives/"
                "uniques; avoid '+% Monster Stun Buildup' on 0-revive Waystones."
            )
        if "archmage_mom" in detected_arch:
            atlas_warnings.append(
                "Archmage / MoM Alert: 'Patches of Mana Siphoning Ground' (Desecrated 'of "
                "Siphoning') drains 1,200 Mana/min and collapses both offense and EHP."
            )

        item_check: WaystoneItemCheckResult | None = None
        if waystone_item is not None:
            item_check = self.inspect_waystone_item(
                waystone_item,
                hazards=hazards,
                brick_regex=brick_rx,
                juicing_regex=juicing_rx,
            )

        return WaystoneRegexPlan(
            detected_archetypes=detected_arch,
            hazards=hazards,
            brick_regex=brick_rx,
            safe_runnable_regex=safe_rx,
            juicing_regex=juicing_rx,
            tablet_regex=tablet_rx,
            revive_schedule=revive_sched,
            atlas_warnings=atlas_warnings,
            item_check=item_check,
        )

    def inspect_waystone_item(
        self,
        waystone: ParsedItem | str,
        *,
        hazards: Sequence[DetectedWaystoneHazard],
        brick_regex: CompiledStashRegex,
        juicing_regex: CompiledStashRegex,
    ) -> WaystoneItemCheckResult:
        parsed = parse_clipboard_item(waystone) if isinstance(waystone, str) else waystone
        explicit_mods = [
            m.raw_text
            for m in parsed.mods
            if m.domain in (ModDomain.EXPLICIT, ModDomain.DESECRATED, ModDomain.FRACTURED)
        ]
        # Count distinct Waystone affixes (6 max; multi-line affixes in plain Ctrl+C cap at 6)
        mod_cnt = min(6, len(explicit_mods))
        revives = (
            parsed.waystone_revives
            if parsed.waystone_revives is not None
            else waystone_revives_for_mod_count(mod_cnt)
        )

        brick_hits: list[str] = []
        danger_hits: list[str] = []
        full_text = parsed.raw_text or "\n".join(explicit_mods)

        for h in hazards:
            tok = h.regex_token
            if tok and re.search(tok, full_text, re.I):
                hit_desc = f"{h.hazard_id} ({h.reason})"
                if h.severity == "brick":
                    brick_hits.append(hit_desc)
                else:
                    danger_hits.append(hit_desc)

        brick_inner = brick_regex.regex.strip('"')
        juice_inner = juicing_regex.regex.strip('"')
        m_brick = bool(brick_inner and re.search(brick_inner, full_text, re.I))
        m_juice = bool(juice_inner and re.search(juice_inner, full_text, re.I))

        rev_warn = (
            "0 revives available — single death bricks this Waystone!"
            if revives == 0
            else (f"Only {revives} revive(s) available." if revives <= 1 else None)
        )
        return WaystoneItemCheckResult(
            waystone_name=parsed.name or parsed.base_type,
            waystone_tier=parsed.waystone_tier,
            mod_count=mod_cnt,
            revives_available=revives,
            waystone_drop_chance_pct=parsed.waystone_drop_chance_pct,
            is_brick=bool(brick_hits) or m_brick,
            brick_mods_hit=brick_hits,
            dangerous_mods_hit=danger_hits,
            matches_brick_regex=m_brick,
            matches_juicing_regex=m_juice,
            revive_warning=rev_warn,
        )


def build_waystone_regex_plan(
    build: BuildStats | DewateredAudit | Mapping[str, Any] | None = None,
    *,
    archetypes: Sequence[str] | None = None,
    avoid_keywords: Sequence[str] | None = None,
    want_keywords: Sequence[str] | None = None,
    tablet_want_keywords: Sequence[str] | None = None,
    min_waystone_drop_pct: int = 120,
    min_pack_size_pct: int | None = 20,
    waystone_item: ParsedItem | str | None = None,
    include_dangerous: bool = False,
    max_chars: int = POE2_STASH_REGEX_MAX_CHARS,
    db: GamedataDB | Path | str | None = None,
) -> WaystoneRegexPlan:
    """Top-level convenience entrypoint for PoB2-aware Waystone & Tablet Stash Regex compilation."""
    with WaystoneRegexEngine(db=db) as eng:
        return eng.build_plan(
            build=build,
            archetypes=archetypes,
            avoid_keywords=avoid_keywords,
            want_keywords=want_keywords,
            tablet_want_keywords=tablet_want_keywords,
            min_waystone_drop_pct=min_waystone_drop_pct,
            min_pack_size_pct=min_pack_size_pct,
            waystone_item=waystone_item,
            include_dangerous=include_dangerous,
            max_chars=max_chars,
        )
