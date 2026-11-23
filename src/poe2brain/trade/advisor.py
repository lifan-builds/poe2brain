from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from poe2brain.config import (
    DEFAULT_LEAGUE,
    GGG_TRADE2_BASE_URL,
    GGG_TRADE2_WEB_BASE_URL,
)
from poe2brain.http import GGGRateLimiter, Poe2HttpClient
from poe2brain.models.builds import SlotStatWeights
from poe2brain.models.common import SaleType
from poe2brain.models.items import ItemRarity, ModDomain, ParsedItem
from poe2brain.models.trade import (
    CurrencyQuote,
    PriceCheckResult,
    PriceDistribution,
    TradeListing,
)
from poe2brain.trade.ee2_bundle import EE2Bundle, get_default_bundle
from poe2brain.trade.item_parser import (
    parse_clipboard_item,
    resolve_slot_or_category,
    resolve_trade_category,
    strip_wiki_markup,
    trade2_fetch_to_pob2_text,
)
from poe2brain.trade.stat_matcher import (
    build_item_trade_query,
    build_natural_trade_query,
)


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
        self._search_limiter = GGGRateLimiter()
        self._fetch_limiter = GGGRateLimiter()

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
