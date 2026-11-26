# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""PoE Ninja PoE2 Builds discovery, multi-facet search, and live PoB2 character import."""

from __future__ import annotations

import base64
import difflib
import gzip
import json
import math
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from poe2brain.config import (
    BUNDLED_DATA_DIR,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    POENINJA_POE2_BASE_URL,
)
from poe2brain.http import Poe2HttpClient, get_cached_json
from poe2brain.knowledge.meta import (
    ASCENDANCY_SHORTHAND_ALIASES,
    POE1_OR_UNRELEASED_TRAP_CLASSES,
    MetaService,
)
from poe2brain.models.builds import CharacterTimelinePoint, CharacterTimelineReport
from poe2brain.models.ninja import (
    NinjaBuildEntry,
    NinjaBuildSearchReport,
    NinjaCharacterImport,
    NinjaClassStatistic,
    NinjaFacetCount,
    NinjaSnapshotInfo,
    NinjaSnapshotStateReport,
    NinjaStatSummary,
)
from poe2brain.ninja.protobuf import decode_ndic_dictionary
from poe2brain.ninja.xml_inspector import decode_pob2_code_to_xml, inspect_pob2_xml_or_code

if TYPE_CHECKING:
    from poe2brain.pob.service import BuildService

POENINJA_ORIGIN: str = "https://poe.ninja"
POENINJA_BUILD_INDEX_URL: str = f"{POENINJA_POE2_BASE_URL}/data/build-index-state"
POENINJA_INDEX_STATE_URL: str = f"{POENINJA_POE2_BASE_URL}/data/index-state"
POENINJA_SNAPSHOT_VERSIONS_URL: str = f"{POENINJA_POE2_BASE_URL}/data/snapshot-versions"
POENINJA_CHAR_POB_RAW_URL: str = f"{POENINJA_ORIGIN}/poe2/pob/raw/overview/code"
POENINJA_SHARE_POB_RAW_BASE_URL: str = f"{POENINJA_ORIGIN}/poe2/pob/raw"
POENINJA_DICTIONARY_BASE_URL: str = f"{POENINJA_ORIGIN}/api/lookup/poe2/builds/dictionary"

DEFAULT_BUILD_INDEX_TTL_SECONDS: float = 1800.0
DEFAULT_SNAPSHOT_TTL_SECONDS: float = 900.0
DEFAULT_CHAR_POB_TTL_SECONDS: float = 1800.0
DEFAULT_DICTIONARY_TTL_SECONDS: float = 86400.0
DEFAULT_MIN_UNCACHED_CHAR_INTERVAL_SECONDS: float = 1.0

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_NINJA_CHAR_URL_RE = re.compile(
    r"^https?://(?:www\.)?poe2?\.ninja/(?:poe2/)?builds/([^/?#]+)/character/([^/?#]+)/([^/?#]+)/?$",
    re.IGNORECASE,
)
_NINJA_PROFILE_URL_RE = re.compile(
    r"^https?://(?:www\.)?poe2?\.ninja/(?:poe2/)?profile/([^/?#]+)/character/([^/?#]+)/?$",
    re.IGNORECASE,
)
_NINJA_POB_SHARE_RE = re.compile(
    r"^https?://(?:www\.)?poe2?\.ninja/(?:poe2/)?pob/(?:raw/)?([A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_POE2DB_POB_RE = re.compile(
    r"^https?://(?:www\.)?poe2db\.tw/pob/([A-Za-z0-9_-]+)(?:/raw)?/?$",
    re.IGNORECASE,
)
_MAXROLL_POB_RE = re.compile(
    r"^https?://(?:www\.)?maxroll\.gg/poe2/(?:api/)?pob/([A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_POBBIN_POB_RE = re.compile(
    r"^https?://(?:www\.)?pobb\.in/(?:u/[^/]+/)?(?:pob/)?([A-Za-z0-9_-]+)(?:/raw)?/?$",
    re.IGNORECASE,
)

_WEAPON_SHORTHAND_MAP: dict[str, tuple[str, ...]] = {
    "quarterstaff": ("Quarterstaff", "Warstaff"),
    "warstaff": ("Quarterstaff", "Warstaff"),
    "qstaff": ("Quarterstaff", "Warstaff"),
    "mace": ("One Hand Mace", "Two Hand Mace"),
    "1hmace": ("One Hand Mace",),
    "2hmace": ("Two Hand Mace",),
    "sword": ("One Hand Sword", "Two Hand Sword"),
    "axe": ("One Hand Axe", "Two Hand Axe"),
    "hollowpalm": ("Unarmed",),
    "unarmed": ("Unarmed",),
}


def _norm_token(value: str) -> str:
    return _NON_ALNUM_RE.sub("", value.strip().lower())


def _default_corpus_path() -> Path:
    return BUNDLED_DATA_DIR / "ninja" / f"forbidden_rites_{POE2_PATCH}_builds.json.gz"


def _league_family_specs(
    league: str,
) -> tuple[
    Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"],
    dict[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], tuple[str, str, str]],
]:
    """Return `(requested_mode, {mode: (display_name, league_url_slug, snapshot_name_slug)})`."""
    raw = league.strip() or DEFAULT_LEAGUE
    low = raw.lower()

    # Handle poe.ninja URL slugs or snapshot names passed directly as `league`
    slug_norm = _norm_token(raw)
    if slug_norm in ("forbiddenrites", "scforbiddenrites"):
        raw, low = "Forbidden Rites", "forbidden rites"
    elif slug_norm in ("forbiddenriteshc", "hcforbiddenrites"):
        raw, low = "HC Forbidden Rites", "hc forbidden rites"
    elif slug_norm in ("forbiddenritesssf", "ssfforbiddenrites"):
        raw, low = "SSF Forbidden Rites", "ssf forbidden rites"
    elif slug_norm in ("forbiddenriteshcssf", "hcssfforbiddenrites"):
        raw, low = "HC SSF Forbidden Rites", "hc ssf forbidden rites"

    is_hc = low.startswith("hc ") or "hardcore" in low or low.endswith("hc")
    is_ssf = "ssf" in low

    base = raw
    for prefix in ("HC SSF ", "HCSSF ", "SSF HC ", "HC ", "Hardcore SSF ", "Hardcore ", "SSF "):
        if base.lower().startswith(prefix.lower()):
            base = base[len(prefix) :].strip()
            break
    if not base:
        base = DEFAULT_LEAGUE

    base_url = _norm_token(base)
    base_snap = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")

    family: dict[Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"], tuple[str, str, str]] = {
        "sc_trade": (base, base_url, base_snap),
        "hc_trade": (f"HC {base}", f"{base_url}hc", f"hc-{base_snap}"),
        "ssf": (f"SSF {base}", f"{base_url}ssf", f"ssf-{base_snap}"),
        "hc_ssf": (f"HC SSF {base}", f"{base_url}hcssf", f"hc-ssf-{base_snap}"),
    }
    if is_hc and is_ssf:
        req_mode: Literal["sc_trade", "hc_trade", "ssf", "hc_ssf"] = "hc_ssf"
    elif is_hc:
        req_mode = "hc_trade"
    elif is_ssf:
        req_mode = "ssf"
    else:
        req_mode = "sc_trade"
    return req_mode, family


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(sorted_vals[lo], 2)
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 2)


def _stat_summary(values: Iterable[float]) -> NinjaStatSummary:
    arr = sorted(float(v) for v in values)
    if not arr:
        return NinjaStatSummary()
    return NinjaStatSummary(
        min=round(arr[0], 2),
        median=_percentile(arr, 0.50),
        p90=_percentile(arr, 0.90),
        max=round(arr[-1], 2),
    )


def _top_facets(
    counts: Mapping[str, int],
    total_denom: int,
    *,
    limit: int = 12,
) -> list[NinjaFacetCount]:
    if not counts or total_denom <= 0:
        return []
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        NinjaFacetCount(
            name=name,
            count=cnt,
            share_pct=round((cnt / total_denom) * 100.0, 2),
        )
        for name, cnt in ordered
    ]


def _normalize_account_tag(account: str) -> str:
    """poe.ninja builds/pob endpoints require `Account-1234` hyphen format, never `#1234`."""
    cleaned = unquote(account.strip())
    if "#" in cleaned:
        cleaned = cleaned.replace("#", "-")
    return cleaned


_TM_LABEL_RE = re.compile(r"^(hour|day|week|month)-(\d+)$", re.IGNORECASE)
_T0_TRADE_CHASE_UNIQUES: tuple[str, ...] = (
    "Temporalis",
    "Mageblood",
    "Headhunter",
    "Kalandra's Touch",
    "Astramentis",
    "The Adorned",
    "Voices",
    "Ingenuity",
)

_MOBILITY_SKILL_TAGS: dict[str, str] = {
    "Blink": "blink_teleport",
    "Lightning Warp": "blink_teleport",
    "Shattering Palm": "dash_melee",
    "Flicker Strike": "dash_melee",
    "Whirling Slash": "dash_melee",
    "Leap Slam": "dash_melee",
    "Shield Charge": "dash_melee",
    "Thunderous Leap": "dash_melee",
    "Pounce": "dash_melee",
    "Stampede": "dash_melee",
    "Herald of Ice": "herald_chain",
    "Herald of Thunder": "herald_chain",
    "Herald of Ash": "herald_chain",
    "Herald of Plague": "herald_chain",
    "Herald of Blood": "herald_chain",
    "Cast on Critical": "trigger_clear",
    "Cast on Elemental Ailment": "trigger_clear",
    "Cast on Freeze": "trigger_clear",
    "Cast on Dodge": "trigger_clear",
    "Mirage Archer": "auto_clear",
    "Mirage Deadeye": "auto_clear",
    "Wind Dancer": "evade_speed",
    "Ghost Dance": "evade_speed",
}

NINJA_SEARCH_PRESETS: tuple[str, ...] = (
    "sleeper",
    "zoom_mapper",
    "league_start",
    "boss_killer",
    "hc_tank",
)


def _split_include_exclude(raw_value: str | None) -> tuple[list[str], list[str]]:
    """Split a comma-separated filter value into `(includes, excludes)` supporting `!Term`."""
    if not raw_value:
        return [], []
    includes: list[str] = []
    excludes: list[str] = []
    for part in raw_value.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        if cleaned.startswith("!"):
            term = cleaned[1:].strip()
            if term:
                excludes.append(term)
        else:
            includes.append(cleaned)
    return includes, excludes


def _derive_mobility_and_chase(rec: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    existing_mob = rec.get("mobility_tags")
    existing_chase = rec.get("t0_chase_uniques")
    if isinstance(existing_mob, list) and isinstance(existing_chase, list):
        return [str(x) for x in existing_mob], [str(x) for x in existing_chase]
    tags: set[str] = set()
    for sk in rec.get("active_skills") or ():
        tag = _MOBILITY_SKILL_TAGS.get(str(sk))
        if tag:
            tags.add(tag)
    uniques = [str(u) for u in (rec.get("unique_items") or ())]
    if "Temporalis" in uniques:
        tags.add("temporalis_zero_cd")
    if "Queen of the Forest" in uniques:
        tags.add("qotf_speed")
    if "The Fall of the Axe" in uniques:
        tags.add("onslaught_speed")
    t0_set = set(_T0_TRADE_CHASE_UNIQUES)
    chase = sorted(u for u in uniques if u in t0_set)
    return sorted(tags), chase


def _compute_sleeper_score(
    rec: Mapping[str, Any],
    *,
    ascendancy_share_pct: float | None,
    corpus_skill_count: int,
    mobility_tags: Sequence[str],
    t0_chase_uniques: Sequence[str],
) -> float:
    dew_dps = rec.get("dewatered_dps")
    real_dps = (
        float(dew_dps)
        if (dew_dps is not None and float(dew_dps) > 0.0)
        else float(rec.get("effective_dps") or 0.0)
    )
    dps_term = math.log10(max(real_dps, 1.0))
    ms_raw = rec.get("movement_speed_mod")
    ms = float(ms_raw) if (ms_raw is not None and float(ms_raw) > 0.0) else 1.0
    skill_ms_raw = rec.get("movement_speed_while_using_skill")
    skill_ms = (
        float(skill_ms_raw) if (skill_ms_raw is not None and float(skill_ms_raw) > 0.0) else 0.0
    )
    mob_bonus = (
        (ms - 1.0) * 1.8
        + min(skill_ms, 1.5) * 0.5
        + min(len(mobility_tags), 3) * 0.25
    )
    asc_share = float(ascendancy_share_pct) if ascendancy_share_pct is not None else 1.2
    rarity_bonus = (
        max(0.0, (8.0 - asc_share) / 8.0) * 1.6
        + (1.0 / math.sqrt(max(1, corpus_skill_count))) * 1.2
    )
    dew_ehp = rec.get("dewatered_ehp")
    ehp_val = (
        float(dew_ehp)
        if (dew_ehp is not None and float(dew_ehp) > 0.0)
        else float(rec.get("total_ehp") or 0.0)
    )
    ehp_penalty = 0.0 if ehp_val >= 20000.0 else -1.5
    infl = rec.get("dps_inflation_pct")
    inflation_penalty = -0.8 if (infl is not None and float(infl) >= 50.0) else 0.0
    chase_penalty = -0.35 * len(t0_chase_uniques)
    raw_score = (
        (dps_term - 4.0) * 2.5
        + mob_bonus
        + rarity_bonus
        + ehp_penalty
        + inflation_penalty
        + chase_penalty
    )
    return round(max(0.0, raw_score), 2)


def _time_machine_age_hours(label: str) -> float:
    cleaned = label.strip().lower()
    if cleaned == "current":
        return float("inf")
    m = _TM_LABEL_RE.match(cleaned)
    if not m:
        return 1e9
    unit = m.group(1).lower()
    val = float(m.group(2))
    if unit == "hour":
        return val
    if unit == "day":
        return val * 24.0
    if unit == "week":
        return val * 24.0 * 7.0
    if unit == "month":
        return val * 24.0 * 30.0
    return 1e9


def sort_time_machine_labels(labels: Iterable[str]) -> list[str]:
    """Return deduplicated `poe.ninja` time-machine labels sorted chronologically."""
    unique: dict[str, str] = {}
    for raw in labels:
        s = str(raw).strip()
        if s and s.lower() not in unique:
            unique[s.lower()] = s
    return sorted(unique.values(), key=lambda lbl: (_time_machine_age_hours(lbl), lbl.lower()))


class NinjaBuildsService:
    """Clean-room Path of Exile 2 `poe.ninja` Builds & PoB2 Character Intelligence Service.

    Strictly uses clean `HTTP 200` `poe.ninja` JSON/raw endpoints (`build-index-state`,
    `snapshot-versions`, `pob/raw/overview/code`, `api/lookup/poe2/builds/dictionary`, and
    public PoB share raw links) plus the bundled MIT-licensed `Forbidden Rites` 353-build
    leaderboard corpus (`data/ninja/forbidden_rites_0.5.5_builds.json.gz`). Never calls
    `/api/protected/...` or attempts to bypass Cloudflare challenges.
    """

    def __init__(
        self,
        *,
        http_client: Poe2HttpClient | None = None,
        build_service: BuildService | None = None,
        meta_service: MetaService | None = None,
        corpus_path: Path | None = None,
        cache_dir: Path | None = None,
        build_index_url: str = POENINJA_BUILD_INDEX_URL,
        snapshot_versions_url: str = POENINJA_SNAPSHOT_VERSIONS_URL,
        char_pob_raw_url: str = POENINJA_CHAR_POB_RAW_URL,
        dictionary_base_url: str = POENINJA_DICTIONARY_BASE_URL,
        min_uncached_char_interval_seconds: float = DEFAULT_MIN_UNCACHED_CHAR_INTERVAL_SECONDS,
        now_fn: Callable[[], float] = time.time,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._owns_http = http_client is None
        self.http_client = http_client or Poe2HttpClient()
        self._build_service = build_service
        self._owns_build_service = False
        self._owns_meta = meta_service is None
        self.meta_service = meta_service or MetaService(
            http_client=self.http_client, cache_dir=cache_dir
        )
        self.corpus_path = corpus_path if corpus_path is not None else _default_corpus_path()
        self.cache_dir = cache_dir
        self.build_index_url = build_index_url
        self.snapshot_versions_url = snapshot_versions_url
        self.char_pob_raw_url = char_pob_raw_url
        self.dictionary_base_url = dictionary_base_url.rstrip("/")
        self.min_uncached_char_interval_seconds = min_uncached_char_interval_seconds
        self._now_fn = now_fn
        self._monotonic_fn = monotonic_fn
        self._sleep_fn = sleep_fn

        self._pace_lock = threading.Lock()
        self._last_uncached_char_ts: float = 0.0
        self._missing_tm_cache: dict[tuple[str, str, str, str], int] = {}
        self._corpus_records: list[dict[str, Any]] | None = None
        self._deduped_corpus: list[dict[str, Any]] | None = None

    def close(self) -> None:
        if self._owns_build_service and self._build_service is not None:
            self._build_service.close()
            self._build_service = None
        if self._owns_meta:
            self.meta_service.close()
        if self._owns_http:
            self.http_client.close()

    def __enter__(self) -> NinjaBuildsService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_build_service(self) -> BuildService:
        if self._build_service is None:
            from poe2brain.pob.bridge import get_default_pob2_bridge
            from poe2brain.pob.service import BuildService

            self._build_service = BuildService(
                bridge=get_default_pob2_bridge(),
                owns_bridge=False,
                http_client=self.http_client,
            )
            self._owns_build_service = True
        return self._build_service

    # ------------------------------------------------------------------
    # 1. Corpus & HTTP Helpers
    # ------------------------------------------------------------------

    def _ensure_corpus_loaded(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._corpus_records is not None and self._deduped_corpus is not None:
            return self._corpus_records, self._deduped_corpus

        if not self.corpus_path.is_file():
            self._corpus_records = []
            self._deduped_corpus = []
            return self._corpus_records, self._deduped_corpus

        with gzip.open(self.corpus_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_list = list(payload.get("builds") or [])

        # Deduplicate by (account.lower(), character.lower()), keeping latest snapshot
        # while preserving leaderboard rank_within_ascendancy.
        by_ident: dict[tuple[str, str], dict[str, Any]] = {}
        for rec in raw_list:
            ident = (
                str(rec.get("account") or "").lower(),
                str(rec.get("character") or "").lower(),
            )
            if not ident[0] or not ident[1]:
                continue
            existing = by_ident.get(ident)
            if existing is None:
                by_ident[ident] = dict(rec)
            else:
                # Keep newer fetched_at if available, preserving rank_within_ascendancy
                cur_ts = str(rec.get("fetched_at") or "")
                old_ts = str(existing.get("fetched_at") or "")
                rnk = existing.get("rank_within_ascendancy") or rec.get("rank_within_ascendancy")
                if cur_ts > old_ts:
                    merged = dict(rec)
                    merged["rank_within_ascendancy"] = rnk
                    by_ident[ident] = merged
                elif existing.get("rank_within_ascendancy") is None and rnk is not None:
                    existing["rank_within_ascendancy"] = rnk

        self._corpus_records = raw_list
        self._deduped_corpus = list(by_ident.values())
        return self._corpus_records, self._deduped_corpus

    @staticmethod
    def _assert_not_protected_url(url: str) -> None:
        parsed = urlparse(url)
        path_low = parsed.path.lower()
        if "/api/protected/" in path_low:
            raise ValueError(
                "Refusing to call poe.ninja '/api/protected/...' endpoint (gated behind "
                "Cloudflare bot protection and marked closed/internal by poe.ninja). "
                "Use '/poe2/pob/raw/overview/code' or a PoB2 share code instead."
            )

    def _pace_uncached_char_request(self) -> None:
        if self.min_uncached_char_interval_seconds <= 0:
            return
        with self._pace_lock:
            now_m = self._monotonic_fn()
            elapsed = now_m - self._last_uncached_char_ts
            if (
                self._last_uncached_char_ts > 0
                and elapsed < self.min_uncached_char_interval_seconds
            ):
                self._sleep_fn(self.min_uncached_char_interval_seconds - elapsed)
            self._last_uncached_char_ts = self._monotonic_fn()

    def _cached_get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_seconds: float,
        force_refresh: bool = False,
    ) -> Any:
        self._assert_not_protected_url(url)

        def _fetch() -> Any:
            return self.http_client.get_json(url, params=params)

        return get_cached_json(
            url,
            params=params,
            ttl_seconds=ttl_seconds,
            fetcher=_fetch,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )

    def _cached_get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        ttl_seconds: float = DEFAULT_CHAR_POB_TTL_SECONDS,
        pace_on_miss: bool = True,
        force_refresh: bool = False,
    ) -> str:
        self._assert_not_protected_url(url)

        def _fetch() -> dict[str, str]:
            if pace_on_miss:
                self._pace_uncached_char_request()
            resp = self.http_client.sync_client.get(
                url,
                params=params,
                headers={"Accept": "text/plain, */*"},
            )
            resp.raise_for_status()
            body = resp.text.strip()
            if not body:
                raise ValueError(f"Empty PoB2 code returned from {url}")
            return {"code": body}

        envelope = get_cached_json(
            url,
            params=params,
            ttl_seconds=ttl_seconds,
            fetcher=_fetch,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )
        if isinstance(envelope, Mapping) and isinstance(envelope.get("code"), str):
            return envelope["code"]
        raise ValueError(f"Unexpected cached text envelope for {url}")

    def fetch_dictionary(
        self,
        dict_hash: str,
        *,
        force_refresh: bool = False,
    ) -> list[str]:
        """Fetch and decode a live `NDIC` v2 dictionary from `poe.ninja/api/lookup/poe2/...`."""
        clean_hash = dict_hash.strip()
        if not re.fullmatch(r"[a-fA-F0-9]{16,128}", clean_hash):
            raise ValueError(f"Invalid poe.ninja dictionary hash {dict_hash!r}")
        url = f"{self.dictionary_base_url}/{clean_hash}"

        def _fetch_b64() -> dict[str, str]:
            resp = self.http_client.sync_client.get(url)
            resp.raise_for_status()
            return {"b64": base64.b64encode(resp.content).decode("ascii")}

        cached = get_cached_json(
            url,
            ttl_seconds=DEFAULT_DICTIONARY_TTL_SECONDS,
            fetcher=_fetch_b64,
            cache_dir=self.cache_dir,
            client=self.http_client,
            force_refresh=force_refresh,
            allow_stale_on_error=True,
            now_fn=self._now_fn,
        )
        blob = base64.b64decode(cached["b64"])
        return decode_ndic_dictionary(blob)

    # ------------------------------------------------------------------
    # 2. Live Snapshot & Build Index State
    # ------------------------------------------------------------------

    def _resolve_class_and_asc(self, raw_cls: str) -> tuple[str, str | None]:
        resolved = self.meta_service._resolve_character_class_and_ascendancy(raw_cls)
        if resolved is not None:
            return resolved
        return (raw_cls, None)

    def get_snapshot_state(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        force_refresh: bool = False,
    ) -> NinjaSnapshotStateReport:
        """Query live `build-index-state` (`30m` TTL) + `snapshot-versions` (`15m` TTL)
        across `Forbidden Rites`, `HC Forbidden Rites`, `SSF Forbidden Rites`, and
        `HC SSF Forbidden Rites`.
        """
        req_mode, family = _league_family_specs(league)
        notes: list[str] = []

        index_by_name: dict[str, Mapping[str, Any]] = {}
        index_by_url: dict[str, Mapping[str, Any]] = {}
        try:
            raw_idx = self._cached_get_json(
                self.build_index_url,
                ttl_seconds=DEFAULT_BUILD_INDEX_TTL_SECONDS,
                force_refresh=force_refresh,
            )
            if isinstance(raw_idx, Mapping) and isinstance(raw_idx.get("leagueBuilds"), list):
                for row in raw_idx["leagueBuilds"]:
                    if isinstance(row, Mapping):
                        lname = str(row.get("leagueName") or "").strip()
                        lurl = str(row.get("leagueUrl") or "").strip()
                        if lname:
                            index_by_name[lname.lower()] = row
                        if lurl:
                            index_by_url[lurl.lower()] = row
        except httpx.HTTPError as exc:
            notes.append(f"poe.ninja build-index-state unreachable ({type(exc).__name__}).")

        modes_out: dict[str, NinjaSnapshotInfo] = {}
        for mode_key, (disp_name, default_url, default_snap) in family.items():
            row = index_by_name.get(disp_name.lower()) or index_by_url.get(default_url.lower())
            league_name = str(row.get("leagueName") or disp_name) if row else disp_name
            league_url = str(row.get("leagueUrl") or default_url) if row else default_url
            total_chars = int(row.get("total") or 0) if row else 0
            hardcore = (
                bool(row.get("hardcore"))
                if (row and row.get("hardcore") is not None)
                else mode_key in ("hc_trade", "hc_ssf")
            )
            ssf = mode_key in ("ssf", "hc_ssf")

            class_stats: list[NinjaClassStatistic] = []
            if row and isinstance(row.get("statistics"), list):
                for st in row["statistics"]:
                    if not isinstance(st, Mapping):
                        continue
                    raw_c = str(st.get("class") or "").strip()
                    if not raw_c:
                        continue
                    pct = float(st.get("percentage") or 0.0)
                    trend = int(st.get("trend") or 0)
                    base_c, asc_c = self._resolve_class_and_asc(raw_c)
                    class_stats.append(
                        NinjaClassStatistic(
                            class_name=base_c,
                            ascendancy_name=asc_c,
                            share_pct=round(pct, 2),
                            character_count=int(round(total_chars * (pct / 100.0))),
                            trend=trend,
                        )
                    )

            version: str | None = None
            snap_name: str | None = default_snap
            ov_type = 0
            passive_tree: str | None = None
            tm_labels: list[str] = []

            try:
                sv_raw = self._cached_get_json(
                    self.snapshot_versions_url,
                    params={"league": league_url},
                    ttl_seconds=DEFAULT_SNAPSHOT_TTL_SECONDS,
                    force_refresh=force_refresh,
                )
                if isinstance(sv_raw, list) and sv_raw:
                    sv0 = sv_raw[0]
                    if isinstance(sv0, Mapping):
                        version = str(sv0["version"]) if sv0.get("version") else None
                        snap_name = (
                            str(sv0["snapshotName"]) if sv0.get("snapshotName") else default_snap
                        )
                        ov_type = int(sv0.get("overviewType") or 0)
                        passive_tree = str(sv0["passiveTree"]) if sv0.get("passiveTree") else None
                        if isinstance(sv0.get("timeMachineLabels"), list):
                            tm_labels = sort_time_machine_labels(
                                str(lbl) for lbl in sv0["timeMachineLabels"] if lbl
                            )
            except httpx.HTTPError as exc:
                notes.append(
                    f"snapshot-versions ({league_url}) unreachable ({type(exc).__name__}); "
                    f"using default overview slug {default_snap!r}."
                )

            modes_out[mode_key] = NinjaSnapshotInfo(
                league_name=league_name,
                league_url=league_url,
                mode=mode_key,
                hardcore=hardcore,
                ssf=ssf,
                total_characters=total_chars,
                version=version,
                snapshot_name=snap_name,
                overview_type=ov_type,
                passive_tree=passive_tree,
                time_machine_labels=tm_labels,
                class_statistics=class_stats,
            )

        primary = modes_out[req_mode]
        return NinjaSnapshotStateReport(
            patch=POE2_PATCH,
            requested_league=league,
            primary=primary,
            modes=modes_out,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # 3. Multi-Facet Build Search & Aggregations
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_not_poe1_trap(query: str | None) -> None:
        if not query:
            return
        norm = _norm_token(query)
        if norm in POE1_OR_UNRELEASED_TRAP_CLASSES:
            raise LookupError(POE1_OR_UNRELEASED_TRAP_CLASSES[norm])

    @staticmethod
    def _resolve_ascendancy_alias(query: str) -> str:
        norm = _norm_token(query)
        return ASCENDANCY_SHORTHAND_ALIASES.get(norm, query.strip())

    @staticmethod
    def _matches_term_in_list(
        target_query: str,
        candidates: Iterable[str],
        vocab_norms: Mapping[str, str] | None = None,
    ) -> bool:
        q_norm = _norm_token(target_query)
        if not q_norm:
            return True
        cand_norms = [_norm_token(c) for c in candidates if c]
        for cn in cand_norms:
            if q_norm == cn or q_norm in cn:
                return True
        if vocab_norms:
            close = difflib.get_close_matches(q_norm, list(vocab_norms.keys()), n=1, cutoff=0.78)
            if close and close[0] in cand_norms:
                return True
        return False

    @staticmethod
    def _matches_weapon(query: str, weapon_types: Iterable[str], keystones: Iterable[str]) -> bool:
        q_norm = _norm_token(query)
        if not q_norm:
            return True
        w_list = list(weapon_types)
        mapped = _WEAPON_SHORTHAND_MAP.get(q_norm)
        if mapped is not None:
            if any(w in mapped for w in w_list):
                return True
            if "Unarmed" in mapped and any("hollowpalm" in _norm_token(k) for k in keystones):
                return True
            return False
        return any(q_norm == _norm_token(w) or q_norm in _norm_token(w) for w in w_list)

    @staticmethod
    def _record_to_entry(
        rec: Mapping[str, Any],
        *,
        include_pob_code: bool = False,
    ) -> NinjaBuildEntry:
        mob_tags, chase_uniques = _derive_mobility_and_chase(rec)
        return NinjaBuildEntry(
            account=str(rec["account"]),
            character=str(rec["character"]),
            league=str(rec.get("league") or DEFAULT_LEAGUE),
            rank_within_ascendancy=rec.get("rank_within_ascendancy"),
            class_name=str(rec.get("class_name") or "Unknown"),
            ascendancy_name=rec.get("ascendancy_name"),
            level=int(rec.get("level") or 1),
            main_skill=rec.get("main_skill"),
            active_skills=list(rec.get("active_skills") or []),
            support_gems=list(rec.get("support_gems") or []),
            unique_items=list(rec.get("unique_items") or []),
            weapon_types=list(rec.get("weapon_types") or []),
            keystones=list(rec.get("keystones") or []),
            ascendancy_notables=list(rec.get("ascendancy_notables") or []),
            anoints=list(rec.get("anoints") or []),
            total_dps=float(rec.get("total_dps") or 0.0),
            combined_dps=float(rec.get("combined_dps") or 0.0),
            full_dps=float(rec.get("full_dps") or 0.0),
            effective_dps=float(rec.get("effective_dps") or 0.0),
            total_ehp=float(rec.get("total_ehp") or 0.0),
            life=float(rec.get("life") or 0.0),
            energy_shield=float(rec.get("energy_shield") or 0.0),
            mana=float(rec.get("mana") or 0.0),
            spirit=int(rec.get("spirit") or 0),
            dewatered_dps=(
                float(rec["dewatered_dps"]) if rec.get("dewatered_dps") is not None else None
            ),
            dps_inflation_pct=(
                float(rec["dps_inflation_pct"])
                if rec.get("dps_inflation_pct") is not None
                else None
            ),
            dewatered_ehp=(
                float(rec["dewatered_ehp"]) if rec.get("dewatered_ehp") is not None else None
            ),
            phys_max_hit=(
                float(rec["phys_max_hit"]) if rec.get("phys_max_hit") is not None else None
            ),
            chaos_max_hit=(
                float(rec["chaos_max_hit"]) if rec.get("chaos_max_hit") is not None else None
            ),
            chaos_immune=bool(rec.get("chaos_immune", False)),
            movement_speed_mod=(
                float(rec["movement_speed_mod"])
                if rec.get("movement_speed_mod") is not None
                else None
            ),
            movement_speed_while_using_skill=(
                float(rec["movement_speed_while_using_skill"])
                if rec.get("movement_speed_while_using_skill") is not None
                else None
            ),
            ascendancy_share_pct=(
                float(rec["ascendancy_share_pct"])
                if rec.get("ascendancy_share_pct") is not None
                else None
            ),
            corpus_skill_count=(
                int(rec["corpus_skill_count"])
                if rec.get("corpus_skill_count") is not None
                else None
            ),
            mobility_tags=mob_tags,
            t0_chase_uniques=chase_uniques,
            sleeper_score=(
                float(rec["sleeper_score"]) if rec.get("sleeper_score") is not None else None
            ),
            ninja_url=str(rec.get("ninja_url") or ""),
            source=str(rec.get("source") or "poeninja_corpus"),
            live_refreshed=bool(rec.get("live_refreshed", False)),
            pob_code=str(rec["pob_code"]) if (include_pob_code and rec.get("pob_code")) else None,
        )

    def search_builds(
        self,
        league: str = DEFAULT_LEAGUE,
        *,
        preset: Literal["sleeper", "zoom_mapper", "league_start", "boss_killer", "hc_tank"]
        | None = None,
        class_name: str | None = None,
        ascendancy: str | None = None,
        skill: str | None = None,
        support_gem: str | None = None,
        unique_item: str | None = None,
        exclude_uniques: Sequence[str] | None = None,
        max_uniques: int | None = None,
        keystone: str | None = None,
        weapon_type: str | None = None,
        min_dps: float | None = None,
        min_ehp: float | None = None,
        sort_by: Literal["dps", "ehp", "level", "es", "life", "sleeper", "mobility"] | None = None,
        ssf_viable_first: bool = False,
        limit: int = 15,
        refresh_live_top: int = 0,
        include_pob_code: bool = False,
        force_refresh: bool = False,
    ) -> NinjaBuildSearchReport:
        """Filter and aggregate PoE2 `Forbidden Rites` `poe.ninja` builds across presets,
        class, ascendancy, skill, support gem, unique item (`!Term` negation supported),
        keystone, weapon type, and DPS/EHP floors.
        """
        norm_preset: (
            Literal["sleeper", "zoom_mapper", "league_start", "boss_killer", "hc_tank"] | None
        ) = None
        if preset is not None and str(preset).strip():
            cand_p = str(preset).strip().lower().replace("-", "_")
            if cand_p not in NINJA_SEARCH_PRESETS:
                valid_p = ", ".join(NINJA_SEARCH_PRESETS)
                raise ValueError(f"Unsupported preset {preset!r} (expected one of: {valid_p}).")
            norm_preset = cand_p  # type: ignore[assignment]

        cls_inc_raw, cls_exc_raw = _split_include_exclude(class_name)
        asc_inc_raw, asc_exc_raw = _split_include_exclude(ascendancy)
        sk_inc, sk_exc = _split_include_exclude(skill)
        sup_inc, sup_exc = _split_include_exclude(support_gem)
        uniq_inc, uniq_exc = _split_include_exclude(unique_item)
        ks_inc, ks_exc = _split_include_exclude(keystone)
        weap_inc, weap_exc = _split_include_exclude(weapon_type)

        for tok in (*cls_inc_raw, *cls_exc_raw, *asc_inc_raw, *asc_exc_raw):
            self._validate_not_poe1_trap(tok)

        cls_inc = [self._resolve_ascendancy_alias(t) for t in cls_inc_raw]
        cls_exc = [self._resolve_ascendancy_alias(t) for t in cls_exc_raw]
        asc_inc = [self._resolve_ascendancy_alias(t) for t in asc_inc_raw]
        asc_exc = [self._resolve_ascendancy_alias(t) for t in asc_exc_raw]

        snap_report = self.get_snapshot_state(league, force_refresh=force_refresh)
        primary_snap = snap_report.primary
        notes = list(snap_report.notes)
        if primary_snap.mode != "sc_trade":
            notes.append(
                f"Live snapshot & class playrates reflect {primary_snap.league_name!r} "
                f"({primary_snap.total_characters:,} indexed characters); build roster rows are "
                f"filtered from the bundled {POE2_PATCH} Forbidden Rites leaderboard corpus "
                "(poe.ninja gated /api/protected/.../search behind Cloudflare 403 on 2026-11-18)."
            )

        _, corpus = self._ensure_corpus_loaded()

        # Build normalized vocabularies, corpus main-skill rarity counts, and live ladder shares
        skill_vocab: dict[str, str] = {}
        supp_vocab: dict[str, str] = {}
        uniq_vocab: dict[str, str] = {}
        ks_vocab: dict[str, str] = {}
        asc_vocab: dict[str, str] = {}
        corpus_skill_counts: dict[str, int] = defaultdict(int)
        for r in corpus:
            if r.get("ascendancy_name"):
                asc_vocab[_norm_token(r["ascendancy_name"])] = r["ascendancy_name"]
            for s in r.get("active_skills") or ():
                skill_vocab[_norm_token(s)] = s
            ms_r = r.get("main_skill")
            if ms_r:
                skill_vocab[_norm_token(ms_r)] = ms_r
                corpus_skill_counts[_norm_token(str(ms_r))] += 1
            for g in r.get("support_gems") or ():
                supp_vocab[_norm_token(g)] = g
            for u in r.get("unique_items") or ():
                uniq_vocab[_norm_token(u)] = u
            for k in r.get("keystones") or ():
                ks_vocab[_norm_token(k)] = k

        live_asc_share: dict[str, float] = {}
        for st in primary_snap.class_statistics:
            key_name = st.ascendancy_name or st.class_name
            if key_name:
                live_asc_share[_norm_token(key_name)] = round(float(st.share_pct), 2)

        # Resolve preset defaults without overriding explicit caller arguments
        eff_sort: Literal["dps", "ehp", "level", "es", "life", "sleeper", "mobility"]
        if sort_by is not None:
            eff_sort = sort_by
        elif norm_preset == "sleeper":
            eff_sort = "sleeper"
        elif norm_preset == "zoom_mapper":
            eff_sort = "mobility"
        elif norm_preset == "hc_tank":
            eff_sort = "ehp"
        else:
            eff_sort = "dps"

        eff_ssf_first = ssf_viable_first or (norm_preset == "league_start")
        eff_max_uniques = max_uniques
        eff_min_dps = min_dps
        eff_min_ehp = min_ehp

        excl_terms = [u.strip() for u in (exclude_uniques or ()) if u and u.strip()]
        excl_terms.extend(uniq_exc)

        uniq_inc_norms = {_norm_token(u) for u in uniq_inc}
        if norm_preset == "sleeper" and "temporalis" not in uniq_inc_norms:
            excl_terms.append("Temporalis")
        elif norm_preset == "league_start":
            for chase_u in (
                "Temporalis",
                "Mageblood",
                "Headhunter",
                "Kalandra's Touch",
                "Astramentis",
            ):
                if _norm_token(chase_u) not in uniq_inc_norms:
                    excl_terms.append(chase_u)
        # Deduplicate excl_terms preserving order
        excl_terms = list(dict.fromkeys(excl_terms))

        applied_filters: dict[str, str | float] = {}
        if norm_preset:
            applied_filters["preset"] = norm_preset
        if class_name:
            applied_filters["class_name"] = class_name.strip()
        if ascendancy:
            applied_filters["ascendancy"] = (
                asc_inc[0] if (len(asc_inc) == 1 and not asc_exc) else ascendancy.strip()
            )
        if skill:
            applied_filters["skill"] = skill.strip()
        if support_gem:
            applied_filters["support_gem"] = support_gem.strip()
        if unique_item:
            applied_filters["unique_item"] = unique_item.strip()
        if excl_terms:
            applied_filters["exclude_uniques"] = ", ".join(excl_terms)
        if eff_max_uniques is not None:
            applied_filters["max_uniques"] = int(eff_max_uniques)
        if keystone:
            applied_filters["keystone"] = keystone.strip()
        if weapon_type:
            applied_filters["weapon_type"] = weapon_type.strip()
        if eff_min_dps is not None:
            applied_filters["min_dps"] = float(eff_min_dps)
        if eff_min_ehp is not None:
            applied_filters["min_ehp"] = float(eff_min_ehp)

        matched: list[dict[str, Any]] = []
        for raw_rec in corpus:
            cls_candidates = [str(raw_rec.get("class_name") or "")]
            if raw_rec.get("ascendancy_name"):
                cls_candidates.append(str(raw_rec["ascendancy_name"]))
            if cls_inc and not any(
                self._matches_term_in_list(t, cls_candidates, asc_vocab) for t in cls_inc
            ):
                continue
            if cls_exc and any(
                self._matches_term_in_list(t, cls_candidates, asc_vocab) for t in cls_exc
            ):
                continue

            asc_val = str(raw_rec.get("ascendancy_name") or "")
            if asc_inc and not any(
                self._matches_term_in_list(t, [asc_val], asc_vocab) for t in asc_inc
            ):
                continue
            if asc_exc and any(
                self._matches_term_in_list(t, [asc_val], asc_vocab) for t in asc_exc
            ):
                continue

            sk_cands = list(raw_rec.get("active_skills") or [])
            if raw_rec.get("main_skill") and raw_rec["main_skill"] not in sk_cands:
                sk_cands.insert(0, raw_rec["main_skill"])
            if sk_inc and not any(
                self._matches_term_in_list(t, sk_cands, skill_vocab) for t in sk_inc
            ):
                continue
            if sk_exc and any(
                self._matches_term_in_list(t, sk_cands, skill_vocab) for t in sk_exc
            ):
                continue

            rec_supps = raw_rec.get("support_gems") or ()
            if sup_inc and not any(
                self._matches_term_in_list(t, rec_supps, supp_vocab) for t in sup_inc
            ):
                continue
            if sup_exc and any(
                self._matches_term_in_list(t, rec_supps, supp_vocab) for t in sup_exc
            ):
                continue

            rec_uniques = raw_rec.get("unique_items") or ()
            if uniq_inc and not any(
                self._matches_term_in_list(t, rec_uniques, uniq_vocab) for t in uniq_inc
            ):
                continue
            if excl_terms and any(
                self._matches_term_in_list(ex_u, rec_uniques, uniq_vocab) for ex_u in excl_terms
            ):
                continue
            if eff_max_uniques is not None and len(rec_uniques) > eff_max_uniques:
                continue

            ks_and_notables = list(raw_rec.get("keystones") or []) + list(
                raw_rec.get("ascendancy_notables") or []
            )
            if ks_inc and not any(
                self._matches_term_in_list(t, ks_and_notables, ks_vocab) for t in ks_inc
            ):
                continue
            if ks_exc and any(
                self._matches_term_in_list(t, ks_and_notables, ks_vocab) for t in ks_exc
            ):
                continue

            rec_weapons = raw_rec.get("weapon_types") or ()
            rec_keystones = raw_rec.get("keystones") or ()
            if weap_inc and not any(
                self._matches_weapon(t, rec_weapons, rec_keystones) for t in weap_inc
            ):
                continue
            if weap_exc and any(
                self._matches_weapon(t, rec_weapons, rec_keystones) for t in weap_exc
            ):
                continue

            dew_dps = raw_rec.get("dewatered_dps")
            real_dps = (
                float(dew_dps)
                if (dew_dps is not None and float(dew_dps) > 0.0)
                else float(raw_rec.get("effective_dps") or 0.0)
            )
            dew_ehp = raw_rec.get("dewatered_ehp")
            real_ehp = (
                float(dew_ehp)
                if (dew_ehp is not None and float(dew_ehp) > 0.0)
                else float(raw_rec.get("total_ehp") or 0.0)
            )
            if eff_min_dps is not None and float(raw_rec.get("effective_dps") or 0.0) < eff_min_dps:
                continue
            if eff_min_ehp is not None and float(raw_rec.get("total_ehp") or 0.0) < eff_min_ehp:
                continue

            mob_tags, chase_uniques = _derive_mobility_and_chase(raw_rec)
            asc_key = _norm_token(asc_val or str(raw_rec.get("class_name") or ""))
            asc_share = live_asc_share.get(asc_key)
            ms_val = raw_rec.get("movement_speed_mod")
            ms_f = float(ms_val) if (ms_val is not None and float(ms_val) > 0.0) else 1.0
            phys_mh = raw_rec.get("phys_max_hit")
            chaos_mh = raw_rec.get("chaos_max_hit")
            chaos_imm = bool(raw_rec.get("chaos_immune", False))

            # Apply intent preset gates
            has_fast_move = ms_f >= 1.25 or (
                ms_f >= 1.15
                and bool(
                    set(mob_tags)
                    & {
                        "blink_teleport",
                        "dash_melee",
                        "qotf_speed",
                        "onslaught_speed",
                        "herald_chain",
                        "auto_clear",
                    }
                )
            )
            if norm_preset == "sleeper":
                if not cls_inc and not asc_inc and asc_share is not None and asc_share >= 5.0:
                    continue
                if eff_min_dps is None and real_dps < 400_000.0:
                    continue
                if eff_min_ehp is None and real_ehp < 15_000.0:
                    continue
                if not has_fast_move:
                    continue
            elif norm_preset == "zoom_mapper":
                if not has_fast_move:
                    continue
            elif norm_preset == "boss_killer":
                if eff_min_ehp is None and real_ehp < 22_000.0:
                    continue
                if phys_mh is not None and float(phys_mh) < 5_500.0:
                    continue
            elif norm_preset == "hc_tank":
                if phys_mh is not None and float(phys_mh) < 6_500.0:
                    continue
                if not chaos_imm and chaos_mh is not None and float(chaos_mh) < 6_000.0:
                    continue

            ms_main = raw_rec.get("main_skill")
            c_skill_cnt = corpus_skill_counts.get(_norm_token(str(ms_main)), 1) if ms_main else 1
            s_score = _compute_sleeper_score(
                raw_rec,
                ascendancy_share_pct=asc_share,
                corpus_skill_count=c_skill_cnt,
                mobility_tags=mob_tags,
                t0_chase_uniques=chase_uniques,
            )

            enriched = dict(raw_rec)
            enriched["ascendancy_share_pct"] = asc_share
            enriched["corpus_skill_count"] = c_skill_cnt
            enriched["mobility_tags"] = mob_tags
            enriched["t0_chase_uniques"] = chase_uniques
            enriched["sleeper_score"] = s_score
            matched.append(enriched)

        # Facet aggregations across all matched builds
        denom = len(matched)
        asc_counts: dict[str, int] = defaultdict(int)
        skill_counts: dict[str, int] = defaultdict(int)
        supp_counts: dict[str, int] = defaultdict(int)
        uniq_counts: dict[str, int] = defaultdict(int)
        ks_counts: dict[str, int] = defaultdict(int)
        weap_counts: dict[str, int] = defaultdict(int)

        for r in matched:
            asc_label = str(r.get("ascendancy_name") or r.get("class_name") or "Unknown")
            asc_counts[asc_label] += 1
            ms = r.get("main_skill") or (r["active_skills"][0] if r.get("active_skills") else None)
            if ms:
                skill_counts[str(ms)] += 1
            for g in set(r.get("support_gems") or ()):
                supp_counts[str(g)] += 1
            for u in set(r.get("unique_items") or ()):
                uniq_counts[str(u)] += 1
            for k in set(r.get("keystones") or ()):
                ks_counts[str(k)] += 1
            for w in set(r.get("weapon_types") or ()):
                weap_counts[str(w)] += 1

        q_skill_norm = _norm_token(sk_inc[0]) if len(sk_inc) == 1 else ""
        q_uniq_norm = _norm_token(uniq_inc[0]) if len(uniq_inc) == 1 else ""
        t0_norms = {
            _norm_token(u): u for u in _T0_TRADE_CHASE_UNIQUES if _norm_token(u) != q_uniq_norm
        }

        if primary_snap.ssf and denom > 0:
            for t0_norm, t0_name in t0_norms.items():
                co_cnt = sum(
                    1
                    for r in matched
                    if any(_norm_token(u) == t0_norm for u in (r.get("unique_items") or ()))
                )
                if co_cnt >= max(2, int(math.ceil(denom * 0.80))):
                    extra_tip = ""
                    if t0_name == "Temporalis" and q_uniq_norm == "choirofthestorm":
                        extra_tip = (
                            " In SSF without 'Temporalis', trigger Choir of the Storm's "
                            "Lightning Bolt via high-crit fast-hitting spells (Spark / Eye of "
                            "Winter / Ball Lightning) instead of 0-cooldown Blink."
                        )
                    notes.append(
                        f"SSF Chase Co-Dependency Warning: {co_cnt}/{denom} "
                        f"({(co_cnt / denom) * 100.0:.0f}%) matched corpus builds also equip "
                        f"{t0_name!r}.{extra_tip}"
                    )

        def _sort_key(r: Mapping[str, Any]) -> tuple[float, ...]:
            # When filtering by skill, prefer builds whose primary main_skill matches!
            main_bonus = (
                1.0
                if (q_skill_norm and q_skill_norm in _norm_token(str(r.get("main_skill") or "")))
                else 0.0
            )
            ssf_prefix: tuple[float, ...] = ()
            if eff_ssf_first:
                r_uniques = list(r.get("unique_items") or ())
                t0_count = sum(1 for u in r_uniques if _norm_token(u) in t0_norms)
                ssf_prefix = (-float(t0_count), -float(len(r_uniques)))

            r_dew_dps = r.get("dewatered_dps")
            r_real_dps = (
                float(r_dew_dps)
                if (r_dew_dps is not None and float(r_dew_dps) > 0.0)
                else float(r.get("effective_dps") or 0.0)
            )
            r_dew_ehp = r.get("dewatered_ehp")
            r_real_ehp = (
                float(r_dew_ehp)
                if (r_dew_ehp is not None and float(r_dew_ehp) > 0.0)
                else float(r.get("total_ehp") or 0.0)
            )

            if eff_sort == "sleeper":
                base_k: tuple[float, ...] = (
                    main_bonus,
                    float(r.get("sleeper_score") or 0.0),
                    r_real_dps,
                )
            elif eff_sort == "mobility":
                ms_v = float(r.get("movement_speed_mod") or 1.0)
                sk_ms = float(r.get("movement_speed_while_using_skill") or 0.0)
                m_tags = r.get("mobility_tags") or ()
                mob_rank = ms_v * (1.0 + 0.35 * min(sk_ms, 1.5)) + 0.12 * len(m_tags)
                base_k = (main_bonus, mob_rank, r_real_dps)
            elif eff_sort == "ehp":
                base_k = (
                    main_bonus,
                    r_real_ehp if norm_preset == "hc_tank" else float(r.get("total_ehp") or 0.0),
                    r_real_dps,
                )
            elif eff_sort == "level":
                rnk = int(r["rank_within_ascendancy"]) if r.get("rank_within_ascendancy") else 999
                base_k = (main_bonus, float(r.get("level") or 0), -float(rnk))
            elif eff_sort == "es":
                base_k = (
                    main_bonus,
                    float(r.get("energy_shield") or 0.0),
                    float(r.get("total_ehp") or 0.0),
                )
            elif eff_sort == "life":
                base_k = (
                    main_bonus,
                    float(r.get("life") or 0.0),
                    float(r.get("total_ehp") or 0.0),
                )
            else:
                dps_metric = (
                    r_real_dps
                    if norm_preset == "boss_killer"
                    else float(r.get("effective_dps") or 0.0)
                )
                base_k = (
                    main_bonus,
                    dps_metric,
                    float(r.get("total_ehp") or 0.0),
                )
            return (*ssf_prefix, *base_k)

        matched.sort(key=_sort_key, reverse=True)
        eff_limit = max(1, min(limit, 100))
        top_records = [dict(r) for r in matched[:eff_limit]]

        # Optional live refresh of top N rows via HTTP 200 /poe2/pob/raw/overview/code
        if refresh_live_top > 0 and top_records:
            ov_slug = snap_report.modes["sc_trade"].snapshot_name or "forbidden-rites"
            n_refresh = min(refresh_live_top, len(top_records))
            for i in range(n_refresh):
                cand = top_records[i]
                try:
                    live_code = self._cached_get_text(
                        self.char_pob_raw_url,
                        params={
                            "overview": ov_slug,
                            "type": "exp",
                            "account": cand["account"],
                            "name": cand["character"],
                        },
                        force_refresh=force_refresh,
                    )
                    insp = inspect_pob2_xml_or_code(live_code)
                    cand.update(
                        {
                            "class_name": insp.class_name,
                            "ascendancy_name": insp.ascendancy_name,
                            "level": insp.level,
                            "main_skill": insp.main_skill or cand.get("main_skill"),
                            "active_skills": insp.active_skills,
                            "support_gems": insp.support_gems,
                            "unique_items": insp.unique_items,
                            "weapon_types": insp.weapon_types,
                            "keystones": insp.keystones,
                            "ascendancy_notables": insp.ascendancy_notables,
                            "anoints": insp.anoints,
                            "total_dps": insp.total_dps,
                            "combined_dps": insp.combined_dps,
                            "full_dps": insp.full_dps,
                            "effective_dps": insp.effective_dps,
                            "total_ehp": insp.total_ehp,
                            "life": insp.life,
                            "energy_shield": insp.energy_shield,
                            "mana": insp.mana,
                            "spirit": insp.spirit,
                            "pob_code": live_code,
                            "source": "poeninja_live_pob",
                            "live_refreshed": True,
                        }
                    )
                except httpx.HTTPStatusError as exc:
                    notes.append(
                        f"Live refresh skipped for {cand['account']}/{cand['character']} "
                        f"(HTTP {exc.response.status_code}); retained corpus snapshot."
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    notes.append(
                        f"Live refresh skipped for {cand['account']}/{cand['character']} "
                        f"({type(exc).__name__}); retained corpus snapshot."
                    )

        entries = [self._record_to_entry(r, include_pob_code=include_pob_code) for r in top_records]

        return NinjaBuildSearchReport(
            patch=POE2_PATCH,
            league=primary_snap.league_name,
            snapshot=primary_snap,
            corpus_total_builds=len(corpus),
            matched_builds=denom,
            returned_builds=len(entries),
            preset=norm_preset,
            sort_by=eff_sort,
            applied_filters=applied_filters,
            top_ascendancies=_top_facets(asc_counts, denom),
            top_skills=_top_facets(skill_counts, denom),
            top_supports=_top_facets(supp_counts, denom),
            top_uniques=_top_facets(uniq_counts, denom),
            top_keystones=_top_facets(ks_counts, denom),
            top_weapons=_top_facets(weap_counts, denom),
            dps_distribution=_stat_summary(float(r.get("effective_dps") or 0.0) for r in matched),
            ehp_distribution=_stat_summary(float(r.get("total_ehp") or 0.0) for r in matched),
            life_distribution=_stat_summary(float(r.get("life") or 0.0) for r in matched),
            es_distribution=_stat_summary(float(r.get("energy_shield") or 0.0) for r in matched),
            builds=entries,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # 4. Live Character & Multi-Site PoB2 Share URL Import -> Pob2Bridge
    # ------------------------------------------------------------------

    def _resolve_overview_slug(
        self, league_or_slug: str, *, force_refresh: bool = False
    ) -> tuple[str, str, str]:
        """Resolve `(league_display_name, league_url_slug, snapshot_name_slug)`."""
        snap_rep = self.get_snapshot_state(league_or_slug, force_refresh=force_refresh)
        p = snap_rep.primary
        return (
            p.league_name,
            p.league_url,
            p.snapshot_name or re.sub(r"[^a-z0-9]+", "-", p.league_name.lower()).strip("-"),
        )

    def _find_corpus_fallback(self, account: str, character: str) -> dict[str, Any] | None:
        _, corpus = self._ensure_corpus_loaded()
        acct_low = _normalize_account_tag(account).lower()
        char_low = character.strip().lower()
        for rec in corpus:
            if (
                str(rec.get("account") or "").lower() == acct_low
                and str(rec.get("character") or "").lower() == char_low
            ):
                return rec
        return None

    def _parse_character_target(
        self,
        account_or_url: str,
        character: str | None = None,
        *,
        default_league: str = DEFAULT_LEAGUE,
    ) -> tuple[str, str, str, str | None]:
        """Parse `(account, character, league_hint, url_timemachine)` from URL or pair."""
        raw_input = account_or_url.strip()
        if not raw_input:
            raise ValueError("account_or_url cannot be empty")
        self._assert_not_protected_url(raw_input)

        parsed_url = urlparse(raw_input) if "://" in raw_input else None
        clean_url_no_qs = (
            f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            if parsed_url is not None
            else raw_input
        )
        url_tm: str | None = None
        if parsed_url is not None and parsed_url.query:
            qs = parse_qs(parsed_url.query)
            tm_vals = qs.get("timemachine") or qs.get("timeMachine")
            if tm_vals and tm_vals[0].strip():
                url_tm = tm_vals[0].strip()

        m_char = _NINJA_CHAR_URL_RE.match(clean_url_no_qs)
        m_prof = _NINJA_PROFILE_URL_RE.match(clean_url_no_qs) if not m_char else None
        if m_char:
            return (
                _normalize_account_tag(m_char.group(2)),
                unquote(m_char.group(3)).strip(),
                unquote(m_char.group(1)),
                url_tm,
            )
        if m_prof:
            return (
                _normalize_account_tag(m_prof.group(1)),
                unquote(m_prof.group(2)).strip(),
                default_league,
                url_tm,
            )
        if "://" not in raw_input:
            if character is None and "/" in raw_input:
                acct_part, char_part = raw_input.split("/", 1)
                return (
                    _normalize_account_tag(acct_part),
                    unquote(char_part).strip(),
                    default_league,
                    None,
                )
            if not character or not character.strip():
                raise ValueError(
                    "character name is required when passing an account name instead of a URL."
                )
            return (
                _normalize_account_tag(raw_input),
                unquote(character).strip(),
                default_league,
                None,
            )
        raise ValueError(
            f"Expected a poe.ninja character URL or (account, character) pair, got {raw_input!r}."
        )

    def _try_fetch_char_pob(
        self,
        *,
        ov_slug: str,
        account: str,
        character: str,
        timemachine: str | None,
        force_refresh: bool = False,
    ) -> tuple[str | None, int]:
        """Return `(pob_code_or_none, http_status_code)` with negative 404 memoization."""
        tm_key = (timemachine or "current").lower()
        neg_key = (ov_slug.lower(), account.lower(), character.lower(), tm_key)
        if not force_refresh and neg_key in self._missing_tm_cache:
            return None, self._missing_tm_cache[neg_key]

        params: dict[str, str] = {
            "overview": ov_slug,
            "type": "exp",
            "account": account,
            "name": character,
        }
        if timemachine:
            params["timemachine"] = timemachine

        try:
            code = self._cached_get_text(
                self.char_pob_raw_url,
                params=params,
                pace_on_miss=True,
                force_refresh=force_refresh,
            )
            self._missing_tm_cache.pop(neg_key, None)
            return code, 200
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                self._missing_tm_cache[neg_key] = 404
                return None, 404
            raise

    def _discover_nearest_timemachine(
        self,
        *,
        ov_slug: str,
        account: str,
        character: str,
        requested_tm: str,
        sorted_labels: Sequence[str],
        max_probes: int = 5,
        force_refresh: bool = False,
    ) -> tuple[str | None, str | None]:
        """Binary-search chronological snapshots after `requested_tm` to find the earliest
        available historical snapshot without hammering poe.ninja.
        """
        req_age = _time_machine_age_hours(requested_tm)
        forward_all = [
            lbl
            for lbl in sorted_labels
            if lbl.lower() != requested_tm.lower() and _time_machine_age_hours(lbl) > req_age
        ]
        # Prefer weekly milestones when probing forward from a week-* request
        if requested_tm.lower().startswith("week-"):
            week_forward = [lbl for lbl in forward_all if lbl.lower().startswith("week-")]
            candidates = week_forward if week_forward else forward_all
        else:
            candidates = forward_all

        remaining = max(1, max_probes)
        if candidates:
            # First check the latest historical snapshot to confirm the character appeared by then
            last_lbl = candidates[-1]
            last_code, _ = self._try_fetch_char_pob(
                ov_slug=ov_slug,
                account=account,
                character=character,
                timemachine=last_lbl,
                force_refresh=force_refresh,
            )
            remaining -= 1
            if last_code is not None:
                best_lbl = last_lbl
                best_code = last_code
                lo, hi = 0, len(candidates) - 2
                while lo <= hi and remaining > 0:
                    mid = (lo + hi) // 2
                    cand_lbl = candidates[mid]
                    code, _ = self._try_fetch_char_pob(
                        ov_slug=ov_slug,
                        account=account,
                        character=character,
                        timemachine=cand_lbl,
                        force_refresh=force_refresh,
                    )
                    remaining -= 1
                    if code is not None:
                        best_lbl = cand_lbl
                        best_code = code
                        hi = mid - 1
                    else:
                        lo = mid + 1
                return best_lbl, best_code

        # Check preceding snapshot in case character left ladder before requested_tm
        earlier = [
            lbl
            for lbl in sorted_labels
            if lbl.lower() != requested_tm.lower() and _time_machine_age_hours(lbl) < req_age
        ]
        if earlier and remaining > 0:
            prev_lbl = earlier[-1]
            prev_code, _ = self._try_fetch_char_pob(
                ov_slug=ov_slug,
                account=account,
                character=character,
                timemachine=prev_lbl,
                force_refresh=force_refresh,
            )
            if prev_code is not None:
                return prev_lbl, prev_code

        return None, None

    def probe_character_timeline(
        self,
        account_or_url: str,
        character: str | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        candidate_labels: Sequence[str] | None = None,
        include_current: bool = True,
        include_pob_code: bool = False,
        max_probes: int = 7,
        force_refresh: bool = False,
    ) -> CharacterTimelineReport:
        """Probe a character's historical `poe.ninja` time-machine snapshots (`week-1` .. `current`)
        using polite binary-search milestone discovery (`<= max_probes` HTTP calls) so callers can
        find the exact earliest & latest snapshots without crashing on HTTP 404.
        """
        acct, char_name, league_hint, _ = self._parse_character_target(
            account_or_url, character, default_league=league
        )
        snap_rep = self.get_snapshot_state(league_hint, force_refresh=force_refresh)
        primary = snap_rep.primary
        ov_slug = primary.snapshot_name or re.sub(
            r"[^a-z0-9]+", "-", primary.league_name.lower()
        ).strip("-")
        all_labels = sort_time_machine_labels(primary.time_machine_labels)
        notes = list(snap_rep.notes)

        points_by_label: dict[str, CharacterTimelinePoint] = {}
        remaining = max(1, max_probes)

        def _probe(lbl: str | None) -> CharacterTimelinePoint:
            nonlocal remaining
            key = lbl or "current"
            if key in points_by_label:
                return points_by_label[key]
            remaining -= 1
            code, status = self._try_fetch_char_pob(
                ov_slug=ov_slug,
                account=acct,
                character=char_name,
                timemachine=lbl,
                force_refresh=force_refresh,
            )
            if code is None:
                pt = CharacterTimelinePoint(label=key, available=False, http_status=status)
            else:
                insp = inspect_pob2_xml_or_code(code)
                pt = CharacterTimelinePoint(
                    label=key,
                    available=True,
                    http_status=200,
                    level=insp.level,
                    class_name=insp.class_name,
                    ascendancy_name=insp.ascendancy_name,
                    main_skill=insp.main_skill,
                    effective_dps=insp.effective_dps,
                    total_ehp=insp.total_ehp,
                    unique_items=insp.unique_items,
                    keystones=insp.keystones,
                    pob_code=code if include_pob_code else None,
                )
            points_by_label[key] = pt
            return pt

        if candidate_labels is not None:
            ordered_cands = sort_time_machine_labels(
                [lbl for lbl in candidate_labels if lbl.strip().lower() != "current"]
            )
            for lbl in ordered_cands:
                if remaining <= 0:
                    break
                _probe(lbl)
            if (
                include_current or any(c.strip().lower() == "current" for c in candidate_labels)
            ) and remaining > 0:
                _probe(None)
        else:
            cur_pt = _probe(None) if (include_current and remaining > 0) else None
            milestones = [lbl for lbl in all_labels if lbl.lower().startswith("week-")] or list(
                all_labels
            )
            if milestones and remaining > 0:
                first_pt = _probe(milestones[0])
                if first_pt.available:
                    # Sample midpoint when character existed from the earliest milestone
                    if len(milestones) >= 3 and remaining > 0:
                        _probe(milestones[len(milestones) // 2])
                elif len(milestones) >= 2 and remaining > 0:
                    # Binary-search milestones[1:] to pinpoint exact first week on ladder
                    last_pt = _probe(milestones[-1])
                    if last_pt.available:
                        lo, hi = 1, len(milestones) - 2
                        while lo <= hi and remaining > 0:
                            mid = (lo + hi) // 2
                            m_pt = _probe(milestones[mid])
                            if m_pt.available:
                                hi = mid - 1
                            else:
                                lo = mid + 1
            if cur_pt is not None and not cur_pt.available:
                notes.append(
                    f"Live 'current' snapshot returned HTTP 404 for {acct}/{char_name} "
                    "(character may have left ladder, renamed, or gone private)."
                )

        ordered_points = sorted(
            points_by_label.values(),
            key=lambda p: (_time_machine_age_hours(p.label), p.label.lower()),
        )
        avail_points = [p for p in ordered_points if p.available]
        avail_labels = [p.label for p in avail_points]
        earliest_lbl = avail_labels[0] if avail_labels else None
        latest_lbl = avail_labels[-1] if avail_labels else None

        stepping_stone_label: str | None = None
        stepping_stone_note: str | None = None
        if len(avail_points) >= 2:
            t0_norms = {_norm_token(u) for u in _T0_TRADE_CHASE_UNIQUES}
            cur_pt = next(
                (p for p in avail_points if p.label.lower() == "current"),
                avail_points[-1],
            )
            earlier_pts = [p for p in avail_points if p is not cur_pt]
            if earlier_pts:

                def _t0_list(pt: CharacterTimelinePoint) -> list[str]:
                    return [u for u in pt.unique_items if _norm_token(u) in t0_norms]

                best_step = min(
                    earlier_pts,
                    key=lambda p: (
                        len(_t0_list(p)),
                        len(p.unique_items),
                        -int(p.level or 0),
                    ),
                )
                stepping_stone_label = best_step.label
                t0_step = _t0_list(best_step)
                t0_cur = _t0_list(cur_pt)
                if len(t0_step) < len(t0_cur):
                    stepping_stone_note = (
                        f"Use {best_step.label!r} (Lv{best_step.level or '?'}) as budget stepping "
                        f"stone: {len(t0_step)} T0 chase uniques vs {len(t0_cur)} in "
                        f"{cur_pt.label!r} ({', '.join(t0_cur)})."
                    )
                else:
                    stepping_stone_note = (
                        f"Budget stepping stone {best_step.label!r} "
                        f"(Lv{best_step.level or '?'}, {len(best_step.unique_items)} uniques vs "
                        f"{len(cur_pt.unique_items)} in {cur_pt.label!r})."
                    )

        if earliest_lbl and ordered_points and not ordered_points[0].available:
            notes.append(
                f"Character {acct}/{char_name} was not yet indexed at {ordered_points[0].label!r}; "
                f"earliest available snapshot is {earliest_lbl!r}."
            )

        return CharacterTimelineReport(
            account=acct,
            character=char_name,
            league=primary.league_name,
            overview_slug=ov_slug,
            available_time_machine_labels=all_labels,
            probed_points=ordered_points,
            earliest_available_label=earliest_lbl,
            latest_available_label=latest_lbl,
            stepping_stone_label=stepping_stone_label,
            stepping_stone_note=stepping_stone_note,
            notes=notes,
        )

    def fetch_character(
        self,
        account_or_url: str,
        character: str | None = None,
        *,
        league: str = DEFAULT_LEAGUE,
        timemachine: str | None = None,
        fallback_nearest_timemachine: bool = True,
        load_into_pob: bool = False,
        slot: str = "main",
        audit: bool = False,
        force_refresh: bool = False,
    ) -> NinjaCharacterImport:
        """Fetch a PoE2 character or shared build (`poe.ninja`, `poe2db`, `maxroll`, `pobb.in`),
        inspect its `<PathOfBuilding2>` XML, and optionally load + audit it inside `Pob2Bridge`.
        """
        raw_input = account_or_url.strip()
        if not raw_input:
            raise ValueError("account_or_url cannot be empty")
        self._assert_not_protected_url(raw_input)

        notes: list[str] = []
        source_kind: Literal[
            "poeninja_live_pob",
            "poeninja_share_pob",
            "poe2db_pob",
            "maxroll_pob",
            "pobbin_pob",
            "poeninja_archived_corpus",
        ]
        resolved_url: str
        resolved_acct: str | None = None
        resolved_char: str | None = None
        resolved_league: str | None = None
        resolved_tm: str | None = timemachine.strip() if timemachine else None
        ninja_url: str | None = None
        pob_code: str | None = None

        # Strip query string before regex path matching while extracting ?timemachine=
        parsed_url = urlparse(raw_input) if "://" in raw_input else None
        clean_url_no_qs = (
            f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            if parsed_url is not None
            else raw_input
        )
        if parsed_url is not None and parsed_url.query and not resolved_tm:
            qs = parse_qs(parsed_url.query)
            tm_vals = qs.get("timemachine") or qs.get("timeMachine")
            if tm_vals and tm_vals[0].strip():
                resolved_tm = tm_vals[0].strip()

        m_char = _NINJA_CHAR_URL_RE.match(clean_url_no_qs)
        m_prof = _NINJA_PROFILE_URL_RE.match(clean_url_no_qs) if not m_char else None
        m_ninja_share = _NINJA_POB_SHARE_RE.match(clean_url_no_qs)
        m_poe2db = _POE2DB_POB_RE.match(clean_url_no_qs)
        m_maxroll = _MAXROLL_POB_RE.match(clean_url_no_qs)
        m_pobbin = _POBBIN_POB_RE.match(clean_url_no_qs)

        if m_char or m_prof or "://" not in raw_input:
            resolved_acct, resolved_char, league_hint, _ = self._parse_character_target(
                raw_input, character, default_league=league
            )
            snap_rep = self.get_snapshot_state(league_hint, force_refresh=force_refresh)
            p_snap = snap_rep.primary
            disp_league = p_snap.league_name
            league_url_slug = p_snap.league_url
            ov_slug = p_snap.snapshot_name or re.sub(
                r"[^a-z0-9]+", "-", p_snap.league_name.lower()
            ).strip("-")
            resolved_league = disp_league
            ninja_url = (
                f"{POENINJA_ORIGIN}/poe2/builds/{league_url_slug}/character/"
                f"{quote(resolved_acct, safe='')}/{quote(resolved_char, safe='')}"
            )

            def _build_char_url(tm_val: str | None) -> str:
                p: dict[str, str] = {
                    "overview": ov_slug,
                    "type": "exp",
                    "account": resolved_acct,
                    "name": resolved_char,
                }
                if tm_val:
                    p["timemachine"] = tm_val
                q_str = "&".join(f"{k}={quote(v, safe='')}" for k, v in p.items())
                return f"{self.char_pob_raw_url}?{q_str}"

            resolved_url = _build_char_url(resolved_tm)
            source_kind = "poeninja_live_pob"

            code_try, status_try = self._try_fetch_char_pob(
                ov_slug=ov_slug,
                account=resolved_acct,
                character=resolved_char,
                timemachine=resolved_tm,
                force_refresh=force_refresh,
            )
            if code_try is not None:
                pob_code = code_try
            elif status_try == 404 and resolved_tm:
                if not fallback_nearest_timemachine:
                    raise LookupError(
                        f"Character {resolved_acct}/{resolved_char} has no snapshot at "
                        f"timemachine={resolved_tm!r} on overview {ov_slug!r} (HTTP 404). "
                        "Use probe_character_timeline() or pass fallback_nearest_timemachine=True."
                    )
                orig_tm = resolved_tm
                nearest_lbl, nearest_code = self._discover_nearest_timemachine(
                    ov_slug=ov_slug,
                    account=resolved_acct,
                    character=resolved_char,
                    requested_tm=orig_tm,
                    sorted_labels=p_snap.time_machine_labels,
                    force_refresh=force_refresh,
                )
                if nearest_code is not None and nearest_lbl is not None:
                    pob_code = nearest_code
                    resolved_tm = nearest_lbl
                    resolved_url = _build_char_url(nearest_lbl)
                    notes.append(
                        f"Requested timemachine={orig_tm!r} returned HTTP 404 (character not yet "
                        f"on ladder); automatically resolved nearest available snapshot "
                        f"timemachine={nearest_lbl!r}."
                    )
                else:
                    cur_code, _ = self._try_fetch_char_pob(
                        ov_slug=ov_slug,
                        account=resolved_acct,
                        character=resolved_char,
                        timemachine=None,
                        force_refresh=force_refresh,
                    )
                    if cur_code is not None:
                        pob_code = cur_code
                        resolved_tm = None
                        resolved_url = _build_char_url(None)
                        notes.append(
                            f"Requested timemachine={orig_tm!r} returned HTTP 404 and no "
                            "historical snapshots matched; served current live snapshot instead."
                        )
                    else:
                        raise LookupError(
                            f"Character {resolved_acct}/{resolved_char} not found at "
                            f"timemachine={orig_tm!r} or nearby snapshots on {ov_slug!r} "
                            "(HTTP 404)."
                        )
            else:
                fallback = self._find_corpus_fallback(resolved_acct, resolved_char)
                if fallback is not None and fallback.get("pob_code"):
                    pob_code = str(fallback["pob_code"])
                    source_kind = "poeninja_archived_corpus"
                    notes.append(
                        f"Live poe.ninja PoB endpoint returned HTTP 404 for "
                        f"{resolved_acct}/{resolved_char} (private, renamed, or off-ladder); "
                        f"served archived {DEFAULT_LEAGUE} {POE2_PATCH} corpus snapshot."
                    )
                else:
                    raise LookupError(
                        f"Character {resolved_acct}/{resolved_char} not found on poe.ninja "
                        f"overview {ov_slug!r} (HTTP 404)."
                    )
        elif m_ninja_share:
            paste_id = m_ninja_share.group(1)
            resolved_url = f"{POENINJA_SHARE_POB_RAW_BASE_URL}/{paste_id}"
            source_kind = "poeninja_share_pob"
            pob_code = self._cached_get_text(
                resolved_url, pace_on_miss=True, force_refresh=force_refresh
            )
        elif m_poe2db:
            paste_id = m_poe2db.group(1)
            resolved_url = f"https://poe2db.tw/pob/{paste_id}/raw"
            source_kind = "poe2db_pob"
            pob_code = self._cached_get_text(
                resolved_url, pace_on_miss=False, force_refresh=force_refresh
            )
        elif m_maxroll:
            paste_id = m_maxroll.group(1)
            resolved_url = f"https://maxroll.gg/poe2/api/pob/{paste_id}"
            source_kind = "maxroll_pob"
            pob_code = self._cached_get_text(
                resolved_url, pace_on_miss=False, force_refresh=force_refresh
            )
        elif m_pobbin:
            paste_id = m_pobbin.group(1)
            resolved_url = f"https://pobb.in/{paste_id}/raw"
            source_kind = "pobbin_pob"
            pob_code = self._cached_get_text(
                resolved_url, pace_on_miss=False, force_refresh=force_refresh
            )
        else:
            raise ValueError(
                f"Unsupported PoE2 character or PoB URL {raw_input!r}. Supported: "
                "poe.ninja character URLs, (account, character) pairs, poe.ninja/poe2/pob/<id>, "
                "poe2db.tw/pob/<id>, maxroll.gg/poe2/pob/<id>, and pobb.in/<id>."
            )

        assert pob_code is not None
        xml_text = decode_pob2_code_to_xml(pob_code)
        insp = inspect_pob2_xml_or_code(xml_text)

        pob_stats = None
        dewatered_audit = None
        loaded = False
        used_slot: str | None = None

        if load_into_pob or audit:
            svc = self._ensure_build_service()
            build_label = resolved_char or insp.ascendancy_name or insp.class_name
            pob_stats = svc.load_build(xml_text, slot=slot, name=build_label)
            loaded = True
            used_slot = slot
            if audit:
                dewatered_audit = svc.audit_build(slot=slot)

        return NinjaCharacterImport(
            source_kind=source_kind,
            requested_source=raw_input,
            resolved_url=resolved_url,
            account=resolved_acct,
            character=resolved_char,
            league=resolved_league,
            timemachine=resolved_tm,
            ninja_url=ninja_url,
            tree_version=insp.tree_version,
            class_name=insp.class_name,
            ascendancy_name=insp.ascendancy_name,
            level=insp.level,
            main_skill=insp.main_skill,
            active_skills=insp.active_skills,
            support_gems=insp.support_gems,
            unique_items=insp.unique_items,
            weapon_types=insp.weapon_types,
            keystones=insp.keystones,
            ascendancy_notables=insp.ascendancy_notables,
            anoints=insp.anoints,
            equipped_items=insp.equipped_items,
            skill_groups=insp.skill_groups,
            xml_stats=insp.xml_stats,
            pob_code=pob_code,
            loaded_into_pob=loaded,
            pob_slot=used_slot,
            pob_stats=pob_stats,
            dewatered_audit=dewatered_audit,
            notes=notes,
        )
