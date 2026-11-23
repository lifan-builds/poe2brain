from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from poe2brain.config import DEFAULT_LEAGUE
from poe2brain.crafting.pool import (
    AffixSide,
    BaseItemEntry,
    CraftingState,
    LeverEntry,
    ModPoolEngine,
    PoolEvalResult,
    TargetSpec,
    _norm_key,
)
from poe2brain.models.crafting import (
    CraftActionEval,
    CraftPlanResult,
    CraftPolicyStep,
    TradeVsCraftComparison,
)
from poe2brain.models.items import ItemRarity, ParsedItem

# Reference Patch 0.5.5 Exalted-equivalent baseline prices used ONLY when live quotes omit an item
# and `use_fallback_prices=True`. Every price is strictly > 0.0 (never free!).
DEFAULT_CRAFTING_PRICES_EX: dict[str, float] = {
    "Exalted Orb": 1.0,
    "Greater Exalted Orb": 4.5,
    "Perfect Exalted Orb": 38.0,
    "Orb of Transmutation": 0.02,
    "Greater Orb of Transmutation": 0.25,
    "Perfect Orb of Transmutation": 2.5,
    "Orb of Augmentation": 0.02,
    "Greater Orb of Augmentation": 0.25,
    "Perfect Orb of Augmentation": 2.5,
    "Regal Orb": 0.15,
    "Greater Regal Orb": 0.9,
    "Perfect Regal Orb": 6.5,
    "Chaos Orb": 12.0,
    "Greater Chaos Orb": 30.0,
    "Perfect Chaos Orb": 95.0,
    "Orb of Alchemy": 0.25,
    "Orb of Annulment": 18.0,
    "Fracturing Orb": 180.0,
    "Divine Orb": 450.0,
    "Vaal Orb": 0.5,
    # Omens
    "Omen of Sinistral Exaltation": 14.0,
    "Omen of Dextral Exaltation": 14.0,
    "Omen of Greater Exaltation": 22.0,
    "Omen of Catalysing Exaltation": 16.0,
    "Omen of Sinistral Coronation": 6.0,
    "Omen of Dextral Coronation": 6.0,
    "Omen of Sinistral Annulment": 40.0,
    "Omen of Dextral Annulment": 40.0,
    "Omen of Sinistral Erasure": 35.0,
    "Omen of Dextral Erasure": 35.0,
    "Omen of Whittling": 140.0,
    "Omen of Sinistral Crystallisation": 32.0,
    "Omen of Dextral Crystallisation": 32.0,
    "Omen of Sinistral Necromancy": 18.0,
    "Omen of Dextral Necromancy": 18.0,
    "Omen of Light": 45.0,
    "Omen of Abyssal Echoes": 55.0,
    "Omen of the Liege": 65.0,
    "Omen of the Blackblooded": 65.0,
    "Omen of the Sovereign": 65.0,
    "Omen of Sanctification": 90.0,
    "Omen of Corruption": 12.0,
    # Abyssal Desecration Bones
    "Gnawed Jawbone": 0.5,
    "Preserved Jawbone": 4.0,
    "Ancient Jawbone": 32.0,
    "Gnawed Rib": 0.5,
    "Preserved Rib": 3.5,
    "Ancient Rib": 28.0,
    "Gnawed Collarbone": 0.8,
    "Preserved Collarbone": 5.0,
    "Ancient Collarbone": 36.0,
    # Corrupted Essences
    "Essence of the Abyss": 45.0,
    "Essence of Horror": 80.0,
    "Essence of Hysteria": 80.0,
    "Essence of Insanity": 80.0,
    "Essence of Delirium": 80.0,
    "Essence of the Breach": 65.0,
}


@dataclass(slots=True)
class PriceBook:
    prices_by_key: dict[str, float]
    divine_rate_ex: float
    used_fallback: bool = False

    @classmethod
    def build(
        cls,
        currency_prices_ex: dict[str, float] | None = None,
        *,
        use_fallback_prices: bool = True,
    ) -> PriceBook:
        table: dict[str, float] = {}
        used_fb = False
        if use_fallback_prices:
            for k, v in DEFAULT_CRAFTING_PRICES_EX.items():
                if v > 0:
                    table[_norm_key(k)] = float(v)
            used_fb = currency_prices_ex is None or len(currency_prices_ex) < 10

        if currency_prices_ex:
            for k, v in currency_prices_ex.items():
                if v is not None and float(v) > 0:
                    table[_norm_key(k)] = float(v)

        table.setdefault("exalted_orb", 1.0)
        table.setdefault("exalted", 1.0)
        div_rate = table.get("divine_orb") or table.get("divine") or 450.0
        return cls(prices_by_key=table, divine_rate_ex=div_rate, used_fallback=used_fb)

    def price_of(self, lever: LeverEntry, *, use_category_fallback: bool = True) -> float | None:
        direct = self.prices_by_key.get(lever.lever_id) or self.prices_by_key.get(
            _norm_key(lever.name)
        )
        if direct is not None and direct > 0:
            return direct
        if not use_category_fallback:
            return None

        # Conservative positive category fallbacks so Essences / Alloys never price at 0!
        if lever.category == "essence":
            if lever.name.startswith("Lesser "):
                return 0.2
            if lever.name.startswith("Greater "):
                return 3.5
            return 1.0
        if lever.category == "perfect_or_corrupted_essence":
            return 42.0
        if lever.category == "alloy":
            return 55.0
        if lever.category == "liquid_emotion":
            return 15.0
        return None

    def action_cost(
        self,
        lever: LeverEntry,
        omens: list[LeverEntry] | tuple[LeverEntry, ...],
        *,
        use_category_fallback: bool = True,
    ) -> float | None:
        base_p = self.price_of(lever, use_category_fallback=use_category_fallback)
        if base_p is None or base_p <= 0:
            return None
        total = base_p
        for om in omens:
            op = self.price_of(om, use_category_fallback=use_category_fallback)
            if op is None or op <= 0:
                return None
            total += op
        return total


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _bone_family_for_base(base: BaseItemEntry) -> str | None:
    slug = base.poe2db_slug
    if slug in ("Amulets", "Rings", "Belts"):
        return "Collarbone"
    if any(
        slug.startswith(p)
        for p in ("Body_Armours", "Helmets", "Gloves", "Boots", "Shields", "Bucklers", "Foci")
    ):
        return "Rib"
    if slug in (
        "Claws",
        "Daggers",
        "Wands",
        "One_Hand_Swords",
        "One_Hand_Axes",
        "One_Hand_Maces",
        "Sceptres",
        "Spears",
        "Flails",
        "Bows",
        "Staves",
        "Two_Hand_Swords",
        "Two_Hand_Axes",
        "Two_Hand_Maces",
        "Quarterstaves",
        "Crossbows",
        "Talismans",
        "Quivers",
    ):
        return "Jawbone"
    return None


@dataclass(frozen=True, slots=True)
class _ChosenAction:
    lever_name: str
    omens: tuple[str, ...]
    cost_ex: float
    hit_prob: float
    collateral_prob: float
    label: str
    notes: str


class CraftingPlanner:
    """Exact single-step evaluator & multi-step PoE2 0.5.5 expected-cost crafting planner."""

    def __init__(self, pool_engine: ModPoolEngine | None = None) -> None:
        self.engine = pool_engine or ModPoolEngine()

    def _coerce_state(
        self,
        base: str | BaseItemEntry,
        ilvl: int,
        starting_state: CraftingState | ParsedItem | None,
    ) -> CraftingState:
        if isinstance(starting_state, CraftingState):
            return starting_state.clone()
        if isinstance(starting_state, ParsedItem):
            return self.engine.parse_state_from_item(starting_state)
        b_entry = base if isinstance(base, BaseItemEntry) else self.engine.resolve_base(base)
        return CraftingState(base=b_entry, ilvl=ilvl, rarity=ItemRarity.NORMAL)

    def _candidate_action_pairs(
        self,
        state: CraftingState,
        target_ids: set[str] | None = None,
    ) -> list[tuple[str, list[str]]]:
        pairs: list[tuple[str, list[str]]] = []
        if state.rarity == ItemRarity.NORMAL:
            pairs.extend(
                [
                    ("Orb of Transmutation", []),
                    ("Greater Orb of Transmutation", []),
                    ("Perfect Orb of Transmutation", []),
                    ("Orb of Alchemy", []),
                    ("Orb of Alchemy", ["Omen of Sinistral Alchemy"]),
                    ("Orb of Alchemy", ["Omen of Dextral Alchemy"]),
                ]
            )
            return pairs

        if state.rarity == ItemRarity.MAGIC:
            if state.has_open_prefix(ItemRarity.MAGIC) or state.has_open_suffix(ItemRarity.MAGIC):
                pairs.extend(
                    [
                        ("Orb of Augmentation", []),
                        ("Greater Orb of Augmentation", []),
                        ("Perfect Orb of Augmentation", []),
                    ]
                )
            pairs.extend(
                [
                    ("Regal Orb", []),
                    ("Greater Regal Orb", []),
                    ("Perfect Regal Orb", []),
                    ("Regal Orb", ["Omen of Sinistral Coronation"]),
                    ("Regal Orb", ["Omen of Dextral Coronation"]),
                    ("Greater Regal Orb", ["Omen of Sinistral Coronation"]),
                    ("Greater Regal Orb", ["Omen of Dextral Coronation"]),
                    ("Perfect Regal Orb", ["Omen of Sinistral Coronation"]),
                    ("Perfect Regal Orb", ["Omen of Dextral Coronation"]),
                ]
            )
            for ess in self.engine.list_levers("essence"):
                if target_ids is not None and not any(
                    mid in target_ids for mid in ess.forced_mods.values()
                ):
                    continue
                if ess.forced_mods:
                    pairs.append((ess.name, []))
            return pairs

        if state.rarity == ItemRarity.RARE:
            for ex in ("Exalted Orb", "Greater Exalted Orb", "Perfect Exalted Orb"):
                if state.has_open_prefix(ItemRarity.RARE) or state.has_open_suffix(ItemRarity.RARE):
                    pairs.append((ex, []))
                if state.has_open_prefix(ItemRarity.RARE):
                    pairs.append((ex, ["Omen of Sinistral Exaltation"]))
                if state.has_open_suffix(ItemRarity.RARE):
                    pairs.append((ex, ["Omen of Dextral Exaltation"]))
                if state.quality > 0 and state.catalyst_tag:
                    pairs.append((ex, ["Omen of Catalysing Exaltation"]))

            for ch in ("Chaos Orb", "Greater Chaos Orb", "Perfect Chaos Orb"):
                pairs.append((ch, []))
                pairs.append((ch, ["Omen of Whittling"]))
                pairs.append((ch, ["Omen of Sinistral Erasure"]))
                pairs.append((ch, ["Omen of Dextral Erasure"]))

            for cat in ("perfect_or_corrupted_essence", "alloy"):
                for lv in self.engine.list_levers(cat):
                    if lv.name == "Essence of the Abyss":
                        continue
                    if target_ids is not None and not any(
                        mid in target_ids for mid in lv.forced_mods.values()
                    ):
                        continue
                    pairs.append((lv.name, []))
                    pairs.append((lv.name, ["Omen of Sinistral Crystallisation"]))
                    pairs.append((lv.name, ["Omen of Dextral Crystallisation"]))

            if len(state.mods) >= 4 and not state.has_fractured:
                pairs.append(("Fracturing Orb", []))

            if not state.has_desecrated:
                bfam = _bone_family_for_base(state.base)
                if bfam:
                    has_desec_target = target_ids is None or any(
                        m.mod_id in target_ids
                        for m in self.engine.get_mod_tiers(state.base.poe2db_slug, "desecrated")
                    )
                    bone_tiers = ["Preserved", "Ancient"]
                    if state.ilvl <= 64:
                        bone_tiers.insert(0, "Gnawed")
                    for bt in bone_tiers:
                        bname = f"{bt} {bfam}"
                        pairs.append((bname, []))
                        pairs.append((bname, ["Omen of Sinistral Necromancy"]))
                        pairs.append((bname, ["Omen of Dextral Necromancy"]))
                        if has_desec_target:
                            pairs.append((bname, ["Omen of Abyssal Echoes"]))
                            if bfam in ("Jawbone", "Collarbone"):
                                for b_omen in (
                                    "Omen of the Liege",
                                    "Omen of the Blackblooded",
                                    "Omen of the Sovereign",
                                ):
                                    pairs.append((bname, [b_omen]))
                                    pairs.append((bname, [b_omen, "Omen of Abyssal Echoes"]))

        return pairs

    def _format_action_label(self, lever_name: str, omens: list[str] | tuple[str, ...]) -> str:
        if not omens:
            return lever_name
        return f"{lever_name} + {' + '.join(omens)}"

    def evaluate_step(
        self,
        base: str,
        ilvl: int,
        target_mods: list[str | dict[str, Any] | TargetSpec],
        *,
        starting_state: CraftingState | ParsedItem | None = None,
        lever: str | None = None,
        omens: list[str] | None = None,
        currency_prices_ex: dict[str, float] | None = None,
        use_fallback_prices: bool = True,
        max_collateral_prob: float = 1.0,
    ) -> list[CraftActionEval]:
        state = self._coerce_state(base, ilvl, starting_state)
        specs = [
            self.engine.resolve_target(state.base.poe2db_slug, t, state.ilvl) for t in target_mods
        ]
        unsatisfied = [sp for sp in specs if not sp.is_satisfied_by(state)]
        active_specs = unsatisfied or specs

        target_ids: set[str] = set()
        for sp in active_specs:
            target_ids.update(sp.matching_mod_ids)

        protected_ids: set[str] = set()
        for sp in specs:
            if sp.is_satisfied_by(state):
                protected_ids.update(sp.matching_mod_ids)

        price_book = PriceBook.build(currency_prices_ex, use_fallback_prices=use_fallback_prices)

        if lever is not None:
            pairs = [(lever, omens or [])]
        else:
            pairs = self._candidate_action_pairs(state, target_ids)

        evals: list[CraftActionEval] = []
        for lv_name, om_list in pairs:
            res: PoolEvalResult = self.engine.evaluate_action(
                state,
                target_ids,
                lv_name,
                om_list,
                protected_mod_ids=protected_ids,
            )
            if lever is None and res.hit_probability <= 0:
                continue
            if res.collateral_remove_probability > max_collateral_prob:
                continue

            step_cost = price_book.action_cost(
                res.lever, res.omens, use_category_fallback=use_fallback_prices
            )
            if step_cost is None:
                if lever is not None:
                    raise ValueError(
                        f"Missing positive market price for {lv_name!r} (+ {om_list}); "
                        "refusing to price unknown currencies at 0."
                    )
                continue

            exp_att = (1.0 / res.hit_probability) if res.hit_probability > 0 else None
            exp_cost = (step_cost * exp_att) if exp_att is not None else None
            note = res.notes
            if res.collateral_remove_probability > 0:
                col_str = f"collateral_risk={res.collateral_remove_probability:.1%}"
                note = f"{note}; {col_str}" if note else col_str

            lever_id_full = res.lever.lever_id
            if res.omens:
                lever_id_full = "+".join([res.lever.lever_id, *(o.lever_id for o in res.omens)])

            evals.append(
                CraftActionEval(
                    lever_id=lever_id_full,
                    action_label=self._format_action_label(
                        res.lever.name, [o.name for o in res.omens]
                    ),
                    step_cost_exalted=round(step_cost, 4),
                    hit_probability=round(res.hit_probability, 6),
                    expected_attempts_to_hit=round(exp_att, 3) if exp_att is not None else None,
                    expected_cost_to_hit_exalted=round(exp_cost, 3)
                    if exp_cost is not None
                    else None,
                    eligible_pool_weight=res.eligible_pool_weight,
                    target_weight=res.target_weight,
                    notes=note or None,
                )
            )

        evals.sort(
            key=lambda e: (
                e.expected_cost_to_hit_exalted
                if e.expected_cost_to_hit_exalted is not None
                else float("inf"),
                -e.hit_probability,
            )
        )
        return evals

    def _pick_best_state_action(
        self,
        state: CraftingState,
        specs: list[TargetSpec],
        price_book: PriceBook,
        base_cost_ex: float,
        *,
        can_restart: bool,
        use_fallback_prices: bool,
        memo: dict[tuple, _ChosenAction | None],
    ) -> _ChosenAction | None:
        unsatisfied = [sp for sp in specs if not sp.is_satisfied_by(state)]
        if not unsatisfied:
            return None

        # Compact abstract state key for O(1) policy lookup across Monte Carlo trials
        no_tags = tuple(sorted(t for t in state.effective_tags() if t.startswith("no_")))
        min_junk_lvl = min(
            (
                m.required_level
                for m in state.mods
                if not m.fractured and not any(m.mod_id in sp.matching_mod_ids for sp in specs)
            ),
            default=999,
        )
        min_hit_lvl = min(
            (
                m.required_level
                for m in state.mods
                if any(m.mod_id in sp.matching_mod_ids for sp in specs)
            ),
            default=999,
        )
        can_whittle_safely = min_junk_lvl < min_hit_lvl
        sig = (
            state.rarity.value,
            tuple(sp.is_satisfied_by(state) for sp in specs),
            tuple(sp.is_family_blocked_by_junk(state) for sp in specs),
            state.prefix_count,
            state.suffix_count,
            state.has_desecrated,
            state.has_fractured,
            can_whittle_safely,
            no_tags,
        )
        if sig in memo:
            return memo[sig]

        protected_ids: set[str] = set()
        for sp in specs:
            if sp.is_satisfied_by(state):
                protected_ids.update(sp.matching_mod_ids)

        target_ids: set[str] = set()
        for sp in unsatisfied:
            target_ids.update(sp.matching_mod_ids)

        has_blocked_family = any(sp.is_family_blocked_by_junk(state) for sp in unsatisfied)
        if (
            has_blocked_family
            and state.rarity == ItemRarity.MAGIC
            and can_restart
            and not state.has_fractured
        ):
            memo[sig] = None
            return None

        candidates = self._candidate_action_pairs(state, target_ids)
        best: _ChosenAction | None = None
        best_score = float("inf")

        for lv_name, om_list in candidates:
            if lv_name == "Fracturing Orb":
                continue
            res = self.engine.evaluate_action(
                state,
                target_ids,
                lv_name,
                om_list,
                protected_mod_ids=protected_ids,
            )
            if res.hit_probability <= 0:
                continue
            # Never destroy already-hit targets when a 0-collateral move or restart exists
            if protected_ids and res.collateral_remove_probability > 0 and can_restart:
                continue

            cost = price_book.action_cost(
                res.lever, res.omens, use_category_fallback=use_fallback_prices
            )
            if cost is None:
                continue

            # Effective single-target acquisition cost (accounting for base cost on Normal opener)
            opener_overhead = base_cost_ex if state.rarity == ItemRarity.NORMAL else 0.0
            eff_cost_per_hit = (cost + opener_overhead) / res.hit_probability
            if res.collateral_remove_probability > 0:
                eff_cost_per_hit /= max(0.05, 1.0 - res.collateral_remove_probability)

            if eff_cost_per_hit < best_score:
                best_score = eff_cost_per_hit
                best = _ChosenAction(
                    lever_name=res.lever.name,
                    omens=tuple(o.name for o in res.omens),
                    cost_ex=cost,
                    hit_prob=res.hit_probability,
                    collateral_prob=res.collateral_remove_probability,
                    label=self._format_action_label(res.lever.name, [o.name for o in res.omens]),
                    notes=res.notes,
                )

        if best is None and state.rarity == ItemRarity.RARE:
            repair_pairs: list[tuple[str, list[str]]] = [
                ("Orb of Annulment", []),
                ("Orb of Annulment", ["Omen of Sinistral Annulment"]),
                ("Orb of Annulment", ["Omen of Dextral Annulment"]),
            ]
            if state.has_desecrated:
                repair_pairs.insert(0, ("Orb of Annulment", ["Omen of Light"]))
            for lv_name, om_list in repair_pairs:
                res = self.engine.evaluate_action(
                    state, target_ids, lv_name, om_list, protected_mod_ids=protected_ids
                )
                if res.hit_probability <= 0:
                    continue
                if can_restart and res.collateral_remove_probability > 0:
                    continue
                cost = price_book.action_cost(
                    res.lever, res.omens, use_category_fallback=use_fallback_prices
                )
                if cost is None:
                    continue
                score = cost / max(
                    0.05, res.hit_probability * (1.0 - res.collateral_remove_probability)
                )
                if score < best_score:
                    best_score = score
                    best = _ChosenAction(
                        lever_name=res.lever.name,
                        omens=tuple(o.name for o in res.omens),
                        cost_ex=cost,
                        hit_prob=res.hit_probability,
                        collateral_prob=res.collateral_remove_probability,
                        label=self._format_action_label(
                            res.lever.name, [o.name for o in res.omens]
                        ),
                        notes=res.notes,
                    )

        memo[sig] = best
        return best

    def plan_craft(
        self,
        base: str,
        ilvl: int = 82,
        target_mods: list[str | dict[str, Any] | TargetSpec] | None = None,
        currency_prices_ex: dict[str, float] | None = None,
        *,
        starting_state: CraftingState | ParsedItem | None = None,
        base_cost_ex: float = 0.0,
        buy_price_ex: float | None = None,
        trade_url: str | None = None,
        league: str = DEFAULT_LEAGUE,
        n_sims: int = 3000,
        seed: int = 42,
        use_fallback_prices: bool = True,
    ) -> CraftPlanResult:
        if not target_mods:
            raise ValueError("plan_craft requires at least 1 target modifier.")

        init_state = self._coerce_state(base, ilvl, starting_state)
        specs = [
            self.engine.resolve_target(init_state.base.poe2db_slug, t, init_state.ilvl)
            for t in target_mods
        ]

        # Validate side slot feasibility on base
        req_p = sum(1 for s in specs if s.affix_side == AffixSide.PREFIX)
        req_s = sum(1 for s in specs if s.affix_side == AffixSide.SUFFIX)
        if req_p > init_state.base.max_prefixes or req_s > init_state.base.max_suffixes:
            raise ValueError(
                f"Targets require {req_p}P/{req_s}S, exceeding {init_state.base.name} "
                f"slot limits ({init_state.base.max_prefixes}P/{init_state.base.max_suffixes}S)."
            )
        fam_sides = [s.family_id for s in specs]
        if len(set(fam_sides)) < len(fam_sides):
            raise ValueError(
                f"Target modifiers share mutually-exclusive family_id in {fam_sides!r} "
                "and cannot coexist on the same item."
            )

        price_book = PriceBook.build(currency_prices_ex, use_fallback_prices=use_fallback_prices)
        warnings: list[str] = []
        if price_book.used_fallback:
            warnings.append(
                "Used reference Patch 0.5.5 fallback currency quotes for unpriced items; "
                "pass live EconomyService prices for real-time market accuracy."
            )

        # Check if any mod weight is unmeasured (Charms / Waystones / Jewels / Tablets)
        unmeasured_specs = [
            sp.label
            for sp in specs
            if any(e.weight_confidence == "equal_unmeasured" for e in sp.matching_entries)
        ]
        if unmeasured_specs:
            joined_specs = ", ".join(unmeasured_specs)
            warnings.append(
                f"Uniform tier weights assumed on non-recombinatable pool for: {joined_specs}."
            )

        candidate_actions = self.evaluate_step(
            init_state.base.name,
            init_state.ilvl,
            specs,
            starting_state=init_state,
            currency_prices_ex=currency_prices_ex,
            use_fallback_prices=use_fallback_prices,
            max_collateral_prob=0.0 if starting_state is None else 1.0,
        )[:8]

        # Build canonical policy playbook along the success path
        policy_steps: list[CraftPolicyStep] = []
        memo: dict[tuple, _ChosenAction | None] = {}
        can_restart = starting_state is None or init_state.rarity == ItemRarity.NORMAL

        probe_state = init_state.clone()
        step_idx = 0
        analytical_success_prob = 1.0
        analytical_attempt_cost = base_cost_ex
        rem_specs = list(specs)

        # Order targets hardest-first for analytical spine
        while rem_specs:
            unsat = [s for s in rem_specs if not s.is_satisfied_by(probe_state)]
            if not unsat:
                break
            chosen = self._pick_best_state_action(
                probe_state,
                specs,
                price_book,
                base_cost_ex,
                can_restart=can_restart,
                use_fallback_prices=use_fallback_prices,
                memo=memo,
            )
            if chosen is None:
                break
            step_idx += 1
            hit_count_now = len(specs) - len(unsat)
            when_desc = (
                f"{probe_state.rarity.value.capitalize()} {probe_state.base.name} "
                f"({hit_count_now}/{len(specs)} targets hit, "
                f"{probe_state.prefix_count}P/{probe_state.suffix_count}S)"
            )
            lv_entry = self.engine.resolve_lever(chosen.lever_name)
            om_ids = [self.engine.resolve_lever(o).lever_id for o in chosen.omens]
            policy_steps.append(
                CraftPolicyStep(
                    step_index=step_idx,
                    when_state=when_desc,
                    recommended_action=chosen.label,
                    lever_ids=[lv_entry.lever_id, *om_ids],
                    step_cost_exalted=round(chosen.cost_ex, 4),
                    transition_probability=round(chosen.hit_prob, 6),
                    on_miss="repair_or_whittle" if not can_restart else "restart_base_or_repair",
                )
            )
            analytical_attempt_cost += analytical_success_prob * chosen.cost_ex
            analytical_success_prob *= chosen.hit_prob

            # Advance probe_state deterministically with the highest-weight matching target entry
            all_unsat_ids = {mid for s in unsat for mid in s.matching_mod_ids}
            lv_eval = self.engine.evaluate_action(
                probe_state, all_unsat_ids, chosen.lever_name, list(chosen.omens)
            )
            if lv_eval.matching_tiers:
                best_tier_entry = max(
                    lv_eval.matching_tiers, key=lambda e: (e.weight, -e.display_tier)
                )
                if "Transmutation" in chosen.lever_name:
                    probe_state.rarity = ItemRarity.MAGIC
                elif "Regal" in chosen.lever_name or lv_entry.category == "essence":
                    probe_state.rarity = ItemRarity.RARE
                from poe2brain.crafting.pool import CraftingMod

                probe_state.mods.append(CraftingMod.from_entry(best_tier_entry))
            else:
                break
            if step_idx >= 8:
                break

        # Concrete-item hybrid Monte Carlo simulation
        rng = random.Random(seed)
        all_target_ids = {mid for s in specs for mid in s.matching_mod_ids}

        def _run_single_base_attempt() -> tuple[bool, float]:
            st = init_state.clone()
            spend = base_cost_ex
            for _ in range(14):
                if all(s.is_satisfied_by(st) for s in specs):
                    return True, spend
                act = self._pick_best_state_action(
                    st,
                    specs,
                    price_book,
                    base_cost_ex,
                    can_restart=can_restart,
                    use_fallback_prices=use_fallback_prices,
                    memo=memo,
                )
                if act is None:
                    return False, spend
                # Restart early if Magic has 2 mods and 0 targets hit (when restarting is allowed)
                if (
                    can_restart
                    and st.rarity == ItemRarity.MAGIC
                    and len(st.mods) >= 2
                    and not any(s.is_satisfied_by(st) for s in specs)
                ):
                    return False, spend
                # Restart if Rare slots are full without a zero-collateral swap/whittle
                if (
                    can_restart
                    and st.rarity == ItemRarity.RARE
                    and len(st.mods) >= (st.base.max_prefixes + st.base.max_suffixes)
                    and act.lever_name
                    not in ("Chaos Orb", "Greater Chaos Orb", "Perfect Chaos Orb")
                    and "Essence" not in act.lever_name
                    and "Alloy" not in act.lever_name
                    and "Jawbone" not in act.lever_name
                    and "Rib" not in act.lever_name
                    and "Collarbone" not in act.lever_name
                ):
                    return False, spend

                spend += act.cost_ex
                st = self.engine.sample_transition(
                    st, act.lever_name, list(act.omens), all_target_ids, rng
                )
            return all(s.is_satisfied_by(st) for s in specs), spend

        completed_costs: list[float] = []
        fail_attempt_costs: list[float] = []
        succ_attempt_costs: list[float] = []
        max_raw_attempts = min(600, max(200, n_sims))
        attempts_run = 0
        running_craft_spend = 0.0

        while len(completed_costs) < n_sims and attempts_run < max_raw_attempts:
            attempts_run += 1
            ok, att_cost = _run_single_base_attempt()
            running_craft_spend += att_cost
            if ok:
                completed_costs.append(running_craft_spend)
                succ_attempt_costs.append(att_cost)
                running_craft_spend = 0.0
            else:
                fail_attempt_costs.append(att_cost)

        # Hybrid exact-Geometric completion for low-probability multi-T1 endgame crafts
        empirical_p_succ = (
            len(succ_attempt_costs) / attempts_run if attempts_run > 0 else analytical_success_prob
        )
        p_succ_eff = (
            empirical_p_succ
            if len(succ_attempt_costs) >= 15
            else max(empirical_p_succ, analytical_success_prob)
        )
        p_succ_eff = max(1e-7, min(1.0, p_succ_eff))

        if len(completed_costs) < n_sims:
            mean_fail = (
                sum(fail_attempt_costs) / len(fail_attempt_costs)
                if fail_attempt_costs
                else analytical_attempt_cost
            )
            succ_pool = succ_attempt_costs or [analytical_attempt_cost]
            log_q = math.log(max(1e-12, 1.0 - p_succ_eff)) if p_succ_eff < 1.0 else -1.0
            while len(completed_costs) < n_sims:
                if p_succ_eff >= 1.0:
                    k_fails = 0
                else:
                    u = max(1e-12, rng.random())
                    k_fails = int(math.floor(math.log(u) / log_q))
                c_succ = rng.choice(succ_pool)
                if fail_attempt_costs and k_fails <= 12:
                    c_fail_sum = sum(rng.choice(fail_attempt_costs) for _ in range(k_fails))
                else:
                    c_fail_sum = k_fails * mean_fail
                completed_costs.append(c_fail_sum + c_succ)

        completed_costs.sort()
        exp_cost_ex = sum(completed_costs) / len(completed_costs)
        p50 = _percentile(completed_costs, 0.50)
        p80 = _percentile(completed_costs, 0.80)
        p95 = _percentile(completed_costs, 0.95)
        exp_div = exp_cost_ex / price_book.divine_rate_ex if price_book.divine_rate_ex > 0 else None

        trade_cmp: TradeVsCraftComparison | None = None
        if buy_price_ex is not None:
            diff = buy_price_ex - exp_cost_ex
            if abs(diff) <= 0.05 * max(buy_price_ex, exp_cost_ex, 1.0):
                rec = "tie"
                rationale = (
                    f"Market price ({buy_price_ex:.1f} Ex) is within 5% of expected craft cost "
                    f"({exp_cost_ex:.1f} Ex); buying avoids p80 tail risk ({p80:.1f} Ex)."
                )
            elif buy_price_ex < exp_cost_ex:
                rec = "buy"
                rationale = (
                    f"Buy on market: saves {exp_cost_ex - buy_price_ex:.1f} Ex on average vs "
                    f"crafting ({exp_cost_ex:.1f} Ex expected, {p80:.1f} Ex p80)."
                )
            else:
                rec = "craft"
                rationale = (
                    f"Craft it: expected cost ({exp_cost_ex:.1f} Ex, p80={p80:.1f} Ex) saves "
                    f"{buy_price_ex - exp_cost_ex:.1f} Ex vs market ({buy_price_ex:.1f} Ex)."
                )
            trade_cmp = TradeVsCraftComparison(
                recommendation=rec,
                market_price_exalted=round(buy_price_ex, 2),
                expected_craft_exalted=round(exp_cost_ex, 2),
                savings_exalted=round(diff, 2),
                trade_url=trade_url,
                rationale=rationale,
            )

        return CraftPlanResult(
            base_type=init_state.base.name,
            item_level=init_state.ilvl,
            league=league,
            target_descriptions=[s.label for s in specs],
            base_cost_exalted=round(base_cost_ex, 3),
            expected_cost_exalted=round(exp_cost_ex, 2),
            expected_cost_divine=round(exp_div, 3) if exp_div is not None else None,
            p50_cost_exalted=round(p50, 2),
            p80_cost_exalted=round(p80, 2),
            p95_cost_exalted=round(p95, 2),
            success_probability_per_base=round(p_succ_eff, 6),
            policy_steps=policy_steps,
            candidate_actions=candidate_actions,
            trade_vs_craft=trade_cmp,
            warnings=warnings,
        )
