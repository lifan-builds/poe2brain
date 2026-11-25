# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from poe2brain.http import GGGRateLimiter, Poe2HttpClient
from poe2brain.trade.arbitrage import (
    ArbitrageMechanism,
    ArbitrageScanner,
    compute_profit_per_10k_gold,
    estimate_alva_gold_per_unit,
    estimate_leg_gold,
)
from poe2brain.trade.economy import EconomyService
from poe2brain.trade.trade2_client import Trade2Client


def _build_mock_economy_and_trade2(
    tmp_path: Path,
    *,
    scout_refs: list[dict[str, Any]],
    scout_items: list[dict[str, Any]],
    ninja_by_type: dict[str, dict[str, Any]],
    pair_histories: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
    exchange_books: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    seen_exchange_statuses: list[str] | None = None,
) -> tuple[EconomyService, Trade2Client]:
    pair_map = pair_histories or {}
    books = exchange_books or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        path = request.url.path

        if path.endswith("/ReferenceCurrencies"):
            return httpx.Response(200, json=scout_refs)
        if path.endswith("/Items"):
            return httpx.Response(200, json=scout_items)
        if "/Currencies/Pairs/" in path and path.endswith("/History"):
            parts = path.split("/")
            id1, id2 = int(parts[-3]), int(parts[-2])
            return httpx.Response(
                200,
                json={"History": pair_map.get((id1, id2), []), "Meta": {"HasMore": False}},
            )
        if "/Currencies/" in path:
            slug = path.rsplit("/", 1)[-1]
            for row in scout_items:
                if row.get("ApiId") == slug:
                    return httpx.Response(
                        200,
                        json={
                            "CurrencyItemId": row.get("ItemId", 1),
                            "ItemId": row.get("ItemId", 1),
                            "ApiId": slug,
                            "CurrentPrice": row.get("CurrentPrice"),
                        },
                    )
            return httpx.Response(404, json={"error": "not found"})
        if "/Items/" in path and path.endswith("/History"):
            item_id = int(path.split("/")[-2])
            for row in scout_items:
                if row.get("ItemId") == item_id:
                    return httpx.Response(
                        200,
                        json={
                            "PriceHistory": [
                                {
                                    "Time": "2026-11-23T04:00:00Z",
                                    "Price": row.get("CurrentPrice", 1.0),
                                    "Quantity": 250,
                                }
                            ]
                        },
                    )
            return httpx.Response(200, json={"PriceHistory": []})

        if path.endswith("/economy/exchange/current/overview"):
            extype = request.url.params.get("type", "Currency")
            payload = ninja_by_type.get(
                extype,
                {
                    "core": {
                        "primary": "divine",
                        "secondary": "chaos",
                        "rates": {"exalted": 450.0, "chaos": 12.5},
                        "items": [],
                    },
                    "lines": [],
                    "items": [],
                },
            )
            return httpx.Response(200, json=payload)

        if path.startswith("/api/trade2/exchange/poe2/"):
            body = json.loads(request.content.decode("utf-8"))
            status_opt = body["query"]["status"]["option"]
            if seen_exchange_statuses is not None:
                seen_exchange_statuses.append(status_opt)
            have_c = body["query"]["have"][0]
            want_c = body["query"]["want"][0]
            entries = books.get((have_c, want_c), [])
            return httpx.Response(
                200,
                json={"id": f"ex_{have_c}_{want_c}", "total": len(entries), "result": entries},
            )

        raise AssertionError(f"Unhandled mock URL: {url_str}")

    limiter = GGGRateLimiter(safety_buffer=0.0, clock=lambda: 1000.0, sleep_sync=lambda _: None)
    http_client = Poe2HttpClient(
        rate_limiter=limiter,
        sync_transport=httpx.MockTransport(handler),
    )
    econ = EconomyService(
        http_client=http_client,
        cache_dir=tmp_path / "econ_cache",
        ninja_exchange_types=tuple(ninja_by_type.keys()),
    )
    t2 = Trade2Client(
        http_client=http_client,
        economy=econ,
        cache_dir=tmp_path / "t2_cache",
    )
    return econ, t2


def _make_exchange_entry(
    listing_id: str,
    *,
    account: str,
    online: bool,
    have_curr: str,
    have_amt: float,
    want_curr: str,
    want_amt: float,
    stock: int,
) -> dict[str, Any]:
    return {
        "id": listing_id,
        "listing": {
            "account": {
                "name": account,
                "online": {"league": "Forbidden Rites"} if online else None,
            },
            "offers": [
                {
                    "exchange": {"currency": have_curr, "amount": have_amt},
                    "item": {
                        "currency": want_curr,
                        "amount": want_amt,
                        "stock": stock,
                        "id": listing_id,
                    },
                }
            ],
        },
    }


def _ninja_payload(
    lines: list[dict[str, Any]],
    items: list[dict[str, str]],
    *,
    div_ex: float = 450.0,
    div_chaos: float = 12.5,
) -> dict[str, Any]:
    return {
        "core": {
            "primary": "divine",
            "secondary": "chaos",
            "rates": {"exalted": div_ex, "chaos": div_chaos},
            "items": [
                {"id": "divine", "name": "Divine Orb"},
                {"id": "chaos", "name": "Chaos Orb"},
                {"id": "exalted", "name": "Exalted Orb"},
            ],
        },
        "lines": lines,
        "items": items,
    }


def test_gold_tax_brackets_and_exalted_churn_penalty() -> None:
    assert estimate_alva_gold_per_unit("simulacrum-splinter", 4.6) == 30
    assert estimate_alva_gold_per_unit("exalted", 1.0) == 100
    assert estimate_alva_gold_per_unit("chaos", 36.0) == 280
    assert estimate_alva_gold_per_unit("divine", 450.0) == 1000
    assert estimate_alva_gold_per_unit("omen-of-whittling", 6400.0) == 1100
    assert estimate_alva_gold_per_unit("uul-netols-embrace", 500_000.0) == 2500

    # Flipping 450 Exalted <-> 1 Divine burns ~46,000 Gold (< 2.0 Ex / 10k Gold on a 2% spread)
    div_ex_gold = estimate_leg_gold("exalted", 450, 1.0) + estimate_leg_gold("divine", 1, 450.0)
    assert div_ex_gold == 46_000
    assert compute_profit_per_10k_gold(9.0, div_ex_gold) == pytest.approx(1.96, abs=0.05)

    # 2-socket Perfect Rune extraction moves 9 units (~4,590 Gold) for > 1,500 Ex profit
    rune_gold = (
        estimate_leg_gold("greater-iron-rune", 2, 15.1)
        + estimate_leg_gold("masterwork-rune", 2, 1050.0)
        + estimate_leg_gold("orb-of-extraction", 1, 710.0)
        + estimate_leg_gold("artificers", 2, 10.7)
        + estimate_leg_gold("perfect-iron-rune", 2, 2375.0)
    )
    assert rune_gold < 6_500
    assert compute_profit_per_10k_gold(1800.0, rune_gold) > 2500.0


def test_numeraire_lag_stripped_while_real_cross_denomination_detected(tmp_path: Path) -> None:
    # poe2scout has Divine = 520 Ex (+15.56% lag vs poe.ninja Divine = 450 Ex)
    scout_refs = [
        {"ApiId": "exalted", "RelativePrice": 1.0},
        {"ApiId": "chaos", "RelativePrice": 41.6},
        {"ApiId": "divine", "RelativePrice": 520.0},
    ]
    scout_items = [
        {
            "ItemId": 1,
            "ApiId": "exalted",
            "Text": "Exalted Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 1.0,
        },
        {
            "ItemId": 2,
            "ApiId": "chaos",
            "Text": "Chaos Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 41.6,
        },
        {
            "ItemId": 3,
            "ApiId": "divine",
            "Text": "Divine Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 520.0,
        },
        # Perfect Chaos Orb: 11.88 Div * 520 = 6177.6 Ex on scout vs 5346.0 Ex on ninja
        # Apparent raw spread = +15.56%, true Divine spread = 0.0% -> pure numeraire lag!
        {
            "ItemId": 10,
            "ApiId": "perfect-chaos-orb",
            "Text": "Perfect Chaos Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 6177.6,
        },
        # Chase Lineage Gem: 1000 Div on ninja, 750 Div on scout, only 2000 Div vol = 2 units!
        {
            "ItemId": 11,
            "ApiId": "uul-netols-embrace",
            "Text": "Uul-Netol's Embrace",
            "CategoryApiId": "lineagesupportgems",
            "CurrentPrice": 390_000.0,
        },
        # Masterwork Rune: cheaper in Ex/Chaos book (1.60 Div = 832 Ex) vs 2.36 Div on ninja!
        {
            "ItemId": 12,
            "ApiId": "masterwork-rune",
            "Text": "Masterwork Rune",
            "CategoryApiId": "runes",
            "CurrentPrice": 832.0,
        },
    ]
    ninja_by_type = {
        "Currency": _ninja_payload(
            lines=[
                {
                    "id": "divine",
                    "primaryValue": 1.0,
                    "volumePrimaryValue": 8500.0,
                    "maxVolumeCurrency": "chaos",
                    "maxVolumeRate": 0.08,
                },
                {
                    "id": "chaos",
                    "primaryValue": 0.08,
                    "volumePrimaryValue": 8500.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 12.5,
                },
                {
                    "id": "exalted",
                    "primaryValue": 1.0 / 450.0,
                    "volumePrimaryValue": 200.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 450.0,
                },
                {
                    "id": "perfect-chaos-orb",
                    "primaryValue": 11.88,
                    "volumePrimaryValue": 1980.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.08418,
                },
            ],
            items=[{"id": "perfect-chaos-orb", "name": "Perfect Chaos Orb"}],
        ),
        "LineageSupportGems": _ninja_payload(
            lines=[
                {
                    "id": "uul-netols-embrace",
                    "primaryValue": 1000.0,
                    "volumePrimaryValue": 2000.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.001,
                },
            ],
            items=[{"id": "uul-netols-embrace", "name": "Uul-Netol's Embrace"}],
        ),
        "Runes": _ninja_payload(
            lines=[
                {
                    "id": "masterwork-rune",
                    "primaryValue": 2.36,
                    "volumePrimaryValue": 110.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.4237,
                },
            ],
            items=[{"id": "masterwork-rune", "name": "Masterwork Rune"}],
        ),
    }

    econ, t2 = _build_mock_economy_and_trade2(
        tmp_path,
        scout_refs=scout_refs,
        scout_items=scout_items,
        ninja_by_type=ninja_by_type,
    )
    try:
        with ArbitrageScanner(economy=econ, trade2=t2) as scanner:
            opps, traps = scanner.scan_cross_denomination_spreads("Forbidden Rites")

        opp_ids = {o.id for o in opps}
        assert "cross_denom:perfect-chaos-orb" not in opp_ids
        assert "cross_denom:uul-netols-embrace" not in opp_ids
        assert "cross_denom:masterwork-rune" in opp_ids

        mw_opp = next(o for o in opps if o.id == "cross_denom:masterwork-rune")
        assert mw_opp.mechanism == ArbitrageMechanism.CROSS_DENOMINATION
        assert mw_opp.gross_profit_exalted > 100.0
        assert any("pair history" in n.lower() for n in mw_opp.risk_notes)

        lag_traps = [t for t in traps if t.kind == "numeraire_index_lag"]
        assert any(
            t.item_or_pair == "Perfect Chaos Orb" and t.true_executable_spread_pct == 0.0
            for t in lag_traps
        )

        thin_traps = [t for t in traps if t.kind == "illiquid_quote_trap"]
        assert any("Uul-Netol's Embrace" in t.item_or_pair for t in thin_traps)
    finally:
        t2.close()
        econ.close()


def test_live_exchange_shark_misclick_defense_and_triangular_cycle(tmp_path: Path) -> None:
    seen_statuses: list[str] = []
    scout_refs = [
        {"ApiId": "exalted", "RelativePrice": 1.0},
        {"ApiId": "chaos", "RelativePrice": 36.0},
        {"ApiId": "divine", "RelativePrice": 450.0},
    ]
    scout_items = [
        {
            "ItemId": 1,
            "ApiId": "exalted",
            "Text": "Exalted Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 1.0,
        },
        {
            "ItemId": 2,
            "ApiId": "chaos",
            "Text": "Chaos Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 36.0,
        },
        {
            "ItemId": 3,
            "ApiId": "divine",
            "Text": "Divine Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 450.0,
        },
    ]
    ninja_by_type = {
        "Currency": _ninja_payload(
            lines=[
                {
                    "id": "divine",
                    "primaryValue": 1.0,
                    "volumePrimaryValue": 8000.0,
                    "maxVolumeCurrency": "chaos",
                    "maxVolumeRate": 0.08,
                },
                {
                    "id": "chaos",
                    "primaryValue": 0.08,
                    "volumePrimaryValue": 8000.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 12.5,
                },
                {
                    "id": "exalted",
                    "primaryValue": 1.0 / 450.0,
                    "volumePrimaryValue": 200.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 450.0,
                },
            ],
            items=[],
            div_ex=450.0,
            div_chaos=12.5,
        )
    }

    # Construct books with:
    # 1. Offline fixer (must be ignored), misclick (`15 div -> 1 ex`, must be ignored),
    #    and shark bid (`1 div -> 100 ex`, `Hunterer#2308`) on divine/exalted.
    # 2. A genuine triangular cycle:
    #    1 divine -> 13.5 chaos (`divine -> chaos`)
    #    1 chaos -> 36.0 exalted (`chaos -> exalted`) -> 13.5 * 36 = 486 exalted
    #    455 exalted -> 1 divine (`exalted -> divine`) -> 486 / 455 = 1.0681 divine (+6.81%!)
    books = {
        ("divine", "exalted"): [
            _make_exchange_entry(
                "off_offline",
                account="DeadBot#0001",
                online=False,
                have_curr="divine",
                have_amt=1.0,
                want_curr="exalted",
                want_amt=470.0,
                stock=500,
            ),
            _make_exchange_entry(
                "off_misclick",
                account="Oops#1111",
                online=True,
                have_curr="divine",
                have_amt=15.0,
                want_curr="exalted",
                want_amt=1.0,
                stock=20,
            ),
            _make_exchange_entry(
                "off_shark_bid",
                account="Hunterer#2308",
                online=True,
                have_curr="divine",
                have_amt=1.0,
                want_curr="exalted",
                want_amt=100.0,
                stock=4000,
            ),
        ],
        ("exalted", "divine"): [
            _make_exchange_entry(
                "off_ex_to_div",
                account="Hunterer#2308",
                online=True,
                have_curr="exalted",
                have_amt=455.0,
                want_curr="divine",
                want_amt=1.0,
                stock=50,
            ),
        ],
        ("divine", "chaos"): [
            _make_exchange_entry(
                "off_div_to_c",
                account="MakerA#1001",
                online=True,
                have_curr="divine",
                have_amt=1.0,
                want_curr="chaos",
                want_amt=13.5,
                stock=120,
            ),
        ],
        ("chaos", "divine"): [
            _make_exchange_entry(
                "off_c_to_div",
                account="MakerB#1002",
                online=True,
                have_curr="chaos",
                have_amt=14.0,
                want_curr="divine",
                want_amt=1.0,
                stock=40,
            ),
        ],
        ("chaos", "exalted"): [
            _make_exchange_entry(
                "off_c_to_ex",
                account="MakerC#1003",
                online=True,
                have_curr="chaos",
                have_amt=1.0,
                want_curr="exalted",
                want_amt=36.0,
                stock=2000,
            ),
        ],
        ("exalted", "chaos"): [
            _make_exchange_entry(
                "off_ex_to_c",
                account="MakerD#1004",
                online=True,
                have_curr="exalted",
                have_amt=37.0,
                want_curr="chaos",
                want_amt=1.0,
                stock=300,
            ),
        ],
    }

    econ, t2 = _build_mock_economy_and_trade2(
        tmp_path,
        scout_refs=scout_refs,
        scout_items=scout_items,
        ninja_by_type=ninja_by_type,
        exchange_books=books,
        seen_exchange_statuses=seen_statuses,
    )
    try:
        with ArbitrageScanner(economy=econ, trade2=t2) as scanner:
            quotes, opps, traps = scanner.scan_live_order_books_and_triangles("Forbidden Rites")

        # Must query online offers, never Trade2Client.exchange's default "any"
        assert seen_statuses == ["online"] * 6

        div_ex_q = next(q for q in quotes if q.pair == "divine/exalted")
        assert div_ex_q.best_bid_quote_per_base == 100.0
        assert div_ex_q.best_ask_quote_per_base == 455.0
        assert "misclick_outlier_filtered" in div_ex_q.flags
        assert "same_account_two_sided_maker" in div_ex_q.flags
        assert "shark_bid_trap" in div_ex_q.flags
        assert any(
            t.kind == "shark_or_misclick_book" and t.item_or_pair == "divine/exalted" for t in traps
        )

        # Triangular cycle: 1 Div -> 13.5 Chaos -> 486 Ex -> 1.0681 Div (+6.81%)
        tri = [o for o in opps if o.mechanism == ArbitrageMechanism.TRIANGULAR_CYCLE]
        assert len(tri) == 1
        assert tri[0].roi_pct == pytest.approx(6.81, abs=0.05)
        assert tri[0].gross_profit_exalted == pytest.approx(30.66, abs=0.5)
        assert tri[0].gold_per_cycle_est > 45_000  # 486 Exalted leg Gold cost captured!
    finally:
        t2.close()
        econ.close()


def test_conversions_and_set_assembly_against_bundled_sqlite(tmp_path: Path) -> None:
    scout_refs = [
        {"ApiId": "exalted", "RelativePrice": 1.0},
        {"ApiId": "chaos", "RelativePrice": 36.0},
        {"ApiId": "divine", "RelativePrice": 450.0},
    ]
    scout_items = [
        {
            "ItemId": 1,
            "ApiId": "exalted",
            "Text": "Exalted Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 1.0,
        },
        {
            "ItemId": 2,
            "ApiId": "chaos",
            "Text": "Chaos Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 36.0,
        },
        {
            "ItemId": 3,
            "ApiId": "divine",
            "Text": "Divine Orb",
            "CategoryApiId": "currency",
            "CurrentPrice": 450.0,
        },
        # Illiquid zero-volume poe2scout traps:
        {
            "ItemId": 20,
            "ApiId": "lesser-glacial-rune",
            "Text": "Lesser Glacial Rune",
            "CategoryApiId": "runes",
            "CurrentPrice": 503.0,
        },
        {
            "ItemId": 21,
            "ApiId": "glacial-rune",
            "Text": "Glacial Rune",
            "CategoryApiId": "runes",
            "CurrentPrice": 224.0,
        },
        {
            "ItemId": 22,
            "ApiId": "transmutation-shard",
            "Text": "Transmutation Shard",
            "CategoryApiId": "currency",
            "CurrentPrice": 414.0,
        },
    ]

    catalyst_lines = []
    catalyst_items = []
    cat_div_prices = {
        "refined-flesh-catalyst": (0.31, 6.0),
        "refined-uul-netols-catalyst": (0.38, 30.0),
        "refined-carapace-catalyst": (0.43, 25.0),
        "refined-chayulas-catalyst": (0.43, 8.0),
        "refined-adaptive-catalyst": (0.44, 14.0),
        "refined-neural-catalyst": (0.48, 56.0),
        "refined-tuls-catalyst": (0.48, 10.0),
        "refined-skittering-catalyst": (0.50, 2.0),
        "refined-eshs-catalyst": (0.63, 10.0),
        "refined-xophs-catalyst": (1.23, 25.0),
        "refined-necrotic-catalyst": (1.35, 58.0),
        "refined-reaver-catalyst": (9.68, 545.0),
        "refined-sibilant-catalyst": (13.91, 760.0),
    }
    for slug, (pval, vol) in cat_div_prices.items():
        catalyst_lines.append(
            {
                "id": slug,
                "primaryValue": pval,
                "volumePrimaryValue": vol,
                "maxVolumeCurrency": "divine",
                "maxVolumeRate": round(1.0 / pval, 4),
            }
        )
        catalyst_items.append({"id": slug, "name": slug.replace("-", " ").title()})

    ninja_by_type = {
        "Currency": _ninja_payload(
            lines=[
                {
                    "id": "divine",
                    "primaryValue": 1.0,
                    "volumePrimaryValue": 8700.0,
                    "maxVolumeCurrency": "chaos",
                    "maxVolumeRate": 0.08,
                },
                {
                    "id": "chaos",
                    "primaryValue": 0.08,
                    "volumePrimaryValue": 8700.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 12.5,
                },
                {
                    "id": "exalted",
                    "primaryValue": 1.0 / 450.0,
                    "volumePrimaryValue": 200.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 450.0,
                },
                {
                    "id": "transmute",
                    "primaryValue": 0.005,
                    "volumePrimaryValue": 3.0,
                    "maxVolumeCurrency": "exalted",
                    "maxVolumeRate": 0.44,
                },
                {
                    "id": "artificers",
                    "primaryValue": 0.024,
                    "volumePrimaryValue": 10.0,
                    "maxVolumeCurrency": "exalted",
                    "maxVolumeRate": 0.09,
                },
                {
                    "id": "orb-of-extraction",
                    "primaryValue": 1.59,
                    "volumePrimaryValue": 96.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.629,
                },
                {
                    "id": "perfect-chaos-orb",
                    "primaryValue": 11.88,
                    "volumePrimaryValue": 1980.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.084,
                },
            ],
            items=[
                {"id": "transmute", "name": "Orb of Transmutation"},
                {"id": "artificers", "name": "Artificer's Orb"},
                {"id": "orb-of-extraction", "name": "Orb of Extraction"},
                {"id": "perfect-chaos-orb", "name": "Perfect Chaos Orb"},
            ],
        ),
        "Runes": _ninja_payload(
            lines=[
                {
                    "id": "masterwork-rune",
                    "primaryValue": 2.36,
                    "volumePrimaryValue": 109.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.423,
                },
                {
                    "id": "greater-iron-rune",
                    "primaryValue": 0.0336,
                    "volumePrimaryValue": 2.0,
                    "maxVolumeCurrency": "exalted",
                    "maxVolumeRate": 0.066,
                },
                {
                    "id": "perfect-iron-rune",
                    "primaryValue": 5.30,
                    "volumePrimaryValue": 81.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.188,
                },
            ],
            items=[
                {"id": "masterwork-rune", "name": "Masterwork Rune"},
                {"id": "greater-iron-rune", "name": "Greater Iron Rune"},
                {"id": "perfect-iron-rune", "name": "Perfect Iron Rune"},
            ],
        ),
        "Delirium": _ninja_payload(
            lines=[
                {
                    "id": "simulacrum-splinter",
                    "primaryValue": 0.0103,
                    "volumePrimaryValue": 4.5,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 97.0,
                },
                {
                    "id": "simulacrum",
                    "primaryValue": 3.70,
                    "volumePrimaryValue": 910.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.27,
                },
                {
                    "id": "concentrated-liquid-fear",
                    "primaryValue": 0.025,
                    "volumePrimaryValue": 1.2,
                    "maxVolumeCurrency": "exalted",
                    "maxVolumeRate": 0.088,
                },
                {
                    "id": "concentrated-liquid-suffering",
                    "primaryValue": 0.17,
                    "volumePrimaryValue": 4.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 5.88,
                },
                {
                    "id": "concentrated-liquid-isolation",
                    "primaryValue": 1.06,
                    "volumePrimaryValue": 25.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.943,
                },
                {
                    "id": "potent-liquid-melancholy",
                    "primaryValue": 0.18,
                    "volumePrimaryValue": 2.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 5.55,
                },
                {
                    "id": "potent-liquid-ferocity",
                    "primaryValue": 15.5,
                    "volumePrimaryValue": 890.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.0645,
                },
            ],
            items=[
                {"id": "simulacrum-splinter", "name": "Simulacrum Splinter"},
                {"id": "simulacrum", "name": "Simulacrum"},
                {"id": "concentrated-liquid-fear", "name": "Concentrated Liquid Fear"},
                {"id": "concentrated-liquid-suffering", "name": "Concentrated Liquid Suffering"},
                {"id": "concentrated-liquid-isolation", "name": "Concentrated Liquid Isolation"},
                {"id": "potent-liquid-melancholy", "name": "Potent Liquid Melancholy"},
                {"id": "potent-liquid-ferocity", "name": "Potent Liquid Ferocity"},
            ],
        ),
        "Breach": _ninja_payload(
            lines=[
                {
                    "id": "breach-splinter",
                    "primaryValue": 0.011,
                    "volumePrimaryValue": 23.5,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 90.9,
                },
                {
                    "id": "breachstone",
                    "primaryValue": 3.43,
                    "volumePrimaryValue": 110.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.291,
                },
                *catalyst_lines,
            ],
            items=[
                {"id": "breach-splinter", "name": "Breach Splinter"},
                {"id": "breachstone", "name": "Breachstone"},
                *catalyst_items,
            ],
        ),
        "Fragments": _ninja_payload(
            lines=[
                {
                    "id": "origin-spark",
                    "primaryValue": 4.21,
                    "volumePrimaryValue": 108.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.237,
                },
                {
                    "id": "origin-cradle",
                    "primaryValue": 4.68,
                    "volumePrimaryValue": 131.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.213,
                },
                {
                    "id": "origin-core",
                    "primaryValue": 10.45,
                    "volumePrimaryValue": 190.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.095,
                },
            ],
            items=[
                {"id": "origin-spark", "name": "Origin Spark"},
                {"id": "origin-cradle", "name": "Origin Cradle"},
                {"id": "origin-core", "name": "Origin Core"},
            ],
        ),
        "Ritual": _ninja_payload(
            lines=[
                {
                    "id": "omen-of-whittling",
                    "primaryValue": 14.23,
                    "volumePrimaryValue": 4900.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 0.0702,
                },
            ],
            items=[{"id": "omen-of-whittling", "name": "Omen of Whittling"}],
        ),
        "Verisium": _ninja_payload(
            lines=[
                {
                    "id": "verisium",
                    "primaryValue": 0.0001,
                    "volumePrimaryValue": 12.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 10000.0,
                },
                {
                    "id": "liquid-verisium",
                    "primaryValue": 0.082,
                    "volumePrimaryValue": 153.0,
                    "maxVolumeCurrency": "divine",
                    "maxVolumeRate": 12.2,
                },
            ],
            items=[
                {"id": "verisium", "name": "Verisium"},
                {"id": "liquid-verisium", "name": "Liquid Verisium"},
            ],
        ),
    }

    econ, t2 = _build_mock_economy_and_trade2(
        tmp_path,
        scout_refs=scout_refs,
        scout_items=scout_items,
        ninja_by_type=ninja_by_type,
    )
    try:
        with ArbitrageScanner(economy=econ, trade2=t2) as scanner:
            report_all = scanner.scan_all(
                "Forbidden Rites",
                include_live_exchange=False,
                include_encounter_gated=True,
                limit=30,
            )
            report_hideout = scanner.scan_all(
                "Forbidden Rites",
                include_live_exchange=False,
                include_encounter_gated=False,
                limit=30,
            )

        ids_all = {o.id for o in report_all.opportunities}
        ids_hideout = {o.id for o in report_hideout.opportunities}

        # 1. 2-Socket Perfect Iron Rune extraction present & deterministic
        assert "perfect_rune_extract:iron:2sock" in ids_all
        iron_opp = next(
            o for o in report_all.opportunities if o.id == "perfect_rune_extract:iron:2sock"
        )
        assert iron_opp.is_deterministic is True
        assert iron_opp.executable_in_hideout is True
        assert iron_opp.gross_profit_exalted > 1500.0

        # 2. DistilledEmotion8->9->10 conversions present; Potent Melancholy->Ferocity blocked!
        assert (
            "reforge_emotion:concentrated-liquid-suffering->concentrated-liquid-isolation"
            in ids_all
        )
        assert (
            "reforge_emotion_chain:concentrated-liquid-fear->concentrated-liquid-isolation"
            in ids_all
        )
        assert not any("potent-liquid-melancholy" in oid for oid in ids_all)

        # 3. Empirical drop-weighted Refined Catalyst EV present (`is_deterministic=False`)
        cat_opps = [o for o in report_all.opportunities if o.id.startswith("weighted_catalyst_ev:")]
        assert cat_opps
        assert all(o.is_deterministic is False for o in cat_opps)

        # 4. Origin Core & 300x Simulacrum Splinter present; Breach Splinter blocked!
        assert "set_assembly:origin_core" in ids_all
        assert "stack_combine:simulacrum-splinter" in ids_all
        assert "stack_combine:breach-splinter" not in ids_all
        sim_opp = next(
            o for o in report_all.opportunities if o.id == "stack_combine:simulacrum-splinter"
        )
        assert sim_opp.units_traded_per_cycle == 301
        assert sim_opp.gold_per_cycle_est >= 9000

        # 5. Kalguuran Remnant recipes included when encounter-gated enabled, omitted when False
        assert any(oid.startswith("kalguuran_remnant:") for oid in ids_all)
        assert not any(oid.startswith("kalguuran_remnant:") for oid in ids_hideout)

        # 6. Trap warnings catch Rune/Emotion bench myths, Breach Hiveblood gate, & illiquid quotes
        trap_pairs = {(t.kind, t.item_or_pair) for t in report_all.trap_warnings}
        assert ("impossible_or_gated_recipe", "3x Greater Rune -> Perfect Rune") in trap_pairs
        assert (
            "impossible_or_gated_recipe",
            "Potent Liquid Melancholy -> Potent Liquid Ferocity",
        ) in trap_pairs
        assert ("impossible_or_gated_recipe", "300x Breach Splinter -> Breachstone") in trap_pairs
        assert ("gold_burn_trap", "divine/exalted") in trap_pairs
        assert any(
            t.kind == "illiquid_quote_trap" and "Lesser Glacial Rune" in t.item_or_pair
            for t in report_all.trap_warnings
        )
        assert any(
            t.kind == "illiquid_quote_trap" and "Transmutation Shard" in t.item_or_pair
            for t in report_all.trap_warnings
        )
    finally:
        t2.close()
        econ.close()
