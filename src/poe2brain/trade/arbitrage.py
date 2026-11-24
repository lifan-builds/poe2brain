from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from poe2brain.config import BUNDLED_DATA_DIR, DEFAULT_LEAGUE, get_gamedata_db_path
from poe2brain.knowledge.db import GamedataDB
from poe2brain.models.common import Poe2Model
from poe2brain.trade.economy import (
    POENINJA_EXCHANGE_TYPES,
    EconomyService,
    _LeagueSnapshot,
    _MergedMarketEntry,
    normalize_market_slug,
)
from poe2brain.trade.trade2_client import ExchangeOffer, Trade2Client

DEFAULT_EXCHANGE_TRIANGLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("divine", "exalted"),
    ("divine", "chaos"),
    ("chaos", "exalted"),
)

TIERED_RUNE_BASES: tuple[str, ...] = (
    "desert",
    "glacial",
    "storm",
    "iron",
    "body",
    "mind",
    "rebirth",
    "inspiration",
    "stone",
    "vision",
    "robust",
    "adept",
    "resolve",
    "ward",
    "charging",
)

# Verified 15,000-sample PoE2 Reforging Bench Catalyst drop weights (non-uniform).
EMPIRICAL_CATALYST_REFORGE_WEIGHTS: dict[str, float] = {
    "neural": 13.58,
    "carapace": 13.26,
    "flesh": 13.16,
    "adaptive": 11.08,
    "necrotic": 8.50,
    "tuls": 8.40,
    "eshs": 8.32,
    "xophs": 8.04,
    "uul-netols": 6.90,
    "sibilant": 5.70,
    "reaver": 4.78,
    "chayulas": 4.08,
    "skittering": 2.70,
}

SPLINTER_OR_SHARD_SLUGS: frozenset[str] = frozenset(
    {
        "simulacrum-splinter",
        "breach-splinter",
        "petition-splinter",
        "runic-splinter",
        "artificers-shard",
        "regal-shard",
        "chance-shard",
        "transmutation-shard",
        "raven-touched-shard",
        "verisium",
    }
)


class ArbitrageMechanism(StrEnum):
    ORDER_BOOK_SPREAD = "order_book_spread"
    TRIANGULAR_CYCLE = "triangular_cycle"
    CROSS_DENOMINATION = "cross_denomination"
    DETERMINISTIC_CONVERSION = "deterministic_conversion"
    FRAGMENT_SET_ASSEMBLY = "fragment_set_assembly"


class ArbitrageTrapWarning(Poe2Model):
    kind: str
    item_or_pair: str
    naive_apparent_spread_pct: float | None = None
    true_executable_spread_pct: float | None = None
    explanation: str


class OrderBookQuote(Poe2Model):
    pair: str
    base_currency: str
    quote_currency: str
    best_bid_quote_per_base: float | None = None
    best_ask_quote_per_base: float | None = None
    bid_stock: int = 0
    ask_stock: int = 0
    bid_account: str | None = None
    ask_account: str | None = None
    crossed_taker_profit_pct: float | None = None
    maker_spread_pct: float | None = None
    alva_vwap_quote_per_base: float | None = None
    flags: list[str] = Field(default_factory=list)


class ArbitrageOpportunity(Poe2Model):
    id: str
    title: str
    mechanism: ArbitrageMechanism
    executable_in_hideout: bool = True
    is_deterministic: bool = True
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    input_cost_exalted: float
    output_value_exalted: float
    gross_profit_exalted: float
    gross_profit_divine: float
    roi_pct: float
    units_traded_per_cycle: int
    gold_per_cycle_est: int
    profit_ex_per_10k_gold: float
    min_leg_volume_divine: float
    min_leg_units_traded: float = 0.0
    primary_denomination: str = "divine"
    steps: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class ArbitrageScanReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    divine_exalted: float
    chaos_exalted: float
    chaos_per_divine: float
    scout_divine_exalted: float | None = None
    ninja_divine_exalted: float | None = None
    numeraire_lag_pct: float | None = None
    max_investment_divine: float | None = None
    max_gold_per_cycle: int | None = None
    opportunities: list[ArbitrageOpportunity] = Field(default_factory=list)
    order_books: list[OrderBookQuote] = Field(default_factory=list)
    trap_warnings: list[ArbitrageTrapWarning] = Field(default_factory=list)
    execution_rules: list[str] = Field(default_factory=list)


class CompoundingRung(Poe2Model):
    rung_index: int
    opportunity_id: str
    title: str
    mechanism: ArbitrageMechanism
    is_deterministic: bool
    unit_cost_exalted: float
    unit_profit_exalted: float
    unit_gold_est: int
    cycles_executed: int
    starting_bankroll_exalted: float
    starting_bankroll_divine: float
    ending_bankroll_exalted: float
    ending_bankroll_divine: float
    gold_spent_on_rung: int
    remaining_gold_after_rung: int
    profit_ex_per_10k_gold: float
    primary_denomination: str = "divine"
    steps: list[str] = Field(default_factory=list)
    rationale: str = ""


class CompoundingLadderReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    divine_exalted: float
    chaos_exalted: float
    starting_exalted: float
    starting_divine: float
    gold_budget: int
    target_divine: float
    target_exalted: float
    final_bankroll_exalted: float
    final_bankroll_divine: float
    total_profit_exalted: float
    total_profit_divine: float
    total_gold_spent: int
    remaining_gold: int
    target_reached: bool
    estimated_extra_gold_needed: int = 0
    rungs: list[CompoundingRung] = Field(default_factory=list)
    skipped_high_capital_or_gold_plays: list[str] = Field(default_factory=list)
    strategy_notes: list[str] = Field(default_factory=list)


def estimate_alva_gold_per_unit(slug: str, unit_exalted: float) -> int:
    """Estimate Alva's Currency Exchange Gold tax per unit traded in PoE2 0.5.5.

    Grounded in empirical PoE2DB / in-game Currency Exchange tax brackets:
    - Splinters / Shards / raw Verisium: ~30 Gold/unit (massive stacks burn Gold fast!)
    - Basic common orbs (< 15 Ex, e.g. Exalted, Regal, Alch, Vaal, Catalysts): ~110 Gold/unit
    - Mid-tier currencies (15–150 Ex, e.g. Chaos, Annul, Greater Orbs): ~320 Gold/unit
    - Upper-mid items (150–1,000 Ex, e.g. Refined Catalysts, Masterwork/Extraction): ~650 Gold/unit
    - High-tier items (1,000–25,000 Ex, e.g. Divine, Omens, Keys, Perfect Runes): ~1,100 Gold/unit
    - Apex chase items (> 25,000 Ex, e.g. Lineage Supports, Lock, Mirror): ~2,500 Gold/unit
    """
    norm = normalize_market_slug(slug)
    if norm in SPLINTER_OR_SHARD_SLUGS or norm.endswith("-splinter") or norm.endswith("-shard"):
        return 30
    if norm in ("divine", "divine-orb"):
        return 1000
    if norm in ("chaos", "chaos-orb"):
        return 280
    if norm in ("exalted", "ex", "exalted-orb"):
        return 100
    if unit_exalted >= 25_000.0:
        return 2500
    if unit_exalted >= 1_000.0:
        return 1100
    if unit_exalted >= 150.0:
        return 650
    if unit_exalted >= 15.0:
        return 320
    return 110


def estimate_leg_gold(slug: str, quantity: float, unit_exalted: float) -> int:
    if quantity <= 0:
        return 0
    return max(25, int(math.ceil(quantity * estimate_alva_gold_per_unit(slug, unit_exalted))))


def compute_profit_per_10k_gold(profit_exalted: float, gold_cost: int) -> float:
    if gold_cost <= 0:
        return 0.0
    return round((profit_exalted / gold_cost) * 10_000.0, 2)


class ArbitrageScanner:
    """Executable PoE2 Currency Exchange & Conversion Arbitrage Scanner (Patch 0.5.5).

    Fixes the core flaws of naive `poe2scout` vs `poe.ninja` spread comparisons:
    1. Strips numeraire index lag (`scout_divine_exalted` vs `ninja_divine_exalted`) before
       evaluating cross-denomination spreads, flagging illusory timestamp-lag spreads.
    2. Filters live `/api/trade2/exchange` offers to online/stocked accounts
       (`status_option="online"`), rejecting misclick stash tags and wide-spread shark traps.
    3. Accounts for Alva's per-unit Currency Exchange Gold tax (`gold_per_cycle_est` and
       `profit_ex_per_10k_gold`) so high-unit Exalted/Splinter churn is penalized accurately
       relative to low-unit Divine/Chaos/Omen/Key legs.
    4. Evaluates verified Patch 0.5.5 deterministic & weighted-EV conversions against
       `data/poe2_0.5.5.sqlite` (`base_items`, `augments`, `runeforge_recipes`) and
       `data/ee2/ee2_remnants.json` while blocking impossible or legacy traps.
    """

    def __init__(
        self,
        *,
        economy: EconomyService | None = None,
        trade2: Trade2Client | None = None,
        db: GamedataDB | Path | str | None = None,
        remnants_path: Path | None = None,
    ) -> None:
        self._owns_economy = economy is None
        self._owns_trade2 = trade2 is None
        self.economy = economy or EconomyService()
        self.trade2 = trade2 or Trade2Client(economy=self.economy)
        self.db = db if isinstance(db, GamedataDB) else GamedataDB(db or get_gamedata_db_path())
        self.remnants_path = remnants_path or (BUNDLED_DATA_DIR / "ee2" / "ee2_remnants.json")

    def close(self) -> None:
        if self._owns_trade2:
            self.trade2.close()
        if self._owns_economy:
            self.economy.close()

    def __enter__(self) -> ArbitrageScanner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def scan(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 2.0,
        min_roi_pct: float = 5.0,
        max_investment_divine: float | None = None,
        max_gold_per_cycle: int | None = None,
        include_live_exchange: bool = True,
        exchange_pairs: Sequence[tuple[str, str]] = DEFAULT_EXCHANGE_TRIANGLE_PAIRS,
        verify_pair_history_top_n: int = 3,
        include_encounter_gated: bool = True,
        limit: int = 25,
        force_refresh: bool = False,
    ) -> ArbitrageScanReport:
        """Primary entrypoint for scanning executable PoE2 arbitrage opportunities with optional
        `max_investment_divine` capital ceiling and `max_gold_per_cycle` Gold tax cap.
        """
        return self.scan_all(
            league=league,
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            max_investment_divine=max_investment_divine,
            max_gold_per_cycle=max_gold_per_cycle,
            include_live_exchange=include_live_exchange,
            exchange_pairs=exchange_pairs,
            verify_pair_history_top_n=verify_pair_history_top_n,
            include_encounter_gated=include_encounter_gated,
            limit=limit,
            force_refresh=force_refresh,
        )

    def scan_all(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 2.0,
        min_roi_pct: float = 5.0,
        max_investment_divine: float | None = None,
        max_gold_per_cycle: int | None = None,
        include_live_exchange: bool = True,
        exchange_pairs: Sequence[tuple[str, str]] = DEFAULT_EXCHANGE_TRIANGLE_PAIRS,
        verify_pair_history_top_n: int = 3,
        include_encounter_gated: bool = True,
        limit: int = 25,
        force_refresh: bool = False,
    ) -> ArbitrageScanReport:
        snap = self.economy._ensure_snapshot(league, force_refresh=force_refresh)
        rates = snap.rates
        div_ex = rates.divine_exalted
        max_cost_ex = max_investment_divine * div_ex if max_investment_divine is not None else None

        div_entry = snap.entries_by_id.get("divine")
        scout_div_ex = (
            div_entry.scout_exalted
            if (div_entry is not None and div_entry.scout_exalted and div_entry.scout_exalted > 0)
            else rates.divine_exalted
        )
        ninja_div_ex = (
            div_entry.ninja_exalted
            if (div_entry is not None and div_entry.ninja_exalted and div_entry.ninja_exalted > 0)
            else (rates.ninja_divine_exalted or rates.divine_exalted)
        )
        lag_pct = (
            round(((scout_div_ex / ninja_div_ex) - 1.0) * 100.0, 2)
            if (scout_div_ex > 0 and ninja_div_ex > 0)
            else None
        )

        opportunities: list[ArbitrageOpportunity] = []
        order_books: list[OrderBookQuote] = []
        traps: list[ArbitrageTrapWarning] = []

        # (A) + (B1): Live trade2/exchange order books, Alva market-making & triangular cycles
        if include_live_exchange and exchange_pairs:
            ob_quotes, ob_opps, ob_traps = self.scan_live_order_books_and_triangles(
                league=league,
                pairs=exchange_pairs,
                snap=snap,
            )
            order_books.extend(ob_quotes)
            opportunities.extend(ob_opps)
            traps.extend(ob_traps)

        alva_vol_floor = (
            max(8.0, min_volume_divine)
            if (max_investment_divine is not None and max_investment_divine < 3.0)
            else max(25.0, min_volume_divine)
        )
        mm_opps, mm_traps = self.scan_alva_spread_making(
            league=league,
            min_volume_divine=alva_vol_floor,
            include_low_capital=(max_investment_divine is not None and max_investment_divine < 3.0),
            snap=snap,
        )
        opportunities.extend(mm_opps)
        traps.extend(mm_traps)

        # (B2): Numeraire-adjusted cross-denomination spreads + pair history verification
        cd_vol_floor = (
            max(2.0, min_volume_divine)
            if (max_investment_divine is not None and max_investment_divine < 3.0)
            else max(10.0, min_volume_divine)
        )
        cd_opps, cd_traps = self.scan_cross_denomination_spreads(
            league=league,
            min_volume_divine=cd_vol_floor,
            min_spread_pct=max(12.0, min_roi_pct),
            verify_pair_history_top_n=verify_pair_history_top_n,
            snap=snap,
        )
        opportunities.extend(cd_opps)
        traps.extend(cd_traps)

        # (C): Deterministic & empirical-weighted 3-to-1 / Socket Extraction / Runeforge conversions
        conv_opps, conv_traps = self.scan_deterministic_conversions(
            league=league,
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            include_encounter_gated=include_encounter_gated,
            snap=snap,
        )
        opportunities.extend(conv_opps)
        traps.extend(conv_traps)

        # (D): Bulk Splinter-Stack & Pinnacle Set Assembly
        frag_opps, frag_traps = self.scan_fragment_and_splinter_sets(
            league=league,
            min_volume_divine=min_volume_divine,
            min_roi_pct=min_roi_pct,
            snap=snap,
        )
        opportunities.extend(frag_opps)
        traps.extend(frag_traps)

        filtered = [
            opp
            for opp in opportunities
            if opp.roi_pct >= min_roi_pct
            and opp.gross_profit_exalted > 0
            and (include_encounter_gated or opp.executable_in_hideout)
            and (max_cost_ex is None or opp.input_cost_exalted <= max_cost_ex + 1e-6)
            and (max_gold_per_cycle is None or opp.gold_per_cycle_est <= max_gold_per_cycle)
        ]

        # Rank by hideout executability -> determinism -> liquidity-weighted Gold & Exalted profit
        def _rank_key(opp: ArbitrageOpportunity) -> tuple[int, float]:
            tier = (
                2
                if (opp.executable_in_hideout and opp.is_deterministic)
                else (1 if opp.executable_in_hideout else 0)
            )
            eff_vol = max(opp.min_leg_volume_divine, opp.min_leg_units_traded * 0.25)
            liq_factor = min(1.0, math.log10(max(1.2, eff_vol + 1.0)) / 1.3)
            gold_eff_bonus = min(opp.gross_profit_exalted, opp.profit_ex_per_10k_gold * 0.5)
            score = (opp.gross_profit_exalted * 0.7 + gold_eff_bonus * 0.3) * max(0.25, liq_factor)
            return (tier, round(score, 4))

        filtered.sort(key=_rank_key, reverse=True)
        deduped_traps = _deduplicate_traps(traps)

        rules = [
            (
                f"Never trade raw poe2scout vs poe.ninja Exalted spreads: poe2scout's Divine "
                f"index ({scout_div_ex:.1f} Ex) and poe.ninja's ({ninja_div_ex:.1f} Ex) currently "
                f"differ by {lag_pct or 0.0:+.1f}% due to VWAP window lag, creating fake spreads "
                f"on Divine-quoted items."
            ),
            (
                f"Price and execute high-value flips in Divine or Chaos (1 Div = {div_ex:.1f} Ex = "
                f"{rates.chaos_per_divine:.2f} Chaos), NOT Exalted stacks: flipping 1 Div <-> "
                f"{int(round(div_ex))} Ex burns "
                f"~{estimate_leg_gold('exalted', div_ex, 1.0) + 1000:,} Gold per round-trip, "
                f"whereas 1-to-3 unit Omen/Key/Perfect-Rune trades cost ~2,000–5,000 Gold total."
            ),
            (
                "Avoid public /api/trade2/exchange taker orders without comparing against Alva's "
                "in-game Currency Exchange: 99%+ of PoE2 currency volume sits on Alva, while "
                "public stash-tab exchange offers are thin and dominated by shark spreads (e.g. "
                "100 Ex bid / 480 Ex ask per Divine) and misclick tags."
            ),
            (
                "Amortize Orb of Extraction across 2-socket (or 3-socket) trash bases when "
                "upgrading Greater Runes with Masterwork Runes: Orb of Extraction retrieves ALL "
                "socketed Perfect Runes simultaneously while destroying the disposable host item."
            ),
            (
                "Require real poe.ninja exchange volume (volumePrimaryValue >= 2–5 Div) on BOTH "
                "input and output legs before committing capital; zero-volume items fall back to "
                "single-listing poe2scout outliers (e.g. 414 Ex Transmutation Shards)."
            ),
        ]

        return ArbitrageScanReport(
            league=league,
            divine_exalted=round(rates.divine_exalted, 4),
            chaos_exalted=round(rates.chaos_exalted, 4),
            chaos_per_divine=round(rates.chaos_per_divine, 4),
            scout_divine_exalted=round(scout_div_ex, 4),
            ninja_divine_exalted=round(ninja_div_ex, 4),
            numeraire_lag_pct=lag_pct,
            max_investment_divine=max_investment_divine,
            max_gold_per_cycle=max_gold_per_cycle,
            opportunities=filtered[: max(1, limit)],
            order_books=order_books,
            trap_warnings=deduped_traps,
            execution_rules=rules,
        )

    # ------------------------------------------------------------------
    # (A) & (B1): Live `/api/trade2/exchange` Order Books & Triangles
    # ------------------------------------------------------------------

    def scan_live_order_books_and_triangles(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        pairs: Sequence[tuple[str, str]] = DEFAULT_EXCHANGE_TRIANGLE_PAIRS,
        min_stock: int = 2,
        snap: _LeagueSnapshot | None = None,
    ) -> tuple[list[OrderBookQuote], list[ArbitrageOpportunity], list[ArbitrageTrapWarning]]:
        resolved_snap = snap or self.economy._ensure_snapshot(league)
        rates = resolved_snap.rates
        div_ex = rates.divine_exalted

        quotes: list[OrderBookQuote] = []
        opportunities: list[ArbitrageOpportunity] = []
        traps: list[ArbitrageTrapWarning] = []

        # Directed rate map: (from_curr, to_curr) -> (to_received_per_1_from_spent, stock, account)
        directed_rates: dict[tuple[str, str], tuple[float, int, str]] = {}

        for base_c, quote_c in pairs:
            alva_ref = self._reference_pair_rate(base_c, quote_c, resolved_snap)

            # Sell 1 `base_c` for `quote_c` (`have=base_c, want=quote_c`) -> Bid for base_c
            sell_resp = self.trade2.exchange(
                have=base_c,
                want=quote_c,
                league=league,
                status_option="online",
            )
            # Buy 1 `base_c` paying `quote_c` (`have=quote_c, want=base_c`) -> Ask for base_c
            buy_resp = self.trade2.exchange(
                have=quote_c,
                want=base_c,
                league=league,
                status_option="online",
            )

            flags: list[str] = []
            bid_offer, bid_flags = _select_clean_offer(
                sell_resp.offers,
                side="sell_base",
                alva_quote_per_base=alva_ref,
                min_stock=min_stock,
            )
            ask_offer, ask_flags = _select_clean_offer(
                buy_resp.offers,
                side="buy_base",
                alva_quote_per_base=alva_ref,
                min_stock=min_stock,
            )
            for fl in (*bid_flags, *ask_flags):
                if fl not in flags:
                    flags.append(fl)

            best_bid: float | None = None
            bid_stock = 0
            bid_acct: str | None = None
            if bid_offer is not None and bid_offer.have_amount > 0:
                best_bid = round(bid_offer.want_amount / bid_offer.have_amount, 6)
                bid_stock = bid_offer.stock
                bid_acct = bid_offer.account_name
                directed_rates[(base_c, quote_c)] = (best_bid, bid_stock, bid_acct)

            best_ask: float | None = None
            ask_stock = 0
            ask_acct: str | None = None
            if ask_offer is not None and ask_offer.want_amount > 0:
                best_ask = round(ask_offer.have_amount / ask_offer.want_amount, 6)
                ask_stock = ask_offer.stock
                ask_acct = ask_offer.account_name
                directed_rates[(quote_c, base_c)] = (
                    round(ask_offer.want_amount / ask_offer.have_amount, 6),
                    ask_stock,
                    ask_acct,
                )

            if not sell_resp.offers or not buy_resp.offers:
                flags.append("one_or_both_sides_empty_on_public_stash_exchange")

            if bid_acct and ask_acct and bid_acct.lower() == ask_acct.lower():
                flags.append("same_account_two_sided_maker")

            crossed_pct: float | None = None
            maker_pct: float | None = None
            if best_bid is not None and best_ask is not None and best_ask > 0 and best_bid > 0:
                crossed_pct = round(((best_bid / best_ask) - 1.0) * 100.0, 2)
                maker_pct = round(((best_ask / best_bid) - 1.0) * 100.0, 2)

                if alva_ref and best_bid < 0.5 * alva_ref:
                    flags.append("shark_bid_trap")
                    traps.append(
                        ArbitrageTrapWarning(
                            kind="shark_or_misclick_book",
                            item_or_pair=f"{base_c}/{quote_c}",
                            naive_apparent_spread_pct=maker_pct,
                            true_executable_spread_pct=crossed_pct,
                            explanation=(
                                f"Public /api/trade2/exchange best bid for {base_c}->{quote_c} is "
                                f"only {best_bid:g} {quote_c} (account {bid_acct!r}) vs Alva "
                                f"Currency Exchange VWAP {alva_ref:.2f} {quote_c} — never sell "
                                f"into public stash shark bids; route through Alva."
                            ),
                        )
                    )

                # Executable crossed order book (`best_bid > best_ask`)
                if crossed_pct > 1.5:
                    base_unit_ex = self._unit_exalted(base_c, resolved_snap) or 1.0
                    quote_unit_ex = self._unit_exalted(quote_c, resolved_snap) or 1.0
                    cost_ex = best_ask * quote_unit_ex
                    out_ex = best_bid * quote_unit_ex
                    prof_ex = out_ex - cost_ex
                    gold_est = estimate_leg_gold(
                        quote_c, best_ask, quote_unit_ex
                    ) + estimate_leg_gold(base_c, 1.0, base_unit_ex)
                    units = int(math.ceil(best_ask)) + 1
                    if prof_ex > 0:
                        opportunities.append(
                            ArbitrageOpportunity(
                                id=f"book_cross:{base_c}_{quote_c}",
                                title=(
                                    f"Crossed Live Exchange Book: "
                                    f"{quote_c} -> {base_c} -> {quote_c}"
                                ),
                                mechanism=ArbitrageMechanism.ORDER_BOOK_SPREAD,
                                executable_in_hideout=True,
                                is_deterministic=True,
                                inputs=[f"{best_ask:g}x {quote_c}"],
                                outputs=[f"{best_bid:g}x {quote_c}"],
                                input_cost_exalted=round(cost_ex, 2),
                                output_value_exalted=round(out_ex, 2),
                                gross_profit_exalted=round(prof_ex, 2),
                                gross_profit_divine=round(prof_ex / div_ex, 4),
                                roi_pct=crossed_pct,
                                units_traded_per_cycle=units,
                                gold_per_cycle_est=gold_est,
                                profit_ex_per_10k_gold=compute_profit_per_10k_gold(
                                    prof_ex, gold_est
                                ),
                                min_leg_volume_divine=round(
                                    min(bid_stock * quote_unit_ex, ask_stock * base_unit_ex)
                                    / div_ex,
                                    2,
                                ),
                                primary_denomination=base_c,
                                steps=[
                                    (
                                        f"Buy 1 {base_c} for {best_ask:g} {quote_c} from "
                                        f"{ask_acct} (stock {ask_stock})."
                                    ),
                                    (
                                        f"Sell 1 {base_c} for {best_bid:g} {quote_c} to "
                                        f"{bid_acct} (stock {bid_stock})."
                                    ),
                                ],
                                risk_notes=[
                                    "Public stash exchange whispers require online counterparty "
                                    "response; verify stock before whispering."
                                ],
                            )
                        )

            # Check Stash-to-Alva discount (`best_ask < alva_ref * 0.94`)
            if alva_ref and best_ask is not None and best_ask < alva_ref * 0.94:
                quote_unit_ex = self._unit_exalted(quote_c, resolved_snap) or 1.0
                base_unit_ex = self._unit_exalted(base_c, resolved_snap) or 1.0
                cost_ex = best_ask * quote_unit_ex
                out_ex = alva_ref * quote_unit_ex
                prof_ex = out_ex - cost_ex
                gold_est = estimate_leg_gold(base_c, 1.0, base_unit_ex)
                if prof_ex > 1.0:
                    opportunities.append(
                        ArbitrageOpportunity(
                            id=f"stash_to_alva:{base_c}_{quote_c}",
                            title=(
                                f"Stash-to-Alva Discount: Buy {base_c} in {quote_c} -> Sell on Alva"
                            ),
                            mechanism=ArbitrageMechanism.ORDER_BOOK_SPREAD,
                            executable_in_hideout=True,
                            is_deterministic=True,
                            inputs=[f"{best_ask:g}x {quote_c}"],
                            outputs=[f"{alva_ref:.2f}x {quote_c} (via Alva)"],
                            input_cost_exalted=round(cost_ex, 2),
                            output_value_exalted=round(out_ex, 2),
                            gross_profit_exalted=round(prof_ex, 2),
                            gross_profit_divine=round(prof_ex / div_ex, 4),
                            roi_pct=round(((out_ex / cost_ex) - 1.0) * 100.0, 2),
                            units_traded_per_cycle=int(math.ceil(best_ask)) + 1,
                            gold_per_cycle_est=gold_est,
                            profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                            min_leg_volume_divine=round((ask_stock * base_unit_ex) / div_ex, 2),
                            primary_denomination=quote_c,
                            steps=[
                                (
                                    f"Buy 1 {base_c} for {best_ask:g} {quote_c} on trade2/exchange "
                                    f"({ask_acct}, stock {ask_stock})."
                                ),
                                (
                                    f"Flip 1 {base_c} on Alva's Currency Exchange at "
                                    f"~{alva_ref:.2f} {quote_c}."
                                ),
                            ],
                            risk_notes=[
                                "Confirm seller replies on public trade2/exchange before scaling."
                            ],
                        )
                    )

            quotes.append(
                OrderBookQuote(
                    pair=f"{base_c}/{quote_c}",
                    base_currency=base_c,
                    quote_currency=quote_c,
                    best_bid_quote_per_base=best_bid,
                    best_ask_quote_per_base=best_ask,
                    bid_stock=bid_stock,
                    ask_stock=ask_stock,
                    bid_account=bid_acct,
                    ask_account=ask_acct,
                    crossed_taker_profit_pct=crossed_pct,
                    maker_spread_pct=maker_pct,
                    alva_vwap_quote_per_base=round(alva_ref, 4) if alva_ref else None,
                    flags=flags,
                )
            )

        # Evaluate 3-leg Triangular Cycles anchored on the highest-denomination currency (`1x div`)
        currencies = sorted(
            {c for pair in pairs for c in pair},
            key=lambda curr: self._unit_exalted(curr, resolved_snap) or 0.0,
            reverse=True,
        )
        if len(currencies) >= 3:
            c0, c1, c2 = currencies[0], currencies[1], currencies[2]
            for cycle in ((c0, c1, c2), (c0, c2, c1)):
                a, b, c = cycle
                leg1 = directed_rates.get((a, b))
                leg2 = directed_rates.get((b, c))
                leg3 = directed_rates.get((c, a))
                if not (leg1 and leg2 and leg3):
                    continue
                mult = leg1[0] * leg2[0] * leg3[0]
                roi_pct = round((mult - 1.0) * 100.0, 2)
                a_unit_ex = self._unit_exalted(a, resolved_snap) or 1.0
                b_unit_ex = self._unit_exalted(b, resolved_snap) or 1.0
                c_unit_ex = self._unit_exalted(c, resolved_snap) or 1.0
                amt_b = leg1[0]
                amt_c = amt_b * leg2[0]
                amt_a_out = amt_c * leg3[0]
                gold_est = (
                    estimate_leg_gold(a, 1.0, a_unit_ex)
                    + estimate_leg_gold(b, amt_b, b_unit_ex)
                    + estimate_leg_gold(c, amt_c, c_unit_ex)
                )
                units = 1 + int(math.ceil(amt_b)) + int(math.ceil(amt_c))
                prof_ex = (amt_a_out - 1.0) * a_unit_ex
                if roi_pct > 1.5 and prof_ex > 0:
                    min_liq_div = min(
                        (leg1[1] * b_unit_ex) / div_ex,
                        (leg2[1] * c_unit_ex) / div_ex,
                        (leg3[1] * a_unit_ex) / div_ex,
                    )
                    opportunities.append(
                        ArbitrageOpportunity(
                            id=f"triangle:{a}_{b}_{c}",
                            title=f"Triangular Exchange Cycle: {a} -> {b} -> {c} -> {a}",
                            mechanism=ArbitrageMechanism.TRIANGULAR_CYCLE,
                            executable_in_hideout=True,
                            is_deterministic=True,
                            inputs=[f"1x {a}"],
                            outputs=[f"{amt_a_out:.4f}x {a}"],
                            input_cost_exalted=round(a_unit_ex, 2),
                            output_value_exalted=round(amt_a_out * a_unit_ex, 2),
                            gross_profit_exalted=round(prof_ex, 2),
                            gross_profit_divine=round(prof_ex / div_ex, 4),
                            roi_pct=roi_pct,
                            units_traded_per_cycle=units,
                            gold_per_cycle_est=gold_est,
                            profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                            min_leg_volume_divine=round(min_liq_div, 2),
                            primary_denomination=a,
                            steps=[
                                f"Leg 1: Trade 1 {a} -> {amt_b:.3f} {b} ({leg1[2]}).",
                                f"Leg 2: Trade {amt_b:.3f} {b} -> {amt_c:.3f} {c} ({leg2[2]}).",
                                f"Leg 3: Trade {amt_c:.3f} {c} -> {amt_a_out:.4f} {a} ({leg3[2]}).",
                            ],
                            risk_notes=[
                                f"Cycle moves {units} currency units "
                                f"(~{gold_est:,} Gold if routed on Alva)."
                            ],
                        )
                    )

        return quotes, opportunities, traps

    def scan_alva_spread_making(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 150.0,
        assumed_maker_capture_pct: float = 6.0,
        include_low_capital: bool = False,
        snap: _LeagueSnapshot | None = None,
    ) -> tuple[list[ArbitrageOpportunity], list[ArbitrageTrapWarning]]:
        """Evaluate Gold-efficient asynchronous limit-order market-making on Alva's Currency
        Exchange, contrasting high-denomination items (`Divine`/`Chaos` pairs) against the
        high-Gold-burn `Divine <-> Exalted` trap.
        """
        resolved_snap = snap or self.economy._ensure_snapshot(league)
        div_ex = resolved_snap.rates.divine_exalted
        chaos_per_div = resolved_snap.rates.chaos_per_divine

        opportunities: list[ArbitrageOpportunity] = []
        traps: list[ArbitrageTrapWarning] = []

        # Explicitly quantify the Exalted Gold-burn trap on 1 Divine <-> N Exalted
        ex_roundtrip_gold = estimate_leg_gold("exalted", div_ex, 1.0) + estimate_leg_gold(
            "divine", 1.0, div_ex
        )
        ex_spread_profit = div_ex * 0.02  # typical tight ~2% spread on Div/Ex
        ex_per_10k = compute_profit_per_10k_gold(ex_spread_profit, ex_roundtrip_gold)
        traps.append(
            ArbitrageTrapWarning(
                kind="gold_burn_trap",
                item_or_pair="divine/exalted",
                naive_apparent_spread_pct=2.0,
                true_executable_spread_pct=round((ex_spread_profit / div_ex) * 100.0, 2),
                explanation=(
                    f"Flipping 1 Divine <-> {div_ex:.0f} Exalted moves ~{int(round(div_ex)) + 1} "
                    f"currency units and burns ~{ex_roundtrip_gold:,} Gold per round-trip "
                    f"(only ~{ex_per_10k:.1f} Ex per 10k Gold at a 2% spread). Flip Divine <-> "
                    f"Chaos ({chaos_per_div:.1f} units) or 5–30 Div Ritual Omens / Abyssal Bones "
                    f"/ Refined Catalysts instead."
                ),
            )
        )

        # High-liquidity single-unit items priced between 3 Div and 60 Div traded in Divine/Chaos
        candidates: list[_MergedMarketEntry] = []
        micro_candidates: list[_MergedMarketEntry] = []
        for entry in resolved_snap.entries_by_id.values():
            vol = entry.volume_divine or 0.0
            if (
                entry.quote is None
                or vol < min_volume_divine
                or entry.max_volume_currency not in ("divine", "chaos", "exalted")
                or entry.id in ("divine", "chaos", "exalted")
            ):
                continue
            val_div = entry.quote.divine or (entry.quote.exalted / div_ex)
            if 3.0 <= val_div <= 60.0 and entry.max_volume_currency in ("divine", "chaos"):
                candidates.append(entry)
            elif include_low_capital and 0.12 <= val_div < 3.0:
                micro_candidates.append(entry)

        candidates.sort(key=lambda e: e.volume_divine or 0.0, reverse=True)
        micro_candidates.sort(key=lambda e: e.volume_divine or 0.0, reverse=True)
        selected_entries = [*candidates[:4], *micro_candidates[:4]]
        for entry in selected_entries:
            assert entry.quote is not None
            val_ex = entry.quote.exalted
            val_div = entry.quote.divine or (val_ex / div_ex)
            raw_mvc = entry.max_volume_currency or "divine"
            denom = (
                "chaos"
                if (val_div < 1.0 and raw_mvc in ("divine", "exalted"))
                else (raw_mvc if raw_mvc in ("divine", "chaos") else "chaos")
            )
            denom_unit_ex = div_ex if denom == "divine" else resolved_snap.rates.chaos_exalted
            denom_qty = val_ex / denom_unit_ex
            # Posting fractional integer ratio bid (e.g. -3%) and ask (+3%) on Alva limit book
            buy_ex = val_ex * (1.0 - assumed_maker_capture_pct / 200.0)
            sell_ex = val_ex * (1.0 + assumed_maker_capture_pct / 200.0)
            prof_ex = sell_ex - buy_ex
            gold_est = estimate_leg_gold(denom, denom_qty, denom_unit_ex) + estimate_leg_gold(
                entry.id, 1.0, val_ex
            )
            units = int(math.ceil(denom_qty)) + 1
            opportunities.append(
                ArbitrageOpportunity(
                    id=f"alva_mm:{entry.id}",
                    title=f"Alva Limit-Order Spread Capture: {entry.name} <-> {denom.title()}",
                    mechanism=ArbitrageMechanism.ORDER_BOOK_SPREAD,
                    executable_in_hideout=True,
                    is_deterministic=True,
                    inputs=[f"~{buy_ex / denom_unit_ex:.2f}x {denom} (limit bid)"],
                    outputs=[f"~{sell_ex / denom_unit_ex:.2f}x {denom} (limit ask)"],
                    input_cost_exalted=round(buy_ex, 2),
                    output_value_exalted=round(sell_ex, 2),
                    gross_profit_exalted=round(prof_ex, 2),
                    gross_profit_divine=round(prof_ex / div_ex, 4),
                    roi_pct=round((prof_ex / buy_ex) * 100.0, 2),
                    units_traded_per_cycle=units,
                    gold_per_cycle_est=gold_est,
                    profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                    min_leg_volume_divine=round(entry.volume_divine or 0.0, 2),
                    primary_denomination=denom,
                    steps=[
                        (
                            f"Open Alva Currency Exchange: post limit BUY for 1x {entry.name} at "
                            f"~{buy_ex / denom_unit_ex:.2f} {denom} (below {val_div:.2f} Div mid)."
                        ),
                        (
                            f"Once filled asynchronously, post limit SELL for 1x {entry.name} at "
                            f"~{sell_ex / denom_unit_ex:.2f} {denom} (capturing "
                            f"~{assumed_maker_capture_pct:.1f}% integer-ratio maker spread)."
                        ),
                    ],
                    risk_notes=[
                        f"7d trend is {entry.change_7d_pct or 0.0:+.1f}%; size inventory <= 2–3 "
                        f"units on cooling items so mid-price drift does not eat maker spread."
                    ],
                )
            )

        return opportunities, traps

    # ------------------------------------------------------------------
    # (B2): Numeraire-Adjusted Cross-Denomination Arbitrage
    # ------------------------------------------------------------------

    def scan_cross_denomination_spreads(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 15.0,
        min_spread_pct: float = 15.0,
        verify_pair_history_top_n: int = 3,
        snap: _LeagueSnapshot | None = None,
    ) -> tuple[list[ArbitrageOpportunity], list[ArbitrageTrapWarning]]:
        resolved_snap = snap or self.economy._ensure_snapshot(league)
        div_ex = resolved_snap.rates.divine_exalted
        chaos_ex = resolved_snap.rates.chaos_exalted

        div_entry = resolved_snap.entries_by_id.get("divine")
        scout_div_ex = (
            div_entry.scout_exalted
            if (div_entry is not None and div_entry.scout_exalted and div_entry.scout_exalted > 0)
            else div_ex
        )
        ninja_div_ex = (
            div_entry.ninja_exalted
            if (div_entry is not None and div_entry.ninja_exalted and div_entry.ninja_exalted > 0)
            else div_ex
        )

        # Map poe.ninja maxVolumeRate for exact quote-leg inspection
        ninja_line_by_id: dict[str, Mapping[str, Any]] = {}
        for ext in POENINJA_EXCHANGE_TYPES:
            try:
                ov = self.economy.get_exchange_overview(ext, league=league, force_refresh=False)
                for line in ov.get("lines") or []:
                    if isinstance(line, Mapping) and line.get("id"):
                        ninja_line_by_id[str(line["id"]).strip().lower()] = line
            except Exception:
                continue

        opportunities: list[ArbitrageOpportunity] = []
        traps: list[ArbitrageTrapWarning] = []
        genuine_candidates: list[tuple[float, _MergedMarketEntry, float, float, float]] = []

        for cid, entry in resolved_snap.entries_by_id.items():
            if cid in ("exalted", "divine", "chaos") or entry.quote is None:
                continue
            vol = entry.volume_divine or 0.0
            if (
                vol < min_volume_divine
                or entry.scout_exalted is None
                or entry.ninja_exalted is None
                or entry.scout_exalted <= 0
                or entry.ninja_exalted <= 0
            ):
                continue

            raw_low = min(entry.scout_exalted, entry.ninja_exalted)
            raw_high = max(entry.scout_exalted, entry.ninja_exalted)
            raw_spread_pct = ((raw_high / raw_low) - 1.0) * 100.0

            # Normalize BOTH sources into their own contemporaneous Divine numeraire!
            scout_in_div = entry.scout_exalted / scout_div_ex
            ninja_in_div = entry.ninja_exalted / ninja_div_ex
            div_low = min(scout_in_div, ninja_in_div)
            div_high = max(scout_in_div, ninja_in_div)
            true_div_spread_pct = ((div_high / div_low) - 1.0) * 100.0

            if raw_spread_pct >= 12.0 and true_div_spread_pct < 4.0:
                traps.append(
                    ArbitrageTrapWarning(
                        kind="numeraire_index_lag",
                        item_or_pair=entry.name,
                        naive_apparent_spread_pct=round(raw_spread_pct, 2),
                        true_executable_spread_pct=round(true_div_spread_pct, 2),
                        explanation=(
                            f"Illusory {raw_spread_pct:.1f}% Exalted spread on {entry.name}: "
                            f"poe.ninja ({ninja_in_div:.3f} Div @ {ninja_div_ex:.1f} Ex/Div) vs "
                            f"poe2scout ({scout_in_div:.3f} Div @ {scout_div_ex:.1f} Ex/Div) is "
                            f"only {true_div_spread_pct:.2f}% in Divine terms (Ex/Div index lag)."
                        ),
                    )
                )
                continue

            if min_spread_pct <= true_div_spread_pct <= 120.0:
                est_units = vol / max(0.001, ninja_in_div)
                if ninja_in_div > 30.0 or est_units < 10.0:
                    traps.append(
                        ArbitrageTrapWarning(
                            kind="illiquid_quote_trap",
                            item_or_pair=entry.name,
                            naive_apparent_spread_pct=round(raw_spread_pct, 2),
                            true_executable_spread_pct=round(true_div_spread_pct, 2),
                            explanation=(
                                f"{entry.name} ({ninja_in_div:.1f} Div) traded ~{est_units:.1f} "
                                f"units ({vol:.0f} Div vol) and has no liquid Exalted/Chaos book — "
                                f"divergence is low-count VWAP lag, not cross-denom arbitrage."
                            ),
                        )
                    )
                    continue
                genuine_candidates.append(
                    (
                        vol,
                        entry,
                        true_div_spread_pct,
                        scout_in_div,
                        ninja_in_div,
                    )
                )

        genuine_candidates.sort(key=lambda row: (row[0], row[2]), reverse=True)

        for idx, (vol, entry, true_spread_pct, scout_div, ninja_div) in enumerate(
            genuine_candidates[:8]
        ):
            # Optional pair-history confirmation on top N candidates
            history_note: str | None = None
            if idx < verify_pair_history_top_n:
                try:
                    ph = self.economy.get_pair_history(
                        entry.id, "divine", league=league, limit=4, force_refresh=False
                    )
                    if ph:
                        latest_rel = float(ph[0].get("relative_price") or 0.0)
                        if latest_rel > 0:
                            scout_div = latest_rel
                            div_low = min(scout_div, ninja_div)
                            div_high = max(scout_div, ninja_div)
                            true_spread_pct = ((div_high / div_low) - 1.0) * 100.0
                            history_note = (
                                f"Verified against latest poe2scout pair history "
                                f"({latest_rel:.3f} Div/unit)."
                            )
                except Exception:
                    pass

            if true_spread_pct < min_spread_pct:
                continue

            line = ninja_line_by_id.get(entry.id, {})
            mvc = str(line.get("maxVolumeCurrency") or entry.max_volume_currency or "divine")
            mvr = line.get("maxVolumeRate")

            # Buy in the cheaper denomination book (`Exalted`/`Chaos` vs `Divine`) and sell richer
            low_div = min(scout_div, ninja_div)
            high_div = max(scout_div, ninja_div)
            # Apply 40% haircut on aggregator-derived cross-denomination spreads for bid/ask slip
            executable_low_ex = low_div * div_ex
            executable_high_ex = executable_low_ex + 0.60 * ((high_div - low_div) * div_ex)
            prof_ex = executable_high_ex - executable_low_ex
            if prof_ex <= 5.0:
                continue

            cheaper_leg = "Exalted/Chaos book" if scout_div < ninja_div else f"{mvc.title()} book"
            richer_leg = f"{mvc.title()} book" if scout_div < ninja_div else "Exalted/Chaos book"

            # Estimate Gold on buying in Exalted/Chaos and selling in Divine (or vice-versa)
            alt_denom = "chaos" if executable_low_ex >= chaos_ex * 2 else "exalted"
            alt_unit_ex = chaos_ex if alt_denom == "chaos" else 1.0
            alt_units = executable_low_ex / alt_unit_ex
            gold_est = (
                estimate_leg_gold(alt_denom, alt_units, alt_unit_ex)
                + estimate_leg_gold(entry.id, 1.0, executable_low_ex)
                + estimate_leg_gold("divine", high_div, div_ex)
            )
            units = int(math.ceil(alt_units)) + 1 + max(1, int(math.ceil(high_div)))

            notes = [
                (
                    f"Numeraire-adjusted spread is {true_spread_pct:.1f}% ({scout_div:.3f} Div "
                    f"Ex-implied vs {ninja_div:.3f} Div {mvc} VWAP; 40% slippage haircut applied)."
                ),
                (
                    "Always check Alva's live in-game ratio preview across all 3 quote tabs "
                    "(Divine, Chaos, Exalted) before placing the order."
                ),
            ]
            if history_note:
                notes.insert(0, history_note)

            rate_hint = f" (poe.ninja {mvc} rate={mvr})" if mvr else ""
            opportunities.append(
                ArbitrageOpportunity(
                    id=f"cross_denom:{entry.id}",
                    title=(
                        f"Cross-Denomination Spread: {entry.name} ({cheaper_leg} -> {richer_leg})"
                    ),
                    mechanism=ArbitrageMechanism.CROSS_DENOMINATION,
                    executable_in_hideout=True,
                    is_deterministic=True,
                    inputs=[f"1x {entry.name} via {cheaper_leg} (~{low_div:.3f} Div eq)"],
                    outputs=[
                        f"Sell 1x {entry.name} via {richer_leg} (~{high_div:.3f} Div eq{rate_hint})"
                    ],
                    input_cost_exalted=round(executable_low_ex, 2),
                    output_value_exalted=round(executable_high_ex, 2),
                    gross_profit_exalted=round(prof_ex, 2),
                    gross_profit_divine=round(prof_ex / div_ex, 4),
                    roi_pct=round((prof_ex / executable_low_ex) * 100.0, 2),
                    units_traded_per_cycle=units,
                    gold_per_cycle_est=gold_est,
                    profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                    min_leg_volume_divine=round(vol, 2),
                    primary_denomination=mvc,
                    steps=[
                        f"Inspect {entry.name} on Alva across Exalted, Chaos, and Divine pairs.",
                        (
                            f"Acquire 1x {entry.name} on the discounted {cheaper_leg} "
                            f"(~{executable_low_ex:.1f} Ex eq)."
                        ),
                        (
                            f"List/sell 1x {entry.name} on the liquid {richer_leg} "
                            f"({vol:.1f} Div volume)."
                        ),
                    ],
                    risk_notes=notes,
                )
            )

        return opportunities, traps

    # ------------------------------------------------------------------
    # (C): Deterministic & Weighted-EV Conversions (Bench / Runes / Remnants)
    # ------------------------------------------------------------------

    def scan_deterministic_conversions(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 0.2,
        min_roi_pct: float = 8.0,
        include_encounter_gated: bool = True,
        snap: _LeagueSnapshot | None = None,
    ) -> tuple[list[ArbitrageOpportunity], list[ArbitrageTrapWarning]]:
        resolved_snap = snap or self.economy._ensure_snapshot(league)
        div_ex = resolved_snap.rates.divine_exalted

        opportunities: list[ArbitrageOpportunity] = []
        traps: list[ArbitrageTrapWarning] = []

        # 1. Perfect Rune Socket-Upgrade + Orb of Extraction (`Greater + Masterwork + Extraction`)
        traps.append(
            ArbitrageTrapWarning(
                kind="impossible_or_gated_recipe",
                item_or_pair="3x Greater Rune -> Perfect Rune",
                naive_apparent_spread_pct=5000.0,
                true_executable_spread_pct=None,
                explanation=(
                    "Reforging Bench 3-to-1 upgrades Lesser -> Normal -> Greater Runes ONLY; "
                    "3x Greater Runes CANNOT reforge into a Perfect Rune. Perfect Runes require "
                    "socketing a Greater Rune into disposable gear, overwriting with a Masterwork "
                    "Rune (100% deterministic), and extracting all sockets via Orb of Extraction."
                ),
            )
        )

        mw_entry = resolved_snap.entries_by_id.get("masterwork-rune")
        ext_entry = resolved_snap.entries_by_id.get("orb-of-extraction")
        art_entry = resolved_snap.entries_by_id.get("artificers")

        if (
            mw_entry
            and mw_entry.quote
            and ext_entry
            and ext_entry.quote
            and (mw_entry.volume_divine or 0.0) >= min_volume_divine
            and (ext_entry.volume_divine or 0.0) >= min_volume_divine
        ):
            mw_ex = mw_entry.quote.exalted
            ext_ex = ext_entry.quote.exalted
            art_ex = art_entry.quote.exalted if (art_entry and art_entry.quote) else 10.7

            for rb in TIERED_RUNE_BASES:
                g_entry = resolved_snap.entries_by_id.get(f"greater-{rb}-rune")
                p_entry = resolved_snap.entries_by_id.get(f"perfect-{rb}-rune")
                if not (g_entry and g_entry.quote and p_entry and p_entry.quote):
                    continue
                g_vol = g_entry.volume_divine or 0.0
                p_vol = p_entry.volume_divine or 0.0
                g_units = (g_vol * div_ex) / max(0.1, g_entry.quote.exalted)
                p_units = (p_vol * div_ex) / max(0.1, p_entry.quote.exalted)
                if p_vol < max(2.0, min_volume_divine) or (
                    g_vol < min_volume_divine and g_units < 8.0
                ):
                    continue

                # 2-socket amortized batch (1x Orb of Extraction extracts BOTH Perfect Runes!)
                batch_sockets = 2
                cost_ex = (
                    batch_sockets * g_entry.quote.exalted
                    + batch_sockets * mw_ex
                    + ext_ex
                    + batch_sockets * art_ex
                )
                out_ex = batch_sockets * p_entry.quote.exalted
                prof_ex = out_ex - cost_ex
                roi_pct = ((out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
                if roi_pct < min_roi_pct or prof_ex <= 0:
                    continue

                units = batch_sockets * 3 + 1 + batch_sockets
                gold_est = (
                    estimate_leg_gold(g_entry.id, batch_sockets, g_entry.quote.exalted)
                    + estimate_leg_gold("masterwork-rune", batch_sockets, mw_ex)
                    + estimate_leg_gold("orb-of-extraction", 1, ext_ex)
                    + estimate_leg_gold("artificers", batch_sockets, art_ex)
                    + estimate_leg_gold(p_entry.id, batch_sockets, p_entry.quote.exalted)
                )
                raw_min_vol = min(
                    g_vol,
                    mw_entry.volume_divine or 0.0,
                    ext_entry.volume_divine or 0.0,
                    p_vol,
                )
                est_g_units = int(round((g_vol * div_ex) / max(1.0, g_entry.quote.exalted)))
                opportunities.append(
                    ArbitrageOpportunity(
                        id=f"perfect_rune_extract:{rb}:2sock",
                        title=(
                            f"2-Socket Masterwork + Extraction: "
                            f"2x {g_entry.name} -> 2x {p_entry.name}"
                        ),
                        mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                        executable_in_hideout=True,
                        is_deterministic=True,
                        inputs=[
                            f"2x {g_entry.name} ({2 * g_entry.quote.exalted:.1f} Ex)",
                            f"2x Masterwork Rune ({2 * mw_ex / div_ex:.2f} Div)",
                            f"1x Orb of Extraction ({ext_ex / div_ex:.2f} Div)",
                            f"1x Trash 2-Socket Armour/Weapon (~{2 * art_ex:.1f} Ex)",
                        ],
                        outputs=[
                            f"2x {p_entry.name} ({out_ex / div_ex:.2f} Div = {out_ex:.1f} Ex)"
                        ],
                        input_cost_exalted=round(cost_ex, 2),
                        output_value_exalted=round(out_ex, 2),
                        gross_profit_exalted=round(prof_ex, 2),
                        gross_profit_divine=round(prof_ex / div_ex, 4),
                        roi_pct=round(roi_pct, 2),
                        units_traded_per_cycle=units,
                        gold_per_cycle_est=gold_est,
                        profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                        min_leg_volume_divine=round(raw_min_vol, 2),
                        min_leg_units_traded=round(min(g_units, p_units), 1),
                        primary_denomination="divine",
                        steps=[
                            (
                                "Take any disposable 2-socket Martial Weapon or Body Armour "
                                "(or apply 2x Artificer's Orb)."
                            ),
                            (
                                f"Socket 2x {g_entry.name}, then overwrite each socket with 1x "
                                f"Masterwork Rune to deterministically upgrade to {p_entry.name}."
                            ),
                            (
                                f"Apply 1x Orb of Extraction to destroy the trash base and recover "
                                f"BOTH loose {p_entry.name}s; sell in Divine ({p_vol:.1f} Div vol)."
                            ),
                        ],
                        risk_notes=[
                            f"Greater Rune exchange volume is {g_vol:.2f} Div (~{est_g_units} "
                            f"units); use limit buy orders if instant stock thins."
                        ],
                    )
                )

        # 2. Lesser -> Normal -> Greater Rune 3-to-1 Reforging Bench Ladder
        for rb in TIERED_RUNE_BASES:
            for src_slug, dst_slug in (
                (f"lesser-{rb}-rune", f"{rb}-rune"),
                (f"{rb}-rune", f"greater-{rb}-rune"),
            ):
                src_e = resolved_snap.entries_by_id.get(src_slug)
                dst_e = resolved_snap.entries_by_id.get(dst_slug)
                if src_e is not None:
                    src_vol_raw = src_e.volume_divine or 0.0
                    if src_e.scout_exalted and src_e.scout_exalted > 100.0 and src_vol_raw < 0.05:
                        traps.append(
                            ArbitrageTrapWarning(
                                kind="illiquid_quote_trap",
                                item_or_pair=src_e.name,
                                naive_apparent_spread_pct=None,
                                true_executable_spread_pct=None,
                                explanation=(
                                    f"{src_e.name} shows {src_e.scout_exalted:.1f} Ex on poe2scout "
                                    f"with {src_vol_raw:.2f} Div poe.ninja exchange volume — "
                                    f"single-listing outlier, not a liquid market price."
                                ),
                            )
                        )
                if not (src_e and src_e.quote and dst_e and dst_e.quote):
                    continue
                src_vol = src_e.volume_divine or 0.0
                dst_vol = dst_e.volume_divine or 0.0
                if src_vol < min_volume_divine or dst_vol < min_volume_divine:
                    continue
                cost_ex = 3.0 * src_e.quote.exalted
                out_ex = dst_e.quote.exalted
                prof_ex = out_ex - cost_ex
                roi_pct = ((out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
                if roi_pct >= min_roi_pct and prof_ex > 1.0:
                    gold_est = estimate_leg_gold(
                        src_slug, 3, src_e.quote.exalted
                    ) + estimate_leg_gold(dst_slug, 1, out_ex)
                    opportunities.append(
                        ArbitrageOpportunity(
                            id=f"reforge_rune:{src_slug}",
                            title=f"Reforging Bench 3-to-1: 3x {src_e.name} -> 1x {dst_e.name}",
                            mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                            executable_in_hideout=True,
                            is_deterministic=True,
                            inputs=[f"3x {src_e.name} ({cost_ex:.1f} Ex)"],
                            outputs=[f"1x {dst_e.name} ({out_ex:.1f} Ex)"],
                            input_cost_exalted=round(cost_ex, 2),
                            output_value_exalted=round(out_ex, 2),
                            gross_profit_exalted=round(prof_ex, 2),
                            gross_profit_divine=round(prof_ex / div_ex, 4),
                            roi_pct=round(roi_pct, 2),
                            units_traded_per_cycle=4,
                            gold_per_cycle_est=gold_est,
                            profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                            min_leg_volume_divine=round(min(src_vol, dst_vol), 2),
                            primary_denomination=dst_e.max_volume_currency or "exalted",
                            steps=[
                                f"Buy 3x {src_e.name} on Alva Currency Exchange.",
                                (
                                    f"Combine 3-to-1 at the hideout Reforging Bench into 1x "
                                    f"{dst_e.name} and sell."
                                ),
                            ],
                            risk_notes=[],
                        )
                    )

        # 3. Verified 10-Step Liquid Emotion Ladder (`DistilledEmotion1..10`)
        traps.append(
            ArbitrageTrapWarning(
                kind="impossible_or_gated_recipe",
                item_or_pair="Potent Liquid Melancholy -> Potent Liquid Ferocity",
                naive_apparent_spread_pct=2689.7,
                true_executable_spread_pct=None,
                explanation=(
                    "Potent Liquid Melancholy / Ferocity / Contempt (EndgameDistilledEmotion1..3, "
                    "drop_level=65) are separate endgame Delirium boss drops and are NOT part of "
                    "the 10-tier DistilledEmotion1..10 Reforging Bench 3-to-1 ladder (which "
                    "terminates at Concentrated Liquid Isolation)."
                ),
            )
        )

        for id_prefix, label_prefix in (
            ("Metadata/Items/Currency/DistilledEmotion", "Liquid Emotion"),
            ("Metadata/Items/Currency/DistilledEmotionTimeLost", "Ancient Liquid Emotion"),
        ):
            ladder_slugs = self._load_emotion_ladder_slugs(id_prefix)
            for idx in range(len(ladder_slugs) - 1):
                src_slug = ladder_slugs[idx]
                dst_slug = ladder_slugs[idx + 1]
                src_e = resolved_snap.entries_by_id.get(src_slug)
                dst_e = resolved_snap.entries_by_id.get(dst_slug)
                if not (src_e and src_e.quote and dst_e and dst_e.quote):
                    continue
                src_vol = src_e.volume_divine or 0.0
                dst_vol = dst_e.volume_divine or 0.0
                src_units = (src_vol * div_ex) / max(0.1, src_e.quote.exalted)
                if (src_vol < min(0.5, min_volume_divine) and src_units < 6.0) or (
                    dst_vol < min_volume_divine
                ):
                    continue
                cost_ex = 3.0 * src_e.quote.exalted
                out_ex = dst_e.quote.exalted
                prof_ex = out_ex - cost_ex
                roi_pct = ((out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
                if roi_pct >= min_roi_pct and prof_ex > 5.0:
                    gold_est = estimate_leg_gold(
                        src_slug, 3, src_e.quote.exalted
                    ) + estimate_leg_gold(dst_slug, 1, out_ex)
                    opportunities.append(
                        ArbitrageOpportunity(
                            id=f"reforge_emotion:{src_slug}->{dst_slug}",
                            title=(
                                f"Reforging Bench {label_prefix}: "
                                f"3x {src_e.name} -> 1x {dst_e.name}"
                            ),
                            mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                            executable_in_hideout=True,
                            is_deterministic=True,
                            inputs=[f"3x {src_e.name} ({cost_ex:.1f} Ex)"],
                            outputs=[
                                f"1x {dst_e.name} ({out_ex:.1f} Ex = {out_ex / div_ex:.2f} Div)"
                            ],
                            input_cost_exalted=round(cost_ex, 2),
                            output_value_exalted=round(out_ex, 2),
                            gross_profit_exalted=round(prof_ex, 2),
                            gross_profit_divine=round(prof_ex / div_ex, 4),
                            roi_pct=round(roi_pct, 2),
                            units_traded_per_cycle=4,
                            gold_per_cycle_est=gold_est,
                            profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                            min_leg_volume_divine=round(min(src_vol, dst_vol), 2),
                            primary_denomination=dst_e.max_volume_currency or "divine",
                            steps=[
                                (
                                    f"Buy 3x {src_e.name} (~{src_e.quote.exalted:.1f} Ex each, "
                                    f"{src_vol:.2f} Div vol) on Alva."
                                ),
                                (
                                    f"Reforge 3-to-1 at the hideout Reforging Bench into 1x "
                                    f"{dst_e.name}."
                                ),
                                (
                                    f"Sell 1x {dst_e.name} for ~{out_ex / div_ex:.2f} Div "
                                    f"({dst_vol:.1f} Div vol)."
                                ),
                            ],
                            risk_notes=[],
                        )
                    )

            # Check 2-step 9-to-1 chain (`DistilledEmotion8 -> 9 -> 10`)
            if len(ladder_slugs) >= 3:
                s8, s9, s10 = ladder_slugs[-3], ladder_slugs[-2], ladder_slugs[-1]
                e8 = resolved_snap.entries_by_id.get(s8)
                e10 = resolved_snap.entries_by_id.get(s10)
                if (
                    e8
                    and e8.quote
                    and e10
                    and e10.quote
                    and (e8.volume_divine or 0.0) >= 0.5
                    and (e10.volume_divine or 0.0) >= min_volume_divine
                ):
                    cost_9 = 9.0 * e8.quote.exalted
                    out_10 = e10.quote.exalted
                    prof_9 = out_10 - cost_9
                    roi_9 = ((out_10 / cost_9) - 1.0) * 100.0 if cost_9 > 0 else 0.0
                    if roi_9 >= min_roi_pct * 2 and prof_9 > 25.0:
                        gold_9 = estimate_leg_gold(s8, 9, e8.quote.exalted) + estimate_leg_gold(
                            s10, 1, out_10
                        )
                        e9_name = (
                            resolved_snap.entries_by_id[s9].name
                            if s9 in resolved_snap.entries_by_id
                            else s9
                        )
                        opportunities.append(
                            ArbitrageOpportunity(
                                id=f"reforge_emotion_chain:{s8}->{s10}",
                                title=(
                                    f"2-Step Reforge Chain: "
                                    f"9x {e8.name} -> 3x {e9_name} -> 1x {e10.name}"
                                ),
                                mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                                executable_in_hideout=True,
                                is_deterministic=True,
                                inputs=[f"9x {e8.name} ({cost_9:.1f} Ex)"],
                                outputs=[
                                    f"1x {e10.name} ({out_10:.1f} Ex = {out_10 / div_ex:.2f} Div)"
                                ],
                                input_cost_exalted=round(cost_9, 2),
                                output_value_exalted=round(out_10, 2),
                                gross_profit_exalted=round(prof_9, 2),
                                gross_profit_divine=round(prof_9 / div_ex, 4),
                                roi_pct=round(roi_9, 2),
                                units_traded_per_cycle=10,
                                gold_per_cycle_est=gold_9,
                                profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_9, gold_9),
                                min_leg_volume_divine=round(
                                    min(e8.volume_divine or 0.0, e10.volume_divine or 0.0), 2
                                ),
                                primary_denomination=e10.max_volume_currency or "divine",
                                steps=[
                                    f"Buy 9x {e8.name} on Alva (~{e8.quote.exalted:.1f} Ex each).",
                                    (
                                        f"Reforge 9x {e8.name} -> 3x {e9_name} -> 1x {e10.name} "
                                        f"at the Reforging Bench."
                                    ),
                                    f"Sell 1x {e10.name} on Alva (~{out_10 / div_ex:.2f} Div).",
                                ],
                                risk_notes=[],
                            )
                        )

        # 4. Empirical Drop-Weighted Catalyst & Refined Catalyst 3-to-1 Reforging EV
        for prefix, min_cat_vol, min_pool_vol in (
            ("refined-", max(5.0, min_volume_divine), 1.0),
            ("", max(0.5, min(1.0, min_volume_divine)), 0.15),
        ):
            pool: dict[str, tuple[_MergedMarketEntry, float]] = {}
            for key, weight in EMPIRICAL_CATALYST_REFORGE_WEIGHTS.items():
                slug = f"{prefix}{key}-catalyst"
                entry = resolved_snap.entries_by_id.get(slug)
                if entry and entry.quote and (entry.volume_divine or 0.0) >= min_pool_vol:
                    pool[key] = (entry, weight)
            if len(pool) < 8:
                continue

            ordered_inputs = sorted(pool.items(), key=lambda kv: kv[1][0].quote.exalted)  # type: ignore[union-attr]
            for key_in, (entry_in, _w_in) in ordered_inputs[:3]:
                assert entry_in.quote is not None
                in_vol = entry_in.volume_divine or 0.0
                in_units = (in_vol * div_ex) / max(0.1, entry_in.quote.exalted)
                if in_vol < min_cat_vol and (prefix != "" or in_units < 25.0):
                    continue
                other_total_w = sum(w for k, (_, w) in pool.items() if k != key_in)
                if other_total_w <= 0:
                    continue
                ev_out_ex = sum(
                    e.quote.exalted * (w / other_total_w)  # type: ignore[union-attr]
                    for k, (e, w) in pool.items()
                    if k != key_in
                )
                jackpot_prob = sum(
                    (w / other_total_w)
                    for k, (e, w) in pool.items()
                    if k != key_in and e.quote.exalted > 3.0 * entry_in.quote.exalted  # type: ignore[union-attr]
                )
                cost_ex = 3.0 * entry_in.quote.exalted
                prof_ex = ev_out_ex - cost_ex
                roi_pct = ((ev_out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
                if roi_pct >= max(25.0, min_roi_pct) and prof_ex > 15.0:
                    gold_est = estimate_leg_gold(
                        entry_in.id, 3, entry_in.quote.exalted
                    ) + estimate_leg_gold(entry_in.id, 1, ev_out_ex)
                    cat_label = prefix.title().rstrip("-")
                    cat_suffix = f"{cat_label} Catalyst" if cat_label else "Catalyst"
                    opportunities.append(
                        ArbitrageOpportunity(
                            id=f"weighted_catalyst_ev:{entry_in.id}",
                            title=(
                                f"Weighted 3-to-1 Catalyst EV: "
                                f"3x {entry_in.name} -> Random {cat_suffix}"
                            ),
                            mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                            executable_in_hideout=True,
                            is_deterministic=False,
                            inputs=[f"3x {entry_in.name} ({cost_ex:.1f} Ex)"],
                            outputs=[
                                f"1x Weighted Random Catalyst "
                                f"(EV {ev_out_ex:.1f} Ex = {ev_out_ex / div_ex:.2f} Div)"
                            ],
                            input_cost_exalted=round(cost_ex, 2),
                            output_value_exalted=round(ev_out_ex, 2),
                            gross_profit_exalted=round(prof_ex, 2),
                            gross_profit_divine=round(prof_ex / div_ex, 4),
                            roi_pct=round(roi_pct, 2),
                            units_traded_per_cycle=4,
                            gold_per_cycle_est=gold_est,
                            profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                            min_leg_volume_divine=round(in_vol, 2),
                            primary_denomination=entry_in.max_volume_currency or "chaos",
                            steps=[
                                (
                                    f"Acquire {entry_in.name} in bulk "
                                    f"(~{entry_in.quote.exalted:.1f} Ex ea, {in_vol:.1f} Div vol)."
                                ),
                                (
                                    "Reforge 3-to-1 at the hideout Reforging Bench (keep Sibilant/"
                                    "Reaver hits; re-feed cheap Flesh/Carapace/Neural misses!)."
                                ),
                            ],
                            risk_notes=[
                                (
                                    f"Computed using empirical non-uniform Reforging Bench weights "
                                    f"(Sibilant 5.7%, Reaver 4.8%, Flesh/Carapace/Neural ~13.3% "
                                    f"each; ~{jackpot_prob * 100:.1f}% jackpot rate — run >= 20–30 "
                                    f"rolls to smooth variance)."
                                )
                            ],
                        )
                    )

        # 5. Kalguuran Remnant Runeforge Recipes (`runeforge_recipes` + `ee2_remnants.json`)
        if include_encounter_gated:
            opportunities.extend(
                self._scan_kalguuran_remnant_recipes(
                    resolved_snap, min_volume_divine=min_volume_divine
                )
            )

        return opportunities, traps

    # ------------------------------------------------------------------
    # (D): Bulk Splinter-Stack & Pinnacle Fragment-Set Assembly
    # ------------------------------------------------------------------

    def scan_fragment_and_splinter_sets(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        min_volume_divine: float = 1.0,
        min_roi_pct: float = 5.0,
        snap: _LeagueSnapshot | None = None,
    ) -> tuple[list[ArbitrageOpportunity], list[ArbitrageTrapWarning]]:
        resolved_snap = snap or self.economy._ensure_snapshot(league)
        div_ex = resolved_snap.rates.divine_exalted

        opportunities: list[ArbitrageOpportunity] = []
        traps: list[ArbitrageTrapWarning] = []

        # 1. Origin Tower Pinnacle Key Assembly (`Origin Spark + Origin Cradle -> Origin Core`)
        spark = resolved_snap.entries_by_id.get("origin-spark")
        cradle = resolved_snap.entries_by_id.get("origin-cradle")
        core = resolved_snap.entries_by_id.get("origin-core")
        if (
            spark
            and spark.quote
            and cradle
            and cradle.quote
            and core
            and core.quote
            and min(
                spark.volume_divine or 0.0,
                cradle.volume_divine or 0.0,
                core.volume_divine or 0.0,
            )
            >= min_volume_divine
        ):
            cost_ex = spark.quote.exalted + cradle.quote.exalted
            out_ex = core.quote.exalted
            prof_ex = out_ex - cost_ex
            roi_pct = ((out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
            if roi_pct >= min_roi_pct and prof_ex > 0:
                gold_est = (
                    estimate_leg_gold("origin-spark", 1, spark.quote.exalted)
                    + estimate_leg_gold("origin-cradle", 1, cradle.quote.exalted)
                    + estimate_leg_gold("origin-core", 1, out_ex)
                    + estimate_leg_gold("divine", cost_ex / div_ex, div_ex)
                )
                min_vol = min(
                    spark.volume_divine or 0.0,
                    cradle.volume_divine or 0.0,
                    core.volume_divine or 0.0,
                )
                opportunities.append(
                    ArbitrageOpportunity(
                        id="set_assembly:origin_core",
                        title="Pinnacle Key Assembly: Origin Spark + Origin Cradle -> Origin Core",
                        mechanism=ArbitrageMechanism.FRAGMENT_SET_ASSEMBLY,
                        executable_in_hideout=True,
                        is_deterministic=True,
                        inputs=[
                            (
                                f"1x Origin Spark ({spark.quote.exalted / div_ex:.2f} Div = "
                                f"{spark.quote.exalted:.1f} Ex)"
                            ),
                            (
                                f"1x Origin Cradle ({cradle.quote.exalted / div_ex:.2f} Div = "
                                f"{cradle.quote.exalted:.1f} Ex)"
                            ),
                        ],
                        outputs=[f"1x Origin Core ({out_ex / div_ex:.2f} Div = {out_ex:.1f} Ex)"],
                        input_cost_exalted=round(cost_ex, 2),
                        output_value_exalted=round(out_ex, 2),
                        gross_profit_exalted=round(prof_ex, 2),
                        gross_profit_divine=round(prof_ex / div_ex, 4),
                        roi_pct=round(roi_pct, 2),
                        units_traded_per_cycle=3 + int(math.ceil(cost_ex / div_ex)),
                        gold_per_cycle_est=gold_est,
                        profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                        min_leg_volume_divine=round(min_vol, 2),
                        primary_denomination="divine",
                        steps=[
                            (
                                f"Buy 1x Origin Spark (~{spark.quote.exalted / div_ex:.2f} Div, "
                                f"{spark.volume_divine:.1f} Div vol) and 1x Origin Cradle "
                                f"(~{cradle.quote.exalted / div_ex:.2f} Div, "
                                f"{cradle.volume_divine:.1f} Div vol) on Alva."
                            ),
                            (
                                "Combine them at the Precursor Reactor / Origin Tower Engine into "
                                "a tradeable Origin Core (no boss kill required to forge)."
                            ),
                            (
                                f"Sell 1x Origin Core on Alva for ~{out_ex / div_ex:.2f} Div "
                                f"({core.volume_divine:.1f} Div vol)."
                            ),
                        ],
                        risk_notes=[
                            "Requires unlocked Atlas access to the Origin Tower Precursor Reactor "
                            "to combine the two halves."
                        ],
                    )
                )

        # 2. Inspect `full_stack_turns_into` stackable conversions from `poe2_0.5.5.sqlite`
        id_to_name = {
            str(r["id"]): str(r["name"])
            for r in self.db.query_all("SELECT id, name FROM base_items")
        }
        stack_rows = self.db.query_all(
            """
            SELECT id, name, properties_json
            FROM base_items
            WHERE is_live_poe2 = 1 AND properties_json LIKE '%full_stack_turns_into%'
            """
        )

        for row in stack_rows:
            src_name = str(row["name"])
            props = json.loads(row["properties_json"])
            stack_size = int(props.get("stack_size") or 0)
            target_mid = str(props.get("full_stack_turns_into") or "")
            dst_name = id_to_name.get(target_mid, "")
            if stack_size <= 0 or not dst_name:
                continue

            src_slug = self._resolve_entry_slug(src_name, resolved_snap)
            dst_slug = self._resolve_entry_slug(dst_name, resolved_snap)
            src_e = resolved_snap.entries_by_id.get(src_slug) if src_slug else None
            dst_e = resolved_snap.entries_by_id.get(dst_slug) if dst_slug else None

            # Patch 0.5.5 trap: 300x Breach Splinter -> Revelatory Wombgift (NOT Breachstone!)
            if src_slug == "breach-splinter":
                bs_e = resolved_snap.entries_by_id.get("breachstone")
                naive_pct: float | None = None
                if src_e and src_e.quote and bs_e and bs_e.quote:
                    naive_pct = round(
                        ((bs_e.quote.exalted / (300.0 * src_e.quote.exalted)) - 1.0) * 100.0, 2
                    )
                traps.append(
                    ArbitrageTrapWarning(
                        kind="impossible_or_gated_recipe",
                        item_or_pair="300x Breach Splinter -> Breachstone",
                        naive_apparent_spread_pct=naive_pct,
                        true_executable_spread_pct=None,
                        explanation=(
                            "In PoE2 0.5.5, 300x Breach Splinters combine into a 'Revelatory "
                            "Wombgift', NOT directly into a Breachstone! Maturing a Revelatory "
                            "Wombgift into a Breachstone at The Genesis Tree requires spending "
                            "non-tradeable Hiveblood (plus ~9,000+ Gold for 300 splinter units)."
                        ),
                    )
                )
                continue

            if src_e is not None and dst_e is not None and dst_e.quote is not None:
                raw_src_ex = (src_e.quote.exalted if src_e.quote else None) or src_e.scout_exalted
                raw_src_vol = src_e.volume_divine or 0.0
                if (
                    raw_src_ex is not None
                    and raw_src_ex * stack_size > dst_e.quote.exalted * 5
                    and raw_src_vol < 0.05
                ):
                    traps.append(
                        ArbitrageTrapWarning(
                            kind="illiquid_quote_trap",
                            item_or_pair=f"{stack_size}x {src_name} -> {dst_name}",
                            naive_apparent_spread_pct=None,
                            true_executable_spread_pct=None,
                            explanation=(
                                f"{src_name} has near-zero exchange liquidity ({raw_src_vol:.4f} "
                                f"Div vol) with a stale quote ({raw_src_ex:.2f} Ex vs "
                                f"{dst_name} {dst_e.quote.exalted:.2f} Ex)."
                            ),
                        )
                    )

            if not (src_e and src_e.quote and dst_e and dst_e.quote):
                continue

            src_vol = src_e.volume_divine or 0.0
            dst_vol = dst_e.volume_divine or 0.0
            if src_vol < min(1.0, min_volume_divine) or dst_vol < min_volume_divine:
                continue

            cost_ex = stack_size * src_e.quote.exalted
            out_ex = dst_e.quote.exalted
            prof_ex = out_ex - cost_ex
            roi_pct = ((out_ex / cost_ex) - 1.0) * 100.0 if cost_ex > 0 else 0.0
            if roi_pct >= min_roi_pct and prof_ex > 5.0:
                gold_est = estimate_leg_gold(
                    src_e.id, stack_size, src_e.quote.exalted
                ) + estimate_leg_gold(dst_e.id, 1, out_ex)
                opportunities.append(
                    ArbitrageOpportunity(
                        id=f"stack_combine:{src_e.id}",
                        title=f"Bulk Stack Assembly: {stack_size}x {src_e.name} -> 1x {dst_e.name}",
                        mechanism=ArbitrageMechanism.FRAGMENT_SET_ASSEMBLY,
                        executable_in_hideout=True,
                        is_deterministic=True,
                        inputs=[
                            (
                                f"{stack_size}x {src_e.name} "
                                f"({src_e.quote.exalted:.2f} Ex ea = {cost_ex:.1f} Ex)"
                            )
                        ],
                        outputs=[f"1x {dst_e.name} ({out_ex:.1f} Ex = {out_ex / div_ex:.2f} Div)"],
                        input_cost_exalted=round(cost_ex, 2),
                        output_value_exalted=round(out_ex, 2),
                        gross_profit_exalted=round(prof_ex, 2),
                        gross_profit_divine=round(prof_ex / div_ex, 4),
                        roi_pct=round(roi_pct, 2),
                        units_traded_per_cycle=stack_size + 1,
                        gold_per_cycle_est=gold_est,
                        profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                        min_leg_volume_divine=round(min(src_vol, dst_vol), 2),
                        primary_denomination=dst_e.max_volume_currency or "divine",
                        steps=[
                            (
                                f"Post a Divine-denominated limit buy order on Alva for "
                                f"{stack_size}x {src_e.name} (~{cost_ex / div_ex:.2f} Div total)."
                            ),
                            (
                                f"Stack {stack_size}x {src_e.name} in inventory (unstack/restack "
                                f"299+1 if auto-combine pauses) to form 1x {dst_e.name}."
                            ),
                            (
                                f"Sell 1x {dst_e.name} on Alva for ~{out_ex / div_ex:.2f} Div "
                                f"({dst_vol:.1f} Div vol)."
                            ),
                        ],
                        risk_notes=[
                            f"Moving {stack_size} splinters burns ~{gold_est:,} Gold per "
                            f"assembled {dst_e.name}; buy splinters in Divine rather than Exalted "
                            f"to avoid a second high-unit Gold leg."
                        ],
                    )
                )

        return opportunities, traps

    # ------------------------------------------------------------------
    # (E): Broke-to-Rich Capital & Gold Compounding Ladder
    # ------------------------------------------------------------------

    def build_compounding_ladder(
        self,
        starting_exalted: float,
        gold_budget: int,
        target_divine: float,
        *,
        league: str = DEFAULT_LEAGUE,
        min_volume_divine: float = 0.8,
        deterministic_only: bool = False,
        include_live_exchange: bool = False,
        max_rungs: int = 10,
        force_refresh: bool = False,
    ) -> CompoundingLadderReport:
        """Chain executable hideout arbitrage & conversion rungs from a small starting bankroll
        (e.g. `250 Ex` = `~0.55 Div` + `80,000 Gold`) up to `target_divine` (e.g. `50+ Div`)
        while strictly respecting both per-step capital affordability AND Alva Gold tax limits.
        """
        if starting_exalted <= 0:
            raise ValueError("starting_exalted must be > 0")
        if gold_budget < 0:
            raise ValueError("gold_budget must be >= 0")
        if target_divine <= 0:
            raise ValueError("target_divine must be > 0")

        snap = self.economy._ensure_snapshot(league, force_refresh=force_refresh)
        div_ex = snap.rates.divine_exalted
        chaos_ex = snap.rates.chaos_exalted
        target_ex = target_divine * div_ex

        # Gather full spectrum of hideout-executable opportunities from micro (<0.5 Div) to high
        eff_vol = min(min_volume_divine, 0.5)
        full_scan = self.scan_all(
            league=league,
            min_volume_divine=eff_vol,
            min_roi_pct=4.0,
            include_live_exchange=include_live_exchange,
            include_encounter_gated=False,
            limit=200,
            force_refresh=False,
        )
        micro_mm, _ = self.scan_alva_spread_making(
            league=league,
            min_volume_divine=max(5.0, eff_vol),
            include_low_capital=True,
            snap=snap,
        )

        by_id: dict[str, ArbitrageOpportunity] = {}
        for opp in (*full_scan.opportunities, *micro_mm):
            if not opp.executable_in_hideout or opp.gross_profit_exalted <= 0:
                continue
            if deterministic_only and not opp.is_deterministic:
                continue
            by_id[opp.id] = opp

        all_opps = sorted(by_id.values(), key=lambda o: o.input_cost_exalted)

        # Document why high-ticket flips & Exalted-stack churn are blocked at t=0
        skipped: list[str] = []
        ex_roundtrip_gold = estimate_leg_gold("exalted", div_ex, 1.0) + estimate_leg_gold(
            "divine", 1.0, div_ex
        )
        skipped.append(
            f"Blocked 1 Divine <-> {div_ex:.0f} Exalted churn (~{ex_roundtrip_gold:,} Gold/cycle "
            f"burns {ex_roundtrip_gold / max(1, gold_budget) * 100:.0f}% of total Gold budget per "
            f"round-trip)."
        )
        for un_opp in sorted(
            [o for o in all_opps if o.input_cost_exalted > starting_exalted * 2.0],
            key=lambda o: o.input_cost_exalted,
            reverse=True,
        )[:3]:
            skipped.append(
                f"Deferred {un_opp.title} at t=0: requires {un_opp.input_cost_exalted:.0f} Ex "
                f"({un_opp.input_cost_exalted / div_ex:.2f} Div) > "
                f"starting {starting_exalted:.0f} Ex."
            )

        def _max_sustainable_cycles(opp: ArbitrageOpportunity) -> int:
            vol_ex = max(opp.min_leg_volume_divine * div_ex, opp.input_cost_exalted * 4.0)
            return max(
                3, min(40, int(math.ceil((vol_ex * 0.45) / max(1.0, opp.input_cost_exalted))))
            )

        def _min_bankroll_multiple(opp: ArbitrageOpportunity) -> float:
            if opp.is_deterministic:
                return 1.0
            # Weighted 3-to-1 Catalyst reforging outputs a tradeable Catalyst (~30% max loss)
            return 1.35

        def _opp_utility(
            opp: ArbitrageOpportunity, bankroll_ex: float, rem_gold_now: int, rem_need_ex: float
        ) -> float:
            if bankroll_ex + 1e-6 < _min_bankroll_multiple(opp) * opp.input_cost_exalted:
                return -1.0
            req_ex_per_10k = max(25.0, (rem_need_ex / max(1, rem_gold_now)) * 10_000.0)
            liq_weight = min(1.0, math.log10(max(1.4, opp.min_leg_volume_divine + 1.0)) / 1.3)
            det_mult = 1.15 if opp.is_deterministic else 0.90
            # Penalize Gold-inefficient churn when Gold is the binding constraint to target
            gold_eff_ratio = opp.profit_ex_per_10k_gold / req_ex_per_10k
            gold_factor = min(2.5, gold_eff_ratio**1.35)
            return (
                (opp.gross_profit_exalted * 0.55 + opp.profit_ex_per_10k_gold * 0.45)
                * max(0.30, liq_weight)
                * det_mult
                * max(0.01, gold_factor)
            )

        cur_ex = float(starting_exalted)
        rem_gold = int(gold_budget)
        exhausted_ids: set[str] = set()
        rungs: list[CompoundingRung] = []

        while cur_ex + 1e-6 < target_ex and rem_gold > 0 and len(rungs) < max_rungs:
            need_ex = target_ex - cur_ex
            req_eff = (need_ex / max(1, rem_gold)) * 10_000.0
            affordable = [
                o
                for o in all_opps
                if o.id not in exhausted_ids
                and o.input_cost_exalted <= cur_ex + 1e-6
                and o.gold_per_cycle_est <= rem_gold
                and _opp_utility(o, cur_ex, rem_gold, need_ex) > 0
            ]
            # Do not force rotation into a Gold-burning filler if all unexhausted options are <35%
            # of required Gold efficiency while a previously used high-efficiency play is available.
            if not affordable or max(o.profit_ex_per_10k_gold for o in affordable) < 0.35 * req_eff:
                all_affordable = [
                    o
                    for o in all_opps
                    if o.input_cost_exalted <= cur_ex + 1e-6
                    and o.gold_per_cycle_est <= rem_gold
                    and _opp_utility(o, cur_ex, rem_gold, need_ex) > 0
                ]
                if all_affordable and (
                    not affordable
                    or max(o.profit_ex_per_10k_gold for o in all_affordable)
                    > 2.0 * max(o.profit_ex_per_10k_gold for o in affordable)
                ):
                    affordable = all_affordable
            if not affordable:
                break

            chosen = max(affordable, key=lambda o: _opp_utility(o, cur_ex, rem_gold, need_ex))
            chosen_util = _opp_utility(chosen, cur_ex, rem_gold, need_ex)

            # Find the lowest capital threshold above `cur_ex` that unlocks a strictly stronger play
            higher_unlocks = [
                o.input_cost_exalted * _min_bankroll_multiple(o)
                for o in all_opps
                if o.input_cost_exalted * _min_bankroll_multiple(o) > cur_ex + 1e-6
                and o.input_cost_exalted <= target_ex
                and o.gold_per_cycle_est <= rem_gold
                and _opp_utility(
                    o,
                    max(cur_ex * 2.0, o.input_cost_exalted * _min_bankroll_multiple(o) * 1.1),
                    rem_gold,
                    need_ex,
                )
                > chosen_util * 1.20
            ]
            rung_goal_ex = min([target_ex, *higher_unlocks]) if higher_unlocks else target_ex

            cycles_to_goal = max(
                1, int(math.ceil((rung_goal_ex - cur_ex) / chosen.gross_profit_exalted))
            )
            # Reserve Gold for higher-efficiency upper rungs when stepping across bridge tiers
            gold_cap_for_rung = (
                max(chosen.gold_per_cycle_est, int(rem_gold * 0.40)) if higher_unlocks else rem_gold
            )
            max_gold_cycles = max(1, gold_cap_for_rung // max(1, chosen.gold_per_cycle_est))
            liq_cap = _max_sustainable_cycles(chosen)
            cycles = max(1, min(cycles_to_goal, max_gold_cycles, liq_cap))

            start_ex = cur_ex
            gold_used = cycles * chosen.gold_per_cycle_est
            profit_gained = cycles * chosen.gross_profit_exalted
            cur_ex = start_ex + profit_gained
            rem_gold -= gold_used
            exhausted_ids.add(chosen.id)

            unlocked_note = (
                f"reaches {target_divine:.1f} Div goal!"
                if cur_ex >= target_ex
                else f"unlocks next capital tier (~{cur_ex / div_ex:.2f} Div bankroll)."
            )
            rungs.append(
                CompoundingRung(
                    rung_index=len(rungs) + 1,
                    opportunity_id=chosen.id,
                    title=chosen.title,
                    mechanism=chosen.mechanism,
                    is_deterministic=chosen.is_deterministic,
                    unit_cost_exalted=chosen.input_cost_exalted,
                    unit_profit_exalted=chosen.gross_profit_exalted,
                    unit_gold_est=chosen.gold_per_cycle_est,
                    cycles_executed=cycles,
                    starting_bankroll_exalted=round(start_ex, 2),
                    starting_bankroll_divine=round(start_ex / div_ex, 4),
                    ending_bankroll_exalted=round(cur_ex, 2),
                    ending_bankroll_divine=round(cur_ex / div_ex, 4),
                    gold_spent_on_rung=gold_used,
                    remaining_gold_after_rung=rem_gold,
                    profit_ex_per_10k_gold=chosen.profit_ex_per_10k_gold,
                    primary_denomination=chosen.primary_denomination,
                    steps=list(chosen.steps),
                    rationale=(
                        f"Affordable at {start_ex:.1f} Ex ({start_ex / div_ex:.2f} Div) with "
                        f"{chosen.profit_ex_per_10k_gold:.1f} Ex / 10k Gold efficiency; "
                        f"{cycles}x cycles (+{profit_gained:.1f} Ex, -{gold_used:,} Gold) "
                        f"{unlocked_note}"
                    ),
                )
            )

        reached = cur_ex + 1e-6 >= target_ex
        extra_gold = 0
        if not reached and all_opps:
            best_gold_eff = (
                max(o.profit_ex_per_10k_gold for o in all_opps if o.input_cost_exalted <= cur_ex)
                if any(o.input_cost_exalted <= cur_ex for o in all_opps)
                else 500.0
            )
            shortfall_ex = max(0.0, target_ex - cur_ex)
            extra_gold = max(
                0, int(math.ceil((shortfall_ex / max(1.0, best_gold_eff)) * 10_000.0)) - rem_gold
            )

        notes = [
            (
                f"Compounded {starting_exalted:.0f} Ex ({starting_exalted / div_ex:.2f} Div) -> "
                f"{cur_ex:.0f} Ex ({cur_ex / div_ex:.2f} Div) across {len(rungs)} hideout rung(s), "
                f"spending {gold_budget - rem_gold:,} / {gold_budget:,} Gold "
                f"({rem_gold:,} Gold remaining)."
            ),
            (
                f"Switch quote denomination from Exalted -> Chaos ({chaos_ex:.1f} Ex) -> Divine "
                f"({div_ex:.1f} Ex) as bankroll crosses 1 Div and 6 Div to keep Gold tax under "
                "~6k–13k Gold per cycle."
            ),
        ]
        if extra_gold > 0:
            notes.append(
                f"Gold budget exhausted before {target_divine:.1f} Div: farm ~{extra_gold:,} "
                "additional Gold (~1–2 juiced T15 Waystones) to finish the final rung."
            )

        return CompoundingLadderReport(
            league=league,
            divine_exalted=round(div_ex, 4),
            chaos_exalted=round(chaos_ex, 4),
            starting_exalted=round(starting_exalted, 2),
            starting_divine=round(starting_exalted / div_ex, 4),
            gold_budget=gold_budget,
            target_divine=round(target_divine, 4),
            target_exalted=round(target_ex, 2),
            final_bankroll_exalted=round(cur_ex, 2),
            final_bankroll_divine=round(cur_ex / div_ex, 4),
            total_profit_exalted=round(cur_ex - starting_exalted, 2),
            total_profit_divine=round((cur_ex - starting_exalted) / div_ex, 4),
            total_gold_spent=gold_budget - rem_gold,
            remaining_gold=rem_gold,
            target_reached=reached,
            estimated_extra_gold_needed=extra_gold,
            rungs=rungs,
            skipped_high_capital_or_gold_plays=skipped,
            strategy_notes=notes,
        )

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _unit_exalted(self, currency: str, snap: _LeagueSnapshot) -> float | None:
        norm = currency.strip().lower()
        if norm in ("exalted", "ex", "exalt", "exalted-orb"):
            return 1.0
        if norm in ("divine", "div", "divine-orb"):
            return snap.rates.divine_exalted
        if norm in ("chaos", "c", "chaos-orb"):
            return snap.rates.chaos_exalted
        cid = snap.alias_to_id.get(normalize_market_slug(norm)) or norm
        entry = snap.entries_by_id.get(cid)
        if entry and entry.quote and entry.quote.exalted > 0:
            return entry.quote.exalted
        return snap.rates.rates_in_exalted.get(norm)

    def _reference_pair_rate(
        self, base_currency: str, quote_currency: str, snap: _LeagueSnapshot
    ) -> float | None:
        base_ex = self._unit_exalted(base_currency, snap)
        quote_ex = self._unit_exalted(quote_currency, snap)
        if base_ex and quote_ex and quote_ex > 0:
            return base_ex / quote_ex
        return None

    def _resolve_entry_slug(self, item_name: str, snap: _LeagueSnapshot) -> str | None:
        norm = normalize_market_slug(item_name)
        if norm in snap.entries_by_id:
            return norm
        return snap.alias_to_id.get(norm)

    def _load_emotion_ladder_slugs(self, metadata_prefix: str) -> list[str]:
        rows = self.db.query_all(
            "SELECT id, name FROM base_items WHERE id LIKE ? AND is_live_poe2 = 1",
            (f"{metadata_prefix}%",),
        )
        numbered: list[tuple[int, str]] = []
        for r in rows:
            mid = str(r["id"])
            suffix = mid[len(metadata_prefix) :]
            if suffix.isdigit():
                numbered.append((int(suffix), normalize_market_slug(str(r["name"]))))
        numbered.sort(key=lambda pair: pair[0])
        return [slug for _, slug in numbered]

    def _scan_kalguuran_remnant_recipes(
        self,
        snap: _LeagueSnapshot,
        *,
        min_volume_divine: float = 1.0,
    ) -> list[ArbitrageOpportunity]:
        if not self.remnants_path.is_file():
            return []
        try:
            rem_data = json.loads(self.remnants_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        rewards = rem_data.get("rewards")
        if not isinstance(rewards, list) or not rewards:
            return []

        # Read `kalguuran_remnant_recipe` rows from `poe2_0.5.5.sqlite` `runeforge_recipes`
        db_rows = self.db.query_all(
            "SELECT recipe_id, inputs_json, output_item FROM runeforge_recipes "
            "WHERE category = 'kalguuran_remnant_recipe'"
        )
        runeshapes_by_reward_idx: dict[int, list[str]] = defaultdict(list)
        slots_by_reward_idx: dict[int, int] = {}

        for r in db_rows:
            rid = str(r["recipe_id"])  # e.g. "remnant:Adaptive|1|6"
            raw_key = rid.split(":", 1)[-1]
            parts = raw_key.split("|")
            if len(parts) != 3:
                continue
            glyph_name, min_cnt_str, total_slots_str = parts
            if not (min_cnt_str.isdigit() and total_slots_str.isdigit()):
                continue
            total_slots = int(total_slots_str)
            inp = json.loads(r["inputs_json"])
            for ridx in inp.get("rune_sequence_ids") or []:
                if isinstance(ridx, int) and 0 <= ridx < len(rewards):
                    prev_slots = slots_by_reward_idx.get(ridx)
                    if prev_slots is None or total_slots < prev_slots:
                        slots_by_reward_idx[ridx] = total_slots
                        runeshapes_by_reward_idx[ridx] = [f"{min_cnt_str}x {glyph_name}"]
                    elif total_slots == prev_slots:
                        spec = f"{min_cnt_str}x {glyph_name}"
                        if spec not in runeshapes_by_reward_idx[ridx]:
                            runeshapes_by_reward_idx[ridx].append(spec)

        div_ex = snap.rates.divine_exalted
        ver_entry = snap.entries_by_id.get("verisium")
        liq_ver_entry = snap.entries_by_id.get("liquid-verisium")
        ver_unit_ex = ver_entry.quote.exalted if (ver_entry and ver_entry.quote) else 0.045
        liq_ver_ex = (
            liq_ver_entry.quote.exalted if (liq_ver_entry and liq_ver_entry.quote) else 36.7
        )

        best_by_slug: dict[str, ArbitrageOpportunity] = {}
        for ridx, specs in runeshapes_by_reward_idx.items():
            rw = rewards[ridx]
            if not isinstance(rw, Mapping):
                continue
            reward_name = str(rw.get("refReward") or "").strip()
            count = int(rw.get("count") or 1)
            gem_lvl = int(rw.get("gemLevel") or 0)
            slots = slots_by_reward_idx.get(ridx, 4)
            if not reward_name or slots > 7:
                continue
            if (
                reward_name
                in ("Uncut Skill Gem", "Uncut Support Gem", "Uncut Spirit Gem", "Thaumaturgic Flux")
                and gem_lvl < 19
            ):
                continue
            slug = self._resolve_entry_slug(reward_name, snap)
            if not slug:
                continue
            out_e = snap.entries_by_id.get(slug)
            if not (out_e and out_e.quote):
                continue
            vol = out_e.volume_divine or 0.0
            est_out_units = (vol * div_ex) / max(0.1, out_e.quote.exalted)
            if vol < max(2.0, min_volume_divine) or est_out_units < 5.0:
                continue

            out_ex = count * out_e.quote.exalted
            # Verisium activation + expected ~2x Liquid Verisium rerolls to align Remnant sockets
            verisium_qty = slots * 250
            cost_ex = verisium_qty * ver_unit_ex + 2.0 * liq_ver_ex
            prof_ex = out_ex - cost_ex
            if out_ex < div_ex * 1.5 or prof_ex <= div_ex:
                continue

            gold_est = estimate_leg_gold("liquid-verisium", 2, liq_ver_ex) + estimate_leg_gold(
                slug, count, out_e.quote.exalted
            )
            opp = ArbitrageOpportunity(
                id=f"kalguuran_remnant:{slug}:{slots}slot",
                title=f"Kalguuran {slots}-Slot Remnant Recipe -> {count}x {out_e.name}",
                mechanism=ArbitrageMechanism.DETERMINISTIC_CONVERSION,
                executable_in_hideout=False,
                is_deterministic=True,
                inputs=[
                    f"{slots}-Slot Ezomyte Remnant ({' + '.join(specs)})",
                    f"~{verisium_qty}x Verisium + ~2x Liquid Verisium ({cost_ex:.1f} Ex)",
                ],
                outputs=[f"{count}x {out_e.name} ({out_ex / div_ex:.2f} Div = {out_ex:.1f} Ex)"],
                input_cost_exalted=round(cost_ex, 2),
                output_value_exalted=round(out_ex, 2),
                gross_profit_exalted=round(prof_ex, 2),
                gross_profit_divine=round(prof_ex / div_ex, 4),
                roi_pct=round(((out_ex / cost_ex) - 1.0) * 100.0, 2),
                units_traded_per_cycle=2 + count,
                gold_per_cycle_est=gold_est,
                profit_ex_per_10k_gold=compute_profit_per_10k_gold(prof_ex, gold_est),
                min_leg_volume_divine=round(vol, 2),
                primary_denomination=out_e.max_volume_currency or "divine",
                steps=[
                    (
                        f"Roll/select a {slots}-socket Ezomyte Remnant in maps using Liquid "
                        f"Verisium (~{liq_ver_ex:.1f} Ex)."
                    ),
                    (
                        f"Slot Runeshapes ({' + '.join(specs)}) and power with Verisium to lock "
                        f"{count}x {out_e.name}."
                    ),
                    (
                        f"Complete the Remnant encounter and sell {count}x {out_e.name} on Alva "
                        f"({vol:.1f} Div vol)."
                    ),
                ],
                risk_notes=[
                    "Encounter-gated: Runeshapes are earned/slotted in-map rather than bought "
                    "loose on Alva (`executable_in_hideout=False`)."
                ],
            )
            prev = best_by_slug.get(slug)
            if prev is None or opp.gross_profit_exalted > prev.gross_profit_exalted:
                best_by_slug[slug] = opp

        ranked = sorted(best_by_slug.values(), key=lambda x: x.gross_profit_exalted, reverse=True)
        return ranked[:3]


def _select_clean_offer(
    offers: Sequence[ExchangeOffer],
    *,
    side: str,
    alva_quote_per_base: float | None,
    min_stock: int,
) -> tuple[ExchangeOffer | None, list[str]]:
    """Filter `/api/trade2/exchange` offers to online, stocked listings while stripping
    obvious misclick tags (e.g. `15 divine -> 1 exalted` when Alva is `448 exalted / divine`).
    """
    flags: list[str] = []
    seen_accounts: set[str] = set()
    valid: list[ExchangeOffer] = []

    for off in offers:
        if not off.online or off.stock < min_stock or off.have_amount <= 0 or off.want_amount <= 0:
            continue
        acct_key = off.account_name.strip().lower()
        if acct_key in seen_accounts:
            continue

        quote_per_base = (
            (off.want_amount / off.have_amount)
            if side == "sell_base"
            else (off.have_amount / off.want_amount)
        )

        # Reject gross stash-tab misclick tags (> 8x off Alva mid)
        if alva_quote_per_base and alva_quote_per_base > 0:
            if side == "sell_base" and (
                quote_per_base < 0.08 * alva_quote_per_base
                or quote_per_base > 8.0 * alva_quote_per_base
            ):
                flags.append("misclick_outlier_filtered")
                continue
            if side == "buy_base" and (
                quote_per_base > 8.0 * alva_quote_per_base
                or quote_per_base < 0.08 * alva_quote_per_base
            ):
                flags.append("misclick_outlier_filtered")
                continue

        seen_accounts.add(acct_key)
        valid.append(off)

    if not valid:
        return None, flags

    if side == "sell_base":
        valid.sort(key=lambda o: o.want_amount / o.have_amount, reverse=True)
    else:
        valid.sort(key=lambda o: o.have_amount / o.want_amount)
    return valid[0], flags


def _deduplicate_traps(traps: Sequence[ArbitrageTrapWarning]) -> list[ArbitrageTrapWarning]:
    seen: set[tuple[str, str]] = set()
    out: list[ArbitrageTrapWarning] = []
    for t in traps:
        key = (t.kind, t.item_or_pair)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out
