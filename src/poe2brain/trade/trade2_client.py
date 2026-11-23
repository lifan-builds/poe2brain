from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
)
from poe2brain.http import (
    GGGRateLimiter,
    Poe2HttpClient,
    aget_cached_json,
    get_cached_json,
)
from poe2brain.models.common import AffixSide, Poe2Model, SaleType, StatRollRange
from poe2brain.models.items import ItemRarity, ItemRequirements, ModDomain, ParsedItem, ParsedMod
from poe2brain.models.trade import PriceCheckResult, PriceDistribution, TradeListing
from poe2brain.trade.economy import EconomyService

DEFAULT_TRADE2_STATIC_TTL_SECONDS: float = 14400.0  # 4 hours
TRADE2_MAX_FETCH_BATCH: int = 10

STATIC_DATA_ENDPOINTS: frozenset[str] = frozenset(
    {"leagues", "stats", "items", "static", "filters"}
)

VALID_STATUS_OPTIONS: frozenset[str] = frozenset(
    {"available", "securable", "onlineleague", "online", "any"}
)

_WIKI_MARKUP_RE = re.compile(r"\[([^\]|]+)\|?([^\]]*)\]")
_NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

_FALLBACK_CURRENCY_RATES_EXALTED: dict[str, float] = {
    "exalted": 1.0,
    "ex": 1.0,
    "divine": 453.0,
    "chaos": 36.5,
    "regal": 0.35,
    "alch": 4.8,
    "vaal": 2.5,
    "annul": 35.0,
    "chance": 4.0,
    "aug": 0.05,
    "transmute": 0.03,
    "wisdom": 0.02,
    "artificers": 2.0,
    "gcp": 6.0,
    "bauble": 3.0,
    "etcher": 1.5,
    "whetstone": 0.4,
    "scrap": 0.4,
    "greater-exalted-orb": 12.0,
    "perfect-exalted-orb": 95.0,
    "greater-chaos-orb": 48.0,
    "perfect-chaos-orb": 220.0,
    "greater-regal-orb": 4.0,
    "perfect-regal-orb": 28.0,
    "fracturing-orb": 650.0,
    "lesser-jewellers-orb": 0.5,
    "greater-jewellers-orb": 22.0,
    "perfect-jewellers-orb": 620.0,
    "mirror": 350000.0,
    "hinekoras-lock": 95000.0,
}

_JEWELRY_KEYWORDS = ("amulet", "ring", "belt")


def strip_wiki_markup(text: str) -> str:
    """Strip PoE2 `[Target|Label]` and `[Label]` wiki markup from item/mod text."""
    return _WIKI_MARKUP_RE.sub(lambda m: m.group(2) if m.group(2) else m.group(1), text)


strip_poe2_wiki_markup = strip_wiki_markup


def build_trade_url(
    league: str,
    query_id: str,
    *,
    web_base_url: str = GGG_TRADE2_WEB_BASE_URL,
) -> str:
    enc_league = quote(league, safe="")
    return f"{web_base_url.rstrip('/')}/{enc_league}/{query_id}"


def extract_rune_sockets(item_obj: Mapping[str, Any]) -> int:
    sockets = item_obj.get("sockets")
    if not isinstance(sockets, Sequence):
        return 0
    count = 0
    for sock in sockets:
        if isinstance(sock, Mapping):
            stype = str(sock.get("type") or "rune").strip().lower()
            if stype in ("rune", "augment"):
                count += 1
    return count


class TradeSearchResponse(Poe2Model):
    id: str
    complexity: int | None = None
    result: list[str] = Field(default_factory=list)
    total: int = 0
    inexact: bool = False
    league: str = DEFAULT_LEAGUE
    trade_url: str


class ExchangeOffer(Poe2Model):
    listing_id: str
    account_name: str
    online: bool = False
    have_currency: str
    have_amount: float
    want_currency: str
    want_amount: float
    stock: int = 0
    have_per_want: float


class TradeExchangeResponse(Poe2Model):
    id: str
    league: str = DEFAULT_LEAGUE
    total: int = 0
    offers: list[ExchangeOffer] = Field(default_factory=list)


class DetailedPriceCheckResult(PriceCheckResult):
    """Extended `PriceCheckResult` compatible with `models.trade.PriceCheckResult`
    while surfacing `p10` and `iqr_mean` in Exalted/Divine plus separated listing views.
    """

    min_exalted: float | None = None
    p10_exalted: float | None = None
    median_exalted: float | None = None
    iqr_mean_exalted: float | None = None

    min_divine: float | None = None
    p10_divine: float | None = None
    median_divine: float | None = None
    iqr_mean_divine: float | None = None

    securable_listings: list[TradeListing] = Field(default_factory=list)
    whisper_listings: list[TradeListing] = Field(default_factory=list)


def parse_structured_mods(item_obj: Mapping[str, Any]) -> list[ParsedMod]:
    """Parse PoE2 structured mod objects from `/api/trade2/fetch`, inspecting `mod.domain` & `flags`
    so desecrated/fractured mods nested inside `explicitMods` are classified accurately.
    """
    mod_sections: tuple[tuple[str, ModDomain], ...] = (
        ("enchantMods", ModDomain.ENCHANT),
        ("runeMods", ModDomain.RUNE),
        ("implicitMods", ModDomain.IMPLICIT),
        ("fracturedMods", ModDomain.FRACTURED),
        ("explicitMods", ModDomain.EXPLICIT),
        ("desecratedMods", ModDomain.DESECRATED),
        ("craftedMods", ModDomain.CRAFTED),
    )

    seen_keys: set[tuple[str, str, str]] = set()
    parsed_mods: list[ParsedMod] = []

    for field_name, default_domain in mod_sections:
        raw_list = item_obj.get(field_name)
        if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
            continue

        for entry in raw_list:
            if isinstance(entry, str):
                clean_desc = strip_wiki_markup(entry).strip()
                if not clean_desc:
                    continue
                dedupe_key = (default_domain.value, clean_desc, "")
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                parsed_mods.append(
                    ParsedMod(
                        raw_text=clean_desc,
                        domain=default_domain,
                        values=[float(m) for m in _NUMBER_RE.findall(clean_desc)],
                        bonded=clean_desc.lower().startswith("bonded:"),
                    )
                )
                continue

            if not isinstance(entry, Mapping):
                continue

            raw_desc = str(entry.get("description") or "").strip()
            clean_desc = strip_wiki_markup(raw_desc).strip()
            if not clean_desc:
                continue

            resolved_domain = _resolve_mod_domain(entry, default_domain)
            raw_hash = str(entry.get("hash") or "").strip()
            stat_id = raw_hash[5:] if raw_hash.startswith("stat.") else raw_hash

            dedupe_key = (resolved_domain.value, clean_desc, stat_id)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            affix_name: str | None = None
            tier_code: str | None = None
            affix_side: AffixSide | None = None
            req_level: int | None = None
            roll_ranges: list[StatRollRange] = []

            sub_mods = entry.get("mods")
            if isinstance(sub_mods, Sequence) and sub_mods:
                first_sub = sub_mods[0]
                if isinstance(first_sub, Mapping):
                    if first_sub.get("name"):
                        affix_name = strip_wiki_markup(str(first_sub["name"])).strip() or None
                    if first_sub.get("tier"):
                        tier_code = str(first_sub["tier"]).strip()
                        if tier_code.upper().startswith("P"):
                            affix_side = AffixSide.PREFIX
                        elif tier_code.upper().startswith("S"):
                            affix_side = AffixSide.SUFFIX
                    if isinstance(first_sub.get("level"), (int, float)):
                        req_level = int(first_sub["level"])
                    mags = first_sub.get("magnitudes")
                    if isinstance(mags, Sequence):
                        for mag in mags:
                            if isinstance(mag, Mapping):
                                lo = _safe_float(mag.get("min"))
                                hi = _safe_float(mag.get("max"))
                                if lo is not None and hi is not None:
                                    roll_ranges.append(
                                        StatRollRange(min=min(lo, hi), max=max(lo, hi))
                                    )

            if affix_side is None:
                if resolved_domain == ModDomain.IMPLICIT:
                    affix_side = AffixSide.IMPLICIT
                elif resolved_domain == ModDomain.ENCHANT:
                    affix_side = AffixSide.ENCHANT
                elif resolved_domain == ModDomain.RUNE:
                    affix_side = AffixSide.SOCKETABLE

            parsed_mods.append(
                ParsedMod(
                    raw_text=clean_desc,
                    domain=resolved_domain,
                    affix_side=affix_side,
                    affix_name=affix_name,
                    tier_code=tier_code,
                    required_level=req_level,
                    values=[float(m) for m in _NUMBER_RE.findall(clean_desc)],
                    roll_ranges=roll_ranges,
                    bonded=clean_desc.lower().startswith("bonded:"),
                    trade_stat_ids=[stat_id] if stat_id else [],
                )
            )

    return parsed_mods


def parse_fetch_item(item_obj: Mapping[str, Any]) -> ParsedItem:
    raw_name = strip_wiki_markup(str(item_obj.get("name") or "")).strip() or None
    base_type = (
        strip_wiki_markup(str(item_obj.get("baseType") or item_obj.get("typeLine") or "")).strip()
        or "Unknown Base"
    )
    rarity_raw = str(item_obj.get("rarity") or "Rare").strip()
    try:
        rarity = ItemRarity(rarity_raw.lower())
    except ValueError:
        rarity = ItemRarity.UNIQUE if rarity_raw.lower() == "uniquefoil" else ItemRarity.RARE

    ilvl = int(item_obj["ilvl"]) if isinstance(item_obj.get("ilvl"), (int, float)) else None
    identified = bool(item_obj.get("identified", True))
    corrupted = bool(item_obj.get("corrupted", False))
    sanctified = bool(item_obj.get("sanctified", False))
    mirrored = bool(item_obj.get("duplicated", False) or item_obj.get("mirrored", False))
    fractured_flag = bool(item_obj.get("fractured", False))
    veiled_flag = bool(item_obj.get("veiled", False))

    reqs = _parse_requirements(item_obj.get("requirements"))
    props = _parse_properties(item_obj.get("properties"))
    ext = item_obj.get("extended") if isinstance(item_obj.get("extended"), Mapping) else {}

    mods = parse_structured_mods(item_obj)
    rune_sockets = extract_rune_sockets(item_obj)

    pdps = _safe_float(ext.get("pdps"))
    edps = _safe_float(ext.get("edps"))
    dps = _safe_float(ext.get("dps"))

    return ParsedItem(
        item_class=props.get("item_class"),
        rarity=rarity,
        name=raw_name,
        base_type=base_type,
        item_level=ilvl,
        identified=identified,
        quality=int(props.get("quality", 0)),
        catalyst_type=props.get("catalyst_type"),
        spirit=props.get("spirit"),
        armour=props.get("armour"),
        evasion=props.get("evasion"),
        energy_shield=props.get("energy_shield"),
        ward=props.get("ward"),
        block_chance=props.get("block_chance"),
        physical_dps=pdps,
        elemental_dps=edps,
        total_dps=dps,
        attacks_per_second=props.get("aps"),
        critical_hit_chance=props.get("crit"),
        reload_time=props.get("reload_time"),
        rune_sockets=rune_sockets,
        gem_level=props.get("gem_level"),
        requirements=reqs,
        corrupted=corrupted,
        sanctified=sanctified,
        fractured=fractured_flag,
        mirrored=mirrored,
        veiled_desecrated=veiled_flag,
        mods=mods,
        note=str(item_obj.get("note")) if item_obj.get("note") else None,
    )


def format_parsed_item_for_pob2(parsed: ParsedItem) -> str:
    """Synthesize PoB2 `Item.lua`-compatible text from a parsed `trade2/fetch` item."""
    lines: list[str] = [f"Rarity: {parsed.rarity.value.upper()}"]
    if parsed.name:
        lines.append(parsed.name)
    lines.append(parsed.base_type)

    is_jewelry = any(k in parsed.base_type.lower() for k in _JEWELRY_KEYWORDS)
    if parsed.quality > 0 and parsed.catalyst_type is None and not is_jewelry:
        lines.append(f"Quality: {parsed.quality}")
    if parsed.rune_sockets > 0:
        lines.append(f"Sockets: {' '.join(['S'] * parsed.rune_sockets)}")
    if parsed.requirements.level > 0:
        lines.append(f"LevelReq: {parsed.requirements.level}")
    if parsed.item_level is not None:
        lines.append(f"Item Level: {parsed.item_level}")

    implicits = [
        m
        for m in parsed.mods
        if m.domain in (ModDomain.ENCHANT, ModDomain.RUNE, ModDomain.IMPLICIT)
    ]
    explicits = [
        m
        for m in parsed.mods
        if m.domain not in (ModDomain.ENCHANT, ModDomain.RUNE, ModDomain.IMPLICIT)
    ]

    lines.append(f"Implicits: {len(implicits)}")
    for mod in implicits:
        tag = f"{{{mod.domain.value}}}" if mod.domain != ModDomain.IMPLICIT else ""
        lines.append(f"{tag}{mod.raw_text}")

    for mod in explicits:
        if mod.domain == ModDomain.FRACTURED:
            lines.append(f"{{fractured}}{mod.raw_text}")
        elif mod.domain == ModDomain.DESECRATED:
            lines.append(f"{{desecrated}}{mod.raw_text}")
        elif mod.domain == ModDomain.CRAFTED:
            lines.append(f"{{crafted}}{mod.raw_text}")
        else:
            lines.append(mod.raw_text)

    if parsed.sanctified:
        lines.append("Sanctified")
    if parsed.corrupted:
        lines.append("Corrupted")

    return "\n".join(lines)


def format_mod_line_display(mod: ParsedMod) -> str:
    if mod.domain == ModDomain.EXPLICIT:
        return mod.raw_text
    return f"{mod.raw_text} ({mod.domain.value})"


def deduplicate_listings_by_account(
    listings: Sequence[TradeListing],
) -> tuple[list[TradeListing], dict[str, int]]:
    """Collapse multiple listings by the same `account_name`, keeping each account's lowest
    priced listing (preferring Instant Buyout `securable` on ties).

    Returns `(deduplicated_sorted_listings, duplicate_extra_counts_by_display_account)`.
    """
    ordered = sorted(
        listings,
        key=lambda item: (item.price_exalted, 0 if item.is_instant_buyout else 1),
    )
    kept: list[TradeListing] = []
    seen_accounts: dict[str, str] = {}
    counts: Counter[str] = Counter()

    for listing in ordered:
        raw_acct = listing.account_name.strip() or f"anon:{listing.listing_id}"
        norm_acct = raw_acct.lower()
        counts[norm_acct] += 1
        if norm_acct not in seen_accounts:
            seen_accounts[norm_acct] = raw_acct
            kept.append(listing)

    extra_by_account: dict[str, int] = {
        seen_accounts[norm_acct]: total - 1 for norm_acct, total in counts.items() if total > 1
    }
    return kept, extra_by_account


def summarize_price_check(
    listings: Sequence[TradeListing],
    *,
    league: str = DEFAULT_LEAGUE,
    query_id: str | None = None,
    trade_url: str | None = None,
    total_matches: int | None = None,
    divine_exalted: float | None = None,
) -> DetailedPriceCheckResult:
    """Compute fixer-resistant PoE2 price distribution separating Instant Buyout (`securable`,
    Gold `fee`) from In-Person (`whisper`) listings, deduplicating same-account walls.
    """
    resolved_url = trade_url or (build_trade_url(league, query_id) if query_id else None)
    div_rate = divine_exalted if (divine_exalted is not None and divine_exalted > 0) else 453.0

    valid_listings = [item for item in listings if item.price_exalted > 0]
    count_total = len(valid_listings)
    deduped, extra_by_account = deduplicate_listings_by_account(valid_listings)
    count_deduped = len(deduped)

    securable = [item for item in deduped if item.is_instant_buyout]
    whispers = [item for item in deduped if not item.is_instant_buyout]

    warnings: list[str] = []
    for acct, extra_cnt in sorted(extra_by_account.items(), key=lambda kv: kv[1], reverse=True):
        if extra_cnt >= 2:
            warnings.append(
                f"Collapsed {extra_cnt} duplicate listings from account '{acct}' "
                f"(possible stash-wall price-fixing)."
            )

    deduped_prices = [item.price_exalted for item in deduped]
    securable_prices = [item.price_exalted for item in securable]
    whisper_prices = [item.price_exalted for item in whispers]

    min_ex = _quantile(deduped_prices, 0.0)
    p10_ex = _quantile(deduped_prices, 0.10)
    p25_ex = _quantile(deduped_prices, 0.25)
    med_ex = _quantile(deduped_prices, 0.50)
    p75_ex = _quantile(deduped_prices, 0.75)

    sec_min_ex = _quantile(securable_prices, 0.0)
    sec_med_ex = _quantile(securable_prices, 0.50)

    # Detect fake whisper lowballs undercutting executable Gold-fee Instant Buyout floor
    filtered_for_estimate = list(deduped_prices)
    if sec_min_ex is not None and len(securable_prices) >= 2 and whisper_prices:
        lowballs = [p for p in whisper_prices if p < 0.5 * sec_min_ex]
        if lowballs:
            warnings.append(
                f"{len(lowballs)} in-person whisper listing(s) (min {min(lowballs):.2f} ex) "
                f"sit >50% below Instant Buyout floor ({sec_min_ex:.2f} ex) — "
                f"likely unresponsive price-fixers."
            )
            non_lowball = [
                item.price_exalted
                for item in deduped
                if item.is_instant_buyout or item.price_exalted >= 0.5 * sec_min_ex
            ]
            if non_lowball:
                filtered_for_estimate = non_lowball
    elif med_ex is not None and len(deduped_prices) >= 5:
        extreme_lows = [p for p in deduped_prices if p < 0.25 * med_ex]
        if extreme_lows and len(extreme_lows) <= len(deduped_prices) // 3:
            warnings.append(
                f"{len(extreme_lows)} extreme low outlier listing(s) "
                f"(<25% of median {med_ex:.2f} ex) detected — "
                f"verify Instant Buyout availability before underpricing."
            )
            trimmed = [p for p in deduped_prices if p >= 0.25 * med_ex]
            if trimmed:
                filtered_for_estimate = trimmed

    iqr_ex = _iqr_mean(filtered_for_estimate)

    def _to_div(val: float | None) -> float | None:
        return round(val / div_rate, 4) if val is not None else None

    dist = PriceDistribution(
        count_total=count_total,
        count_deduplicated=count_deduped,
        count_securable=len(securable),
        count_whisper=len(whispers),
        min_exalted=min_ex,
        p25_exalted=p25_ex,
        median_exalted=med_ex,
        p75_exalted=p75_ex,
        securable_min_exalted=sec_min_ex,
        securable_median_exalted=sec_med_ex,
    )

    return DetailedPriceCheckResult(
        league=league,
        query_id=query_id,
        trade_url=resolved_url,
        total_matches=total_matches if total_matches is not None else count_total,
        estimated_exalted=iqr_ex,
        estimated_divine=_to_div(iqr_ex),
        distribution=dist,
        listings=deduped,
        fixer_warnings=warnings,
        min_exalted=min_ex,
        p10_exalted=p10_ex,
        median_exalted=med_ex,
        iqr_mean_exalted=iqr_ex,
        min_divine=_to_div(min_ex),
        p10_divine=_to_div(p10_ex),
        median_divine=_to_div(med_ex),
        iqr_mean_divine=_to_div(iqr_ex),
        securable_listings=securable,
        whisper_listings=whispers,
    )


class Trade2Client:
    """Official Path of Exile 2 Trade API client (`https://www.pathofexile.com/api/trade2`).

    Features:
    - Mandatory `USER_AGENT` + dynamic `GGGRateLimiter` across search/fetch/exchange buckets.
    - 4-hour disk TTL caching for `/api/trade2/data/{leagues,stats,items,static,filters}`.
    - Batched `<=10` ID `fetch` calls with structured PoE2 mod parsing, wiki-markup stripping,
      Instant Buyout (`securable` Gold `fee`) vs `whisper` classification, and fixer deduplication.
    """

    def __init__(
        self,
        *,
        http_client: Poe2HttpClient | None = None,
        client: Poe2HttpClient | None = None,
        rate_limiter: GGGRateLimiter | None = None,
        economy: EconomyService | None = None,
        cache_dir: Path | None = None,
        static_ttl_seconds: float = DEFAULT_TRADE2_STATIC_TTL_SECONDS,
        base_url: str = GGG_TRADE2_BASE_URL,
        web_base_url: str = GGG_TRADE2_WEB_BASE_URL,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        injected = http_client or client
        self._owns_client = injected is None
        self.http_client = injected or Poe2HttpClient(rate_limiter=rate_limiter)
        self.rate_limiter = self.http_client.rate_limiter
        self._economy = economy
        self.cache_dir = cache_dir
        self.static_ttl_seconds = static_ttl_seconds
        self.base_url = base_url.rstrip("/")
        self.web_base_url = web_base_url.rstrip("/")
        self._now_fn = now_fn

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    def __enter__(self) -> Trade2Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    async def __aenter__(self) -> Trade2Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # --- Static Metadata (`/api/trade2/data/*`, 4h TTL) ---

    def get_static_data(
        self,
        endpoint: str,
        *,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        key = endpoint.strip().lower()
        if key not in STATIC_DATA_ENDPOINTS:
            raise ValueError(
                f"Unknown trade2 static data endpoint {endpoint!r}; "
                f"expected one of {sorted(STATIC_DATA_ENDPOINTS)}"
            )
        url = f"{self.base_url}/data/{key}"
        raw = get_cached_json(
            url,
            ttl_seconds=self.static_ttl_seconds,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )
        result = raw.get("result", []) if isinstance(raw, Mapping) else []
        return [dict(item) for item in result if isinstance(item, Mapping)]

    async def aget_static_data(
        self,
        endpoint: str,
        *,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        key = endpoint.strip().lower()
        if key not in STATIC_DATA_ENDPOINTS:
            raise ValueError(
                f"Unknown trade2 static data endpoint {endpoint!r}; "
                f"expected one of {sorted(STATIC_DATA_ENDPOINTS)}"
            )
        url = f"{self.base_url}/data/{key}"
        raw = await aget_cached_json(
            url,
            ttl_seconds=self.static_ttl_seconds,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )
        result = raw.get("result", []) if isinstance(raw, Mapping) else []
        return [dict(item) for item in result if isinstance(item, Mapping)]

    def get_data_leagues(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_static_data("leagues", force_refresh=force_refresh)

    def get_data_stats(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_static_data("stats", force_refresh=force_refresh)

    def get_data_items(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_static_data("items", force_refresh=force_refresh)

    def get_data_static(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_static_data("static", force_refresh=force_refresh)

    def get_data_filters(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        return self.get_static_data("filters", force_refresh=force_refresh)

    # --- Search (`POST /api/trade2/search/poe2/<League>`) ---

    def _prepare_search_body(
        self,
        query: Mapping[str, Any] | None,
        *,
        status_option: str,
        securable_only: bool,
        sort: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        resolved_status = "securable" if securable_only else status_option
        if resolved_status not in VALID_STATUS_OPTIONS:
            raise ValueError(
                f"Invalid trade2 status.option {resolved_status!r}; "
                f"expected one of {sorted(VALID_STATUS_OPTIONS)}"
            )

        raw = dict(query) if query is not None else {}
        if "query" in raw and isinstance(raw["query"], Mapping):
            inner_query = dict(raw["query"])
            resolved_sort = (
                dict(sort) if sort is not None else dict(raw.get("sort") or {"price": "asc"})
            )
        else:
            inner_query = raw
            resolved_sort = dict(sort) if sort is not None else {"price": "asc"}

        if securable_only or status_option != "available" or "status" not in inner_query:
            inner_query["status"] = {"option": resolved_status}
        elif isinstance(inner_query.get("status"), str):
            inner_query["status"] = {"option": inner_query["status"]}

        if "stats" not in inner_query:
            inner_query["stats"] = [{"type": "and", "filters": []}]

        return {"query": inner_query, "sort": resolved_sort}

    def search(
        self,
        query: Mapping[str, Any] | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "available",
        securable_only: bool = False,
        sort: Mapping[str, str] | None = None,
    ) -> TradeSearchResponse:
        body = self._prepare_search_body(
            query,
            status_option=status_option,
            securable_only=securable_only,
            sort=sort,
        )
        url = f"{self.base_url}/search/poe2/{quote(league, safe='')}"
        resp = self.http_client.post_json(url, json_body=body)
        qid = str(resp.get("id") or "")
        return TradeSearchResponse(
            id=qid,
            complexity=resp.get("complexity"),
            result=[str(x) for x in (resp.get("result") or []) if x],
            total=int(resp.get("total") or 0),
            inexact=bool(resp.get("inexact", False)),
            league=league,
            trade_url=build_trade_url(league, qid, web_base_url=self.web_base_url),
        )

    async def asearch(
        self,
        query: Mapping[str, Any] | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "available",
        securable_only: bool = False,
        sort: Mapping[str, str] | None = None,
    ) -> TradeSearchResponse:
        body = self._prepare_search_body(
            query,
            status_option=status_option,
            securable_only=securable_only,
            sort=sort,
        )
        url = f"{self.base_url}/search/poe2/{quote(league, safe='')}"
        resp = await self.http_client.apost_json(url, json_body=body)
        qid = str(resp.get("id") or "")
        return TradeSearchResponse(
            id=qid,
            complexity=resp.get("complexity"),
            result=[str(x) for x in (resp.get("result") or []) if x],
            total=int(resp.get("total") or 0),
            inexact=bool(resp.get("inexact", False)),
            league=league,
            trade_url=build_trade_url(league, qid, web_base_url=self.web_base_url),
        )

    # --- Batched Item Fetch (`GET /api/trade2/fetch/<up_to_10_ids>?query=<id>&realm=poe2`) ---

    def fetch(
        self,
        item_ids: Sequence[str],
        *,
        query_id: str | None = None,
        max_items: int | None = None,
        realm: str = "poe2",
    ) -> list[dict[str, Any]]:
        clean_ids = _dedupe_ids(item_ids, max_items=max_items)
        if not clean_ids:
            return []

        params: dict[str, str] = {"realm": realm}
        if query_id:
            params["query"] = query_id

        fetched: list[dict[str, Any]] = []
        for start in range(0, len(clean_ids), TRADE2_MAX_FETCH_BATCH):
            batch = clean_ids[start : start + TRADE2_MAX_FETCH_BATCH]
            url = f"{self.base_url}/fetch/{','.join(batch)}"
            resp = self.http_client.get_json(url, params=params)
            for row in resp.get("result") or []:
                if isinstance(row, Mapping):
                    fetched.append(dict(row))
        return fetched

    async def afetch(
        self,
        item_ids: Sequence[str],
        *,
        query_id: str | None = None,
        max_items: int | None = None,
        realm: str = "poe2",
    ) -> list[dict[str, Any]]:
        clean_ids = _dedupe_ids(item_ids, max_items=max_items)
        if not clean_ids:
            return []

        params: dict[str, str] = {"realm": realm}
        if query_id:
            params["query"] = query_id

        fetched: list[dict[str, Any]] = []
        for start in range(0, len(clean_ids), TRADE2_MAX_FETCH_BATCH):
            batch = clean_ids[start : start + TRADE2_MAX_FETCH_BATCH]
            url = f"{self.base_url}/fetch/{','.join(batch)}"
            resp = await self.http_client.aget_json(url, params=params)
            for row in resp.get("result") or []:
                if isinstance(row, Mapping):
                    fetched.append(dict(row))
        return fetched

    # --- Bulk Exchange (`POST /api/trade2/exchange/poe2/<League>`) ---

    def _prepare_exchange_body(
        self,
        have: Sequence[str] | str,
        want: Sequence[str] | str,
        *,
        status_option: str,
        minimum: int | None,
    ) -> dict[str, Any]:
        have_list = [have] if isinstance(have, str) else list(have)
        want_list = [want] if isinstance(want, str) else list(want)
        q: dict[str, Any] = {
            "status": {"option": status_option},
            "have": have_list,
            "want": want_list,
        }
        if minimum is not None and minimum > 0:
            q["minimum"] = int(minimum)
        return {"engine": "new", "query": q, "sort": {"have": "asc"}}

    def exchange(
        self,
        have: Sequence[str] | str,
        want: Sequence[str] | str,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "any",
        minimum: int | None = None,
    ) -> TradeExchangeResponse:
        body = self._prepare_exchange_body(have, want, status_option=status_option, minimum=minimum)
        url = f"{self.base_url}/exchange/poe2/{quote(league, safe='')}"
        resp = self.http_client.post_json(url, json_body=body)
        return _parse_exchange_response(resp, league=league)

    async def aexchange(
        self,
        have: Sequence[str] | str,
        want: Sequence[str] | str,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "any",
        minimum: int | None = None,
    ) -> TradeExchangeResponse:
        body = self._prepare_exchange_body(have, want, status_option=status_option, minimum=minimum)
        url = f"{self.base_url}/exchange/poe2/{quote(league, safe='')}"
        resp = await self.http_client.apost_json(url, json_body=body)
        return _parse_exchange_response(resp, league=league)

    # --- Listing Parsing & Price-Check Pipeline ---

    def _resolve_currency_rates(self, league: str) -> tuple[dict[str, float], float]:
        rates = dict(_FALLBACK_CURRENCY_RATES_EXALTED)
        div_ex = rates["divine"]
        if self._economy is not None:
            try:
                ref = self._economy.get_reference_rates(league)
                rates.update(ref.rates_in_exalted)
                if ref.divine_exalted > 0:
                    div_ex = ref.divine_exalted
            except Exception:
                pass
        return rates, div_ex

    def parse_listing(
        self,
        raw_entry: Mapping[str, Any],
        *,
        league: str = DEFAULT_LEAGUE,
        currency_rates: Mapping[str, float] | None = None,
    ) -> TradeListing | None:
        listing = raw_entry.get("listing")
        item_obj = raw_entry.get("item")
        if not isinstance(listing, Mapping) or not isinstance(item_obj, Mapping):
            return None

        price = listing.get("price")
        if not isinstance(price, Mapping):
            return None

        amount = _safe_float(price.get("amount"))
        currency = str(price.get("currency") or "").strip().lower()
        if amount is None or amount <= 0 or not currency:
            return None

        rates_map = currency_rates
        if rates_map is None:
            rates_map, _ = self._resolve_currency_rates(league)

        unit_ex = rates_map.get(currency)
        if unit_ex is None and self._economy is not None:
            unit_ex = self._economy.convert_to_exalted(1.0, currency, league=league)
        if unit_ex is None or unit_ex <= 0:
            return None

        price_exalted = round(amount * unit_ex, 4)

        fee_raw = listing.get("fee")
        fee = int(fee_raw) if isinstance(fee_raw, (int, float)) else None
        whisper = str(listing.get("whisper")) if listing.get("whisper") else None
        sale_type = (
            SaleType.SECURABLE
            if fee is not None
            else (SaleType.WHISPER if whisper else SaleType.UNKNOWN)
        )

        acct_obj = listing.get("account") if isinstance(listing.get("account"), Mapping) else {}
        account_name = str(acct_obj.get("name") or "unknown")
        online = acct_obj.get("online") is not None and acct_obj.get("online") is not False

        parsed_item = parse_fetch_item(item_obj)
        pob_text = format_parsed_item_for_pob2(parsed_item)
        mod_lines = [format_mod_line_display(m) for m in parsed_item.mods]

        return TradeListing(
            listing_id=str(raw_entry.get("id") or item_obj.get("id") or ""),
            item_name=parsed_item.name,
            base_type=parsed_item.base_type,
            item_level=parsed_item.item_level,
            corrupted=parsed_item.corrupted,
            price_amount=amount,
            price_currency=currency,
            price_exalted=price_exalted,
            sale_type=sale_type,
            fee=fee,
            whisper=whisper,
            account_name=account_name,
            online=online,
            indexed_at=str(listing.get("indexed")) if listing.get("indexed") else None,
            pob_item_text=pob_text,
            mod_lines=mod_lines,
        )

    def parse_listings(
        self,
        raw_entries: Sequence[Mapping[str, Any]],
        *,
        league: str = DEFAULT_LEAGUE,
        currency_rates: Mapping[str, float] | None = None,
    ) -> list[TradeListing]:
        resolved_rates = currency_rates
        if resolved_rates is None:
            resolved_rates, _ = self._resolve_currency_rates(league)
        out: list[TradeListing] = []
        for entry in raw_entries:
            parsed = self.parse_listing(entry, league=league, currency_rates=resolved_rates)
            if parsed is not None:
                out.append(parsed)
        return out

    def price_check(
        self,
        query: Mapping[str, Any] | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "available",
        securable_only: bool = False,
        max_fetch: int = 20,
        limit: int | None = None,
        sort: Mapping[str, str] | None = None,
    ) -> DetailedPriceCheckResult:
        fetch_cap = max(limit, 10) if limit is not None else max_fetch
        search_res = self.search(
            query,
            league=league,
            status_option=status_option,
            securable_only=securable_only,
            sort=sort,
        )
        rates_map, div_ex = self._resolve_currency_rates(league)
        if not search_res.result:
            return summarize_price_check(
                [],
                league=league,
                query_id=search_res.id,
                trade_url=search_res.trade_url,
                total_matches=search_res.total,
                divine_exalted=div_ex,
            )

        raw_fetched = self.fetch(
            search_res.result,
            query_id=search_res.id,
            max_items=fetch_cap,
        )
        listings = self.parse_listings(raw_fetched, league=league, currency_rates=rates_map)
        return summarize_price_check(
            listings,
            league=league,
            query_id=search_res.id,
            trade_url=search_res.trade_url,
            total_matches=search_res.total,
            divine_exalted=div_ex,
        )

    async def aprice_check(
        self,
        query: Mapping[str, Any] | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        status_option: str = "available",
        securable_only: bool = False,
        max_fetch: int = 20,
        limit: int | None = None,
        sort: Mapping[str, str] | None = None,
    ) -> DetailedPriceCheckResult:
        fetch_cap = max(limit, 10) if limit is not None else max_fetch
        search_res = await self.asearch(
            query,
            league=league,
            status_option=status_option,
            securable_only=securable_only,
            sort=sort,
        )
        rates_map, div_ex = self._resolve_currency_rates(league)
        if not search_res.result:
            return summarize_price_check(
                [],
                league=league,
                query_id=search_res.id,
                trade_url=search_res.trade_url,
                total_matches=search_res.total,
                divine_exalted=div_ex,
            )

        raw_fetched = await self.afetch(
            search_res.result,
            query_id=search_res.id,
            max_items=fetch_cap,
        )
        listings = self.parse_listings(raw_fetched, league=league, currency_rates=rates_map)
        return summarize_price_check(
            listings,
            league=league,
            query_id=search_res.id,
            trade_url=search_res.trade_url,
            total_matches=search_res.total,
            divine_exalted=div_ex,
        )

    search_and_fetch = price_check
    asearch_and_fetch = aprice_check


def _dedupe_ids(item_ids: Sequence[str], *, max_items: int | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in item_ids:
        token = str(raw).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if max_items is not None and len(out) >= max_items:
            break
    return out


def _resolve_mod_domain(entry: Mapping[str, Any], default_domain: ModDomain) -> ModDomain:
    flags = entry.get("flags")
    if isinstance(flags, Mapping):
        if flags.get("desecrated"):
            return ModDomain.DESECRATED
        if flags.get("fractured"):
            return ModDomain.FRACTURED

    raw_dom = str(entry.get("domain") or "").strip().lower()
    if raw_dom == "desecrated":
        return ModDomain.DESECRATED
    if raw_dom == "fractured":
        return ModDomain.FRACTURED
    if raw_dom in ("rune", "augment"):
        return ModDomain.RUNE
    if raw_dom == "implicit":
        return ModDomain.IMPLICIT
    if raw_dom == "enchant":
        return ModDomain.ENCHANT
    if raw_dom in ("crafted", "bench"):
        return ModDomain.CRAFTED
    if raw_dom == "corrupted":
        return ModDomain.CORRUPTED
    if raw_dom == "skill":
        return ModDomain.SKILL
    return default_domain


def _safe_float(val: Any) -> float | None:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip().replace("%", "").replace("+", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _extract_first_prop_number(prop: Mapping[str, Any]) -> float | None:
    vals = prop.get("values")
    if not isinstance(vals, Sequence) or not vals:
        return None
    first = vals[0]
    raw = first[0] if (isinstance(first, Sequence) and first) else first
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        match = _NUMBER_RE.search(raw)
        if match:
            return float(match.group(0))
    return None


def _parse_requirements(raw_reqs: Any) -> ItemRequirements:
    if not isinstance(raw_reqs, Sequence):
        return ItemRequirements()
    lvl = 0
    strength = 0
    dexterity = 0
    intelligence = 0
    for req in raw_reqs:
        if not isinstance(req, Mapping):
            continue
        name = strip_wiki_markup(str(req.get("name") or "")).strip().lower()
        num = _extract_first_prop_number(req)
        if num is None:
            continue
        ival = int(num)
        if "level" in name:
            lvl = ival
        elif name in ("str", "strength"):
            strength = ival
        elif name in ("dex", "dexterity"):
            dexterity = ival
        elif name in ("int", "intelligence"):
            intelligence = ival
    return ItemRequirements(
        level=lvl,
        strength=strength,
        dexterity=dexterity,
        intelligence=intelligence,
    )


def _parse_properties(raw_props: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw_props, Sequence):
        return out
    for idx, prop in enumerate(raw_props):
        if not isinstance(prop, Mapping):
            continue
        name_clean = strip_wiki_markup(str(prop.get("name") or "")).strip()
        name_low = name_clean.lower()
        vals = prop.get("values")
        if idx == 0 and isinstance(vals, Sequence) and len(vals) == 0 and name_clean:
            out["item_class"] = name_clean
            continue

        num = _extract_first_prop_number(prop)
        if num is None:
            continue

        if name_low.startswith("quality"):
            out["quality"] = int(abs(num))
            cat_match = re.search(r"\(([^)]+)\)", name_clean)
            if cat_match:
                out["catalyst_type"] = cat_match.group(1).strip()
        elif name_low == "spirit":
            out["spirit"] = int(num)
        elif name_low == "armour":
            out["armour"] = int(num)
        elif "evasion" in name_low:
            out["evasion"] = int(num)
        elif "energy shield" in name_low:
            out["energy_shield"] = int(num)
        elif "ward" in name_low:
            out["ward"] = int(num)
        elif "block" in name_low:
            out["block_chance"] = float(num)
        elif "attacks per second" in name_low:
            out["aps"] = float(num)
        elif "critical" in name_low:
            out["crit"] = float(num)
        elif "reload time" in name_low:
            out["reload_time"] = float(num)
        elif name_low == "level":
            out["gem_level"] = int(num)
    return out


def _parse_exchange_response(resp: Mapping[str, Any], *, league: str) -> TradeExchangeResponse:
    qid = str(resp.get("id") or "")
    total = int(resp.get("total") or 0)
    raw_result = resp.get("result")
    offers: list[ExchangeOffer] = []

    entries: Sequence[Any]
    if isinstance(raw_result, Mapping):
        entries = list(raw_result.values())
    elif isinstance(raw_result, Sequence):
        entries = raw_result
    else:
        entries = []

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        listing = entry.get("listing")
        if not isinstance(listing, Mapping):
            continue
        acct_obj = listing.get("account") if isinstance(listing.get("account"), Mapping) else {}
        acct_name = str(acct_obj.get("name") or "unknown")
        online = acct_obj.get("online") is not None and acct_obj.get("online") is not False

        for off in listing.get("offers") or []:
            if not isinstance(off, Mapping):
                continue
            ex_part = off.get("exchange") if isinstance(off.get("exchange"), Mapping) else {}
            it_part = off.get("item") if isinstance(off.get("item"), Mapping) else {}
            have_curr = str(ex_part.get("currency") or "")
            want_curr = str(it_part.get("currency") or "")
            have_amt = _safe_float(ex_part.get("amount"))
            want_amt = _safe_float(it_part.get("amount"))
            if not have_curr or not want_curr or not have_amt or not want_amt or want_amt <= 0:
                continue
            stock = int(it_part["stock"]) if isinstance(it_part.get("stock"), (int, float)) else 0
            offers.append(
                ExchangeOffer(
                    listing_id=str(entry.get("id") or it_part.get("id") or ""),
                    account_name=acct_name,
                    online=online,
                    have_currency=have_curr,
                    have_amount=have_amt,
                    want_currency=want_curr,
                    want_amount=want_amt,
                    stock=stock,
                    have_per_want=round(have_amt / want_amt, 6),
                )
            )

    return TradeExchangeResponse(id=qid, league=league, total=total, offers=offers)


def _quantile(sorted_vals: Sequence[float], q: float) -> float | None:
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return round(sorted_vals[0], 4)
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(sorted_vals[lo], 4)
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 4)


def _iqr_mean(vals: Sequence[float]) -> float | None:
    if not vals:
        return None
    ordered = sorted(vals)
    n = len(ordered)
    if n <= 3:
        return round(sum(ordered) / n, 4)
    lo_idx = n // 4
    hi_idx = n - (n // 4)
    core = ordered[lo_idx:hi_idx] or ordered
    return round(sum(core) / len(core), 4)
