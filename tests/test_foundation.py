# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from poe2brain.cli import app
from poe2brain.config import (
    BUNDLED_DATA_DIR,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    REPO_ROOT,
    USER_AGENT,
    get_cache_dir,
    get_pob2_paths,
)
from poe2brain.http import (
    GGGRateLimiter,
    Poe2HttpClient,
    aget_cached_json,
    compute_rate_limit_delay,
    create_async_client,
    create_sync_client,
    get_cached_json,
    parse_rate_limit_headers,
)
from poe2brain.mcp_server import mcp, poe2_status
from poe2brain.models import (
    AffixSide,
    BaseItemSummary,
    BuildStats,
    CraftActionEval,
    CraftPlanResult,
    CraftPolicyStep,
    CurrencyQuote,
    DewateredAudit,
    GemSummary,
    ItemRarity,
    ItemUpgradeDiff,
    KnowledgeRecord,
    MechanicFact,
    ModDomain,
    ModTierSpec,
    ParsedItem,
    ParsedMod,
    PriceCheckResult,
    PriceDistribution,
    SaleType,
    SlotStatWeights,
    StatWeightEntry,
    StrippedPobFlag,
    TradeListing,
    TradeVsCraftComparison,
    UniqueSummary,
)


def test_config_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert REPO_ROOT.is_dir()
    assert BUNDLED_DATA_DIR == REPO_ROOT / "data"
    assert DEFAULT_LEAGUE == "Forbidden Rites"
    assert POE2_PATCH == "0.5.5"
    assert USER_AGENT.startswith("poe2brain/0.1.0")

    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(tmp_path / "custom-cache"))
    assert get_cache_dir() == (tmp_path / "custom-cache").resolve()
    assert get_pob2_paths().root_dir == (tmp_path / "custom-cache" / "pob2").resolve()


def test_http_clients_enforce_user_agent() -> None:
    with create_sync_client(headers={"User-Agent": ""}) as sync_client:
        assert sync_client.headers["User-Agent"] == USER_AGENT

    async_client = create_async_client()
    assert async_client.headers["User-Agent"] == USER_AGENT


def test_parse_ggg_rate_limit_headers() -> None:
    headers = {
        "X-Rate-Limit-Policy": "trade-search-request-limit",
        "X-Rate-Limit-Rules": "Ip",
        "X-Rate-Limit-Ip": "5:10:60,15:60:300,30:300:1800,600:21600:3600",
        "X-Rate-Limit-Ip-State": "2:10:0,7:60:0,12:300:0,44:21600:0",
    }
    windows = parse_rate_limit_headers(headers)
    assert len(windows) == 4
    assert windows[0].max_hits == 5
    assert windows[0].window_seconds == 10.0
    assert windows[0].ban_penalty_seconds == 60.0
    assert windows[0].current_hits == 2
    assert windows[0].remaining_hits == 3
    assert not windows[0].is_exhausted
    assert compute_rate_limit_delay(headers) == 0.0


def test_rate_limiter_safety_buffer_and_sliding_window() -> None:
    now = 100.0
    slept: list[float] = []

    def fake_clock() -> float:
        return now

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        slept.append(seconds)
        now += seconds

    limiter = GGGRateLimiter(safety_buffer=1.0, clock=fake_clock, sleep_sync=fake_sleep)
    bucket = "trade-search-request-limit"

    # Without prior local timestamps, hitting 5/5 waits full 10s window + 1.0s buffer = 11.0s
    delay = limiter.update_from_headers(
        {
            "X-Rate-Limit-Policy": bucket,
            "X-Rate-Limit-Ip": "5:10:60,15:60:300",
            "X-Rate-Limit-Ip-State": "5:10:0,5:60:0",
        }
    )
    assert delay == pytest.approx(11.0)
    waited = limiter.acquire_sync(bucket)
    assert waited == pytest.approx(11.0)
    assert slept == [pytest.approx(11.0)]

    # Active server ban adds +1.0s safety buffer
    ban_delay = limiter.update_from_headers(
        {
            "X-Rate-Limit-Policy": bucket,
            "X-Rate-Limit-Ip": "5:10:60",
            "X-Rate-Limit-Ip-State": "6:10:58",
        }
    )
    assert ban_delay == pytest.approx(59.0)

    # Sliding-window timestamp awareness: 5 local hits spaced over 3s -> 7s remaining + 1s buffer
    now2 = 500.0
    limiter2 = GGGRateLimiter(safety_buffer=1.0, clock=lambda: now2)
    for offset in (0.0, 0.5, 1.0, 2.0, 3.0):
        limiter2.record_request("trade-fetch-request-limit", now=500.0 + offset)
    now2 = 503.0
    sliding_delay = limiter2.update_from_headers(
        {
            "X-Rate-Limit-Policy": "trade-fetch-request-limit",
            "X-Rate-Limit-Ip": "5:10:10",
            "X-Rate-Limit-Ip-State": "5:10:0",
        },
        now=503.0,
    )
    assert sliding_delay == pytest.approx(8.0)


async def test_http_client_429_retry_and_disk_ttl_cache(tmp_path: Path) -> None:
    calls = 0
    slept_async: list[float] = []

    async def fake_sleep_async(seconds: float) -> None:
        slept_async.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["User-Agent"] == USER_AGENT
        if calls == 1:
            return httpx.Response(
                429,
                headers={
                    "X-Rate-Limit-Policy": "trade-search-request-limit",
                    "X-Rate-Limit-Ip": "5:10:60",
                    "X-Rate-Limit-Ip-State": "6:10:4",
                },
                json={"error": {"message": "Rate limit exceeded"}},
            )
        return httpx.Response(
            200,
            headers={
                "X-Rate-Limit-Policy": "trade-search-request-limit",
                "X-Rate-Limit-Ip": "5:10:60",
                "X-Rate-Limit-Ip-State": "1:10:0",
            },
            json={"id": "q123", "total": 42},
        )

    clock_val = 10.0

    def fake_clock() -> float:
        return clock_val

    async def advancing_sleep_async(seconds: float) -> None:
        nonlocal clock_val
        slept_async.append(seconds)
        clock_val += seconds

    limiter = GGGRateLimiter(safety_buffer=1.0, clock=fake_clock, sleep_async=advancing_sleep_async)
    client = Poe2HttpClient(
        rate_limiter=limiter,
        async_transport=httpx.MockTransport(handler),
    )
    data = await client.apost_json(
        "https://www.pathofexile.com/api/trade2/search/poe2/Forbidden%20Rites",
        json_body={"query": {"status": {"option": "available"}}},
    )
    await client.aclose()
    assert data == {"id": "q123", "total": 42}
    assert calls == 2
    assert slept_async == [pytest.approx(5.0)]

    # Disk TTL cache sync + stale fallback
    fetch_count = 0
    fake_epoch = 1000.0

    def fetch_payload() -> dict[str, int]:
        nonlocal fetch_count
        fetch_count += 1
        return {"run": fetch_count}

    res1 = get_cached_json(
        "poe2scout:items",
        ttl_seconds=60.0,
        fetcher=fetch_payload,
        cache_dir=tmp_path,
        now_fn=lambda: fake_epoch,
    )
    res2 = get_cached_json(
        "poe2scout:items",
        ttl_seconds=60.0,
        fetcher=fetch_payload,
        cache_dir=tmp_path,
        now_fn=lambda: fake_epoch + 30.0,
    )
    assert res1 == {"run": 1}
    assert res2 == {"run": 1}
    assert fetch_count == 1

    # Expired TTL triggers refresh
    res3 = get_cached_json(
        "poe2scout:items",
        ttl_seconds=60.0,
        fetcher=fetch_payload,
        cache_dir=tmp_path,
        now_fn=lambda: fake_epoch + 120.0,
    )
    assert res3 == {"run": 2}

    # Stale fallback on upstream error
    def broken_fetcher() -> dict[str, int]:
        raise httpx.ConnectError("network down")

    stale = get_cached_json(
        "poe2scout:items",
        ttl_seconds=60.0,
        fetcher=broken_fetcher,
        cache_dir=tmp_path,
        now_fn=lambda: fake_epoch + 500.0,
    )
    assert stale == {"run": 2}

    async_res = await aget_cached_json(
        "poe2ninja:currency",
        ttl_seconds=60.0,
        fetcher=lambda: _async_val({"divine": 453}),
        cache_dir=tmp_path,
    )
    assert async_res == {"divine": 453}


async def _async_val(val: dict[str, int]) -> dict[str, int]:
    return val


def test_domain_models_contracts() -> None:
    expected_domains = {
        "explicit",
        "implicit",
        "fractured",
        "desecrated",
        "rune",
        "enchant",
        "crafted",
        "corrupted",
        "skill",
    }
    assert {d.value for d in ModDomain} == expected_domains

    item = ParsedItem(
        item_class="Sceptres",
        rarity="Rare",
        name="Sol Chant",
        base_type="Omen Sceptre",
        item_level=82,
        quality=20,
        catalyst_type="Caster Modifiers",
        spirit=154,
        rune_sockets=2,
        gem_sockets=4,
        unidentified_tier=3,
        twice_corrupted=True,
        sanctified=True,
        waystone_revives=2,
        mods=[
            ParsedMod(raw_text="+35% to Spirit", domain="fractured", values=[35.0]),
            ParsedMod(raw_text="Bonded: +15% to Chaos Resistance", domain="augment", bonded=True),
            ParsedMod(raw_text="+2 to Level of all Minion Skills", domain="desecrated"),
        ],
    )
    assert item.rarity == ItemRarity.RARE
    assert item.corrupted is True
    assert item.fractured is True
    assert item.identified is False
    assert item.mods[1].domain == ModDomain.RUNE

    # Knowledge schemas
    fact = MechanicFact(
        slug="es_chaos_bleed",
        title="Energy Shield vs Chaos, Bleeding, and Poison",
        poe1_trap="Chaos bypasses ES; Poison hits ES.",
        poe2_live_rule="Chaos removes 2x ES; Bleeding and Poison both bypass ES directly to Life.",
        source_ref="PoB2 CalcDefence.lua",
    )
    rec = KnowledgeRecord(
        id=fact.slug, kind="mechanic", title=fact.title, summary=fact.poe2_live_rule
    )
    gem = GemSummary(id="archmage", name="Archmage", gem_kind="spirit", spirit_cost=100)
    base = BaseItemSummary(
        id="Metadata/Items/Weapons/Wand1", name="Attuned Wand", item_class="Wands"
    )
    uniq = UniqueSummary(
        id="kaoms", name="Kaom's Heart", base_type="Conqueror Plate", price_exalted=900.0
    )
    assert (
        rec.patch == POE2_PATCH
        and gem.spirit_cost == 100
        and base.is_live_poe2
        and uniq.price_exalted == 900.0
    )

    # Build schemas (with PoB2 PascalCase alias support)
    raw_stats = BuildStats.model_validate(
        {
            "FullDPS": 125_000.0,
            "TotalDPS": 125_000.0,
            "TotalEHP": 42_000.0,
            "Life": 2100,
            "ES": 4800,
            "Mana": 1600,
            "Spirit": 240,
            "SpiritReserved": 190,
            "SpiritUnreserved": 50,
            "FireResist": 75,
            "ColdResist": 75,
            "LightningResist": 75,
            "ChaosResist": 45,
            "BlockChance": 50,
            "DeflectionChance": 38,
            "Evasion": 8400,
            "Armour": 1200,
        }
    )
    sustainable = raw_stats.model_copy(update={"full_dps": 82_000.0, "total_dps": 82_000.0})
    audit = DewateredAudit(
        raw_stats=raw_stats,
        sustainable_pinnacle_stats=sustainable,
        dps_inflation_pct=52.4,
        stripped_flags=[StrippedPobFlag(flag="overrideShock=50", dps_inflation_pct=35.0)],
    )
    diff = ItemUpgradeDiff(
        equipment_slot="Weapon 1",
        full_dps_before=82_000.0,
        full_dps_after=98_400.0,
        full_dps_pct=20.0,
    )
    weights = SlotStatWeights(
        equipment_slot="Weapon 1",
        weights=[StatWeightEntry(stat_id="explicit.stat_1", stat_label="Spell Damage", weight=1.0)],
    )
    assert (
        audit.raw_stats.energy_shield == 4800.0
        and diff.full_dps_pct == 20.0
        and len(weights.weights) == 1
    )

    # Trade schemas (Instant Buyout Gold fee vs In-Person whisper)
    quote = CurrencyQuote(
        id="divine",
        name="Divine Orb",
        exalted=461.9,
        chaos=12.67,
        divine=1.0,
        volume_primary=4200.0,
        change_7d_pct=4.5,
        source="poe2scout+poe.ninja",
    )
    buyout_listing = TradeListing(
        listing_id="l1",
        base_type="Dualstring Bow",
        price_amount=4.0,
        price_currency="exalted",
        price_exalted=4.0,
        fee=12028,
        account_name="Merchant#0001",
    )
    whisper_listing = TradeListing(
        listing_id="l2",
        base_type="Dualstring Bow",
        price_amount=3.0,
        price_currency="exalted",
        price_exalted=3.0,
        whisper="@Trader Hi, I would like to buy...",
        account_name="Trader#0002",
        online=True,
    )
    assert buyout_listing.sale_type == SaleType.SECURABLE
    assert buyout_listing.is_instant_buyout is True
    assert whisper_listing.sale_type == SaleType.WHISPER
    assert whisper_listing.is_instant_buyout is False

    pc = PriceCheckResult(
        trade_url="https://www.pathofexile.com/trade2/search/poe2/Forbidden%20Rites/abc",
        total_matches=2,
        estimated_exalted=4.0,
        distribution=PriceDistribution(
            count_total=2, count_deduplicated=2, count_securable=1, count_whisper=1
        ),
        listings=[buyout_listing, whisper_listing],
        fixer_warnings=["1 lowball offline whisper outlier below Instant Buyout floor"],
    )
    assert quote.exalted > 400 and pc.listings[0].fee == 12028

    # Crafting schemas (internal_rank_asc vs display_tier=1 best)
    t1_mod = ModTierSpec(
        mod_id="SpellDamageOnWeapon8_",
        family_id="WeaponSpellDamage",
        affix_side=AffixSide.PREFIX,
        internal_rank_asc=8,
        display_tier=1,
        required_level=80,
        weight=250,
        tags=["caster", "damage"],
        stat_text="(100-109)% increased Spell Damage",
    )
    assert t1_mod.display_tier == 1 and t1_mod.internal_rank_asc == 8
    with pytest.raises(ValueError):
        ModTierSpec(
            mod_id="Bad",
            family_id="Bad",
            internal_rank_asc=0,
            display_tier=0,
            required_level=1,
            weight=100,
        )

    plan = CraftPlanResult(
        base_type="Siphoning Wand",
        item_level=82,
        expected_cost_exalted=620.0,
        expected_cost_divine=1.34,
        p50_cost_exalted=410.0,
        p80_cost_exalted=940.0,
        p95_cost_exalted=1850.0,
        policy_steps=[
            CraftPolicyStep(
                step_index=1,
                when_state="Clean Magic base",
                recommended_action="Apply Greater Essence of Sorcery",
                lever_ids=["essence_greater_sorcery"],
                step_cost_exalted=18.0,
            )
        ],
        candidate_actions=[
            CraftActionEval(
                lever_id="exalted_perfect",
                action_label="Perfect Exalted Orb",
                step_cost_exalted=95.0,
                hit_probability=0.085,
            )
        ],
        trade_vs_craft=TradeVsCraftComparison(
            recommendation="craft",
            market_price_exalted=920.0,
            expected_craft_exalted=620.0,
            savings_exalted=300.0,
        ),
    )
    assert plan.trade_vs_craft is not None
    assert plan.trade_vs_craft.recommendation == "craft"


async def test_cli_and_mcp_status() -> None:
    runner = CliRunner()
    help_res = runner.invoke(app, ["--help"])
    assert help_res.exit_code == 0
    assert "poe2brain" in help_res.stdout

    ver_res = runner.invoke(app, ["version", "--json"])
    assert ver_res.exit_code == 0
    parsed = json.loads(ver_res.stdout)
    assert parsed["version"] == "0.1.0"
    assert parsed["patch"] == POE2_PATCH
    assert parsed["default_league"] == DEFAULT_LEAGUE

    assert mcp.name == "poe2brain"
    tools = await mcp.list_tools()
    assert any(t.name == "poe2_status" for t in tools)
    status_dict = poe2_status()
    assert status_dict["patch"] == POE2_PATCH


def test_pob2_runtime_ready() -> None:
    paths = get_pob2_paths()
    assert paths.is_ready, f"Expected bootstrapped PoB2 runtime at {paths.root_dir}"
    env = os.environ.copy()
    if paths.uses_windows_dll_host:
        env["LUA_CPATH"] = f"{paths.runtime_dir}\\?.dll;{paths.bin_dir}\\?.dll;;"
        env["POB2_RUNTIME_DIR"] = str(paths.runtime_dir)
        cmd = [
            sys.executable,
            "-m",
            "poe2brain.pob.win_luajit",
            "-e",
            'local u = require("lua-utf8"); local z = require("ffi").load("zlib1"); '
            'local x = 1; x += 2; assert(x == 3 and u.len("PoE2") == 4); print("POB2_RT_OK")',
        ]
    else:
        env["LUA_CPATH"] = f"{paths.bin_dir}/?.so;;"
        cmd = [
            str(paths.luajit_bin),
            "-e",
            'local u = require("lua-utf8"); local z = require("ffi").load("z"); '
            'local x = 1; x += 2; assert(x == 3 and u.len("PoE2") == 4); print("POB2_RT_OK")',
        ]
    proc = subprocess.run(
        cmd,
        cwd=paths.src_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "POB2_RT_OK" in proc.stdout
    assert (paths.src_dir / "HeadlessWrapper.lua").is_file()
