from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from poe2brain.crafting.pool import ModPoolEngine, RemovedLeverError
from poe2brain.crafting.profit import (
    CraftProfitScanner,
    CraftRecipeSpec,
    CraftStageSpec,
    MarketQuerySpec,
    build_sanitized_crafting_prices,
    build_trade_query_from_market_spec,
)
from poe2brain.models.trade import CurrencyQuote, PriceDistribution
from poe2brain.trade.trade2_client import DetailedPriceCheckResult


@pytest.fixture(scope="module")
def scanner() -> CraftProfitScanner:
    eng = ModPoolEngine()
    sc = CraftProfitScanner(pool_engine=eng)
    yield sc
    eng.close()


def test_curated_recipes_accounting_tiers_and_deterministic_variance(
    scanner: CraftProfitScanner,
) -> None:
    report = scanner.scan(
        live_economy=False,
        live_trade=False,
        n_sims=1200,
        seed=77,
    )
    assert report.total_recipes_evaluated == 13
    assert set(report.top_by_tier.keys()) == {"low", "mid", "high"}
    assert len(report.top_by_tier["low"]) >= 2
    assert len(report.top_by_tier["mid"]) >= 2
    assert len(report.top_by_tier["high"]) >= 1

    by_id = {ev.recipe_id: ev for ev in report.evaluations}

    for ev in report.evaluations:
        assert ev.expected_cost_exalted == pytest.approx(
            ev.base_cost_ex + ev.expected_craft_cost_ex, abs=0.02
        )
        assert ev.expected_profit_ex == pytest.approx(
            ev.exit_price_ex - ev.base_cost_ex - ev.expected_craft_cost_ex, abs=0.02
        )
        assert ev.p50_ex <= ev.p80_ex + 1e-6 <= ev.p95_ex + 2e-6
        assert ev.p80_drawdown_ex == pytest.approx(ev.p80_ex)
        assert len(ev.execution_steps) >= 1
        assert [s.step_index for s in ev.execution_steps] == list(
            range(1, len(ev.execution_steps) + 1)
        )
        if ev.expected_profit_ex > 0:
            assert ev.success_probability_per_base > ev.breakeven_hit_rate
        elif ev.expected_profit_ex < 0:
            assert ev.success_probability_per_base < ev.breakeven_hit_rate

    # Zero-RNG cross-side Sinistral Crystallisation recipes must have zero cost variance
    for det_id in (
        "staff_plus6_spell_archmage",
        "boots_30ms_60augment_2s",
        "ring_breach_quality_mana_chaos",
    ):
        det_ev = by_id[det_id]
        assert det_ev.success_probability_per_base == 1.0
        assert det_ev.p50_ex == pytest.approx(det_ev.p80_ex, abs=0.01)
        assert det_ev.p80_ex == pytest.approx(det_ev.p95_ex, abs=0.01)

    # Sekhema Sandals 35% MS uses Perfect Transmute + Perfect Augment (50% combined hit rate)
    boots_ev = by_id["sekhema_boots_35ms_es"]
    assert boots_ev.execution_steps[0].step_hit_probability == pytest.approx(0.50, abs=0.01)

    # Headhunter chancing trap must be ranked below profitable crafts with negative EV
    hh_ev = by_id["headhunter_chance_negative_ev_check"]
    assert hh_ev.expected_profit_ex < 0
    assert hh_ev.expected_roi_pct < -80.0
    assert hh_ev.rank > report.profitable_count


def test_liquidity_sanitizer_blocks_dust_anomalies_and_zero_prices() -> None:
    @dataclass
    class _StubRates:
        divine_exalted: float = 450.0
        chaos_exalted: float = 36.0

    class _StubEconomy:
        def get_reference_rates(self, league: str) -> _StubRates:
            return _StubRates()

        def get_bulk_crafting_prices(self, league: str) -> dict[str, CurrencyQuote | None]:
            return {
                "orb-of-transmutation": CurrencyQuote(
                    id="transmute",
                    name="Orb of Transmutation",
                    category="currency",
                    league=league,
                    exalted=2.2,
                    volume_primary=3.0,
                    source="poe.ninja",
                ),
                "greater-orb-of-transmutation": CurrencyQuote(
                    id="greater-orb-of-transmutation",
                    name="Greater Orb of Transmutation",
                    category="currency",
                    league=league,
                    exalted=0.28,
                    volume_primary=0.05,
                    source="poe.ninja",
                ),
                "omen-of-the-liege": CurrencyQuote(
                    id="omen-of-the-liege",
                    name="Omen of the Liege",
                    category="ritual",
                    league=league,
                    exalted=0.09,
                    volume_primary=0.002,
                    source="poe.ninja",
                ),
                "prismatic-alloy": CurrencyQuote(
                    id="prismatic-alloy",
                    name="Prismatic Alloy",
                    category="verisium",
                    league=league,
                    exalted=0.05,
                    volume_primary=0.01,
                    source="poe.ninja",
                ),
                "gnawed-rib": CurrencyQuote(
                    id="gnawed-rib",
                    name="Gnawed Rib",
                    category="abyss",
                    league=league,
                    exalted=8.5,
                    volume_primary=0.02,
                    source="poe.ninja",
                ),
                "recombinator": None,
            }

    prices, div_ex, chaos_ex, src, anomalies = build_sanitized_crafting_prices(
        _StubEconomy(),  # type: ignore[arg-type]
        "Forbidden Rites",
    )
    assert div_ex == 450.0
    assert chaos_ex == 36.0
    assert src == "live_economy:Forbidden Rites"
    assert prices["Omen of the Liege"] == 65.0
    assert prices["Prismatic Alloy"] == 25.0
    assert prices["Gnawed Rib"] == 0.5
    assert prices["Greater Orb of Transmutation"] > prices["Orb of Transmutation"]
    assert len(anomalies) >= 4
    assert all(v > 0 for v in prices.values())

    with pytest.raises(ValueError, match="Invalid non-positive crafting price"):
        build_sanitized_crafting_prices(None, raw_overrides={"Divine Orb": 0.0})


def test_trade_query_compilation_strips_hashes_and_expands_domains() -> None:
    spec = MarketQuerySpec(
        category="weapon.wand",
        base_type="Galvanic Wand",
        rarity="rare",
        min_ilvl=81,
        stat_filters=[
            ("+# to Level of all Lightning Spell Skills", 5, None),
            ("#% increased Elemental Damage", 70, None),
        ],
        match_any_explicit_domain=True,
    )
    payload = build_trade_query_from_market_spec(spec, securable_only=True)
    q = payload["query"]
    assert q["status"]["option"] == "securable"
    assert q["type"] == "Galvanic Wand"
    assert q["filters"]["type_filters"]["filters"]["ilvl"] == {"min": 81}
    assert q["filters"]["type_filters"]["filters"]["rarity"] == {"option": "rare"}

    # Both affixes exist across explicit + fractured + desecrated -> 2 count min:1 groups
    count_groups = [g for g in q["stats"] if g["type"] == "count"]
    assert len(count_groups) == 2
    lt_ids = {f["id"] for f in count_groups[0]["filters"]}
    assert "explicit.stat_1545858329" in lt_ids
    assert "fractured.stat_1545858329" in lt_ids
    assert "desecrated.stat_1545858329" in lt_ids

    # Every curated recipe's base & exit market spec must compile cleanly to valid trade2 JSON
    # AND obey GGG's unauthenticated <= 3 non-empty stat-group ceiling!
    sc = CraftProfitScanner()
    try:
        by_id = {r.recipe_id: r for r in sc.default_recipes()}
        for rec in sc.default_recipes():
            b_q = build_trade_query_from_market_spec(rec.base_market, securable_only=True)
            e_q = build_trade_query_from_market_spec(rec.exit_market, securable_only=True)
            assert b_q["query"]["status"]["option"] == "securable"
            assert e_q["query"]["status"]["option"] == "securable"
            b_non_empty = sum(1 for g in b_q["query"]["stats"] if g.get("filters"))
            e_non_empty = sum(1 for g in e_q["query"]["stats"] if g.get("filters"))
            assert b_non_empty <= 3, f"{rec.recipe_id} base query has {b_non_empty} stat groups"
            assert e_non_empty <= 3, f"{rec.recipe_id} exit query has {e_non_empty} stat groups"

        for multi_id in ("staff_plus6_spell_archmage", "wand_plus6_lightning_archmage"):
            mq = build_trade_query_from_market_spec(
                by_id[multi_id].exit_market, securable_only=True
            )["query"]
            non_empty = [g for g in mq["stats"] if g.get("filters")]
            assert len(non_empty) == 3
            assert non_empty[0]["type"] == "and"
            assert non_empty[1]["type"] == "count"
            assert non_empty[2]["type"] == "count"
    finally:
        sc.close()


def test_live_trade2_securable_p10_cap_pacing_and_illiquidity() -> None:
    calls: list[dict] = []
    sleeps: list[float] = []

    class _FakeTrade2:
        def price_check(
            self,
            payload: dict,
            *,
            league: str,
            securable_only: bool = True,
            max_fetch: int = 20,
        ) -> DetailedPriceCheckResult:
            calls.append(payload)
            idx = len(calls)
            if idx == 1:
                return DetailedPriceCheckResult(
                    league=league,
                    query_id="q1",
                    trade_url="https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/q1",
                    total_matches=42,
                    estimated_exalted=2600.0,
                    min_exalted=1900.0,
                    p10_exalted=2200.0,
                    median_exalted=2650.0,
                    distribution=PriceDistribution(
                        count_total=10,
                        count_deduplicated=8,
                        count_securable=8,
                        count_whisper=0,
                        min_exalted=1900.0,
                        median_exalted=2650.0,
                        securable_min_exalted=1900.0,
                        securable_median_exalted=2650.0,
                    ),
                )
            return DetailedPriceCheckResult(
                league=league,
                query_id="q2",
                trade_url="https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/q2",
                total_matches=0,
                distribution=PriceDistribution(),
            )

        def close(self) -> None:
            pass

    eng = ModPoolEngine()
    try:
        sc = CraftProfitScanner(
            pool_engine=eng,
            trade2=_FakeTrade2(),  # type: ignore[arg-type]
            sleep_fn=sleeps.append,
        )
        subset = sc.default_recipes()[:3]
        rep = sc.scan(
            recipes=subset,
            live_economy=False,
            live_trade=True,
            max_trade_queries=2,
            trade_delay_seconds=1.5,
            n_sims=400,
        )
        assert rep.live_trade_queries_used == 2
        assert len(calls) == 2
        assert sleeps == [1.5]

        by_id = {e.recipe_id: e for e in rep.evaluations}
        r1 = by_id[subset[0].recipe_id]
        assert r1.exit_market_quote.source == "live_trade2_securable"
        assert r1.exit_price_ex == 2200.0
        assert r1.exit_market_quote.total_active_listings == 42

        r2 = by_id[subset[1].recipe_id]
        assert r2.exit_market_quote.source == "live_trade2_securable"
        assert r2.exit_market_quote.total_active_listings == 0
        assert any("0 active Instant Buyout" in w for w in r2.warnings)

        r3 = by_id[subset[2].recipe_id]
        assert r3.exit_market_quote.source == "reference_benchmark"
    finally:
        eng.close()


def test_shorthand_planner_recipe_and_removed_lever_rejection(
    scanner: CraftProfitScanner,
) -> None:
    shorthand = CraftRecipeSpec(
        recipe_id="custom_wand_spell",
        name="Custom Shorthand Wand",
        budget_tier="low",
        archetype="Planner Shorthand",
        base_type="Galvanic Wand",
        ilvl=81,
        base_market=MarketQuerySpec(fallback_price_ex=5.0),
        exit_market=MarketQuerySpec(fallback_price_ex=1200.0),
        target_mods=["T2 Spell Damage"],
    )
    ev = scanner.evaluate_recipe(shorthand, live_trade=False, n_sims=500)
    assert ev.expected_cost_exalted > 5.0
    assert ev.expected_profit_ex == pytest.approx(
        1200.0 - ev.base_cost_ex - ev.expected_craft_cost_ex, abs=0.02
    )

    bad_omen_recipe = CraftRecipeSpec(
        recipe_id="illegal_homogenising",
        name="Removed 0.4 Omen Trap",
        budget_tier="mid",
        archetype="Invalid",
        base_type="Galvanic Wand",
        ilvl=81,
        base_market=MarketQuerySpec(fallback_price_ex=5.0),
        exit_market=MarketQuerySpec(fallback_price_ex=1000.0),
        stages=[
            CraftStageSpec(
                kind="magic_opener",
                description="Open",
                lever="Orb of Transmutation",
                accept_any_roll=True,
            ),
            CraftStageSpec(
                kind="regal_step",
                description="Removed Omen",
                lever="Regal Orb",
                omens=["Omen of Homogenising Coronation"],
            ),
        ],
    )
    with pytest.raises(RemovedLeverError):
        scanner.evaluate_recipe(bad_omen_recipe, live_trade=False, n_sims=100)


@pytest.mark.skipif(
    os.environ.get("POE2_LIVE_TESTS") != "1",
    reason="Set POE2_LIVE_TESTS=1 to verify live trade2 complexity cap on craft profit",
)
def test_live_craft_profit_unauth_trade2_roundtrip() -> None:
    with CraftProfitScanner() as sc:
        by_id = {r.recipe_id: r for r in sc.default_recipes()}
        target_recipes = [
            by_id["staff_plus6_spell_archmage"],
            by_id["wand_plus6_lightning_archmage"],
        ]
        rep = sc.scan(
            recipes=target_recipes,
            league="Forbidden Rites",
            live_economy=True,
            live_trade=True,
            max_trade_queries=2,
            n_sims=300,
        )
        assert rep.live_trade_queries_used == 2
        for ev in rep.evaluations:
            assert ev.exit_market_quote.source == "live_trade2_securable"
            assert ev.exit_market_quote.total_active_listings >= 0
