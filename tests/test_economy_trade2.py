# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from poe2brain.config import DEFAULT_LEAGUE, USER_AGENT
from poe2brain.http import GGGRateLimiter, Poe2HttpClient
from poe2brain.models.items import ModDomain
from poe2brain.models.trade import PriceCheckResult, SaleType
from poe2brain.trade.economy import EconomyService
from poe2brain.trade.trade2_client import (
    DetailedPriceCheckResult,
    Trade2Client,
    build_trade_url,
    extract_rune_sockets,
    parse_fetch_item,
    strip_poe2_wiki_markup,
    strip_wiki_markup,
    summarize_price_check,
)

# ---------------------------------------------------------------------------
# Deterministic Unit Tests: Wiki Markup, Structured Mods, Sockets & PoB2 Text
# ---------------------------------------------------------------------------


def test_strip_poe2_wiki_markup() -> None:
    assert strip_wiki_markup("+142 to [MaximumLife|maximum Life]") == "+142 to maximum Life"
    assert (
        strip_poe2_wiki_markup("27% increased [Physical] Damage") == "27% increased Physical Damage"
    )
    assert (
        strip_wiki_markup("[ManaLeech|Leeches] 5.62% of [Physical|Physical] Damage as Mana")
        == "Leeches 5.62% of Physical Damage as Mana"
    )
    assert (
        strip_wiki_markup("+16% to all [ElementalDamage|Elemental] [Resistances]")
        == "+16% to all Elemental Resistances"
    )


def test_parse_fetch_item_desecrated_in_explicit_and_rune_sockets() -> None:
    raw_item: dict[str, Any] = {
        "realm": "poe2",
        "league": "Forbidden Rites",
        "name": "Honour Mark",
        "typeLine": "Dualstring Bow",
        "baseType": "Dualstring Bow",
        "rarity": "Rare",
        "ilvl": 82,
        "identified": True,
        "corrupted": False,
        "sanctified": True,
        "sockets": [{"group": 0, "type": "rune"}, {"group": 1, "type": "rune"}],
        "properties": [
            {"name": "Bows", "values": [], "displayMode": 0},
            {"name": "[Quality]", "values": [["+20%", 1]], "displayMode": 0},
            {
                "name": "[AttacksPerSecond|Attacks per Second]",
                "values": [["1.45", 1]],
                "displayMode": 0,
            },
        ],
        "requirements": [
            {"name": "Level", "values": [["78", 0]], "displayMode": 0},
            {"name": "[Dexterity|Dex]", "values": [["165", 0]], "displayMode": 0},
        ],
        "runeMods": [
            {
                "description": "Bonded: +15% to [Chaos] [Resistances|Resistance]",
                "domain": "augment",
                "hash": "stat.rune.stat_111",
            }
        ],
        "implicitMods": [
            {
                "description": "Bow Attacks fire an additional [Projectile|Arrow]",
                "domain": "implicit",
                "hash": "stat.implicit.stat_222",
            }
        ],
        "explicitMods": [
            {
                "description": "148% increased [Physical] Damage",
                "domain": "explicit",
                "hash": "stat.explicit.stat_1509134228",
                "mods": [
                    {
                        "name": "Merciless",
                        "tier": "P1",
                        "level": 82,
                        "magnitudes": [{"min": "140", "max": "159"}],
                    }
                ],
            },
            {
                "description": "[ManaLeech|Leeches] 5.62% of [Physical|Physical] Damage as Mana",
                "flags": {"desecrated": True},
                "domain": "desecrated",
                "hash": "stat.desecrated.stat_669069897",
                "mods": [
                    {
                        "name": "of the Parched",
                        "tier": "S4",
                        "level": 21,
                        "magnitudes": [{"min": "5", "max": "5.9"}],
                    }
                ],
            },
            {
                "description": "+32% to [CriticalHit|Critical Damage Bonus]",
                "flags": {"fractured": True},
                "domain": "fractured",
                "hash": "stat.fractured.stat_3556824919",
                "mods": [
                    {
                        "name": "of Destruction",
                        "tier": "S1",
                        "level": 74,
                        "magnitudes": [{"min": "30", "max": "35"}],
                    }
                ],
            },
        ],
        "extended": {"dps": 412.5, "pdps": 390.0, "edps": 22.5},
    }

    assert extract_rune_sockets(raw_item) == 2
    parsed = parse_fetch_item(raw_item)
    assert parsed.name == "Honour Mark"
    assert parsed.base_type == "Dualstring Bow"
    assert parsed.rune_sockets == 2
    assert parsed.quality == 20
    assert parsed.sanctified is True
    assert parsed.fractured is True
    assert parsed.requirements.level == 78
    assert parsed.requirements.dexterity == 165
    assert parsed.physical_dps == 390.0

    by_domain = {m.domain: m for m in parsed.mods}
    assert ModDomain.DESECRATED in by_domain
    assert by_domain[ModDomain.DESECRATED].raw_text == "Leeches 5.62% of Physical Damage as Mana"
    assert by_domain[ModDomain.DESECRATED].trade_stat_ids == ["desecrated.stat_669069897"]

    assert ModDomain.FRACTURED in by_domain
    assert by_domain[ModDomain.FRACTURED].raw_text == "+32% to Critical Damage Bonus"

    assert ModDomain.RUNE in by_domain
    assert by_domain[ModDomain.RUNE].bonded is True
    assert "[" not in by_domain[ModDomain.RUNE].raw_text


# ---------------------------------------------------------------------------
# Deterministic Unit Tests: Securable vs Whisper, Price-Fixer Dedup & Rate Limit
# ---------------------------------------------------------------------------


def _make_raw_fetch_entry(
    listing_id: str,
    *,
    account: str,
    amount: float,
    currency: str = "exalted",
    fee: int | None = None,
    whisper: str | None = None,
    online: bool = False,
) -> dict[str, Any]:
    listing_obj: dict[str, Any] = {
        "indexed": "2026-11-23T01:00:00Z",
        "price": {"type": "~b/o", "amount": amount, "currency": currency},
        "account": {
            "name": account,
            "online": {"league": "Forbidden Rites"} if online else None,
        },
    }
    if fee is not None:
        listing_obj["fee"] = fee
    if whisper is not None:
        listing_obj["whisper"] = whisper

    return {
        "id": listing_id,
        "listing": listing_obj,
        "item": {
            "name": "Storm Torc",
            "baseType": "Solar Amulet",
            "rarity": "Rare",
            "ilvl": 82,
            "identified": True,
            "sockets": [{"group": 0, "type": "rune"}],
            "implicitMods": [
                {
                    "description": "+15 to [Spirit]",
                    "domain": "implicit",
                    "hash": "stat.implicit.stat_1",
                }
            ],
            "explicitMods": [
                {
                    "description": "+42 to [Spirit]",
                    "domain": "explicit",
                    "hash": "stat.explicit.stat_2",
                }
            ],
        },
    }


def test_securable_vs_whisper_split_and_price_fixer_deduplication() -> None:
    client = Trade2Client()
    try:
        raw_entries = [
            # Stash-wall price-fixer posting 4 whisper-only lowball listings at 2 ex
            _make_raw_fetch_entry(
                "fix1", account="WallFixer#9999", amount=2.0, whisper="@Fix hi", online=True
            ),
            _make_raw_fetch_entry(
                "fix2", account="WallFixer#9999", amount=2.0, whisper="@Fix hi", online=True
            ),
            _make_raw_fetch_entry(
                "fix3", account="WallFixer#9999", amount=2.5, whisper="@Fix hi", online=True
            ),
            _make_raw_fetch_entry(
                "fix4", account="wallfixer#9999", amount=3.0, whisper="@Fix hi", online=True
            ),
            # Real Instant Buyout (securable, Gold fee, even offline!) & real whisper listings
            _make_raw_fetch_entry(
                "real1", account="MerchantA#1001", amount=48.0, fee=14200, online=False
            ),
            _make_raw_fetch_entry(
                "real2", account="MerchantB#1002", amount=50.0, fee=15100, online=False
            ),
            _make_raw_fetch_entry(
                "real3", account="TraderC#1003", amount=52.0, whisper="@TraderC buy", online=True
            ),
            _make_raw_fetch_entry(
                "real4", account="MerchantD#1004", amount=55.0, fee=16800, online=True
            ),
        ]

        listings = client.parse_listings(
            raw_entries,
            league="Forbidden Rites",
            currency_rates={"exalted": 1.0, "divine": 450.0},
        )
        assert len(listings) == 8

        result = summarize_price_check(
            listings,
            league="Forbidden Rites",
            query_id="q_solar_123",
            total_matches=42,
            divine_exalted=450.0,
        )
        assert isinstance(result, PriceCheckResult)
        assert isinstance(result, DetailedPriceCheckResult)
        assert (
            result.trade_url
            == "https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/q_solar_123"
        )
        assert result.distribution.count_total == 8
        assert result.distribution.count_deduplicated == 5
        assert result.distribution.count_securable == 3
        assert result.distribution.count_whisper == 2
        assert result.distribution.securable_min_exalted == 48.0
        assert result.distribution.securable_median_exalted == 50.0

        # Offline Gold-fee listing is Instant Buyout (`securable`)
        sec0 = result.securable_listings[0]
        assert sec0.sale_type == SaleType.SECURABLE
        assert sec0.is_instant_buyout is True
        assert sec0.fee == 14200
        assert sec0.online is False
        assert sec0.pob_item_text is not None and "Sockets: S" in sec0.pob_item_text

        # Whisper listing without fee is `whisper`
        assert all(
            w.sale_type == SaleType.WHISPER and not w.is_instant_buyout
            for w in result.whisper_listings
        )

        # Both stash-wall spam warning and whisper-below-Instant-Buyout-floor warning emitted
        assert len(result.fixer_warnings) == 2
        assert any("WallFixer#9999" in w for w in result.fixer_warnings)
        assert any("Instant Buyout floor" in w for w in result.fixer_warnings)

        # Fixer's 2 ex whisper lowball is excluded from estimated_exalted / iqr_mean_exalted!
        assert result.min_exalted == 2.0
        assert result.p10_exalted is not None and result.p10_exalted > 2.0
        assert result.median_exalted == 50.0
        assert result.iqr_mean_exalted == pytest.approx(51.0)
        assert result.estimated_exalted == pytest.approx(51.0)
        assert result.estimated_divine == pytest.approx(51.0 / 450.0, rel=1e-3)
        assert result.iqr_mean_divine == result.estimated_divine
    finally:
        client.close()


def test_trade2_rate_limiter_respect_batch_fetch_and_4h_static_cache(tmp_path: Path) -> None:
    clock_now = 1000.0
    slept: list[float] = []
    fetch_batches: list[list[str]] = []
    static_hits = 0
    seen_search_statuses: list[str] = []

    def fake_clock() -> float:
        return clock_now

    def fake_sleep(seconds: float) -> None:
        nonlocal clock_now
        slept.append(seconds)
        clock_now += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal static_hits
        assert request.headers["User-Agent"] == USER_AGENT
        path = request.url.path

        if path == "/api/trade2/data/leagues":
            static_hits += 1
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": "Forbidden Rites", "realm": "poe2", "text": "Forbidden Rites"}
                    ]
                },
            )

        if path.startswith("/api/trade2/search/poe2/"):
            import json as _json

            payload = _json.loads(request.content.decode("utf-8"))
            seen_search_statuses.append(payload["query"]["status"]["option"])
            ids = [f"id_{i}" for i in range(23)]
            return httpx.Response(
                200,
                headers={
                    "X-Rate-Limit-Policy": "trade-search-request-limit",
                    "X-Rate-Limit-Rules": "Ip",
                    "X-Rate-Limit-Ip": "5:10:60",
                    "X-Rate-Limit-Ip-State": "5:10:0",
                },
                json={"id": "search_xyz", "complexity": 4, "result": ids, "total": 23},
            )

        if path.startswith("/api/trade2/fetch/"):
            batch_ids = path.removeprefix("/api/trade2/fetch/").split(",")
            fetch_batches.append(batch_ids)
            assert len(batch_ids) <= 10
            assert request.url.params.get("realm") == "poe2"
            assert request.url.params.get("query") == "search_xyz"
            entries = [
                _make_raw_fetch_entry(item_id, account=f"Acct#{item_id}", amount=10.0, fee=5000)
                for item_id in batch_ids
            ]
            return httpx.Response(
                200,
                headers={
                    "X-Rate-Limit-Policy": "trade-fetch-request-limit",
                    "X-Rate-Limit-Ip": "12:4:10",
                    "X-Rate-Limit-Ip-State": "1:4:0",
                },
                json={"result": entries},
            )

        raise AssertionError(f"Unexpected request path: {path}")

    limiter = GGGRateLimiter(safety_buffer=1.0, clock=fake_clock, sleep_sync=fake_sleep)
    http_client = Poe2HttpClient(
        rate_limiter=limiter,
        sync_transport=httpx.MockTransport(handler),
    )
    epoch = 50_000.0
    tclient = Trade2Client(
        http_client=http_client,
        cache_dir=tmp_path,
        now_fn=lambda: epoch,
    )
    try:
        # 1. Static cache respects 4h (14,400s) TTL
        lg1 = tclient.get_data_leagues()
        epoch += 3600.0  # +1h -> still cached
        lg2 = tclient.get_data_leagues()
        assert lg1 == lg2 and static_hits == 1
        epoch += 15_000.0  # >4h -> refreshes
        _ = tclient.get_data_leagues()
        assert static_hits == 2

        # 2. Default search uses status.option="available"; 23 IDs fetched in 10+10+3 chunks
        s1 = tclient.search({"type": "Solar Amulet"}, league="Forbidden Rites")
        assert seen_search_statuses == ["available"]
        assert s1.trade_url == build_trade_url("Forbidden Rites", "search_xyz")
        fetched = tclient.fetch(s1.result, query_id=s1.id, max_items=23)
        assert len(fetched) == 23
        assert [len(b) for b in fetch_batches] == [10, 10, 3]

        # 3. Prior search returned 5:10:0 on 5:10:60 -> next search sleeps 10s + 1s buffer!
        _ = tclient.search({"type": "Solar Amulet"}, league="Forbidden Rites", securable_only=True)
        assert seen_search_statuses == ["available", "securable"]
        assert slept == [pytest.approx(11.0)]
    finally:
        http_client.close()


# ---------------------------------------------------------------------------
# Deterministic Unit Tests: EconomyService Normalization, Zero-Guard & Guardrails
# ---------------------------------------------------------------------------


def test_economy_service_normalization_bulk_zero_guard_and_no_stash_url(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        assert "/stash/" not in request.url.path
        path = request.url.path

        if path == "/poe2/Leagues":
            return httpx.Response(
                200,
                json=[
                    {
                        "Value": "Forbidden Rites",
                        "ShortName": "forbiddenrites",
                        "IsCurrent": True,
                        "DivinePrice": 450.0,
                        "ChaosDivinePrice": 12.5,
                        "BaseCurrencyApiId": "exalted",
                    }
                ],
            )
        if path == "/poe2/Leagues/Forbidden Rites/ReferenceCurrencies":
            assert b"Forbidden%20Rites" in request.url.raw_path
            return httpx.Response(
                200,
                json=[
                    {"ApiId": "exalted", "Text": "Exalted Orb", "RelativePrice": 1.0},
                    {"ApiId": "chaos", "Text": "Chaos Orb", "RelativePrice": 36.0},
                    {"ApiId": "divine", "Text": "Divine Orb", "RelativePrice": 450.0},
                ],
            )
        if path == "/poe2/Leagues/Forbidden Rites/Items":
            return httpx.Response(
                200,
                json=[
                    {
                        "ItemId": 1,
                        "CategoryApiId": "currency",
                        "ApiId": "exalted",
                        "Text": "Exalted Orb",
                        "CurrentPrice": 1.0,
                    },
                    {
                        "ItemId": 2,
                        "CategoryApiId": "currency",
                        "ApiId": "chaos",
                        "Text": "Chaos Orb",
                        "CurrentPrice": 36.0,
                    },
                    {
                        "ItemId": 3,
                        "CategoryApiId": "currency",
                        "ApiId": "divine",
                        "Text": "Divine Orb",
                        "CurrentPrice": 450.0,
                    },
                    {
                        "ItemId": 4,
                        "CategoryApiId": "currency",
                        "ApiId": "perfect-exalted-orb",
                        "Text": "Perfect Exalted Orb",
                        "CurrentPrice": 90.0,
                    },
                    {
                        "ItemId": 5,
                        "CategoryApiId": "ritual",
                        "ApiId": "omen-of-whittling",
                        "Text": "Omen of Whittling",
                        "CurrentPrice": 1400.0,
                    },
                    # Zero-priced item in poe2scout that must NEVER become a 0-cost crafting orb
                    {
                        "ItemId": 6,
                        "CategoryApiId": "verisium",
                        "ApiId": "mutated-verisium",
                        "Text": "Mutated Verisium",
                        "CurrentPrice": 0,
                    },
                    # Base unique item
                    {
                        "ItemId": 99,
                        "CategoryApiId": "accessory",
                        "ApiId": None,
                        "Name": "Astramentis",
                        "Type": "Stellar Amulet",
                        "Text": "Astramentis Stellar Amulet",
                        "CurrentPrice": 45000.0,
                    },
                ],
            )
        if path == "/poe2/Leagues/Forbidden Rites/Uniques/ByCategory":
            return httpx.Response(
                200,
                json={
                    "CurrentPage": 1,
                    "Pages": 1,
                    "Total": 1,
                    "Items": [
                        {
                            "ItemId": 99,
                            "Name": "Astramentis",
                            "Type": "Stellar Amulet",
                            "CategoryApiId": "accessory",
                            "CurrentPrice": 45000.0,
                            "CurrentQuantity": 40,
                            "IsChanceable": True,
                            "PriceLogs": [
                                {"Price": 40000.0, "Time": "2026-11-16T00:00:00Z"},
                                {"Price": 45000.0, "Time": "2026-11-23T00:00:00Z"},
                            ],
                        }
                    ],
                },
            )
        if path == "/poe2/api/economy/exchange/current/overview":
            extype = request.url.params.get("type")
            if extype == "Currency":
                return httpx.Response(
                    200,
                    json={
                        "core": {"primary": "divine", "rates": {"exalted": 450.0, "chaos": 12.5}},
                        "lines": [
                            {
                                "id": "divine",
                                "primaryValue": 1.0,
                                "volumePrimaryValue": 8000.0,
                                "maxVolumeCurrency": "exalted",
                                "sparkline": {"totalChange": 5.0},
                            },
                            {
                                "id": "chaos",
                                "primaryValue": 0.08,
                                "volumePrimaryValue": 3200.0,
                                "maxVolumeCurrency": "divine",
                                "sparkline": {"totalChange": -2.0},
                            },
                            {
                                "id": "perfect-exalted-orb",
                                "primaryValue": 0.25,
                                "volumePrimaryValue": 120.0,
                                "maxVolumeCurrency": "divine",
                                "sparkline": {"totalChange": 28.5},
                            },
                        ],
                        "items": [
                            {"id": "divine", "name": "Divine Orb", "detailsId": "divine-orb"},
                            {"id": "chaos", "name": "Chaos Orb", "detailsId": "chaos-orb"},
                            {
                                "id": "perfect-exalted-orb",
                                "name": "Perfect Exalted Orb",
                                "detailsId": "perfect-exalted-orb",
                            },
                        ],
                    },
                )
            if extype == "Ritual":
                return httpx.Response(
                    200,
                    json={
                        "core": {"primary": "divine", "rates": {"exalted": 450.0, "chaos": 12.5}},
                        "lines": [
                            {
                                "id": "omen-of-whittling",
                                "primaryValue": 4.0,
                                "volumePrimaryValue": 55.0,
                                "maxVolumeCurrency": "divine",
                                "sparkline": {"totalChange": 18.0},
                            },
                        ],
                        "items": [
                            {
                                "id": "omen-of-whittling",
                                "name": "Omen of Whittling",
                                "detailsId": "omen-of-whittling",
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "core": {"primary": "divine", "rates": {"exalted": 450.0, "chaos": 12.5}},
                    "lines": [],
                    "items": [],
                },
            )

        raise AssertionError(f"Unexpected path: {path}")

    http_client = Poe2HttpClient(sync_transport=httpx.MockTransport(handler))
    econ = EconomyService(
        http_client=http_client,
        cache_dir=tmp_path,
        ninja_exchange_types=("Currency", "Ritual"),
    )
    try:
        # Guardrail: poe.ninja /stash/ endpoint is explicitly forbidden
        with pytest.raises(ValueError, match="stash"):
            econ._ninja_url("/economy/stash/current/item/overview")

        leagues = econ.list_leagues()
        assert leagues[0].id == "Forbidden Rites"
        assert leagues[0].base_currency == "exalted"

        rates = econ.get_reference_rates("Forbidden Rites")
        assert rates.exalted == 1.0
        assert rates.divine_exalted == pytest.approx(450.0)
        assert rates.chaos_exalted == pytest.approx(36.0)

        ex_quote = econ.get_price("exalted", "Forbidden Rites")
        div_quote = econ.get_price("Divine Orb", "Forbidden Rites")
        omen_quote = econ.get_price("omen_whittling", "Forbidden Rites")
        uniq_quote = econ.get_price("astramentis", "Forbidden Rites")

        assert ex_quote is not None and ex_quote.exalted == 1.0
        assert div_quote is not None and div_quote.divine == pytest.approx(1.0)
        assert omen_quote is not None
        assert omen_quote.exalted == pytest.approx(1800.0)  # 4.0 div * 450 ex
        assert omen_quote.chaos == pytest.approx(50.0)
        assert omen_quote.divine == pytest.approx(4.0)
        assert uniq_quote is not None and uniq_quote.divine == pytest.approx(100.0)

        # Bulk crafting prices: NEVER 0 for unpriced/removed orbs!
        bulk = econ.get_bulk_crafting_prices("Forbidden Rites")
        assert bulk["exalted"] is not None and bulk["exalted"].exalted == 1.0
        assert bulk["exalted_perfect"] is not None and bulk[
            "exalted_perfect"
        ].exalted == pytest.approx(112.5)
        assert bulk["mutated-verisium"] is None
        assert bulk["omen-of-homogenising-exaltation"] is None
        assert bulk["recombinator"] is None
        assert all(q is None or q.exalted > 0 for q in bulk.values())

        # Market scanner surfaces high-volume mover, cross-source spread, and chanceable unique
        opps = econ.scan_market_opportunities("Forbidden Rites", min_volume_divine=5.0)
        kinds = {o.kind for o in opps}
        assert "high_volume_mover" in kinds
        assert "cross_source_spread" in kinds
        assert "chanceable_unique" in kinds
        assert all("/stash/" not in u for u in requested_urls)
    finally:
        http_client.close()


def test_trade2_exchange_defaults_status_to_online(tmp_path: Path) -> None:
    import asyncio
    import json as _json

    from poe2brain.trade import ArbitrageOpportunity, ArbitrageScanner, ArbitrageScanReport

    assert ArbitrageScanner is not None
    assert ArbitrageScanReport is not None
    assert ArbitrageOpportunity is not None

    seen_exchange_statuses: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/trade2/exchange/poe2/"):
            payload = _json.loads(request.content.decode("utf-8"))
            seen_exchange_statuses.append(payload["query"]["status"]["option"])
            return httpx.Response(
                200,
                json={"id": "exch_123", "total": 4, "result": {}},
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    http_client = Poe2HttpClient(
        sync_transport=httpx.MockTransport(handler),
        async_transport=httpx.MockTransport(handler),
    )
    tclient = Trade2Client(http_client=http_client, cache_dir=tmp_path)
    try:
        res_sync = tclient.exchange("divine", "chaos", league="Forbidden Rites")
        assert res_sync.id == "exch_123"
        res_any = tclient.exchange("divine", "chaos", league="Forbidden Rites", status_option="any")
        assert res_any.id == "exch_123"
        res_async = asyncio.run(tclient.aexchange("divine", "chaos", league="Forbidden Rites"))
        assert res_async.id == "exch_123"
        assert seen_exchange_statuses == ["online", "any", "online"]
    finally:
        http_client.close()


def test_economy_reliquary_key_disambiguation_troll_filter_and_lag_free_spread(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/poe2/Leagues/Forbidden Rites/ReferenceCurrencies":
            # poe2scout Divine=510 Ex vs poe.ninja Divine=450 Ex (13.3% Ex/Div index lag)
            return httpx.Response(
                200,
                json=[
                    {"ApiId": "exalted", "Text": "Exalted Orb", "RelativePrice": 1.0},
                    {"ApiId": "chaos", "Text": "Chaos Orb", "RelativePrice": 40.8},
                    {"ApiId": "divine", "Text": "Divine Orb", "RelativePrice": 510.0},
                ],
            )
        if path == "/poe2/Leagues/Forbidden Rites/Items":
            return httpx.Response(
                200,
                json=[
                    {
                        "ItemId": 1,
                        "CategoryApiId": "currency",
                        "ApiId": "exalted",
                        "Text": "Exalted Orb",
                        "CurrentPrice": 1.0,
                    },
                    {
                        "ItemId": 2,
                        "CategoryApiId": "currency",
                        "ApiId": "chaos",
                        "Text": "Chaos Orb",
                        "CurrentPrice": 40.8,
                    },
                    {
                        "ItemId": 3,
                        "CategoryApiId": "currency",
                        "ApiId": "divine",
                        "Text": "Divine Orb",
                        "CurrentPrice": 510.0,
                    },
                    # Placeholder internal item that must be filtered out
                    {
                        "ItemId": 10,
                        "CategoryApiId": "weapon",
                        "ApiId": None,
                        "Name": "INCOMPLETE Dull Hatchet",
                        "Type": "Dull Hatchet",
                        "Text": "INCOMPLETE Dull Hatchet",
                        "CurrentPrice": 999.0,
                    },
                    # Unique Jewel vs Zarokh's Reliquary Key sharing ApiId="against-the-darkness"
                    {
                        "ItemId": 221,
                        "CategoryApiId": "jewel",
                        "ApiId": None,
                        "Name": "Against the Darkness",
                        "Type": "Time-Lost Diamond",
                        "Text": "Against the Darkness Time-Lost Diamond",
                        "CurrentPrice": 1980.0,
                    },
                    {
                        "ItemId": 477,
                        "CategoryApiId": "vaultkeys",
                        "ApiId": "against-the-darkness",
                        "Name": None,
                        "Text": "Zarokh's Reliquary Key: Against the Darkness",
                        "CurrentPrice": 99000.0,
                    },
                    # Priced Unique Armour vs unpriced Reliquary Key on ApiId="sandstorm-visage"
                    {
                        "ItemId": 230,
                        "CategoryApiId": "armour",
                        "ApiId": None,
                        "Name": "Sandstorm Visage",
                        "Type": "Chain Tiara",
                        "Text": "Sandstorm Visage Chain Tiara",
                        "CurrentPrice": 2250.0,
                    },
                    {
                        "ItemId": 480,
                        "CategoryApiId": "vaultkeys",
                        "ApiId": "sandstorm-visage",
                        "Name": None,
                        "Text": "Zarokh's Reliquary Key: Sandstorm Visage",
                        "CurrentPrice": None,
                    },
                    # Zero-volume single-listing poe2scout troll quotes
                    {
                        "ItemId": 301,
                        "CategoryApiId": "currency",
                        "ApiId": "orb-of-transmutation",
                        "Text": "Orb of Transmutation",
                        "CurrentPrice": 2.2,
                    },
                    {
                        "ItemId": 302,
                        "CategoryApiId": "currency",
                        "ApiId": "greater-orb-of-transmutation",
                        "Text": "Greater Orb of Transmutation",
                        "CurrentPrice": 5.5,
                    },
                    {
                        "ItemId": 303,
                        "CategoryApiId": "currency",
                        "ApiId": "perfect-orb-of-transmutation",
                        "Text": "Perfect Orb of Transmutation",
                        "CurrentPrice": 14.0,
                    },
                    {
                        "ItemId": 304,
                        "CategoryApiId": "currency",
                        "ApiId": "transmutation-shard",
                        "Text": "Transmutation Shard",
                        "CurrentPrice": 414.4,
                    },
                    {
                        "ItemId": 310,
                        "CategoryApiId": "runes",
                        "ApiId": "lesser-charging-rune",
                        "Text": "Lesser Charging Rune",
                        "CurrentPrice": 7649.0,
                    },
                    {
                        "ItemId": 311,
                        "CategoryApiId": "runes",
                        "ApiId": "greater-charging-rune",
                        "Text": "Greater Charging Rune",
                        "CurrentPrice": 2.0,
                    },
                    {
                        "ItemId": 312,
                        "CategoryApiId": "runes",
                        "ApiId": "lesser-glacial-rune",
                        "Text": "Lesser Glacial Rune",
                        "CurrentPrice": 503.0,
                    },
                    {
                        "ItemId": 320,
                        "CategoryApiId": "essences",
                        "ApiId": "essence-of-ruin",
                        "Text": "Essence of Ruin",
                        "CurrentPrice": 1343.0,
                    },
                    {
                        "ItemId": 321,
                        "CategoryApiId": "essences",
                        "ApiId": "greater-essence-of-ruin",
                        "Text": "Greater Essence of Ruin",
                        "CurrentPrice": 1.2,
                    },
                    # Pure Ex/Div index-lag item: 2.0 Div in scout (1020/510) AND 2.0 Div in ninja
                    {
                        "ItemId": 401,
                        "CategoryApiId": "currency",
                        "ApiId": "perfect-exalted-orb",
                        "Text": "Perfect Exalted Orb",
                        "CurrentPrice": 1020.0,
                    },
                    # Genuine Divine-normalized spread: 2.0 Div scout (1020/510) vs 2.6 Div ninja
                    {
                        "ItemId": 402,
                        "CategoryApiId": "currency",
                        "ApiId": "vaal-cultivation-orb",
                        "Text": "Vaal Cultivation Orb",
                        "CurrentPrice": 1020.0,
                    },
                ],
            )
        if path == "/poe2/api/economy/exchange/current/overview":
            extype = request.url.params.get("type")
            core = {"primary": "divine", "rates": {"exalted": 450.0, "chaos": 12.5}}
            if extype == "Currency":
                return httpx.Response(
                    200,
                    json={
                        "core": core,
                        "lines": [
                            {
                                "id": "divine",
                                "primaryValue": 1.0,
                                "volumePrimaryValue": 8500.0,
                                "maxVolumeCurrency": "chaos",
                            },
                            {
                                "id": "chaos",
                                "primaryValue": 0.08,
                                "volumePrimaryValue": 4000.0,
                                "maxVolumeCurrency": "divine",
                            },
                            {
                                "id": "orb-of-transmutation",
                                "primaryValue": 2.16 / 450.0,
                                "volumePrimaryValue": 12.0,
                            },
                            {
                                "id": "greater-orb-of-transmutation",
                                "primaryValue": 0.28 / 450.0,
                                "volumePrimaryValue": 0.02,
                            },
                            {
                                "id": "perfect-orb-of-transmutation",
                                "primaryValue": 13.6 / 450.0,
                                "volumePrimaryValue": 5.0,
                            },
                            {
                                "id": "prismatic-alloy",
                                "primaryValue": 0.05 / 450.0,
                                "volumePrimaryValue": 0.01,
                            },
                            {
                                "id": "perfect-exalted-orb",
                                "primaryValue": 2.0,
                                "volumePrimaryValue": 500.0,
                                "maxVolumeCurrency": "divine",
                            },
                            {
                                "id": "vaal-cultivation-orb",
                                "primaryValue": 2.6,
                                "volumePrimaryValue": 120.0,
                                "maxVolumeCurrency": "divine",
                            },
                        ],
                        "items": [
                            {"id": "divine", "name": "Divine Orb"},
                            {"id": "chaos", "name": "Chaos Orb"},
                            {"id": "orb-of-transmutation", "name": "Orb of Transmutation"},
                            {
                                "id": "greater-orb-of-transmutation",
                                "name": "Greater Orb of Transmutation",
                            },
                            {
                                "id": "perfect-orb-of-transmutation",
                                "name": "Perfect Orb of Transmutation",
                            },
                            {"id": "prismatic-alloy", "name": "Prismatic Alloy"},
                            {"id": "perfect-exalted-orb", "name": "Perfect Exalted Orb"},
                            {"id": "vaal-cultivation-orb", "name": "Vaal Cultivation Orb"},
                        ],
                    },
                )
            if extype == "Ritual":
                return httpx.Response(
                    200,
                    json={
                        "core": core,
                        "lines": [
                            {
                                "id": "omen-of-the-liege",
                                "primaryValue": 0.09 / 450.0,
                                "volumePrimaryValue": 0.01,
                            }
                        ],
                        "items": [{"id": "omen-of-the-liege", "name": "Omen of the Liege"}],
                    },
                )
            if extype == "Essences":
                return httpx.Response(
                    200,
                    json={
                        "core": core,
                        "lines": [
                            {
                                "id": "greater-essence-of-ruin",
                                "primaryValue": 1.2 / 450.0,
                                "volumePrimaryValue": 2.0,
                            },
                            {
                                "id": "lesser-essence-of-sorcery",
                                "primaryValue": 1.0,
                                "volumePrimaryValue": 0.01,
                            },
                            {
                                "id": "greater-essence-of-sorcery",
                                "primaryValue": 23.0 / 450.0,
                                "volumePrimaryValue": 4.0,
                            },
                        ],
                        "items": [
                            {"id": "greater-essence-of-ruin", "name": "Greater Essence of Ruin"},
                            {
                                "id": "lesser-essence-of-sorcery",
                                "name": "Lesser Essence of Sorcery",
                            },
                            {
                                "id": "greater-essence-of-sorcery",
                                "name": "Greater Essence of Sorcery",
                            },
                        ],
                    },
                )
            if extype == "Runes":
                return httpx.Response(
                    200,
                    json={
                        "core": core,
                        "lines": [
                            {
                                "id": "greater-charging-rune",
                                "primaryValue": 2.0 / 450.0,
                                "volumePrimaryValue": 3.0,
                            },
                            {
                                "id": "lesser-glacial-rune",
                                "primaryValue": 28.0 / 450.0,
                                "volumePrimaryValue": 0.04,
                            },
                        ],
                        "items": [
                            {"id": "greater-charging-rune", "name": "Greater Charging Rune"},
                            {"id": "lesser-glacial-rune", "name": "Lesser Glacial Rune"},
                        ],
                    },
                )
            if extype == "Fragments":
                return httpx.Response(
                    200,
                    json={
                        "core": core,
                        "lines": [
                            {
                                "id": "against-the-darkness",
                                "primaryValue": 220.0,
                                "volumePrimaryValue": 15.0,
                            }
                        ],
                        "items": [
                            {
                                "id": "against-the-darkness",
                                "name": "Zarokh's Reliquary Key: Against the Darkness",
                                "detailsId": "zarokhs-reliquary-key-against-the-darkness",
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"core": core, "lines": [], "items": []})
        if path == "/poe2/Leagues/Forbidden Rites/Uniques/ByCategory":
            return httpx.Response(200, json={"CurrentPage": 1, "Pages": 1, "Total": 0, "Items": []})
        raise AssertionError(f"Unexpected path: {path}")

    http_client = Poe2HttpClient(sync_transport=httpx.MockTransport(handler))
    econ = EconomyService(
        http_client=http_client,
        cache_dir=tmp_path,
        ninja_exchange_types=("Currency", "Ritual", "Essences", "Runes", "Fragments"),
    )
    try:
        # 1. INCOMPLETE placeholder rows filtered out
        assert econ.get_price("INCOMPLETE Dull Hatchet", "Forbidden Rites") is None

        # 2. Reliquary Key disambiguation preserves Unique Jewel & Unique Armour prices
        atd = econ.get_price("Against the Darkness", "Forbidden Rites")
        zrk = econ.get_price("Zarokh's Reliquary Key: Against the Darkness", "Forbidden Rites")
        sv = econ.get_price("Sandstorm Visage", "Forbidden Rites")
        assert atd is not None and atd.category == "jewel"
        assert atd.exalted == pytest.approx(1980.0)
        assert atd.divine == pytest.approx(1980.0 / 450.0)
        assert zrk is not None and zrk.category == "vaultkeys"
        assert zrk.divine == pytest.approx(220.0)
        assert sv is not None and sv.category == "armour"
        assert sv.exalted == pytest.approx(2250.0)

        # 3. Zero-volume poe2scout troll quotes dropped; low-vol executed ninja trade kept
        assert econ.get_price("Transmutation Shard", "Forbidden Rites") is None
        assert econ.get_price("Lesser Charging Rune", "Forbidden Rites") is None
        assert econ.get_price("Essence of Ruin", "Forbidden Rites") is None
        glacial = econ.get_price("Lesser Glacial Rune", "Forbidden Rites")
        assert glacial is not None and glacial.exalted == pytest.approx(28.0)

        # 4. Bulk crafting prices sanitize thin-volume dust outliers & tier inversions
        bulk = econ.get_bulk_crafting_prices("Forbidden Rites")
        assert bulk["omen-of-the-liege"] is not None
        assert bulk["omen-of-the-liege"].exalted == pytest.approx(25.0)
        assert bulk["prismatic-alloy"] is not None
        assert bulk["prismatic-alloy"].exalted == pytest.approx(25.0)
        assert (
            bulk["orb-of-transmutation"].exalted  # type: ignore[union-attr]
            <= bulk["greater-orb-of-transmutation"].exalted  # type: ignore[union-attr]
            <= bulk["perfect-orb-of-transmutation"].exalted  # type: ignore[union-attr]
        )
        assert (
            bulk["lesser-essence-of-sorcery"].exalted  # type: ignore[union-attr]
            < bulk["greater-essence-of-sorcery"].exalted  # type: ignore[union-attr]
        )
        assert bulk["omen-of-homogenising-exaltation"] is None
        assert all(q is None or q.exalted > 0 for q in bulk.values())

        # 5. Cross-source spread normalizes by each source's Divine rate (no fake Ex/Div lag!)
        opps = econ.scan_market_opportunities("Forbidden Rites", min_volume_divine=5.0)
        spread_ids = {o.id for o in opps if o.kind == "cross_source_spread"}
        assert "perfect-exalted-orb" not in spread_ids
        assert "divine" not in spread_ids
        assert "vaal-cultivation-orb" in spread_ids
    finally:
        http_client.close()


# ---------------------------------------------------------------------------
# Live Integration Tests against `Forbidden Rites`
# ---------------------------------------------------------------------------


def test_live_economy_service_forbidden_rites(tmp_path: Path) -> None:
    with EconomyService(cache_dir=tmp_path) as econ:
        leagues = econ.list_leagues()
        league_ids = {lg.id for lg in leagues}
        assert DEFAULT_LEAGUE in league_ids

        rates = econ.get_reference_rates(DEFAULT_LEAGUE)
        assert rates.exalted == 1.0
        assert rates.divine_exalted > 50.0
        assert rates.chaos_exalted > 1.0

        # Verify lookup across currencies, omens, essences, soul cores, runes, bones, gems, uniques
        queries = [
            "exalted",
            "Divine Orb",
            "Omen of Whittling",
            "Greater Essence of Sorcery",
            "Soul Core of Tacati",
            "Iron Rune",
            "Ancient Jawbone",
            "Exceptional Verisium",
            "Uncut Skill Gem (Level 18)",
            "Tecrod's Revenge",
            "Mageblood",
        ]
        for q in queries:
            quote = econ.get_price(q, league=DEFAULT_LEAGUE)
            assert quote is not None, f"Expected live quote for {q!r} in {DEFAULT_LEAGUE}"
            assert quote.exalted > 0
            assert quote.chaos is not None and quote.chaos > 0
            assert quote.divine is not None and quote.divine > 0

        assert econ.get_price("exalted", league=DEFAULT_LEAGUE).exalted == 1.0  # type: ignore[union-attr]
        atd_live = econ.get_price("Against the Darkness", league=DEFAULT_LEAGUE)
        assert atd_live is not None and atd_live.category == "jewel"

        bulk = econ.get_bulk_crafting_prices(DEFAULT_LEAGUE)
        assert bulk["exalted"] is not None and bulk["exalted"].exalted == 1.0
        assert bulk["omen-of-whittling"] is not None and bulk["omen-of-whittling"].exalted > 1.0
        assert all(v is None or v.exalted > 0 for v in bulk.values())

        ritual_quotes = econ.get_currencies_by_category("ritual", league=DEFAULT_LEAGUE)
        assert len(ritual_quotes) >= 10

        acc_uniques = econ.get_uniques_by_category("accessory", league=DEFAULT_LEAGUE, per_page=30)
        assert len(acc_uniques) >= 5

        div_hist = econ.get_item_history("divine", league=DEFAULT_LEAGUE, log_count=24)
        assert len(div_hist) >= 2

        pair_hist = econ.get_pair_history("divine", "exalted", league=DEFAULT_LEAGUE, limit=12)
        assert len(pair_hist) >= 1

        opps = econ.scan_market_opportunities(DEFAULT_LEAGUE, min_volume_divine=5.0, limit=15)
        assert len(opps) >= 3


def test_live_trade2_client_search_fetch_and_exchange(tmp_path: Path) -> None:
    with (
        EconomyService(cache_dir=tmp_path, ninja_exchange_types=("Currency",)) as econ,
        Trade2Client(
            economy=econ,
            cache_dir=tmp_path,
        ) as client,
    ):
        leagues = client.get_data_leagues()
        assert any(lg.get("id") == DEFAULT_LEAGUE for lg in leagues)

        pc = client.price_check(
            {
                "type": "Solar Amulet",
                "filters": {
                    "type_filters": {"filters": {"rarity": {"option": "rare"}}},
                },
            },
            league=DEFAULT_LEAGUE,
            max_fetch=10,
        )
        assert isinstance(pc, PriceCheckResult)
        assert pc.total_matches > 0
        assert pc.query_id
        assert (
            pc.trade_url
            == f"https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/{pc.query_id}"
        )
        assert len(pc.listings) >= 1
        assert pc.estimated_exalted is not None and pc.estimated_exalted > 0
        assert pc.estimated_divine is not None and pc.estimated_divine > 0

        for listing in pc.listings:
            assert listing.sale_type in (SaleType.SECURABLE, SaleType.WHISPER)
            assert listing.pob_item_text is not None
            for mod_line in listing.mod_lines:
                assert "[" not in mod_line and "]" not in mod_line

        exch = client.exchange("exalted", "divine", league=DEFAULT_LEAGUE)
        assert exch.id
        assert exch.total >= 0
