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
    setup_side: AffixSide | None = None


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

    def _estimate_spec_normal_acq_costs(
        self,
        base_entry: BaseItemEntry,
        ilvl: int,
        specs: list[TargetSpec],
        price_book: PriceBook,
        base_cost_ex: float,
        *,
        use_fallback_prices: bool,
    ) -> tuple[list[float], float, list[float]]:
        norm_state = CraftingState(base=base_entry, ilvl=ilvl, rarity=ItemRarity.NORMAL)
        openers = [
            ("Orb of Transmutation", []),
            ("Greater Orb of Transmutation", []),
            ("Perfect Orb of Transmutation", []),
            ("Orb of Alchemy", []),
        ]
        acq_costs: list[float] = []
        best_probs: list[float] = []
        for sp in specs:
            best_c = float("inf")
            best_p = 0.0
            t_ids = set(sp.matching_mod_ids)
            for lv_n, om_l in openers:
                ev = self.engine.evaluate_action(norm_state, t_ids, lv_n, om_l)
                if ev.hit_probability <= 0:
                    continue
                c = price_book.action_cost(
                    ev.lever, ev.omens, use_category_fallback=use_fallback_prices
                )
                if c is None:
                    continue
                eff = (c + base_cost_ex) / ev.hit_probability
                if eff < best_c:
                    best_c = eff
                    best_p = ev.hit_probability
            if best_c == float("inf"):
                best_c = base_cost_ex + 150.0
                best_p = 1.0
            acq_costs.append(best_c)
            best_probs.append(best_p)

        all_ids = {mid for s in specs for mid in s.matching_mod_ids}
        best_any = float("inf")
        for lv_n, om_l in openers:
            ev_all = self.engine.evaluate_action(norm_state, all_ids, lv_n, om_l)
            if ev_all.hit_probability <= 0:
                continue
            c = price_book.action_cost(
                ev_all.lever, ev_all.omens, use_category_fallback=use_fallback_prices
            )
            if c is not None:
                best_any = min(best_any, (c + base_cost_ex) / ev_all.hit_probability)

        return acq_costs, best_any, best_probs

    def _find_crystallisation_setup_action(
        self,
        state: CraftingState,
        target_ids: set[str],
        protected_ids: set[str],
        price_book: PriceBook,
        *,
        use_fallback_prices: bool,
    ) -> tuple[float, _ChosenAction] | None:
        if state.rarity not in (ItemRarity.MAGIC, ItemRarity.RARE):
            return None

        best_pair: tuple[float, _ChosenAction] | None = None
        for cat in ("perfect_or_corrupted_essence", "alloy"):
            for lv in self.engine.list_levers(cat):
                if lv.name == "Essence of the Abyss":
                    continue
                forced = self.engine._lookup_forced_mod_entry(state.base, lv)
                if forced is None or forced.mod_id not in target_ids:
                    continue
                if any(f in state.blocked_families() for f in forced.families):
                    continue

                for rem_side, cryst_omen_name, reg_omen_name, ex_omen_name in (
                    (
                        AffixSide.PREFIX,
                        "Omen of Sinistral Crystallisation",
                        "Omen of Sinistral Coronation",
                        "Omen of Sinistral Exaltation",
                    ),
                    (
                        AffixSide.SUFFIX,
                        "Omen of Dextral Crystallisation",
                        "Omen of Dextral Coronation",
                        "Omen of Dextral Exaltation",
                    ),
                ):
                    # Side must have 0 non-fractured protected mods (0% Crystallisation collateral)
                    has_unfractured_protected_on_side = any(
                        m.affix_side == rem_side and not m.fractured and m.mod_id in protected_ids
                        for m in state.mods
                    )
                    if has_unfractured_protected_on_side:
                        continue
                    has_removable_junk_on_side = any(
                        m.affix_side == rem_side
                        and not m.fractured
                        and m.mod_id not in protected_ids
                        for m in state.mods
                    )
                    if has_removable_junk_on_side and state.rarity == ItemRarity.RARE:
                        continue

                    # Verify forced mod side has an open Rare slot after removing 1 mod on rem_side
                    if forced.affix_side != rem_side:
                        if forced.affix_side == AffixSide.PREFIX and not state.has_open_prefix(
                            ItemRarity.RARE
                        ):
                            continue
                        if forced.affix_side == AffixSide.SUFFIX and not state.has_open_suffix(
                            ItemRarity.RARE
                        ):
                            continue

                    cryst_om = self.engine.resolve_lever(cryst_omen_name)
                    swap_cost = price_book.action_cost(
                        lv, (cryst_om,), use_category_fallback=use_fallback_prices
                    )
                    if swap_cost is None:
                        continue

                    if state.rarity == ItemRarity.MAGIC:
                        reg_lv = self.engine.resolve_lever("Regal Orb")
                        reg_om = [self.engine.resolve_lever(reg_omen_name)]
                        setup_cost = price_book.action_cost(
                            reg_lv, reg_om, use_category_fallback=use_fallback_prices
                        )
                        if setup_cost is None:
                            continue
                        act = _ChosenAction(
                            lever_name=reg_lv.name,
                            omens=(reg_omen_name,),
                            cost_ex=setup_cost,
                            hit_prob=1.0,
                            collateral_prob=0.0,
                            label=self._format_action_label(reg_lv.name, [reg_omen_name]),
                            notes=f"Setup filler {rem_side.value} for {lv.name}",
                            setup_side=rem_side,
                        )
                    else:
                        if rem_side == AffixSide.PREFIX and not state.has_open_prefix(
                            ItemRarity.RARE
                        ):
                            continue
                        if rem_side == AffixSide.SUFFIX and not state.has_open_suffix(
                            ItemRarity.RARE
                        ):
                            continue
                        ex_lv = self.engine.resolve_lever("Exalted Orb")
                        other_open = (
                            state.has_open_suffix(ItemRarity.RARE)
                            if rem_side == AffixSide.PREFIX
                            else state.has_open_prefix(ItemRarity.RARE)
                        )
                        ex_oms = [self.engine.resolve_lever(ex_omen_name)] if other_open else []
                        ex_om_names = (ex_omen_name,) if other_open else ()
                        setup_cost = price_book.action_cost(
                            ex_lv, ex_oms, use_category_fallback=use_fallback_prices
                        )
                        if setup_cost is None:
                            continue
                        act = _ChosenAction(
                            lever_name=ex_lv.name,
                            omens=ex_om_names,
                            cost_ex=setup_cost,
                            hit_prob=1.0,
                            collateral_prob=0.0,
                            label=self._format_action_label(ex_lv.name, list(ex_om_names)),
                            notes=f"Setup filler {rem_side.value} for {lv.name}",
                            setup_side=rem_side,
                        )

                    combo_score = setup_cost + swap_cost
                    if best_pair is None or combo_score < best_pair[0]:
                        best_pair = (combo_score, act)

        return best_pair

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
        spec_acq_costs: list[float] | None = None,
        best_normal_any_score: float = float("inf"),
        enable_fracture_checkpoint: bool = False,
    ) -> _ChosenAction | None:
        unsatisfied = [sp for sp in specs if not sp.is_satisfied_by(state)]
        if not unsatisfied:
            return None

        protected_ids: set[str] = set()
        for sp in specs:
            if sp.is_satisfied_by(state):
                protected_ids.update(sp.matching_mod_ids)

        has_locked_target = any(m.fractured and m.mod_id in protected_ids for m in state.mods)
        eff_can_restart = can_restart and not has_locked_target

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
                if not m.fractured and any(m.mod_id in sp.matching_mod_ids for sp in specs)
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
            has_locked_target,
            can_whittle_safely,
            no_tags,
        )
        if sig in memo:
            return memo[sig]

        target_ids: set[str] = set()
        for sp in unsatisfied:
            target_ids.update(sp.matching_mod_ids)

        has_blocked_family = any(sp.is_family_blocked_by_junk(state) for sp in unsatisfied)
        if has_blocked_family and state.rarity == ItemRarity.MAGIC and eff_can_restart:
            memo[sig] = None
            return None

        # Compute sunk replacement value of currently hit targets on an unfractured base
        reacquire_cost = 0.0
        if spec_acq_costs is not None and protected_ids:
            hit_costs = [
                spec_acq_costs[i] for i, sp in enumerate(specs) if sp.is_satisfied_by(state)
            ]
            if len(hit_costs) == 1:
                reacquire_cost = hit_costs[0]
            elif hit_costs:
                reacquire_cost = hit_costs[0] * max(
                    2.0, sum(hit_costs[1:]) / max(1.0, base_cost_ex + 2.0)
                )

        # Optional 4-mod Fracturing Orb checkpoint on multi-mod endgame crafts
        if (
            enable_fracture_checkpoint
            and not state.has_fractured
            and len(protected_ids) > 0
            and len(unsatisfied) >= 2
        ):
            frac_lv = self.engine.resolve_lever("Fracturing Orb")
            frac_c = price_book.action_cost(frac_lv, (), use_category_fallback=use_fallback_prices)
            if frac_c is not None:
                if state.rarity == ItemRarity.MAGIC:
                    reg_lv = self.engine.resolve_lever("Regal Orb")
                    rc = price_book.action_cost(
                        reg_lv, (), use_category_fallback=use_fallback_prices
                    )
                    if rc is not None:
                        act_reg = _ChosenAction(
                            lever_name=reg_lv.name,
                            omens=(),
                            cost_ex=rc,
                            hit_prob=1.0,
                            collateral_prob=0.0,
                            label=reg_lv.name,
                            notes="Advance toward 4-mod Fracturing Orb checkpoint",
                            setup_side=AffixSide.PREFIX
                            if state.prefix_count <= state.suffix_count
                            else AffixSide.SUFFIX,
                        )
                        memo[sig] = act_reg
                        return act_reg
                elif state.rarity == ItemRarity.RARE and len(state.mods) < 4:
                    ex_lv = self.engine.resolve_lever("Exalted Orb")
                    ec = price_book.action_cost(
                        ex_lv, (), use_category_fallback=use_fallback_prices
                    )
                    if ec is not None:
                        act_pad = _ChosenAction(
                            lever_name=ex_lv.name,
                            omens=(),
                            cost_ex=ec,
                            hit_prob=1.0,
                            collateral_prob=0.0,
                            label=ex_lv.name,
                            notes="Pad to 4 mods for 25% Fracturing Orb lock",
                            setup_side=AffixSide.PREFIX
                            if state.has_open_prefix(ItemRarity.RARE)
                            else AffixSide.SUFFIX,
                        )
                        memo[sig] = act_pad
                        return act_pad
                elif state.rarity == ItemRarity.RARE and len(state.mods) == 4:
                    ev_fr = self.engine.evaluate_action(state, protected_ids, "Fracturing Orb")
                    if ev_fr.hit_probability > 0:
                        act_fr = _ChosenAction(
                            lever_name="Fracturing Orb",
                            omens=(),
                            cost_ex=frac_c,
                            hit_prob=ev_fr.hit_probability,
                            collateral_prob=0.0,
                            label="Fracturing Orb",
                            notes=ev_fr.notes,
                        )
                        memo[sig] = act_fr
                        return act_fr

        candidates = self._candidate_action_pairs(state, target_ids)
        best: _ChosenAction | None = None
        best_score = float("inf")

        # Include 2-step Crystallisation filler setup when deterministic Essence/Alloy applies
        cryst_setup = self._find_crystallisation_setup_action(
            state,
            target_ids,
            protected_ids,
            price_book,
            use_fallback_prices=use_fallback_prices,
        )
        if cryst_setup is not None:
            best_score, best = cryst_setup

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
            if protected_ids and res.collateral_remove_probability > 0 and eff_can_restart:
                continue

            cost = price_book.action_cost(
                res.lever, res.omens, use_category_fallback=use_fallback_prices
            )
            if cost is None:
                continue

            # Sunk-cost guard 1: On a 0-hit Magic/Rare item, never Regal/Exalt/Chaos if restarting
            # from Normal has strictly lower expected cost per hit!
            if (
                eff_can_restart
                and not protected_ids
                and state.rarity != ItemRarity.NORMAL
                and best_normal_any_score < float("inf")
            ):
                if (cost / res.hit_probability) > best_normal_any_score * 1.05:
                    continue

            # Sunk-cost guard 2: On an unfractured Rare item, never burn Chaos/Erasure/Whittling
            # when re-acquiring the current hit mods on a fresh base + clean slam is cheaper!
            if (
                eff_can_restart
                and state.rarity == ItemRarity.RARE
                and "Chaos" in lv_name
                and res.hit_probability < 0.5
            ):
                clean_restart_eff = (reacquire_cost + 25.0) / max(res.hit_probability, 1e-4)
                if (cost / res.hit_probability) > clean_restart_eff * 1.15 or cost > max(
                    150.0, reacquire_cost * 1.5
                ):
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

        if best is None and state.rarity == ItemRarity.RARE and not eff_can_restart:
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
        elif best is None and state.rarity == ItemRarity.RARE and state.has_desecrated:
            # Even when restartable, Omen of Light + Annul safely resets a missed Desecration
            res_l = self.engine.evaluate_action(
                state,
                target_ids,
                "Orb of Annulment",
                ["Omen of Light"],
                protected_mod_ids=protected_ids,
            )
            cost_l = price_book.action_cost(
                res_l.lever, res_l.omens, use_category_fallback=use_fallback_prices
            )
            if (
                res_l.hit_probability > 0
                and res_l.collateral_remove_probability == 0
                and cost_l is not None
                and cost_l <= max(150.0, reacquire_cost)
            ):
                best = _ChosenAction(
                    lever_name=res_l.lever.name,
                    omens=("Omen of Light",),
                    cost_ex=cost_l,
                    hit_prob=res_l.hit_probability,
                    collateral_prob=0.0,
                    label=self._format_action_label(res_l.lever.name, ["Omen of Light"]),
                    notes=res_l.notes,
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

        spec_acq_costs, best_normal_any_score, spec_best_probs = (
            self._estimate_spec_normal_acq_costs(
                init_state.base,
                init_state.ilvl,
                specs,
                price_book,
                base_cost_ex,
                use_fallback_prices=use_fallback_prices,
            )
        )

        # Decide endogenously whether a 4-mod Fracturing Orb checkpoint beats unfractured restarts
        enable_fracture_checkpoint = False
        if can_restart and not init_state.has_fractured and len(specs) >= 2:
            det_forced_ids = {
                mid
                for cat in ("essence", "perfect_or_corrupted_essence", "alloy")
                for lv in self.engine.list_levers(cat)
                if lv.name != "Essence of the Abyss"
                for mid in lv.forced_mods.values()
            }
            stoch_indices = [
                i
                for i, sp in enumerate(specs)
                if not (sp.matching_mod_ids & det_forced_ids)
                and all(e.source_pool == "normal" for e in sp.matching_entries)
            ]
            if len(stoch_indices) >= 2:
                frac_lv_probe = self.engine.resolve_lever("Fracturing Orb")
                frac_price = price_book.action_cost(
                    frac_lv_probe, (), use_category_fallback=use_fallback_prices
                )
                if frac_price is not None:
                    probs_s = sorted(max(1e-5, spec_best_probs[i]) for i in stoch_indices)
                    min_acq = min(spec_acq_costs[i] for i in stoch_indices)
                    joint_p = 1.0
                    for p_v in probs_s:
                        joint_p *= p_v
                    brute_est = (base_cost_ex + 5.0) / max(1e-12, joint_p)
                    chaos_lv = self.engine.resolve_lever("Greater Chaos Orb")
                    chaos_c = (
                        price_book.action_cost(
                            chaos_lv, (), use_category_fallback=use_fallback_prices
                        )
                        or 40.0
                    )
                    rem_prod_p = 1.0
                    for p_v in probs_s[1:]:
                        rem_prod_p *= p_v
                    post_frac_est = (
                        (chaos_c / probs_s[1])
                        if len(probs_s) == 2
                        else (chaos_c / max(1e-6, rem_prod_p * 6.0))
                    )
                    frac_est = 4.0 * (min_acq + frac_price + 6.0) + post_frac_est
                    if len(stoch_indices) >= 3 or brute_est > 1.5 * frac_est:
                        enable_fracture_checkpoint = True

        probe_state = init_state.clone()
        step_idx = 0
        analytical_success_prob = 1.0
        analytical_attempt_cost = base_cost_ex
        analytical_post_lock_cost = 0.0
        rem_specs = list(specs)

        from poe2brain.crafting.pool import CraftingMod

        # Build canonical policy playbook along the success path
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
                spec_acq_costs=spec_acq_costs,
                best_normal_any_score=best_normal_any_score,
                enable_fracture_checkpoint=enable_fracture_checkpoint,
            )
            if chosen is None:
                break
            step_idx += 1
            hit_count_now = len(specs) - len(unsat)
            locked_now = any(
                m.fractured and any(m.mod_id in s.matching_mod_ids for s in specs)
                for m in probe_state.mods
            )
            when_desc = (
                f"{probe_state.rarity.value.capitalize()} {probe_state.base.name} "
                f"({hit_count_now}/{len(specs)} targets hit"
                + (", fractured" if locked_now else "")
                + f", {probe_state.prefix_count}P/{probe_state.suffix_count}S)"
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
                    on_miss=(
                        "repeat_on_fractured_base"
                        if locked_now
                        else ("repair_or_whittle" if not can_restart else "restart_base_or_repair")
                    ),
                )
            )
            if locked_now or not can_restart:
                # Once target is fractured (or item cannot restart), misses stay on same base
                repair_overhead = 80.0 if chosen.hit_prob < 0.25 else 0.0
                analytical_post_lock_cost += (chosen.cost_ex + repair_overhead) / max(
                    1e-5, chosen.hit_prob
                )
            else:
                analytical_attempt_cost += analytical_success_prob * chosen.cost_ex
                analytical_success_prob *= chosen.hit_prob

            # Advance probe_state deterministically along the chosen transition
            if chosen.lever_name == "Fracturing Orb":
                for i_m, mod_cur in enumerate(probe_state.mods):
                    if any(mod_cur.mod_id in s.matching_mod_ids for s in specs):
                        probe_state.mods[i_m] = CraftingMod(
                            mod_id=mod_cur.mod_id,
                            mod_type=mod_cur.mod_type,
                            family_id=mod_cur.family_id,
                            families=mod_cur.families,
                            affix_side=mod_cur.affix_side,
                            display_tier=mod_cur.display_tier,
                            required_level=mod_cur.required_level,
                            stat_text=mod_cur.stat_text,
                            adds_tags=mod_cur.adds_tags,
                            mod_tags=mod_cur.mod_tags,
                            fractured=True,
                            is_desecrated=mod_cur.is_desecrated,
                            is_abyss_mark=mod_cur.is_abyss_mark,
                        )
                        break
            elif chosen.setup_side is not None:
                if "Regal" in chosen.lever_name:
                    probe_state.rarity = ItemRarity.RARE
                probe_state.mods.append(
                    CraftingMod(
                        mod_id=f"__setup_{step_idx}__",
                        mod_type=f"__setup_{step_idx}__",
                        family_id=f"__setup_{step_idx}__",
                        families=(f"__setup_{step_idx}__",),
                        affix_side=chosen.setup_side,
                        display_tier=8,
                        required_level=1,
                        stat_text=f"Setup {chosen.setup_side.value} modifier",
                    )
                )
            else:
                all_unsat_ids = {mid for s in unsat for mid in s.matching_mod_ids}
                lv_eval = self.engine.evaluate_action(
                    probe_state, all_unsat_ids, chosen.lever_name, list(chosen.omens)
                )
                if lv_eval.matching_tiers:
                    best_tier_entry = max(
                        lv_eval.matching_tiers, key=lambda e: (e.weight, -e.display_tier)
                    )
                    if "Transmutation" in chosen.lever_name or "Alchemy" in chosen.lever_name:
                        probe_state.rarity = (
                            ItemRarity.RARE if "Alchemy" in chosen.lever_name else ItemRarity.MAGIC
                        )
                    elif "Regal" in chosen.lever_name or lv_entry.category == "essence":
                        probe_state.rarity = ItemRarity.RARE
                    elif lv_entry.category in ("perfect_or_corrupted_essence", "alloy"):
                        side_c = next(
                            (
                                self.engine.resolve_lever(o).omen_side_constraint
                                for o in chosen.omens
                                if self.engine.resolve_lever(o).omen_side_constraint is not None
                            ),
                            None,
                        )
                        for idx_rm, m_rm in enumerate(probe_state.mods):
                            if (
                                not m_rm.fractured
                                and not any(m_rm.mod_id in s.matching_mod_ids for s in specs)
                                and (side_c is None or m_rm.affix_side == side_c)
                            ):
                                probe_state.mods.pop(idx_rm)
                                break
                    probe_state.mods.append(
                        CraftingMod.from_entry(
                            best_tier_entry,
                            is_desecrated=(lv_entry.category == "bone"),
                        )
                    )
                else:
                    break
            if step_idx >= 10:
                break

        # Concrete-item hybrid Monte Carlo simulation
        rng = random.Random(seed)
        all_target_ids = {mid for s in specs for mid in s.matching_mod_ids}

        def _run_single_base_attempt() -> tuple[bool, float]:
            st = init_state.clone()
            spend = base_cost_ex
            step_counter = 0
            while True:
                if all(s.is_satisfied_by(st) for s in specs):
                    return True, spend
                has_locked_hit = any(m.fractured and m.mod_id in all_target_ids for m in st.mods)
                # Abandon immediately if a Fracturing Orb missed the target modifier
                if can_restart and st.has_fractured and not has_locked_hit:
                    return False, spend

                max_steps = 600 if (has_locked_hit or not can_restart) else 14
                if step_counter >= max_steps:
                    return False, spend
                step_counter += 1

                act = self._pick_best_state_action(
                    st,
                    specs,
                    price_book,
                    base_cost_ex,
                    can_restart=can_restart,
                    use_fallback_prices=use_fallback_prices,
                    memo=memo,
                    spec_acq_costs=spec_acq_costs,
                    best_normal_any_score=best_normal_any_score,
                    enable_fracture_checkpoint=enable_fracture_checkpoint,
                )
                if act is None:
                    return False, spend
                if (
                    can_restart
                    and not has_locked_hit
                    and st.rarity == ItemRarity.MAGIC
                    and len(st.mods) >= 2
                    and not any(s.is_satisfied_by(st) for s in specs)
                ):
                    return False, spend
                if (
                    can_restart
                    and not has_locked_hit
                    and st.rarity == ItemRarity.RARE
                    and len(st.mods) >= (st.base.max_prefixes + st.base.max_suffixes)
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
            succ_pool = succ_attempt_costs or [analytical_attempt_cost + analytical_post_lock_cost]
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
