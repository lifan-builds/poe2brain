from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
)
from poe2brain.http import GGGRateLimiter, Poe2HttpClient
from poe2brain.models.builds import SlotStatWeights
from poe2brain.models.common import Poe2Model, SaleType
from poe2brain.models.items import ItemRarity, ModDomain, ParsedItem
from poe2brain.models.trade import (
    CurrencyQuote,
    PriceCheckResult,
    PriceDistribution,
    TradeListing,
)
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle
from poe2brain.trade.item_parser import (
    extract_stack_size,
    extract_unrevealed_desecrated_sides,
    parse_clipboard_item,
    resolve_slot_or_category,
    resolve_trade_category,
    strip_wiki_markup,
    trade2_fetch_to_pob2_text,
)
from poe2brain.trade.stat_matcher import (
    DISCRETE_EXACT_MIN_RE,
    build_item_trade_query,
    build_natural_trade_query,
)

DumpTriageBucket = Literal[
    "sell_instant",
    "craft_finish_ev",
    "high_variance_id_or_unveil",
    "salvage_or_vendor",
]


class DumpTriageEntry(Poe2Model):
    index: int
    item_label: str
    item_class: str | None = None
    rarity: ItemRarity
    base_type: str
    bucket: DumpTriageBucket
    stack_size: int = 1
    unit_exalted: float | None = None
    estimated_total_exalted: float | None = None
    estimated_total_divine: float | None = None
    securable_min_exalted: float | None = None
    price_source: str = "trade2"
    open_prefixes: int = 0
    open_suffixes: int = 0
    recommended_next_step: str
    rationale: str
    trade_url: str | None = None
    fixer_warnings: list[str] = Field(default_factory=list)


class DumpTriageReport(Poe2Model):
    league: str = DEFAULT_LEAGUE
    total_items: int
    total_estimated_exalted: float = 0.0
    total_estimated_divine: float | None = None
    counts_by_bucket: dict[str, int] = Field(default_factory=dict)
    entries: list[DumpTriageEntry] = Field(default_factory=list)
    summary_notes: list[str] = Field(default_factory=list)


class TradeCandidate(TradeListing):
    """`TradeListing` enriched with `.pob2_item_text` alias for PoB2 eval."""

    @property
    def pob2_item_text(self) -> str | None:
        return self.pob_item_text


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 4)
    idx = (len(sorted_vals) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 4)


def _quote_to_price_check(quote_obj: CurrencyQuote, league: str) -> PriceCheckResult:
    ex = round(quote_obj.exalted, 4)
    div = round(quote_obj.divine, 4) if quote_obj.divine is not None else None
    dist = PriceDistribution(
        count_total=1,
        count_deduplicated=1,
        count_securable=1,
        count_whisper=0,
        min_exalted=ex,
        p25_exalted=ex,
        median_exalted=ex,
        p75_exalted=ex,
        securable_min_exalted=ex,
        securable_median_exalted=ex,
    )
    return PriceCheckResult(
        league=league,
        query_id=f"economy:{quote_obj.source}:{quote_obj.id}",
        trade_url=None,
        total_matches=1,
        estimated_exalted=ex,
        estimated_divine=div,
        distribution=dist,
        listings=[],
        fixer_warnings=[],
    )


def _should_route_to_economy(item: ParsedItem, roll_tolerance: float, securable_only: bool) -> bool:
    """
    Route bulk-exchange items (Currency, Omens, Essences, Runes/Soul Cores, Uncut Gems)
    and uncorrupted Base-Uniques (unidentified, roll_tolerance <= 0, or fixed-roll uniques)
    through `EconomyService` for instant sub-ms pricing.
    """
    if item.rarity == ItemRarity.CURRENCY:
        if item.area_level is not None or item.item_class in ("Trial Coins", "Inscribed Ultimatum"):
            return False
        return True

    if (
        item.rarity == ItemRarity.UNIQUE
        and not item.corrupted
        and not item.sanctified
        and not securable_only
        and item.name
    ):
        if not item.identified or roll_tolerance <= 0.0:
            return True
        has_variable_rolls = any(
            any(r.min != r.max for r in m.roll_ranges)
            for m in item.mods
            if m.domain != ModDomain.RUNE
        )
        if not has_variable_rolls and any(m.roll_ranges for m in item.mods):
            return True

    return False


class TradeAdvisor:
    """
    High-level PoE2 Trade & Pricing orchestrator uniting:
    - Clipboard `Ctrl+C` / `Ctrl+Alt+C` item parsing (`parse_clipboard_item`)
    - Fast bulk `EconomyService` lookup (`poe2scout` + `poe.ninja`)
    - Official `trade2` live search + fetch (`Trade2Client` or built-in HTTP fallback)
    - PoB2 `Item.lua` conversion (`trade2_fetch_to_pob2_text`) for `GetMiscCalculator()`
    """

    def __init__(
        self,
        *,
        economy: Any | None = None,
        trade2: Any | None = None,
        bundle: EE2Bundle | None = None,
    ) -> None:
        self._economy = economy
        self._trade2 = trade2
        self._bundle = bundle or get_default_bundle()
        self._fallback_http: Poe2HttpClient | None = None
        self._pool_engine: Any | None = None
        self._search_limiter = GGGRateLimiter()
        self._fetch_limiter = GGGRateLimiter()

    def _get_pool_engine(self) -> Any | None:
        if self._pool_engine is not None:
            return self._pool_engine
        try:
            from poe2brain.crafting.pool import ModPoolEngine

            self._pool_engine = ModPoolEngine()
            return self._pool_engine
        except Exception:
            return None

    def _get_economy(self) -> Any | None:
        if self._economy is not None:
            return self._economy
        try:
            from poe2brain.trade.economy import EconomyService

            self._economy = EconomyService()
            return self._economy
        except Exception:
            return None

    def _get_trade2(self) -> Any | None:
        if self._trade2 is not None:
            return self._trade2
        try:
            from poe2brain.trade.trade2_client import Trade2Client

            self._trade2 = Trade2Client(economy=self._get_economy())
        except TypeError:
            from poe2brain.trade.trade2_client import Trade2Client

            self._trade2 = Trade2Client()
        except Exception:
            return None
        return self._trade2

    def _convert_price_to_exalted(self, amount: float, currency: str, league: str) -> float:
        econ = self._get_economy()
        if econ is not None and hasattr(econ, "convert_to_exalted"):
            conv = econ.convert_to_exalted(amount, currency, league=league)
            if conv is not None:
                return round(conv, 4)
        norm = currency.strip().lower()
        fallback_rates = {
            "exalted": 1.0,
            "ex": 1.0,
            "chaos": 36.0,
            "divine": 460.0,
            "div": 460.0,
            "regal": 0.25,
            "alch": 5.0,
            "aug": 0.05,
            "transmute": 0.02,
            "vaal": 2.0,
            "annul": 80.0,
        }
        return round(amount * fallback_rates.get(norm, 1.0), 4)

    def _divine_rate_exalted(self, league: str) -> float | None:
        econ = self._get_economy()
        if econ is not None:
            if hasattr(econ, "get_reference_rates"):
                try:
                    rates = econ.get_reference_rates(league)
                    div_ex = getattr(rates, "divine_in_exalted", None) or (
                        getattr(rates, "rates_in_exalted", {}) or {}
                    ).get("divine")
                    if div_ex and div_ex > 0:
                        return float(div_ex)
                except Exception:
                    pass
            if hasattr(econ, "convert_to_exalted"):
                try:
                    val = econ.convert_to_exalted(1.0, "divine", league=league)
                    if val and val > 0:
                        return float(val)
                except Exception:
                    pass
        return None

    def _ensure_candidates_with_pob(
        self,
        res: PriceCheckResult,
        raw_entries_by_id: dict[str, dict[str, Any]] | None = None,
        limit: int | None = None,
    ) -> PriceCheckResult:
        enriched: list[TradeListing] = []
        for lst in res.listings:
            pob_txt = lst.pob_item_text
            if not pob_txt and raw_entries_by_id and lst.listing_id in raw_entries_by_id:
                pob_txt = trade2_fetch_to_pob2_text(raw_entries_by_id[lst.listing_id])
            cand = TradeCandidate(
                **{
                    **lst.model_dump(),
                    "pob_item_text": pob_txt,
                }
            )
            enriched.append(cand)
        if limit is not None:
            enriched = enriched[:limit]
        return res.model_copy(update={"listings": enriched})

    def _execute_trade2_query(
        self,
        payload: dict[str, Any],
        *,
        league: str,
        limit: int = 20,
    ) -> PriceCheckResult:
        client = self._get_trade2()
        if client is not None:
            # Support whichever high-level search+price-check method Trade2Client exposes
            for method_name in (
                "price_check",
                "search_and_price_check",
                "execute_price_check",
                "price_check_query",
            ):
                fn = getattr(client, method_name, None)
                if callable(fn):
                    try:
                        res = fn(payload, league=league, max_fetch=max(limit, 15))
                    except TypeError:
                        try:
                            res = fn(payload, league=league, limit=limit)
                        except TypeError:
                            res = fn(payload, league)
                    if isinstance(res, PriceCheckResult):
                        return self._ensure_candidates_with_pob(res, limit=limit)

            # Or two-step `search` + `fetch` on Trade2Client
            search_fn = getattr(client, "search", None) or getattr(client, "search_items", None)
            fetch_fn = getattr(client, "fetch", None) or getattr(client, "fetch_items", None)
            if callable(search_fn) and callable(fetch_fn):
                s_data = search_fn(payload, league=league)
                q_id = s_data.get("id", "")
                ids = (s_data.get("result") or [])[: max(limit, 10)]
                raw_items = fetch_fn(ids, query_id=q_id) if ids else []
                if isinstance(raw_items, Mapping):
                    raw_items = raw_items.get("result", [])
                return self._build_result_from_raw_fetch(
                    league=league,
                    query_id=q_id,
                    total_matches=int(s_data.get("total", 0)),
                    raw_entries=list(raw_items),
                    limit=limit,
                )

        # Direct built-in fallback via shared Poe2HttpClient + GGGRateLimiter
        if self._fallback_http is None:
            self._fallback_http = Poe2HttpClient()
        lg_enc = quote(league, safe="")
        s_resp = self._fallback_http.request(
            "POST",
            f"{GGG_TRADE2_BASE_URL}/search/poe2/{lg_enc}",
            json_body=payload,
            rate_limiter=self._search_limiter,
        )
        s_data = s_resp.json()
        q_id = s_data.get("id", "")
        all_ids: list[str] = list(s_data.get("result") or [])
        fetch_target = min(len(all_ids), max(limit, 10), 20)
        raw_entries: list[dict[str, Any]] = []
        for offset in range(0, fetch_target, 10):
            batch_ids = ",".join(all_ids[offset : offset + 10])
            f_resp = self._fallback_http.request(
                "GET",
                f"{GGG_TRADE2_BASE_URL}/fetch/{batch_ids}?query={quote(q_id, safe='')}&realm=poe2",
                rate_limiter=self._fetch_limiter,
            )
            f_json = f_resp.json()
            raw_entries.extend(f_json.get("result") or [])

        return self._build_result_from_raw_fetch(
            league=league,
            query_id=q_id,
            total_matches=int(s_data.get("total", 0)),
            raw_entries=raw_entries,
            limit=limit,
        )

    def _build_result_from_raw_fetch(
        self,
        *,
        league: str,
        query_id: str,
        total_matches: int,
        raw_entries: list[dict[str, Any]],
        limit: int,
    ) -> PriceCheckResult:
        candidates: list[TradeListing] = []
        deduped: list[TradeCandidate] = []
        account_counts: dict[str, int] = {}

        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            listing = entry.get("listing") or {}
            item = entry.get("item") or {}
            price = listing.get("price") or {}
            amount = price.get("amount")
            currency = price.get("currency")
            if amount is None or not currency:
                continue

            acct_obj = listing.get("account") or {}
            acct_name = str(acct_obj.get("name") or "unknown")
            fee = listing.get("fee")
            whisper = listing.get("whisper")
            sale_type = (
                SaleType.SECURABLE
                if fee is not None
                else (SaleType.WHISPER if whisper else SaleType.UNKNOWN)
            )
            price_ex = self._convert_price_to_exalted(float(amount), str(currency), league)

            mod_lines: list[str] = []
            for key in (
                "enchantMods",
                "runeMods",
                "implicitMods",
                "fracturedMods",
                "explicitMods",
                "desecratedMods",
                "craftedMods",
            ):
                for m_ent in item.get(key) or []:
                    desc = m_ent.get("description") if isinstance(m_ent, dict) else str(m_ent)
                    if desc:
                        mod_lines.append(strip_wiki_markup(desc))

            cand = TradeCandidate(
                listing_id=str(entry.get("id") or item.get("id") or ""),
                item_name=strip_wiki_markup(item["name"]) if item.get("name") else None,
                base_type=strip_wiki_markup(
                    str(item.get("baseType") or item.get("typeLine") or "Unknown")
                ),
                item_level=item.get("ilvl"),
                corrupted=bool(item.get("corrupted")),
                price_amount=float(amount),
                price_currency=str(currency),
                price_exalted=price_ex,
                sale_type=sale_type,
                fee=int(fee) if fee is not None else None,
                whisper=whisper,
                account_name=acct_name,
                online=acct_obj.get("online") is not None,
                indexed_at=listing.get("indexed"),
                pob_item_text=trade2_fetch_to_pob2_text(entry),
                mod_lines=mod_lines,
            )
            candidates.append(cand)
            account_counts[acct_name] = account_counts.get(acct_name, 0) + 1
            if account_counts[acct_name] == 1:
                deduped.append(cand)

        fixer_warnings: list[str] = []
        for acct, cnt in account_counts.items():
            if cnt >= 3:
                fixer_warnings.append(
                    f"Account {acct} holds {cnt}/{len(candidates)} top listings (deduplicated)."
                )
        if len(deduped) >= 4:
            ex_sorted = sorted(c.price_exalted for c in deduped)
            med = statistics.median(ex_sorted)
            if med > 0 and ex_sorted[0] < med * 0.35:
                fixer_warnings.append(
                    f"Lowest listing ({ex_sorted[0]:.2f} ex) is >65% below median "
                    f"({med:.2f} ex); probable price-fixer outlier."
                )

        ex_vals = sorted(c.price_exalted for c in deduped)
        sec_vals = sorted(c.price_exalted for c in deduped if c.is_instant_buyout)
        whisp_count = sum(1 for c in deduped if not c.is_instant_buyout)
        median_ex = round(statistics.median(ex_vals), 4) if ex_vals else None
        div_rate = self._divine_rate_exalted(league)
        est_div = round(median_ex / div_rate, 4) if (median_ex is not None and div_rate) else None

        dist = PriceDistribution(
            count_total=len(candidates),
            count_deduplicated=len(deduped),
            count_securable=len(sec_vals),
            count_whisper=whisp_count,
            min_exalted=round(ex_vals[0], 4) if ex_vals else None,
            p25_exalted=_percentile(ex_vals, 0.25),
            median_exalted=median_ex,
            p75_exalted=_percentile(ex_vals, 0.75),
            securable_min_exalted=round(sec_vals[0], 4) if sec_vals else None,
            securable_median_exalted=(round(statistics.median(sec_vals), 4) if sec_vals else None),
        )
        trade_url = (
            f"{GGG_TRADE2_WEB_BASE_URL}/{quote(league, safe='')}/{query_id}" if query_id else None
        )

        return PriceCheckResult(
            league=league,
            query_id=query_id or None,
            trade_url=trade_url,
            total_matches=total_matches,
            estimated_exalted=median_ex,
            estimated_divine=est_div,
            distribution=dist,
            listings=candidates[:limit],
            fixer_warnings=fixer_warnings,
        )

    def price_check_item(
        self,
        raw_text: str,
        league: str = DEFAULT_LEAGUE,
        roll_tolerance: float = 0.9,
        securable_only: bool = False,
    ) -> PriceCheckResult:
        """
        Price-check any PoE2 `Ctrl+C` or `Ctrl+Alt+C` clipboard item:
        - Fast-routes Currencies, Omens, Essences, Runes/Soul Cores, Uncut Gems, and Base-Uniques
          via `EconomyService`.
        - Routes Rares, Waystones, Precursor Tablets, Cut/Corrupted Gems, Sanctum Relics, and
          variable-roll Uniques via official `Trade2Client` (attaching PoB2 item text to listings).
        """
        item = parse_clipboard_item(raw_text, bundle=self._bundle)

        if _should_route_to_economy(
            item, roll_tolerance=roll_tolerance, securable_only=securable_only
        ):
            econ = self._get_economy()
            lookup_name = item.name or item.base_type
            if econ is not None and hasattr(econ, "get_price"):
                quote_obj = econ.get_price(lookup_name, league=league)
                if quote_obj is not None:
                    return _quote_to_price_check(quote_obj, league=league)

        status = "securable" if securable_only else "available"
        payload = build_item_trade_query(
            item,
            status=status,
            roll_tolerance=roll_tolerance,
            bundle=self._bundle,
        )
        # For pure currency/stackable fallback on trade2 when EconomyService was unavailable
        if item.rarity == ItemRarity.CURRENCY and not payload["query"].get("type"):
            payload["query"]["type"] = item.base_type
            payload["query"].pop("filters", None)
        resolve_trade_category(item, self._bundle)
        return self._execute_trade2_query(payload, league=league, limit=15)

    def find_trade_candidates(
        self,
        league: str = DEFAULT_LEAGUE,
        slot_or_category: str | None = None,
        stat_filters_or_weights: (
            SlotStatWeights | Sequence[Any] | Mapping[str, Any] | None
        ) = None,
        max_price_exalted: float | None = None,
        min_spirit: int | None = None,
        securable_only: bool = False,
        limit: int = 10,
    ) -> PriceCheckResult:
        """
        Search official PoE2 trade for equipment/gem upgrades matching natural-language
        constraints or PoB2 `SlotStatWeights`, returning priced `TradeCandidate` listings
        with ready-to-evaluate `.pob_item_text` / `.pob2_item_text` for `GetMiscCalculator()`.
        """
        status = "securable" if securable_only else "available"

        if (
            isinstance(stat_filters_or_weights, Mapping)
            and "query" in stat_filters_or_weights
            and isinstance(stat_filters_or_weights["query"], Mapping)
        ):
            payload = dict(stat_filters_or_weights)
        elif isinstance(stat_filters_or_weights, SlotStatWeights):
            cat = resolve_slot_or_category(
                slot_or_category or stat_filters_or_weights.equipment_slot
            )
            if stat_filters_or_weights.trade2_weighted_filter and "query" in (
                stat_filters_or_weights.trade2_weighted_filter
            ):
                payload = dict(stat_filters_or_weights.trade2_weighted_filter)
            else:
                payload = build_natural_trade_query(
                    category=cat,
                    weighted_stats=stat_filters_or_weights,
                    min_spirit_prop=min_spirit,
                    max_price_exalted=max_price_exalted,
                    status=status,
                    bundle=self._bundle,
                )
        else:
            payload = build_natural_trade_query(
                category=slot_or_category,
                stat_constraints=stat_filters_or_weights,
                min_spirit_prop=min_spirit,
                max_price_exalted=max_price_exalted,
                status=status,
                bundle=self._bundle,
            )

        return self._execute_trade2_query(payload, league=league, limit=limit)

    def _format_triage_label(self, item: ParsedItem, stack_size: int) -> str:
        if not item.identified:
            tier_str = f" (Tier {item.unidentified_tier})" if item.unidentified_tier else ""
            ilvl_str = f" [ilvl {item.item_level}]" if item.item_level else ""
            return f"Unidentified{tier_str} {item.rarity.value.title()} {item.base_type}{ilvl_str}"
        if item.rarity == ItemRarity.GEM:
            parts = []
            if item.gem_level:
                parts.append(f"Lv{item.gem_level}")
            if item.gem_sockets:
                parts.append(f"{item.gem_sockets}S")
            if item.quality:
                parts.append(f"{item.quality}%Q")
            if item.corrupted:
                parts.append("Corrupted")
            meta = f" ({', '.join(parts)})" if parts else ""
            return f"{item.name or item.base_type}{meta}"
        if stack_size > 1:
            return f"{stack_size}x {item.name or item.base_type}"
        if item.name and item.name != item.base_type:
            return f"{item.name} ({item.base_type})"
        return item.base_type

    def _heuristic_unit_exalted(self, item: ParsedItem, strong_mod_count: int) -> float:
        if not item.identified:
            if (item.unidentified_tier or 0) >= 5:
                return 35.0
            if (item.unidentified_tier or 0) >= 4:
                return 12.0 if (item.item_level or 0) >= 81 else 5.0
            return 1.0
        if item.rarity == ItemRarity.GEM:
            if (item.gem_level or 0) >= 20 and (item.gem_sockets or 0) >= 5:
                return 180.0 if not item.corrupted else 95.0
            if (item.gem_sockets or 0) >= 5:
                return 60.0
            if (item.gem_level or 0) >= 20:
                return 35.0
            return 1.0
        if item.waystone_tier:
            if item.waystone_tier >= 16:
                return 18.0
            if item.waystone_tier == 15 and (item.waystone_drop_chance_pct or 0) >= 250:
                return 8.0
            return 2.0
        if resolve_trade_category(item, self._bundle) == "map.tablet":
            explicit_cnt = sum(1 for m in item.mods if m.domain == ModDomain.EXPLICIT)
            return 15.0 if explicit_cnt >= 2 else 4.0
        if item.veiled_desecrated:
            has_skill_lvl = any(DISCRETE_EXACT_MIN_RE.search(m.raw_text) for m in item.mods)
            return 120.0 if has_skill_lvl else 25.0
        if item.fractured and any(
            m.domain == ModDomain.FRACTURED and DISCRETE_EXACT_MIN_RE.search(m.raw_text)
            for m in item.mods
        ):
            return 450.0
        if strong_mod_count >= 4:
            return 90.0
        if strong_mod_count >= 3:
            return 30.0
        return 2.0

    def triage_clipboard_dump(
        self,
        items_raw_list: Sequence[str],
        *,
        league: str = DEFAULT_LEAGUE,
        min_sell_exalted: float = 5.0,
        roll_tolerance: float = 0.9,
        securable_only: bool = False,
        live_trade: bool = True,
    ) -> DumpTriageReport:
        """Batch price-check and triage a dump tab of mixed PoE2 `Ctrl+C` / `Ctrl+Alt+C` items.

        Classifies every item into one of four actionable buckets:
        - `sell_instant`: Stackable for Alva Exchange or Instant Buyout (`securable`).
        - `craft_finish_ev`: Uncorrupted Rare with fractured/high-tier core and open affix slot(s)
          where Desecrating or slamming adds positive expected value before selling.
        - `high_variance_id_or_unveil`: Unidentified Tier 3–5 high-ilvl gear or unrevealed
          `Desecrated Prefix`/`Suffix` item awaiting Well of Souls unveil.
        - `salvage_or_vendor`: Sub-threshold items best routed to Salvage Bench, Disenchant
          (Chance Shards), or 3-to-1 Reforging Bench rather than burning Gold listing fees.
        """
        from poe2brain.crafting.planner import _bone_family_for_base

        div_rate = self._divine_rate_exalted(league) or 460.0
        pool_eng = self._get_pool_engine()

        entries: list[DumpTriageEntry] = []
        counts: dict[str, int] = {
            "sell_instant": 0,
            "craft_finish_ev": 0,
            "high_variance_id_or_unveil": 0,
            "salvage_or_vendor": 0,
        }
        total_ex_sum = 0.0

        for idx, raw_text in enumerate(items_raw_list):
            item = parse_clipboard_item(raw_text, bundle=self._bundle)
            stack_qty, _ = extract_stack_size(raw_text)
            label = self._format_triage_label(item, stack_qty)
            trade_cat = resolve_trade_category(item, self._bundle)

            open_p = 0
            open_s = 0
            strong_mods = 0
            has_desecrated = any(m.domain == ModDomain.DESECRATED for m in item.mods)
            bone_family: str | None = None

            is_craftable_gear = (
                item.identified
                and item.rarity == ItemRarity.RARE
                and trade_cat is not None
                and not trade_cat.startswith(("map", "gem", "currency", "flask", "card", "sanctum"))
            )
            if is_craftable_gear and pool_eng is not None:
                try:
                    st = pool_eng.parse_state_from_item(item)
                    open_p = max(0, st.base.max_prefixes - st.prefix_count)
                    open_s = max(0, st.base.max_suffixes - st.suffix_count)
                    strong_mods = sum(
                        1
                        for m in st.mods
                        if m.display_tier <= 2
                        or m.fractured
                        or DISCRETE_EXACT_MIN_RE.search(m.stat_text)
                    )
                    has_desecrated = st.has_desecrated
                    bone_family = _bone_family_for_base(st.base)
                except Exception:
                    pass

            unit_ex: float | None = None
            sec_min_ex: float | None = None
            price_source = "heuristic_offline"
            trade_url: str | None = None
            fixer_warnings: list[str] = []

            if _should_route_to_economy(
                item, roll_tolerance=roll_tolerance, securable_only=securable_only
            ):
                econ = self._get_economy()
                lookup_name = item.name or item.base_type
                if econ is not None and hasattr(econ, "get_price"):
                    q_obj = econ.get_price(lookup_name, league=league)
                    if q_obj is not None:
                        unit_ex = round(q_obj.exalted, 4)
                        sec_min_ex = unit_ex
                        price_source = f"economy:{q_obj.source}"

            if unit_ex is None and (live_trade or self._trade2 is not None):
                pc = self.price_check_item(
                    raw_text,
                    league=league,
                    roll_tolerance=roll_tolerance,
                    securable_only=securable_only,
                )
                trade_url = pc.trade_url
                fixer_warnings = list(pc.fixer_warnings)
                unit_ex = pc.distribution.securable_median_exalted or pc.estimated_exalted
                sec_min_ex = pc.distribution.securable_min_exalted
                if unit_ex is not None:
                    price_source = (
                        "economy" if (pc.query_id or "").startswith("economy:") else "trade2"
                    )

            if unit_ex is None:
                unit_ex = self._heuristic_unit_exalted(item, strong_mods)
                price_source = "trade2_scarce_heuristic_floor" if trade_url else "heuristic_offline"

            est_total_ex = round(unit_ex * stack_qty, 4)
            est_total_div = round(est_total_ex / div_rate, 4) if div_rate > 0 else None
            total_ex_sum += est_total_ex

            # Classify into actionable PoE2 0.5.5 bucket
            bucket: DumpTriageBucket
            next_step: str
            rationale: str

            if item.veiled_desecrated:
                bucket = "high_variance_id_or_unveil"
                dp, ds = extract_unrevealed_desecrated_sides(raw_text)
                side_lbl = "Prefix" if dp and not ds else ("Suffix" if ds and not dp else "Affix")
                next_step = (
                    f"Unveil Desecrated {side_lbl} at the Well of Souls (3-choice selection; "
                    "activate Omen of Abyssal Echoes before unveiling if existing mods are T1/T2), "
                    "then re-price."
                )
                rationale = (
                    f"Unrevealed Desecrated {side_lbl} hides a 3-choice Abyssal unveil on top of "
                    f"{len(item.mods)} existing modifiers (~{est_total_ex:.1f} Ex floor)."
                )
            elif not item.identified and (
                (item.unidentified_tier or 0) >= 3
                or (
                    item.rarity in (ItemRarity.RARE, ItemRarity.UNIQUE)
                    and (item.item_level or 0) >= 81
                )
            ):
                bucket = "high_variance_id_or_unveil"
                t_info = (
                    f"Tier {item.unidentified_tier} culls low-rank affix tiers on ID"
                    if item.unidentified_tier
                    else "High-ilvl unidentified base"
                )
                next_step = (
                    f"List un-ID'd via Instant Buyout (~{est_total_ex:.1f} Ex) for guaranteed bulk "
                    f"return, or Scroll of Wisdom ID to gamble T1 rolls on {item.base_type}."
                )
                rationale = (
                    f"{t_info} (ilvl {item.item_level or '?'}); "
                    "high variance between ID vs bulk sale."
                )
            elif (
                is_craftable_gear
                and not item.corrupted
                and not item.mirrored
                and not item.sanctified
                and (open_p + open_s) >= 1
                and (
                    item.fractured
                    or strong_mods >= 3
                    or (strong_mods >= 2 and est_total_ex >= max(25.0, min_sell_exalted * 3))
                )
            ):
                bucket = "craft_finish_ev"
                open_desc = f"{open_p}P/{open_s}S open"
                if not has_desecrated and bone_family:
                    target_omen = (
                        "Omen of Sinistral Necromancy"
                        if open_p > 0 and open_s == 0
                        else (
                            "Omen of Dextral Necromancy"
                            if open_s > 0 and open_p == 0
                            else "directional Necromancy / Boss Omen"
                        )
                    )
                    next_step = (
                        f"Finish ({open_desc}) before selling: Desecrate with Preserved/Ancient "
                        f"{bone_family} (+ {target_omen}) for a 3-choice Well of Souls unveil, or "
                        "slam with Greater Exalted Orb."
                    )
                else:
                    next_step = (
                        f"Finish ({open_desc}) before selling: slam open slot with Greater/Perfect "
                        "Exalted Orb (+ Sinistral/Dextral Exaltation Omen if isolating side)."
                    )
                rationale = (
                    f"Uncorrupted Rare {item.base_type} with {strong_mods} strong/fractured "
                    f"affix(es) and {open_desc} (~{est_total_ex:.1f} Ex as-is; finishing open "
                    "slot captures full finished-item premium)."
                )
            elif trade_cat == "map.waystone" and (
                est_total_ex < max(min_sell_exalted, 8.0) and (item.waystone_tier or 0) <= 15
            ):
                bucket = "salvage_or_vendor"
                rev_warn = (
                    " (0 revives — 1 death bricks map!)" if item.waystone_revives == 0 else ""
                )
                next_step = (
                    "Reforge 3-to-1 at the hideout Reforging Bench into a Tier 16 Waystone "
                    f"(or run if build-safe{rev_warn}); skip individual trade listing."
                )
                rationale = (
                    f"T{item.waystone_tier or '?'} Waystone (~{est_total_ex:.1f} Ex) is not worth "
                    "individual stash-tab management / buyer Gold fee unless T16 or high-Delirious."
                )
            elif est_total_ex >= min_sell_exalted:
                bucket = "sell_instant"
                if item.rarity == ItemRarity.CURRENCY:
                    is_cheap_cat = "Catalyst" in item.base_type and unit_ex < 12.0
                    cat_tip = (
                        " (or 3-to-1 at Reforging Bench if cheap Flesh/Carapace/Neural Catalyst)"
                        if is_cheap_cat
                        else ""
                    )
                    next_step = (
                        f"Sell {stack_qty}x {item.base_type} on Alva's Currency Exchange "
                        f"(~{unit_ex:.1f} Ex/ea = {est_total_ex:.1f} Ex total){cat_tip}."
                    )
                    rationale = (
                        f"Liquid Alva exchange stackable worth {est_total_ex:.1f} Ex "
                        f"({stack_qty}x @ {unit_ex:.1f} Ex)."
                    )
                else:
                    price_ref = sec_min_ex or unit_ex
                    next_step = (
                        f"List in Instant Buyout (securable) tab at ~{price_ref:.1f} Ex "
                        f"({est_total_div:.2f} Div)."
                    )
                    rationale = (
                        f"Ready-to-sell item (~{est_total_ex:.1f} Ex >= {min_sell_exalted:.1f} Ex "
                        "threshold) with no further deterministic crafting upside."
                    )
            else:
                bucket = "salvage_or_vendor"
                if item.rarity == ItemRarity.UNIQUE:
                    next_step = (
                        "Disenchant at vendor for Chance Shards (salvage sockets/quality first if "
                        "needed)."
                    )
                elif item.rune_sockets > 0 or item.quality > 0:
                    next_step = (
                        "Salvage at the Salvage Bench for Artificer's Shards / quality currency."
                    )
                else:
                    next_step = "Vendor for Gold / Regal Shards; below Instant Buyout threshold."
                rationale = (
                    f"Estimated value ({est_total_ex:.1f} Ex) is below {min_sell_exalted:.1f} Ex "
                    "listing threshold."
                )

            counts[bucket] = counts.get(bucket, 0) + 1
            entries.append(
                DumpTriageEntry(
                    index=idx,
                    item_label=label,
                    item_class=item.item_class,
                    rarity=item.rarity,
                    base_type=item.base_type,
                    bucket=bucket,
                    stack_size=stack_qty,
                    unit_exalted=round(unit_ex, 4),
                    estimated_total_exalted=est_total_ex,
                    estimated_total_divine=est_total_div,
                    securable_min_exalted=sec_min_ex,
                    price_source=price_source,
                    open_prefixes=open_p,
                    open_suffixes=open_s,
                    recommended_next_step=next_step,
                    rationale=rationale,
                    trade_url=trade_url,
                    fixer_warnings=fixer_warnings,
                )
            )

        total_div = round(total_ex_sum / div_rate, 4) if div_rate > 0 else None
        notes = [
            f"Triaged {len(entries)} items worth ~{total_ex_sum:.1f} Ex "
            f"({total_div or 0.0:.2f} Div): "
            f"{counts['sell_instant']} sell_instant, {counts['craft_finish_ev']} craft_finish_ev, "
            f"{counts['high_variance_id_or_unveil']} high_variance_id_or_unveil, "
            f"{counts['salvage_or_vendor']} salvage_or_vendor.",
            "Route stackables (Soul Cores, Catalysts, Runes, Omens) through Alva's Currency "
            "Exchange and equipment/gems/tablets through Gold-fee Instant Buyout tabs.",
        ]
        return DumpTriageReport(
            league=league,
            total_items=len(entries),
            total_estimated_exalted=round(total_ex_sum, 2),
            total_estimated_divine=total_div,
            counts_by_bucket=counts,
            entries=entries,
            summary_notes=notes,
        )
