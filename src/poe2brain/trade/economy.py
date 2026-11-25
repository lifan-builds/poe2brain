# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import difflib
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from poe2brain.config import (
    DEFAULT_LEAGUE,
    POE2SCOUT_API_BASE_URL,
    POENINJA_POE2_BASE_URL,
)
from poe2brain.http import Poe2HttpClient, get_cached_json
from poe2brain.models.common import Poe2Model
from poe2brain.models.trade import CurrencyQuote

DEFAULT_ECONOMY_TTL_SECONDS: float = 900.0

POENINJA_EXCHANGE_TYPES: tuple[str, ...] = (
    "Currency",
    "Fragments",
    "Runes",
    "Essences",
    "Ritual",
    "SoulCores",
    "Delirium",
    "Breach",
    "Expedition",
    "Verisium",
    "Abyss",
    "UncutGems",
    "LineageSupportGems",
)

POE2SCOUT_UNIQUE_CATEGORIES: tuple[str, ...] = (
    "accessory",
    "armour",
    "flask",
    "jewel",
    "map",
    "weapon",
    "sanctum",
)

CRAFTING_CATEGORIES: frozenset[str] = frozenset(
    {
        "currency",
        "essences",
        "ritual",
        "abyss",
        "verisium",
        "breach",
        "ultimatum",
        "soulcores",
        "runes",
        "vaal",
        "delirium",
    }
)

# Canonical crafting items including removed/legacy levers that must never be priced at 0.
REQUIRED_CRAFTING_SLUGS: tuple[str, ...] = (
    "exalted",
    "greater-exalted-orb",
    "perfect-exalted-orb",
    "chaos",
    "greater-chaos-orb",
    "perfect-chaos-orb",
    "regal",
    "greater-regal-orb",
    "perfect-regal-orb",
    "transmute",
    "greater-orb-of-transmutation",
    "perfect-orb-of-transmutation",
    "aug",
    "greater-orb-of-augmentation",
    "perfect-orb-of-augmentation",
    "alch",
    "annul",
    "chance",
    "divine",
    "vaal",
    "fracturing-orb",
    "artificers",
    "mirror",
    "hinekoras-lock",
    "omen-of-whittling",
    "omen-of-light",
    "omen-of-abyssal-echoes",
    "omen-of-sinistral-exaltation",
    "omen-of-dextral-exaltation",
    "omen-of-greater-exaltation",
    "omen-of-sinistral-erasure",
    "omen-of-dextral-erasure",
    "omen-of-sinistral-annulment",
    "omen-of-dextral-annulment",
    "omen-of-greater-annulment",
    "omen-of-sinistral-crystallisation",
    "omen-of-dextral-crystallisation",
    "omen-of-sinistral-coronation",
    "omen-of-dextral-coronation",
    "omen-of-sanctification",
    "omen-of-corruption",
    "omen-of-chance",
    "gnawed-jawbone",
    "preserved-jawbone",
    "ancient-jawbone",
    "gnawed-rib",
    "preserved-rib",
    "ancient-rib",
    "gnawed-collarbone",
    "preserved-collarbone",
    "ancient-collarbone",
    # Removed in PoE2 0.4 / 0.5 — must be present as None if unpriced on league market
    "omen-of-homogenising-exaltation",
    "omen-of-homogenising-coronation",
    "recombinator",
)

DIRECT_SLUG_ALIASES: dict[str, str] = {
    "ex": "exalted",
    "exalt": "exalted",
    "exalted-orb": "exalted",
    "exalted_basic": "exalted",
    "exalt_basic": "exalted",
    "exalted_greater": "greater-exalted-orb",
    "exalt_greater": "greater-exalted-orb",
    "exalted_perfect": "perfect-exalted-orb",
    "exalt_perfect": "perfect-exalted-orb",
    "div": "divine",
    "divine-orb": "divine",
    "c": "chaos",
    "chaos-orb": "chaos",
    "chaos_basic": "chaos",
    "chaos_greater": "greater-chaos-orb",
    "chaos_perfect": "perfect-chaos-orb",
    "regal-orb": "regal",
    "regal_basic": "regal",
    "regal_greater": "greater-regal-orb",
    "regal_perfect": "perfect-regal-orb",
    "alch": "alch",
    "alchemy": "alch",
    "orb-of-alchemy": "alch",
    "aug": "aug",
    "augment": "aug",
    "augmentation": "aug",
    "orb-of-augmentation": "aug",
    "aug_basic": "aug",
    "aug_greater": "greater-orb-of-augmentation",
    "aug_perfect": "perfect-orb-of-augmentation",
    "transmute": "transmute",
    "transmutation": "transmute",
    "orb-of-transmutation": "transmute",
    "transmute_basic": "transmute",
    "transmute_greater": "greater-orb-of-transmutation",
    "transmute_perfect": "perfect-orb-of-transmutation",
    "annul": "annul",
    "annulment": "annul",
    "orb-of-annulment": "annul",
    "chance": "chance",
    "orb-of-chance": "chance",
    "vaal-orb": "vaal",
    "artificer": "artificers",
    "artificers-orb": "artificers",
    "fracture": "fracturing-orb",
    "fracturing": "fracturing-orb",
    "mirror-of-kalandra": "mirror",
    "gcp": "gcp",
    "gemcutters-prism": "gcp",
    "bauble": "bauble",
    "glassblowers-bauble": "bauble",
    "scrap": "scrap",
    "armourers-scrap": "scrap",
    "whetstone": "whetstone",
    "blacksmiths-whetstone": "whetstone",
    "etcher": "etcher",
    "arcanists-etcher": "etcher",
    "scroll-of-wisdom": "wisdom",
}

_NINJA_TYPE_TO_CATEGORY: dict[str, str] = {
    "Currency": "currency",
    "Fragments": "fragments",
    "Runes": "runes",
    "Essences": "essences",
    "Ritual": "ritual",
    "SoulCores": "ultimatum",
    "Delirium": "delirium",
    "Breach": "breach",
    "Expedition": "expedition",
    "Verisium": "verisium",
    "Abyss": "abyss",
    "UncutGems": "uncutgems",
    "LineageSupportGems": "lineagesupportgems",
}

_TIERED_ORB_SLUG_CHAINS: tuple[tuple[str, str, str], ...] = (
    ("transmute", "greater-orb-of-transmutation", "perfect-orb-of-transmutation"),
    ("aug", "greater-orb-of-augmentation", "perfect-orb-of-augmentation"),
    ("regal", "greater-regal-orb", "perfect-regal-orb"),
    ("exalted", "greater-exalted-orb", "perfect-exalted-orb"),
    ("chaos", "greater-chaos-orb", "perfect-chaos-orb"),
)

_SHARD_PARENT_ORBS: dict[str, str] = {
    "transmutation-shard": "transmute",
    "regal-shard": "regal",
    "chance-shard": "chance",
    "artificers-shard": "artificers",
}


def _is_placeholder_item_name(name: str | None) -> bool:
    if not name:
        return True
    cleaned = name.strip()
    if not cleaned:
        return True
    upper = cleaned.upper()
    return (
        upper in ("INCOMPLETE", "UNKNOWN", "NULL")
        or upper.startswith("INCOMPLETE ")
        or cleaned.startswith("[DNT]")
    )


_APOSTROPHE_RE = re.compile(r"['’`]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_market_slug(raw: str) -> str:
    cleaned = _APOSTROPHE_RE.sub("", raw.strip().lower())
    return _NON_ALNUM_RE.sub("-", cleaned).strip("-")


def _candidate_alias_slugs(query: str) -> list[str]:
    raw_lower = query.strip().lower()
    slug = normalize_market_slug(query)
    candidates: list[str] = []

    for token in (raw_lower, slug, raw_lower.replace("-", "_")):
        mapped = DIRECT_SLUG_ALIASES.get(token)
        if mapped and mapped not in candidates:
            candidates.append(mapped)

    if slug and slug not in candidates:
        candidates.append(slug)

    parts = [p for p in re.split(r"[-_\s]+", _APOSTROPHE_RE.sub("", raw_lower)) if p]
    if len(parts) >= 2:
        head = parts[0]
        if head == "omen" and parts[1] != "of":
            rest = "-".join(parts[1:])
            candidates.append(f"omen-of-{rest}")
        elif head == "essence":
            if parts[1] in ("lesser", "greater", "perfect") and len(parts) >= 3:
                tier = parts[1]
                rest = "-".join(parts[2:])
                candidates.append(f"{tier}-essence-of-{rest}")
                candidates.append(f"{tier}-essence-of-the-{rest}")
            else:
                rest = "-".join(parts[1:])
                candidates.append(f"essence-of-{rest}")
                candidates.append(f"essence-of-the-{rest}")
        elif head == "bone" and len(parts) == 3:
            part_a, part_b = parts[1], parts[2]
            if part_b in ("gnawed", "preserved", "ancient", "altered"):
                candidates.append(f"{part_b}-{part_a}")
            elif part_a in ("gnawed", "preserved", "ancient", "altered"):
                candidates.append(f"{part_a}-{part_b}")
        elif head == "alloy":
            rest = "-".join(parts[1:])
            candidates.append(f"{rest}-alloy")
        elif head in ("jeweller", "jewellers") and parts[1] in ("lesser", "greater", "perfect"):
            candidates.append(f"{parts[1]}-jewellers-orb")

    if slug.endswith("-orb") and slug[:-4] not in candidates:
        candidates.append(slug[:-4])
    if slug.startswith("orb-of-") and slug[7:] not in candidates:
        candidates.append(slug[7:])

    deduped: list[str] = []
    for c in candidates:
        if c and c not in deduped:
            deduped.append(c)
    return deduped


class LeagueEconomySummary(Poe2Model):
    id: str
    short_name: str | None = None
    is_current: bool = False
    base_currency: str = "exalted"
    divine_exalted: float | None = None
    chaos_exalted: float | None = None
    chaos_per_divine: float | None = None


class ReferenceRates(Poe2Model):
    league: str = DEFAULT_LEAGUE
    base_currency: str = "exalted"
    exalted: float = 1.0
    chaos_exalted: float
    divine_exalted: float
    chaos_per_divine: float
    ninja_divine_exalted: float | None = None
    ninja_chaos_per_divine: float | None = None
    rates_in_exalted: dict[str, float] = {}

    @property
    def divine_in_exalted(self) -> float:
        return self.divine_exalted

    @property
    def chaos_in_exalted(self) -> float:
        return self.chaos_exalted


class UniqueMarketQuote(CurrencyQuote):
    base_type: str | None = None
    is_chanceable: bool = False
    quantity: int | None = None


class PriceHistoryPoint(Poe2Model):
    timestamp: str | int
    price_exalted: float
    quantity: float | None = None


class MarketOpportunity(Poe2Model):
    id: str
    name: str
    category: str
    kind: str
    exalted: float
    divine: float | None = None
    volume_divine: float | None = None
    max_volume_currency: str | None = None
    change_7d_pct: float | None = None
    spread_pct: float | None = None
    scout_exalted: float | None = None
    ninja_exalted: float | None = None
    is_chanceable: bool | None = None
    base_type: str | None = None
    rationale: str


@dataclass(slots=True)
class _MergedMarketEntry:
    id: str
    name: str
    category: str
    league: str
    item_id: int | None
    base_type: str | None
    scout_exalted: float | None
    ninja_exalted: float | None
    volume_divine: float | None
    max_volume_currency: str | None
    change_7d_pct: float | None
    quote: CurrencyQuote | None


@dataclass(slots=True)
class _LeagueSnapshot:
    fetched_at: float
    rates: ReferenceRates
    entries_by_id: dict[str, _MergedMarketEntry]
    alias_to_id: dict[str, str]


class EconomyService:
    """PoE2 Tier-1 Economy & Exchange service (`poe2scout` + `poe.ninja`).

    Exalted Orb (`exalted = 1.0`) is the unit currency in Path of Exile 2.
    Never calls `poe.ninja`'s deprecated/404 `/stash/` endpoints.
    """

    def __init__(
        self,
        *,
        http_client: Poe2HttpClient | None = None,
        client: Poe2HttpClient | None = None,
        cache_dir: Path | None = None,
        ttl_seconds: float = DEFAULT_ECONOMY_TTL_SECONDS,
        poe2scout_base_url: str = POE2SCOUT_API_BASE_URL,
        poeninja_base_url: str = POENINJA_POE2_BASE_URL,
        ninja_exchange_types: Sequence[str] = POENINJA_EXCHANGE_TYPES,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        injected = http_client or client
        self._owns_client = injected is None
        self.http_client = injected or Poe2HttpClient()
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.poe2scout_base_url = poe2scout_base_url.rstrip("/")
        self.poeninja_base_url = poeninja_base_url.rstrip("/")
        self.ninja_exchange_types = tuple(ninja_exchange_types)
        self._now_fn = now_fn
        self._snapshots: dict[str, _LeagueSnapshot] = {}

    def close(self) -> None:
        if self._owns_client:
            self.http_client.close()

    def __enter__(self) -> EconomyService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _scout_url(self, path: str) -> str:
        rel = path if path.startswith("/") else f"/{path}"
        return f"{self.poe2scout_base_url}{rel}"

    def _ninja_url(self, path: str) -> str:
        rel = path if path.startswith("/") else f"/{path}"
        if "/stash/" in rel.lower() or rel.lower().endswith("/stash"):
            raise ValueError(
                "poe.ninja /stash/ endpoints are disabled (HTTP 404) on PoE2 leagues; "
                "use /economy/exchange/current/overview or poe2scout Uniques/ByCategory."
            )
        return f"{self.poeninja_base_url}{rel}"

    def _cached_get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> Any:
        return get_cached_json(
            url,
            params=params,
            ttl_seconds=self.ttl_seconds,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )

    def list_leagues(self, *, force_refresh: bool = False) -> list[LeagueEconomySummary]:
        raw = self._cached_get(self._scout_url("/poe2/Leagues"), force_refresh=force_refresh)
        out: list[LeagueEconomySummary] = []
        for row in raw or []:
            div_ex = _positive_float(row.get("DivinePrice"))
            chaos_per_div = _positive_float(row.get("ChaosDivinePrice"))
            chaos_ex = (div_ex / chaos_per_div) if (div_ex and chaos_per_div) else None
            out.append(
                LeagueEconomySummary(
                    id=str(row.get("Value", "")),
                    short_name=row.get("ShortName"),
                    is_current=bool(row.get("IsCurrent", False)),
                    base_currency=str(row.get("BaseCurrencyApiId") or "exalted"),
                    divine_exalted=div_ex,
                    chaos_exalted=chaos_ex,
                    chaos_per_divine=chaos_per_div,
                )
            )
        return out

    def get_exchange_overview(
        self,
        exchange_type: str = "Currency",
        league: str = DEFAULT_LEAGUE,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if exchange_type not in POENINJA_EXCHANGE_TYPES:
            raise ValueError(
                f"Unsupported poe.ninja PoE2 exchange type {exchange_type!r}; "
                f"expected one of {POENINJA_EXCHANGE_TYPES}"
            )
        url = self._ninja_url("/economy/exchange/current/overview")
        data = self._cached_get(
            url,
            params={"league": league, "type": exchange_type},
            force_refresh=force_refresh,
        )
        return dict(data) if isinstance(data, Mapping) else {}

    def get_reference_rates(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        force_refresh: bool = False,
    ) -> ReferenceRates:
        snap = self._ensure_snapshot(league, force_refresh=force_refresh)
        return snap.rates

    def convert_to_exalted(
        self,
        amount: float,
        currency: str,
        league: str = DEFAULT_LEAGUE,
    ) -> float | None:
        if amount < 0:
            return None
        if amount == 0:
            return 0.0
        norm = currency.strip().lower()
        if norm in ("exalted", "ex", "exalt", "exalted-orb"):
            return float(amount)

        rates = self.get_reference_rates(league)
        for cand in _candidate_alias_slugs(currency):
            unit = rates.rates_in_exalted.get(cand)
            if unit is not None and unit > 0:
                return float(amount) * unit

        quote_obj = self.get_price(currency, league=league)
        if quote_obj is not None and quote_obj.exalted > 0:
            return float(amount) * quote_obj.exalted
        return None

    def get_price(
        self,
        query_or_id: str,
        league: str = DEFAULT_LEAGUE,
        *,
        category: str | None = None,
        force_refresh: bool = False,
    ) -> CurrencyQuote | None:
        query_clean = query_or_id.strip()
        if not query_clean:
            return None

        snap = self._ensure_snapshot(league, force_refresh=force_refresh)
        cat_filter = _normalize_category_filter(category)

        # 1. Exact ID or alias lookup
        matched_unpriced_unique = False
        for cand in _candidate_alias_slugs(query_clean):
            canonical_id = snap.alias_to_id.get(cand) or (
                cand if cand in snap.entries_by_id else None
            )
            if canonical_id:
                entry = snap.entries_by_id.get(canonical_id)
                if entry is not None and _matches_category(entry.category, cat_filter):
                    if entry.quote is not None:
                        return entry.quote
                    if entry.category in POE2SCOUT_UNIQUE_CATEGORIES:
                        matched_unpriced_unique = True
                        break
                    return None

        # 2. Normalized token/prefix lookup (reject low-score edit-distance substitutions)
        min_score = 0.99 if matched_unpriced_unique else 0.78
        matches = self.search_prices(
            query_clean,
            league=league,
            category=category,
            limit=1,
            force_refresh=False,
            min_score=min_score,
        )
        return matches[0] if matches else None

    def search_prices(
        self,
        query: str,
        league: str = DEFAULT_LEAGUE,
        *,
        category: str | None = None,
        limit: int = 10,
        force_refresh: bool = False,
        min_score: float = 0.0,
    ) -> list[CurrencyQuote]:
        query_slug = normalize_market_slug(query)
        if not query_slug:
            return []

        snap = self._ensure_snapshot(league, force_refresh=force_refresh)
        cat_filter = _normalize_category_filter(category)
        query_tokens = [t for t in query_slug.split("-") if t]
        wants_reliquary = any(tok in query_slug for tok in ("reliquary", "zarokh", "vaultkey"))
        exact_non_vault_entry = snap.entries_by_id.get(query_slug)
        if exact_non_vault_entry is not None and exact_non_vault_entry.category == "vaultkeys":
            exact_non_vault_entry = None

        scored: list[tuple[float, float, CurrencyQuote]] = []
        for entry in snap.entries_by_id.values():
            if entry.quote is None or not _matches_category(entry.category, cat_filter):
                continue
            if (
                not wants_reliquary
                and exact_non_vault_entry is not None
                and (entry.category == "vaultkeys" or "reliquary key:" in entry.name.lower())
            ):
                continue

            name_slug = normalize_market_slug(entry.name)
            full_slug = (
                normalize_market_slug(f"{entry.name} {entry.base_type}")
                if entry.base_type
                else name_slug
            )
            id_slug = normalize_market_slug(entry.id)

            score = 0.0
            if query_slug in (id_slug, name_slug, full_slug):
                score = 1.0
            elif name_slug.startswith(query_slug) or id_slug.startswith(query_slug):
                score = 0.93 - min(0.1, (len(name_slug) - len(query_slug)) * 0.003)
            else:
                hay_tokens = set(full_slug.split("-")) | set(id_slug.split("-"))
                if query_tokens and all(qt in hay_tokens for qt in query_tokens):
                    score = 0.88 - min(0.12, max(0, len(hay_tokens) - len(query_tokens)) * 0.02)
                elif query_slug in full_slug or query_slug in id_slug:
                    score = 0.80
                else:
                    ratio = max(
                        difflib.SequenceMatcher(None, query_slug, name_slug).ratio(),
                        difflib.SequenceMatcher(None, query_slug, id_slug).ratio(),
                    )
                    if ratio >= 0.74:
                        score = ratio * 0.78

            if score > 0:
                liquidity_tiebreak = entry.volume_divine or 0.0
                scored.append((score, liquidity_tiebreak, entry.quote))

        scored.sort(key=lambda row: (row[0], row[1], row[2].exalted), reverse=True)
        has_exact = any(row[0] >= 0.99 for row in scored)
        if not has_exact and not wants_reliquary:
            probe_cats: list[str] = []
            if cat_filter in POE2SCOUT_UNIQUE_CATEGORIES:
                probe_cats = [cat_filter]
            elif (
                exact_non_vault_entry is not None
                and exact_non_vault_entry.category in POE2SCOUT_UNIQUE_CATEGORIES
            ):
                probe_cats = [exact_non_vault_entry.category]
            elif cat_filter in (None, "unique", "uniques") and (
                not scored or all("reliquary-key" in r[2].id for r in scored)
            ):
                probe_cats = ["jewel"]
            for ucat in probe_cats:
                try:
                    uqs = self.get_uniques_by_category(
                        ucat, league=league, page=1, per_page=250, force_refresh=False
                    )
                except (httpx.HTTPError, OSError, AssertionError):
                    continue
                for uq in uqs:
                    uq_slug = normalize_market_slug(uq.name)
                    if query_slug in (
                        uq_slug,
                        normalize_market_slug(f"{uq.name} {uq.base_type or ''}"),
                    ):
                        scored.append((1.0, float(uq.quantity or 0), uq))
                    elif query_slug in uq_slug:
                        scored.append((0.85, float(uq.quantity or 0), uq))
                scored.sort(key=lambda row: (row[0], row[1], row[2].exalted), reverse=True)

        return [row[2] for row in scored if row[0] >= min_score][: max(1, limit)]

    def get_bulk_crafting_prices(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        force_refresh: bool = False,
    ) -> dict[str, CurrencyQuote | None]:
        """Return `{currency_slug: CurrencyQuote | None}` for the crafting engine.

        Invariants:
        - `exalted` is always `1.0`.
        - Unpriced (`<= 0`) or unavailable/removed crafting orbs are NEVER returned as `0`;
          they are explicitly mapped to `None`.
        - Thin-volume (`< 0.08 Div`) dust/RMT outliers are floored/capped and Basic <= Greater
          <= Perfect orb monotonicity is enforced.
        """
        snap = self._ensure_snapshot(league, force_refresh=force_refresh)
        rates = snap.rates
        bulk: dict[str, CurrencyQuote | None] = {}

        def _reprice(q: CurrencyQuote, new_ex: float) -> CurrencyQuote:
            return _build_quote(
                item_id=q.id,
                name=q.name,
                category=q.category,
                league=q.league,
                exalted_val=round(new_ex, 6),
                rates=rates,
                volume_primary=q.volume_primary,
                change_7d_pct=q.change_7d_pct,
                source=q.source,
            )

        for entry in snap.entries_by_id.values():
            if entry.category not in CRAFTING_CATEGORIES:
                continue
            val = entry.quote if (entry.quote is not None and entry.quote.exalted > 0) else None
            if val is not None:
                vol = entry.volume_divine
                is_thin = vol is None or vol < 0.08
                if is_thin:
                    if (
                        entry.id
                        in (
                            "omen-of-the-liege",
                            "omen-of-the-blackblooded",
                            "omen-of-the-sovereign",
                        )
                        and val.exalted < 25.0
                    ):
                        val = _reprice(val, 25.0)
                    elif entry.id.endswith("-alloy") and val.exalted < 25.0:
                        val = _reprice(val, 25.0)
                    elif entry.id.startswith("perfect-essence-of-") and val.exalted < 15.0:
                        val = _reprice(val, 15.0)

                # Cap illiquid lesser/normal essences & runes that exceed their greater counterpart
                if vol is None or vol < 0.25:
                    g_slug: str | None = None
                    cap_mult = 0.8
                    if entry.id.startswith("lesser-essence-of-"):
                        g_slug = "greater-" + entry.id.removeprefix("lesser-")
                        cap_mult = 0.5
                    elif entry.id.startswith("essence-of-"):
                        rest = entry.id.removeprefix("essence-of-")
                        if rest not in (
                            "delirium",
                            "horror",
                            "hysteria",
                            "insanity",
                            "the-breach",
                            "the-abyss",
                        ):
                            g_slug = f"greater-essence-of-{rest}"
                    elif entry.id.startswith("lesser-") and entry.id.endswith("-rune"):
                        g_slug = "greater-" + entry.id.removeprefix("lesser-")
                        cap_mult = 0.5
                    if g_slug:
                        g_entry = snap.entries_by_id.get(g_slug)
                        if (
                            g_entry
                            and g_entry.quote
                            and g_entry.quote.exalted > 0
                            and val.exalted > g_entry.quote.exalted
                        ):
                            val = _reprice(val, max(0.5, g_entry.quote.exalted * cap_mult))

            bulk[entry.id] = val
            name_slug = normalize_market_slug(entry.name)
            if name_slug and name_slug not in bulk:
                bulk[name_slug] = val

        # Bridge canonical slugs <-> full-name aliases before tier monotonicity checks
        for alias_key, target_slug in DIRECT_SLUG_ALIASES.items():
            if len(alias_key) <= 2:
                continue
            if bulk.get(target_slug) is None and bulk.get(alias_key) is not None:
                bulk[target_slug] = bulk[alias_key]
            elif bulk.get(alias_key) is None and bulk.get(target_slug) is not None:
                bulk[alias_key] = bulk[target_slug]

        # Enforce Basic <= Greater <= Perfect orb monotonicity on thin-volume inversions
        for low_s, mid_s, high_s in _TIERED_ORB_SLUG_CHAINS:
            q_low = bulk.get(low_s)
            q_mid = bulk.get(mid_s)
            q_high = bulk.get(high_s)
            if q_low is not None and q_mid is not None and q_mid.exalted < q_low.exalted:
                mid_vol = q_mid.volume_primary
                if mid_vol is None or mid_vol < 0.5:
                    mid_entry = snap.entries_by_id.get(mid_s)
                    scout_mid = mid_entry.scout_exalted if mid_entry else None
                    upper_bound = q_high.exalted if q_high else q_low.exalted * 25.0
                    fixed_mid = (
                        scout_mid
                        if (scout_mid is not None and q_low.exalted <= scout_mid <= upper_bound)
                        else q_low.exalted * 1.25
                    )
                    q_mid = _reprice(q_mid, fixed_mid)
                    bulk[mid_s] = q_mid
                    bulk[normalize_market_slug(q_mid.name)] = q_mid
            if q_mid is not None and q_high is not None and q_high.exalted < q_mid.exalted:
                high_vol = q_high.volume_primary
                if high_vol is None or high_vol < 0.5:
                    q_high = _reprice(q_high, q_mid.exalted * 1.5)
                    bulk[high_s] = q_high
                    bulk[normalize_market_slug(q_high.name)] = q_high

        for required_slug in REQUIRED_CRAFTING_SLUGS:
            if required_slug not in bulk:
                entry = snap.entries_by_id.get(required_slug)
                bulk[required_slug] = (
                    entry.quote
                    if (entry is not None and entry.quote is not None and entry.quote.exalted > 0)
                    else None
                )

        # Register clean crafting lever / full-name aliases without clobbering priced entries
        for alias_key, target_slug in DIRECT_SLUG_ALIASES.items():
            if len(alias_key) <= 2:
                continue
            if bulk.get(target_slug) is not None:
                bulk[alias_key] = bulk[target_slug]
            elif alias_key not in bulk and target_slug in bulk:
                bulk[alias_key] = None

        return bulk

    def get_currencies_by_category(
        self,
        category: str,
        league: str = DEFAULT_LEAGUE,
        *,
        page: int = 1,
        per_page: int = 250,
        force_refresh: bool = False,
    ) -> list[CurrencyQuote]:
        rates = self.get_reference_rates(league, force_refresh=force_refresh)
        snap = self._ensure_snapshot(league, force_refresh=False)
        enc_league = quote(league, safe="")
        raw = self._cached_get(
            self._scout_url(f"/poe2/Leagues/{enc_league}/Currencies/ByCategory"),
            params={
                "category": category.strip().lower(),
                "page": page,
                "perPage": per_page,
                "referenceCurrency": "exalted",
            },
            force_refresh=force_refresh,
        )
        items = raw.get("Items", []) if isinstance(raw, Mapping) else []
        out: list[CurrencyQuote] = []
        for row in items:
            raw_text = str(row.get("Text") or "")
            if _is_placeholder_item_name(raw_text):
                continue
            api_id = str(row.get("ApiId") or normalize_market_slug(raw_text))
            if not api_id:
                continue
            merged_entry = snap.entries_by_id.get(api_id)
            if merged_entry is not None:
                if merged_entry.quote is not None:
                    out.append(merged_entry.quote)
                continue
            price_ex = 1.0 if api_id == "exalted" else _positive_float(row.get("CurrentPrice"))
            if price_ex is None:
                continue
            chg_7d = _compute_7d_change_from_logs(row.get("PriceLogs"))
            out.append(
                _build_quote(
                    item_id=api_id,
                    name=str(row.get("Text") or api_id),
                    category=str(row.get("CategoryApiId") or category).lower(),
                    league=league,
                    exalted_val=price_ex,
                    rates=rates,
                    volume_primary=None,
                    change_7d_pct=chg_7d,
                    source="poe2scout",
                )
            )
        return out

    def get_uniques_by_category(
        self,
        category: str,
        league: str = DEFAULT_LEAGUE,
        *,
        page: int = 1,
        per_page: int = 250,
        force_refresh: bool = False,
    ) -> list[UniqueMarketQuote]:
        rates = self.get_reference_rates(league, force_refresh=force_refresh)
        enc_league = quote(league, safe="")
        raw = self._cached_get(
            self._scout_url(f"/poe2/Leagues/{enc_league}/Uniques/ByCategory"),
            params={
                "category": category.strip().lower(),
                "page": page,
                "perPage": per_page,
            },
            force_refresh=force_refresh,
        )
        items = raw.get("Items", []) if isinstance(raw, Mapping) else []
        out: list[UniqueMarketQuote] = []
        for row in items:
            price_ex = _positive_float(row.get("CurrentPrice"))
            if price_ex is None:
                continue
            name = str(row.get("Name") or row.get("Text") or "").strip()
            base_type = str(row.get("Type") or "").strip() or None
            if _is_placeholder_item_name(name):
                continue
            slug = normalize_market_slug(name)
            chg_7d = _compute_7d_change_from_logs(row.get("PriceLogs"))
            qty = row.get("CurrentQuantity")
            out.append(
                UniqueMarketQuote(
                    id=slug,
                    name=name,
                    category=str(row.get("CategoryApiId") or category).lower(),
                    league=league,
                    exalted=price_ex,
                    chaos=(price_ex / rates.chaos_exalted) if rates.chaos_exalted > 0 else None,
                    divine=(price_ex / rates.divine_exalted) if rates.divine_exalted > 0 else None,
                    volume_primary=None,
                    change_7d_pct=chg_7d,
                    source="poe2scout",
                    base_type=base_type,
                    is_chanceable=bool(row.get("IsChanceable", False)),
                    quantity=int(qty) if isinstance(qty, (int, float)) else None,
                )
            )
        return out

    def get_item_history(
        self,
        item_id_or_query: int | str,
        league: str = DEFAULT_LEAGUE,
        *,
        log_count: int = 168,
        force_refresh: bool = False,
    ) -> list[PriceHistoryPoint]:
        resolved_id = self._resolve_scout_item_id(item_id_or_query, league=league)
        enc_league = quote(league, safe="")
        raw = self._cached_get(
            self._scout_url(f"/poe2/Leagues/{enc_league}/Items/{resolved_id}/History"),
            params={"logCount": log_count},
            force_refresh=force_refresh,
        )
        rows = raw.get("PriceHistory", []) if isinstance(raw, Mapping) else (raw or [])
        points: list[PriceHistoryPoint] = []
        for entry in rows:
            price = _positive_float(entry.get("Price"))
            if price is None:
                continue
            qty = entry.get("Quantity")
            points.append(
                PriceHistoryPoint(
                    timestamp=str(entry.get("Time", "")),
                    price_exalted=price,
                    quantity=float(qty) if isinstance(qty, (int, float)) else None,
                )
            )
        return points

    def get_pair_history(
        self,
        currency_one: int | str,
        currency_two: int | str,
        league: str = DEFAULT_LEAGUE,
        *,
        limit: int = 168,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        id_one = self._resolve_scout_currency_item_id(currency_one, league=league)
        id_two = self._resolve_scout_currency_item_id(currency_two, league=league)
        enc_league = quote(league, safe="")
        raw = self._cached_get(
            self._scout_url(
                f"/poe2/Leagues/{enc_league}/Currencies/Pairs/{id_one}/{id_two}/History"
            ),
            params={"limit": limit},
            force_refresh=force_refresh,
        )
        history = raw.get("History", []) if isinstance(raw, Mapping) else []
        rows = [dict(item) for item in history if isinstance(item, Mapping)]
        if rows:
            return rows

        # When poe2scout returns empty pair buckets on a fresh league, derive ratio series
        # from the two items' 7-day `/Items/{itemId}/History` series.
        hist_one = self.get_item_history(
            currency_one, league=league, log_count=limit, force_refresh=force_refresh
        )
        if str(currency_two).strip().lower() in ("exalted", "ex", "exalted-orb"):
            return [
                {
                    "timestamp": pt.timestamp,
                    "relative_price": pt.price_exalted,
                    "quantity": pt.quantity,
                    "source": "poe2scout_item_history",
                }
                for pt in hist_one[:limit]
            ]

        hist_two = self.get_item_history(
            currency_two, league=league, log_count=limit, force_refresh=force_refresh
        )
        by_hour_two = {
            str(pt.timestamp)[:13]: pt.price_exalted for pt in hist_two if pt.price_exalted > 0
        }
        derived: list[dict[str, Any]] = []
        for pt in hist_one:
            denom = by_hour_two.get(str(pt.timestamp)[:13])
            if denom and denom > 0:
                derived.append(
                    {
                        "timestamp": pt.timestamp,
                        "relative_price": round(pt.price_exalted / denom, 6),
                        "quantity": pt.quantity,
                        "source": "poe2scout_item_history",
                    }
                )
        return derived[:limit]

    def scan_market_opportunities(
        self,
        league: str = DEFAULT_LEAGUE,
        category: str | None = None,
        min_volume_divine: float = 5.0,
        *,
        limit: int = 25,
        force_refresh: bool = False,
    ) -> list[MarketOpportunity]:
        snap = self._ensure_snapshot(league, force_refresh=force_refresh)
        rates = snap.rates
        cat_filter = _normalize_category_filter(category)
        opportunities: list[MarketOpportunity] = []

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

        for entry in snap.entries_by_id.values():
            if entry.quote is None or not _matches_category(entry.category, cat_filter):
                continue

            vol_div = entry.volume_divine or 0.0
            chg_7d = entry.change_7d_pct

            # 1. High-liquidity exchange movers (7d trend + real exchange volume)
            if vol_div >= min_volume_divine and chg_7d is not None and abs(chg_7d) >= 10.0:
                direction = "surging" if chg_7d > 0 else "cooling"
                opportunities.append(
                    MarketOpportunity(
                        id=entry.id,
                        name=entry.name,
                        category=entry.category,
                        kind="high_volume_mover",
                        exalted=entry.quote.exalted,
                        divine=entry.quote.divine,
                        volume_divine=vol_div,
                        max_volume_currency=entry.max_volume_currency,
                        change_7d_pct=chg_7d,
                        scout_exalted=entry.scout_exalted,
                        ninja_exalted=entry.ninja_exalted,
                        rationale=(
                            f"Liquid exchange {direction} ({chg_7d:+.1f}% 7d, "
                            f"{vol_div:.1f} Divine volume via "
                            f"{entry.max_volume_currency or 'exchange'})."
                        ),
                    )
                )

            # 2. Numeraire-normalized cross-source spread (eliminating Ex/Div VWAP lag)
            if (
                entry.id not in ("divine", "exalted")
                and vol_div >= min_volume_divine
                and entry.scout_exalted is not None
                and entry.ninja_exalted is not None
                and entry.scout_exalted > 0
                and entry.ninja_exalted > 0
                and scout_div_ex > 0
                and ninja_div_ex > 0
            ):
                scout_in_div = entry.scout_exalted / scout_div_ex
                ninja_in_div = entry.ninja_exalted / ninja_div_ex
                est_units = vol_div / max(0.001, ninja_in_div)
                low_div = min(scout_in_div, ninja_in_div)
                high_div = max(scout_in_div, ninja_in_div)
                spread_pct = ((high_div / low_div) - 1.0) * 100.0
                if 12.0 <= spread_pct <= 400.0 and est_units >= 5.0:
                    opportunities.append(
                        MarketOpportunity(
                            id=entry.id,
                            name=entry.name,
                            category=entry.category,
                            kind="cross_source_spread",
                            exalted=entry.quote.exalted,
                            divine=entry.quote.divine,
                            volume_divine=vol_div,
                            max_volume_currency=entry.max_volume_currency,
                            change_7d_pct=chg_7d,
                            spread_pct=round(spread_pct, 2),
                            scout_exalted=entry.scout_exalted,
                            ninja_exalted=entry.ninja_exalted,
                            rationale=(
                                f"{spread_pct:.1f}% Divine-normalized divergence "
                                f"(poe.ninja {ninja_in_div:.3f} Div vs "
                                f"poe2scout {scout_in_div:.3f} Div; {vol_div:.1f} Div vol)."
                            ),
                        )
                    )

        # 3. High-value chanceable & meta uniques (from poe2scout Uniques/ByCategory)
        unique_cats_to_scan: list[str] = []
        if cat_filter is None or cat_filter in ("unique", "uniques"):
            unique_cats_to_scan = ["accessory", "flask", "jewel"]
        elif cat_filter in POE2SCOUT_UNIQUE_CATEGORIES:
            unique_cats_to_scan = [cat_filter]

        for ucat in unique_cats_to_scan:
            try:
                uniques = self.get_uniques_by_category(
                    ucat, league=league, page=1, per_page=60, force_refresh=False
                )
            except httpx.HTTPError:
                continue

            for uq in uniques:
                qty = uq.quantity or 0
                div_val = uq.divine or (uq.exalted / rates.divine_exalted)
                if qty < 5 or div_val < 1.0:
                    continue
                if uq.is_chanceable and div_val >= 2.0:
                    opportunities.append(
                        MarketOpportunity(
                            id=uq.id,
                            name=uq.name,
                            category=uq.category,
                            kind="chanceable_unique",
                            exalted=uq.exalted,
                            divine=uq.divine,
                            change_7d_pct=uq.change_7d_pct,
                            scout_exalted=uq.exalted,
                            is_chanceable=True,
                            base_type=uq.base_type,
                            rationale=(
                                f"High-value chanceable unique on {uq.base_type or 'base'} "
                                f"({div_val:.1f} Divine, {qty} active listings)."
                            ),
                        )
                    )
                elif div_val >= 15.0:
                    opportunities.append(
                        MarketOpportunity(
                            id=uq.id,
                            name=uq.name,
                            category=uq.category,
                            kind="high_value_meta",
                            exalted=uq.exalted,
                            divine=uq.divine,
                            change_7d_pct=uq.change_7d_pct,
                            scout_exalted=uq.exalted,
                            is_chanceable=uq.is_chanceable,
                            base_type=uq.base_type,
                            rationale=(
                                f"Liquid meta chase unique ({div_val:.1f} Divine, "
                                f"{qty} active listings)."
                            ),
                        )
                    )

        opportunities.sort(
            key=lambda opp: (
                1 if opp.kind == "chanceable_unique" else 0,
                opp.volume_divine or (opp.divine or 0.0),
                abs(opp.change_7d_pct or opp.spread_pct or 0.0),
            ),
            reverse=True,
        )
        return opportunities[: max(1, limit)]

    def _resolve_scout_currency_item_id(self, item_id_or_query: int | str, *, league: str) -> int:
        if isinstance(item_id_or_query, int):
            return item_id_or_query
        text = str(item_id_or_query).strip()
        if text.isdigit():
            return int(text)

        snap = self._ensure_snapshot(league, force_refresh=False)
        api_id: str | None = None
        for cand in _candidate_alias_slugs(text):
            cid = snap.alias_to_id.get(cand) or (cand if cand in snap.entries_by_id else None)
            if cid:
                api_id = cid
                break

        if api_id:
            enc_league = quote(league, safe="")
            try:
                raw_curr = self._cached_get(
                    self._scout_url(
                        f"/poe2/Leagues/{enc_league}/Currencies/{quote(api_id, safe='')}"
                    )
                )
                if isinstance(raw_curr, Mapping) and isinstance(
                    raw_curr.get("CurrencyItemId"), int
                ):
                    return int(raw_curr["CurrencyItemId"])
            except (httpx.HTTPError, OSError):
                pass

        return self._resolve_scout_item_id(item_id_or_query, league=league)

    def _resolve_scout_item_id(self, item_id_or_query: int | str, *, league: str) -> int:
        if isinstance(item_id_or_query, int):
            return item_id_or_query
        text = str(item_id_or_query).strip()
        if text.isdigit():
            return int(text)

        snap = self._ensure_snapshot(league, force_refresh=False)
        for cand in _candidate_alias_slugs(text):
            cid = snap.alias_to_id.get(cand) or (cand if cand in snap.entries_by_id else None)
            if cid and snap.entries_by_id[cid].item_id is not None:
                return int(snap.entries_by_id[cid].item_id)  # type: ignore[arg-type]

        norm = normalize_market_slug(text)
        for entry in snap.entries_by_id.values():
            if entry.item_id is not None and normalize_market_slug(entry.name) == norm:
                return entry.item_id

        raise KeyError(f"Could not resolve poe2scout ItemId for {item_id_or_query!r} in {league!r}")

    def _ensure_snapshot(self, league: str, *, force_refresh: bool = False) -> _LeagueSnapshot:
        now = self._now_fn()
        cached = self._snapshots.get(league)
        if (
            not force_refresh
            and cached is not None
            and (now - cached.fetched_at) <= self.ttl_seconds
        ):
            return cached

        enc_league = quote(league, safe="")

        # Warm sync_client once before parallel poe.ninja fetches
        _ = self.http_client.sync_client

        scout_refs: list[dict[str, Any]] = []
        scout_items: list[dict[str, Any]] = []
        scout_error: Exception | None = None

        try:
            raw_refs = self._cached_get(
                self._scout_url(f"/poe2/Leagues/{enc_league}/ReferenceCurrencies"),
                force_refresh=force_refresh,
            )
            if isinstance(raw_refs, list):
                scout_refs = raw_refs
            raw_items = self._cached_get(
                self._scout_url(f"/poe2/Leagues/{enc_league}/Items"),
                force_refresh=force_refresh,
            )
            if isinstance(raw_items, list):
                scout_items = raw_items
        except (httpx.HTTPError, OSError) as exc:
            scout_error = exc

        ninja_payloads: dict[str, dict[str, Any]] = {}
        ninja_errors: list[Exception] = []

        def _load_ninja(extype: str) -> tuple[str, dict[str, Any] | None, Exception | None]:
            try:
                payload = self.get_exchange_overview(
                    extype, league=league, force_refresh=force_refresh
                )
                return extype, payload, None
            except (httpx.HTTPError, OSError) as exc:
                return extype, None, exc

        if len(self.ninja_exchange_types) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(self.ninja_exchange_types))) as pool:
                for extype, payload, err in pool.map(_load_ninja, self.ninja_exchange_types):
                    if payload:
                        ninja_payloads[extype] = payload
                    elif err is not None:
                        ninja_errors.append(err)
        elif self.ninja_exchange_types:
            extype, payload, err = _load_ninja(self.ninja_exchange_types[0])
            if payload:
                ninja_payloads[extype] = payload
            elif err is not None:
                ninja_errors.append(err)

        if not scout_refs and not scout_items and not ninja_payloads:
            if scout_error is not None:
                raise scout_error
            if ninja_errors:
                raise ninja_errors[0]

        snap = _build_league_snapshot(
            league=league,
            fetched_at=self._now_fn(),
            scout_refs=scout_refs,
            scout_items=scout_items,
            ninja_payloads=ninja_payloads,
        )
        self._snapshots[league] = snap
        return snap


def _positive_float(val: Any) -> float | None:
    if isinstance(val, (int, float)):
        fval = float(val)
        if fval > 0:
            return fval
    return None


def _normalize_category_filter(category: str | None) -> str | None:
    if category is None:
        return None
    norm = category.strip().lower()
    if not norm:
        return None
    if norm in ("omen", "omens"):
        return "ritual"
    if norm in ("essence",):
        return "essences"
    if norm in ("rune",):
        return "runes"
    if norm in ("bone", "bones", "desecrated"):
        return "abyss"
    if norm in ("soulcore", "soulcores", "soul-cores", "soul_cores"):
        return "ultimatum"
    if norm in ("uncut", "uncutgem", "uncut-gems"):
        return "uncutgems"
    if norm in ("lineage", "lineagesupport", "lineage-supports"):
        return "lineagesupportgems"
    return norm


def _matches_category(entry_cat: str, cat_filter: str | None) -> bool:
    if cat_filter is None:
        return True
    if cat_filter in ("unique", "uniques"):
        return entry_cat in POE2SCOUT_UNIQUE_CATEGORIES
    if cat_filter in ("ultimatum", "soulcores"):
        return entry_cat in ("ultimatum", "soulcores")
    return entry_cat == cat_filter


def _compute_7d_change_from_logs(logs: Any) -> float | None:
    if not isinstance(logs, Iterable):
        return None
    prices: list[tuple[str, float]] = []
    for item in logs:
        if not isinstance(item, Mapping):
            continue
        p = _positive_float(item.get("Price"))
        t = str(item.get("Time") or "")
        if p is not None:
            prices.append((t, p))
    if len(prices) < 2:
        return None
    prices.sort(key=lambda pair: pair[0])
    oldest = prices[0][1]
    newest = prices[-1][1]
    if oldest <= 0:
        return None
    return round(((newest - oldest) / oldest) * 100.0, 2)


def _build_quote(
    *,
    item_id: str,
    name: str,
    category: str,
    league: str,
    exalted_val: float,
    rates: ReferenceRates,
    volume_primary: float | None,
    change_7d_pct: float | None,
    source: str,
) -> CurrencyQuote:
    chaos_val = (exalted_val / rates.chaos_exalted) if rates.chaos_exalted > 0 else None
    divine_val = (exalted_val / rates.divine_exalted) if rates.divine_exalted > 0 else None
    return CurrencyQuote(
        id=item_id,
        name=name,
        category=category,
        league=league,
        exalted=exalted_val,
        chaos=chaos_val,
        divine=divine_val,
        volume_primary=volume_primary,
        change_7d_pct=change_7d_pct,
        source=source,
    )


def _extract_ninja_exalted_price(line: Mapping[str, Any], core: Mapping[str, Any]) -> float | None:
    pval = _positive_float(line.get("primaryValue"))
    if pval is None:
        return None
    primary_curr = str(core.get("primary") or "divine").lower()
    core_rates = core.get("rates") if isinstance(core.get("rates"), Mapping) else {}
    ex_per_primary = _positive_float(core_rates.get("exalted"))
    if primary_curr == "exalted":
        return pval
    if ex_per_primary is not None:
        return pval * ex_per_primary
    return None


def _build_league_snapshot(
    *,
    league: str,
    fetched_at: float,
    scout_refs: Sequence[Mapping[str, Any]],
    scout_items: Sequence[Mapping[str, Any]],
    ninja_payloads: Mapping[str, Mapping[str, Any]],
) -> _LeagueSnapshot:
    ref_map: dict[str, float] = {"exalted": 1.0}
    for r in scout_refs:
        api_id = str(r.get("ApiId") or "").strip().lower()
        rel_price = _positive_float(r.get("RelativePrice"))
        if api_id and rel_price is not None:
            ref_map[api_id] = rel_price
    ref_map["exalted"] = 1.0

    ninja_div_ex: float | None = None
    ninja_chaos_per_div: float | None = None
    for payload in ninja_payloads.values():
        core = payload.get("core")
        if isinstance(core, Mapping) and str(core.get("primary") or "").lower() == "divine":
            crates = core.get("rates")
            if isinstance(crates, Mapping):
                ninja_div_ex = _positive_float(crates.get("exalted"))
                ninja_chaos_per_div = _positive_float(crates.get("chaos"))
                if ninja_div_ex is not None:
                    break

    divine_ex = ref_map.get("divine") or ninja_div_ex or 450.0
    chaos_ex = ref_map.get("chaos")
    if chaos_ex is None:
        if ninja_div_ex and ninja_chaos_per_div:
            chaos_ex = ninja_div_ex / ninja_chaos_per_div
        else:
            chaos_ex = max(1.0, divine_ex / 12.5)
    chaos_per_div = divine_ex / chaos_ex if chaos_ex > 0 else 12.5

    rates = ReferenceRates(
        league=league,
        base_currency="exalted",
        exalted=1.0,
        chaos_exalted=chaos_ex,
        divine_exalted=divine_ex,
        chaos_per_divine=chaos_per_div,
        ninja_divine_exalted=ninja_div_ex,
        ninja_chaos_per_divine=ninja_chaos_per_div,
        rates_in_exalted=dict(ref_map),
    )

    entries_by_id: dict[str, _MergedMarketEntry] = {}
    alias_to_id: dict[str, str] = {}

    def _register_alias(alias: str | None, target_id: str) -> None:
        if not alias:
            return
        norm = normalize_market_slug(alias)
        if norm and norm not in alias_to_id:
            alias_to_id[norm] = target_id
        raw_low = alias.strip().lower()
        if raw_low and raw_low not in alias_to_id:
            alias_to_id[raw_low] = target_id

    # 1. Ingest poe2scout bulk /Items snapshot (currencies + 800+ uniques)
    for row in scout_items:
        api_id_raw = row.get("ApiId")
        category = str(row.get("CategoryApiId") or "currency").strip().lower()
        name_val = str(row.get("Name") or row.get("Text") or api_id_raw or "").strip()
        text_val = str(row.get("Text") or "").strip()
        if (
            not name_val
            or _is_placeholder_item_name(name_val)
            or _is_placeholder_item_name(text_val)
        ):
            continue
        base_type = str(row.get("Type") or "").strip() or None
        scout_item_id = int(row["ItemId"]) if isinstance(row.get("ItemId"), int) else None
        raw_price = _positive_float(row.get("CurrentPrice"))
        is_reliquary_key = category == "vaultkeys" or "reliquary key:" in name_val.lower()

        if is_reliquary_key:
            cid = (
                normalize_market_slug(name_val)
                or f"vaultkey-{str(api_id_raw or scout_item_id or 'key').strip().lower()}"
            )
        elif api_id_raw:
            cid = str(api_id_raw).strip().lower()
        else:
            base_slug = normalize_market_slug(name_val)
            if not base_slug:
                continue
            existing = entries_by_id.get(base_slug)
            if existing is None:
                cid = base_slug
            elif existing.base_type == base_type:
                # Duplicate unique variant (e.g., unpriced foil vs priced standard): keep priced one
                if existing.scout_exalted is None and raw_price is not None:
                    existing.scout_exalted = raw_price
                    if scout_item_id is not None:
                        existing.item_id = scout_item_id
                continue
            else:
                cid = normalize_market_slug(f"{name_val}-{base_type or scout_item_id or 'alt'}")

        if cid == "exalted":
            raw_price = 1.0

        entries_by_id[cid] = _MergedMarketEntry(
            id=cid,
            name=name_val,
            category=category,
            league=league,
            item_id=scout_item_id,
            base_type=base_type,
            scout_exalted=raw_price,
            ninja_exalted=None,
            volume_divine=None,
            max_volume_currency=None,
            change_7d_pct=None,
            quote=None,
        )
        _register_alias(cid, cid)
        _register_alias(name_val, cid)
        _register_alias(row.get("Text"), cid)
        if base_type:
            _register_alias(f"{name_val} {base_type}", cid)
        if scout_item_id is not None:
            alias_to_id[str(scout_item_id)] = cid

    # Ensure the 3 reference currencies exist even if /Items was empty in a minimal mock
    for ref_slug, ref_name, ref_val in (
        ("exalted", "Exalted Orb", 1.0),
        ("chaos", "Chaos Orb", chaos_ex),
        ("divine", "Divine Orb", divine_ex),
    ):
        if ref_slug not in entries_by_id:
            entries_by_id[ref_slug] = _MergedMarketEntry(
                id=ref_slug,
                name=ref_name,
                category="currency",
                league=league,
                item_id=None,
                base_type=None,
                scout_exalted=ref_val,
                ninja_exalted=None,
                volume_divine=None,
                max_volume_currency=None,
                change_7d_pct=None,
                quote=None,
            )
            _register_alias(ref_slug, ref_slug)
            _register_alias(ref_name, ref_slug)

    # 2. Enrich & merge with poe.ninja exchange overviews (volume, sparklines, VWAP rates)
    for extype, payload in ninja_payloads.items():
        core = payload.get("core") if isinstance(payload.get("core"), Mapping) else {}
        ninja_cat = _NINJA_TYPE_TO_CATEGORY.get(extype, extype.lower())

        item_meta_by_id: dict[str, Mapping[str, Any]] = {}
        for meta in payload.get("items") or []:
            if isinstance(meta, Mapping) and meta.get("id"):
                item_meta_by_id[str(meta["id"]).strip().lower()] = meta
        for meta in core.get("items") or []:
            if isinstance(meta, Mapping) and meta.get("id"):
                mid = str(meta["id"]).strip().lower()
                item_meta_by_id.setdefault(mid, meta)

        for line in payload.get("lines") or []:
            if not isinstance(line, Mapping) or not line.get("id"):
                continue
            lid = str(line["id"]).strip().lower()
            meta = item_meta_by_id.get(lid, {})
            details_id = str(meta.get("detailsId") or "").strip().lower() or None
            display_name = str(meta.get("name") or lid).strip()
            if _is_placeholder_item_name(display_name):
                continue

            ninja_ex = 1.0 if lid == "exalted" else _extract_ninja_exalted_price(line, core)
            vol_div = _positive_float(line.get("volumePrimaryValue"))
            max_vol_curr = (
                str(line.get("maxVolumeCurrency")) if line.get("maxVolumeCurrency") else None
            )
            spark = line.get("sparkline")
            chg_7d: float | None = None
            if isinstance(spark, Mapping) and isinstance(spark.get("totalChange"), (int, float)):
                chg_7d = float(spark["totalChange"])

            is_reliquary_line = "reliquary key:" in display_name.lower() or bool(
                details_id and "reliquary-key" in details_id
            )
            if is_reliquary_line:
                rel_slug = normalize_market_slug(display_name) or details_id or f"vaultkey-{lid}"
                if rel_slug in entries_by_id:
                    target_id = rel_slug
                elif details_id and details_id in entries_by_id:
                    target_id = details_id
                else:
                    target_id = rel_slug
            else:
                target_id = lid
                if target_id not in entries_by_id and details_id and details_id in entries_by_id:
                    target_id = details_id
                elif target_id not in entries_by_id:
                    norm_name = normalize_market_slug(display_name)
                    if norm_name in alias_to_id:
                        target_id = alias_to_id[norm_name]

            entry = entries_by_id.get(target_id)
            if entry is None:
                entry = _MergedMarketEntry(
                    id=target_id,
                    name=display_name,
                    category="vaultkeys" if is_reliquary_line else ninja_cat,
                    league=league,
                    item_id=None,
                    base_type=None,
                    scout_exalted=None,
                    ninja_exalted=ninja_ex,
                    volume_divine=vol_div,
                    max_volume_currency=max_vol_curr,
                    change_7d_pct=chg_7d,
                    quote=None,
                )
                entries_by_id[target_id] = entry
            else:
                if ninja_ex is not None:
                    entry.ninja_exalted = ninja_ex
                if vol_div is not None:
                    entry.volume_divine = vol_div
                if max_vol_curr is not None:
                    entry.max_volume_currency = max_vol_curr
                if chg_7d is not None:
                    entry.change_7d_pct = chg_7d

            if not is_reliquary_line:
                _register_alias(lid, target_id)
            _register_alias(details_id, target_id)
            _register_alias(display_name, target_id)

    # 3. Resolve self-consistent reference rates (`exalted=1`, `chaos.chaos=1`, `divine.divine=1`)
    def _pick_exalted(cid: str, entry: _MergedMarketEntry) -> tuple[float | None, str]:
        if cid == "exalted":
            src = (
                "poe2scout+poe.ninja"
                if (entry.scout_exalted is not None and entry.ninja_exalted is not None)
                else ("poe.ninja" if entry.ninja_exalted is not None else "poe2scout")
            )
            return 1.0, src
        if entry.ninja_exalted is not None and entry.ninja_exalted > 0:
            src = "poe2scout+poe.ninja" if entry.scout_exalted is not None else "poe.ninja"
            return entry.ninja_exalted, src
        if entry.scout_exalted is not None and entry.scout_exalted > 0:
            if cid in _SHARD_PARENT_ORBS and "Currency" in ninja_payloads:
                parent_entry = entries_by_id.get(_SHARD_PARENT_ORBS[cid])
                parent_ex = (
                    (parent_entry.ninja_exalted or parent_entry.scout_exalted)
                    if parent_entry is not None
                    else None
                )
                if entry.scout_exalted > max(5.0, (parent_ex or 5.0) * 0.5):
                    return None, "poe2scout"
            if (
                entry.category == "runes"
                and "Runes" in ninja_payloads
                and cid.startswith("lesser-")
                and cid.endswith("-rune")
            ):
                g_entry = entries_by_id.get("greater-" + cid.removeprefix("lesser-"))
                b_entry = entries_by_id.get(cid.removeprefix("lesser-"))
                refs = [
                    val
                    for val in (
                        g_entry.ninja_exalted if g_entry is not None else None,
                        b_entry.ninja_exalted if b_entry is not None else None,
                    )
                    if val is not None and val > 0
                ]
                if entry.scout_exalted > max(25.0, min(refs, default=25.0)):
                    return None, "poe2scout"
            if entry.category == "essences" and "Essences" in ninja_payloads:
                g_slug: str | None = None
                if cid.startswith("lesser-essence-of-"):
                    g_slug = "greater-" + cid.removeprefix("lesser-")
                elif cid.startswith("essence-of-"):
                    rest = cid.removeprefix("essence-of-")
                    if rest not in (
                        "delirium",
                        "horror",
                        "hysteria",
                        "insanity",
                        "the-breach",
                        "the-abyss",
                    ):
                        g_slug = f"greater-essence-of-{rest}"
                if g_slug is not None:
                    g_entry = entries_by_id.get(g_slug)
                    g_ex = (
                        (g_entry.ninja_exalted or g_entry.scout_exalted)
                        if g_entry is not None
                        else None
                    )
                    if entry.scout_exalted > max(25.0, g_ex or 25.0):
                        return None, "poe2scout"
            return entry.scout_exalted, "poe2scout"
        return None, "poe2scout"

    if "divine" in entries_by_id:
        resolved_div, _ = _pick_exalted("divine", entries_by_id["divine"])
        if resolved_div is not None and resolved_div > 0:
            rates.divine_exalted = resolved_div
    if "chaos" in entries_by_id:
        resolved_chaos, _ = _pick_exalted("chaos", entries_by_id["chaos"])
        if resolved_chaos is not None and resolved_chaos > 0:
            rates.chaos_exalted = resolved_chaos
    if rates.chaos_exalted > 0:
        rates.chaos_per_divine = rates.divine_exalted / rates.chaos_exalted

    # 4. Finalize normalized CurrencyQuote (`exalted = 1.0` unit) & populate rates_in_exalted
    for cid, entry in entries_by_id.items():
        chosen_ex, source = _pick_exalted(cid, entry)

        if chosen_ex is not None and chosen_ex > 0:
            entry.quote = _build_quote(
                item_id=cid,
                name=entry.name,
                category=entry.category,
                league=league,
                exalted_val=chosen_ex,
                rates=rates,
                volume_primary=entry.volume_divine,
                change_7d_pct=entry.change_7d_pct,
                source=source,
            )
            if entry.category in CRAFTING_CATEGORIES or entry.category == "fragments":
                rates.rates_in_exalted[cid] = chosen_ex

    rates.rates_in_exalted["exalted"] = 1.0
    rates.rates_in_exalted["chaos"] = rates.chaos_exalted
    rates.rates_in_exalted["divine"] = rates.divine_exalted

    for alias_key, target_slug in DIRECT_SLUG_ALIASES.items():
        if target_slug in entries_by_id:
            alias_to_id.setdefault(alias_key, target_slug)
        if target_slug in rates.rates_in_exalted:
            rates.rates_in_exalted.setdefault(alias_key, rates.rates_in_exalted[target_slug])

    return _LeagueSnapshot(
        fetched_at=fetched_at,
        rates=rates,
        entries_by_id=entries_by_id,
        alias_to_id=alias_to_id,
    )
