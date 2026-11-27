# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Runtime SQLite build store with ETag-based remote index sync and write-through caching."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from poe2brain.config import DEFAULT_LEAGUE, POE2_PATCH
from poe2brain.http import Poe2HttpClient
from poe2brain.models.ninja import BuildIndexSyncReport

DEFAULT_BUILD_INDEX_SYNC_TTL_SECONDS: float = 43200.0  # 12 hours

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS builds (
    league_slug TEXT NOT NULL,
    mode TEXT NOT NULL,
    account_norm TEXT NOT NULL,
    character_norm TEXT NOT NULL,
    account TEXT NOT NULL,
    character TEXT NOT NULL,
    league TEXT NOT NULL,
    rank_within_ascendancy INTEGER,
    experience INTEGER,
    fetched_at TEXT,
    audit_patch TEXT,
    source TEXT NOT NULL DEFAULT 'build_index',
    live_refreshed INTEGER NOT NULL DEFAULT 0,
    class_name TEXT NOT NULL DEFAULT 'Unknown',
    ascendancy_name TEXT,
    level INTEGER NOT NULL DEFAULT 1,
    main_skill TEXT,
    active_skills_json TEXT NOT NULL DEFAULT '[]',
    support_gems_json TEXT NOT NULL DEFAULT '[]',
    unique_items_json TEXT NOT NULL DEFAULT '[]',
    weapon_types_json TEXT NOT NULL DEFAULT '[]',
    keystones_json TEXT NOT NULL DEFAULT '[]',
    ascendancy_notables_json TEXT NOT NULL DEFAULT '[]',
    anoints_json TEXT NOT NULL DEFAULT '[]',
    mobility_tags_json TEXT NOT NULL DEFAULT '[]',
    t0_chase_uniques_json TEXT NOT NULL DEFAULT '[]',
    total_dps REAL NOT NULL DEFAULT 0.0,
    combined_dps REAL NOT NULL DEFAULT 0.0,
    full_dps REAL NOT NULL DEFAULT 0.0,
    effective_dps REAL NOT NULL DEFAULT 0.0,
    total_ehp REAL NOT NULL DEFAULT 0.0,
    life REAL NOT NULL DEFAULT 0.0,
    energy_shield REAL NOT NULL DEFAULT 0.0,
    mana REAL NOT NULL DEFAULT 0.0,
    spirit INTEGER NOT NULL DEFAULT 0,
    dewatered_dps REAL,
    dps_inflation_pct REAL,
    dewatered_ehp REAL,
    phys_max_hit REAL,
    chaos_max_hit REAL,
    chaos_immune INTEGER NOT NULL DEFAULT 0,
    movement_speed_mod REAL,
    movement_speed_while_using_skill REAL,
    ninja_url TEXT NOT NULL DEFAULT '',
    pob_hash TEXT,
    pob_code TEXT,
    PRIMARY KEY (league_slug, mode, account_norm, character_norm)
);

CREATE INDEX IF NOT EXISTS idx_builds_league_mode
    ON builds (league_slug, mode);
CREATE INDEX IF NOT EXISTS idx_builds_ascendancy
    ON builds (league_slug, mode, ascendancy_name);
CREATE INDEX IF NOT EXISTS idx_builds_ident
    ON builds (account_norm, character_norm);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_LIST_FIELDS: tuple[str, ...] = (
    "active_skills",
    "support_gems",
    "unique_items",
    "weapon_types",
    "keystones",
    "ascendancy_notables",
    "anoints",
    "mobility_tags",
    "t0_chase_uniques",
)


def normalize_league_slug(league: str) -> str:
    raw = (league or DEFAULT_LEAGUE).strip()
    low = raw.lower()
    for prefix in ("hc ssf ", "hcssf ", "ssf hc ", "hc ", "hardcore ssf ", "hardcore ", "ssf "):
        if low.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    slug = _NON_ALNUM_RE.sub("", raw.lower())
    for suffix in ("hcssf", "hc", "ssf"):
        if slug.endswith(suffix) and len(slug) > len(suffix) + 3:
            slug = slug[: -len(suffix)]
            break
    return slug or _NON_ALNUM_RE.sub("", DEFAULT_LEAGUE.lower())


def compute_pob_hash(pob_code: str | None) -> str | None:
    if not pob_code or not pob_code.strip():
        return None
    return hashlib.sha256(pob_code.strip().encode("utf-8")).hexdigest()[:24]


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dump_list(val: Any) -> str:
    if not val:
        return "[]"
    return json.dumps([str(x) for x in val], ensure_ascii=False, separators=(",", ":"))


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "league_slug": row["league_slug"],
        "mode": row["mode"],
        "account": row["account"],
        "character": row["character"],
        "league": row["league"],
        "rank_within_ascendancy": row["rank_within_ascendancy"],
        "experience": row["experience"],
        "fetched_at": row["fetched_at"],
        "audit_patch": row["audit_patch"],
        "source": row["source"],
        "live_refreshed": bool(row["live_refreshed"]),
        "class_name": row["class_name"],
        "ascendancy_name": row["ascendancy_name"],
        "level": int(row["level"] or 1),
        "main_skill": row["main_skill"],
        "total_dps": float(row["total_dps"] or 0.0),
        "combined_dps": float(row["combined_dps"] or 0.0),
        "full_dps": float(row["full_dps"] or 0.0),
        "effective_dps": float(row["effective_dps"] or 0.0),
        "total_ehp": float(row["total_ehp"] or 0.0),
        "life": float(row["life"] or 0.0),
        "energy_shield": float(row["energy_shield"] or 0.0),
        "mana": float(row["mana"] or 0.0),
        "spirit": int(row["spirit"] or 0),
        "dewatered_dps": row["dewatered_dps"],
        "dps_inflation_pct": row["dps_inflation_pct"],
        "dewatered_ehp": row["dewatered_ehp"],
        "phys_max_hit": row["phys_max_hit"],
        "chaos_max_hit": row["chaos_max_hit"],
        "chaos_immune": bool(row["chaos_immune"]),
        "movement_speed_mod": row["movement_speed_mod"],
        "movement_speed_while_using_skill": row["movement_speed_while_using_skill"],
        "ninja_url": row["ninja_url"] or "",
        "pob_hash": row["pob_hash"],
        "pob_code": row["pob_code"],
    }
    for field in _LIST_FIELDS:
        rec[field] = _load_list(row[f"{field}_json"])
    return rec


def _record_to_params(
    rec: Mapping[str, Any],
    *,
    league_slug: str,
    mode: str,
) -> tuple[Any, ...]:
    acct = str(rec.get("account") or "").strip()
    char = str(rec.get("character") or "").strip()
    pob_code = str(rec.get("pob_code") or "").strip() or None
    pob_hash = str(rec.get("pob_hash") or "").strip() or compute_pob_hash(pob_code)
    rank_val = rec.get("rank_within_ascendancy")
    exp_val = rec.get("experience")
    return (
        league_slug,
        mode,
        acct.lower(),
        char.lower(),
        acct,
        char,
        str(rec.get("league") or DEFAULT_LEAGUE),
        int(rank_val) if isinstance(rank_val, int) else None,
        int(exp_val) if isinstance(exp_val, int) else None,
        str(rec.get("fetched_at") or "") or None,
        str(rec.get("audit_patch") or POE2_PATCH),
        str(rec.get("source") or "build_index"),
        1 if rec.get("live_refreshed") else 0,
        str(rec.get("class_name") or "Unknown"),
        str(rec["ascendancy_name"]) if rec.get("ascendancy_name") else None,
        int(rec.get("level") or 1),
        str(rec["main_skill"]) if rec.get("main_skill") else None,
        _dump_list(rec.get("active_skills")),
        _dump_list(rec.get("support_gems")),
        _dump_list(rec.get("unique_items")),
        _dump_list(rec.get("weapon_types")),
        _dump_list(rec.get("keystones")),
        _dump_list(rec.get("ascendancy_notables")),
        _dump_list(rec.get("anoints")),
        _dump_list(rec.get("mobility_tags")),
        _dump_list(rec.get("t0_chase_uniques")),
        float(rec.get("total_dps") or 0.0),
        float(rec.get("combined_dps") or 0.0),
        float(rec.get("full_dps") or 0.0),
        float(rec.get("effective_dps") or 0.0),
        float(rec.get("total_ehp") or 0.0),
        float(rec.get("life") or 0.0),
        float(rec.get("energy_shield") or 0.0),
        float(rec.get("mana") or 0.0),
        int(rec.get("spirit") or 0),
        float(rec["dewatered_dps"]) if rec.get("dewatered_dps") is not None else None,
        float(rec["dps_inflation_pct"]) if rec.get("dps_inflation_pct") is not None else None,
        float(rec["dewatered_ehp"]) if rec.get("dewatered_ehp") is not None else None,
        float(rec["phys_max_hit"]) if rec.get("phys_max_hit") is not None else None,
        float(rec["chaos_max_hit"]) if rec.get("chaos_max_hit") is not None else None,
        1 if rec.get("chaos_immune") else 0,
        float(rec["movement_speed_mod"]) if rec.get("movement_speed_mod") is not None else None,
        (
            float(rec["movement_speed_while_using_skill"])
            if rec.get("movement_speed_while_using_skill") is not None
            else None
        ),
        str(rec.get("ninja_url") or ""),
        pob_hash,
        pob_code,
    )


_UPSERT_SQL = """
INSERT INTO builds (
    league_slug, mode, account_norm, character_norm,
    account, character, league, rank_within_ascendancy, experience,
    fetched_at, audit_patch, source, live_refreshed,
    class_name, ascendancy_name, level, main_skill,
    active_skills_json, support_gems_json, unique_items_json,
    weapon_types_json, keystones_json, ascendancy_notables_json,
    anoints_json, mobility_tags_json, t0_chase_uniques_json,
    total_dps, combined_dps, full_dps, effective_dps,
    total_ehp, life, energy_shield, mana, spirit,
    dewatered_dps, dps_inflation_pct, dewatered_ehp,
    phys_max_hit, chaos_max_hit, chaos_immune,
    movement_speed_mod, movement_speed_while_using_skill,
    ninja_url, pob_hash, pob_code
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
)
ON CONFLICT(league_slug, mode, account_norm, character_norm) DO UPDATE SET
    account = excluded.account,
    character = excluded.character,
    league = excluded.league,
    rank_within_ascendancy = COALESCE(
        excluded.rank_within_ascendancy,
        builds.rank_within_ascendancy
    ),
    experience = COALESCE(excluded.experience, builds.experience),
    fetched_at = CASE
        WHEN excluded.fetched_at IS NOT NULL
             AND (builds.fetched_at IS NULL OR excluded.fetched_at >= builds.fetched_at)
        THEN excluded.fetched_at
        ELSE builds.fetched_at
    END,
    audit_patch = COALESCE(excluded.audit_patch, builds.audit_patch),
    source = excluded.source,
    live_refreshed = excluded.live_refreshed,
    class_name = excluded.class_name,
    ascendancy_name = COALESCE(excluded.ascendancy_name, builds.ascendancy_name),
    level = excluded.level,
    main_skill = COALESCE(excluded.main_skill, builds.main_skill),
    active_skills_json = excluded.active_skills_json,
    support_gems_json = excluded.support_gems_json,
    unique_items_json = excluded.unique_items_json,
    weapon_types_json = excluded.weapon_types_json,
    keystones_json = excluded.keystones_json,
    ascendancy_notables_json = excluded.ascendancy_notables_json,
    anoints_json = excluded.anoints_json,
    mobility_tags_json = excluded.mobility_tags_json,
    t0_chase_uniques_json = excluded.t0_chase_uniques_json,
    total_dps = excluded.total_dps,
    combined_dps = excluded.combined_dps,
    full_dps = excluded.full_dps,
    effective_dps = excluded.effective_dps,
    total_ehp = excluded.total_ehp,
    life = excluded.life,
    energy_shield = excluded.energy_shield,
    mana = excluded.mana,
    spirit = excluded.spirit,
    dewatered_dps = COALESCE(excluded.dewatered_dps, builds.dewatered_dps),
    dps_inflation_pct = COALESCE(excluded.dps_inflation_pct, builds.dps_inflation_pct),
    dewatered_ehp = COALESCE(excluded.dewatered_ehp, builds.dewatered_ehp),
    phys_max_hit = COALESCE(excluded.phys_max_hit, builds.phys_max_hit),
    chaos_max_hit = COALESCE(excluded.chaos_max_hit, builds.chaos_max_hit),
    chaos_immune = CASE
        WHEN excluded.dewatered_dps IS NOT NULL THEN excluded.chaos_immune
        ELSE builds.chaos_immune
    END,
    movement_speed_mod = COALESCE(excluded.movement_speed_mod, builds.movement_speed_mod),
    movement_speed_while_using_skill = COALESCE(
        excluded.movement_speed_while_using_skill,
        builds.movement_speed_while_using_skill
    ),
    ninja_url = CASE
        WHEN excluded.ninja_url != '' THEN excluded.ninja_url
        ELSE builds.ninja_url
    END,
    pob_hash = COALESCE(excluded.pob_hash, builds.pob_hash),
    pob_code = COALESCE(excluded.pob_code, builds.pob_code)
"""


class BuildStore:
    """League- and mode-keyed SQLite cache of PoE2 builds with ETag remote sync."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            self._initialized = True
        return conn

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
                return str(row["value"]) if row else None
            finally:
                conn.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
                conn.commit()
            finally:
                conn.close()

    def upsert_build(
        self,
        record: Mapping[str, Any],
        *,
        league_slug: str | None = None,
        mode: str | None = None,
    ) -> bool:
        acct = str(record.get("account") or "").strip()
        char = str(record.get("character") or "").strip()
        if not acct or not char:
            return False
        lslug = league_slug or normalize_league_slug(str(record.get("league") or DEFAULT_LEAGUE))
        eff_mode = mode or str(record.get("mode") or "sc_trade")
        params = _record_to_params(record, league_slug=lslug, mode=eff_mode)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(_UPSERT_SQL, params)
                conn.commit()
                return True
            finally:
                conn.close()

    def upsert_many(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        league_slug: str | None = None,
        mode: str | None = None,
    ) -> int:
        batch: list[tuple[Any, ...]] = []
        for rec in records:
            acct = str(rec.get("account") or "").strip()
            char = str(rec.get("character") or "").strip()
            if not acct or not char:
                continue
            lslug = league_slug or normalize_league_slug(str(rec.get("league") or DEFAULT_LEAGUE))
            eff_mode = mode or str(rec.get("mode") or "sc_trade")
            batch.append(_record_to_params(rec, league_slug=lslug, mode=eff_mode))
        if not batch:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(_UPSERT_SQL, batch)
                conn.commit()
                return len(batch)
            finally:
                conn.close()

    def load_builds(
        self,
        *,
        league_slug: str | None = None,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if league_slug is not None:
            clauses.append("league_slug = ?")
            args.append(league_slug)
        if mode is not None:
            clauses.append("mode = ?")
            args.append(mode)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM builds{where}"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, args).fetchall()
                return [_row_to_record(r) for r in rows]
            finally:
                conn.close()

    def find_character(
        self,
        account: str,
        character: str,
        *,
        league_slug: str | None = None,
    ) -> dict[str, Any] | None:
        acct_n = account.strip().lower()
        char_n = character.strip().lower()
        with self._lock:
            conn = self._connect()
            try:
                if league_slug is not None:
                    row = conn.execute(
                        "SELECT * FROM builds WHERE league_slug = ? AND account_norm = ? "
                        "AND character_norm = ? ORDER BY fetched_at DESC LIMIT 1",
                        (league_slug, acct_n, char_n),
                    ).fetchone()
                    if row:
                        return _row_to_record(row)
                row = conn.execute(
                    "SELECT * FROM builds WHERE account_norm = ? AND character_norm = ? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (acct_n, char_n),
                ).fetchone()
                return _row_to_record(row) if row else None
            finally:
                conn.close()

    def count_builds(
        self,
        *,
        league_slug: str | None = None,
        mode: str | None = None,
    ) -> int:
        clauses: list[str] = []
        args: list[Any] = []
        if league_slug is not None:
            clauses.append("league_slug = ?")
            args.append(league_slug)
        if mode is not None:
            clauses.append("mode = ?")
            args.append(mode)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(f"SELECT COUNT(*) AS cnt FROM builds{where}", args).fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()

    def import_json_gz(
        self,
        bundle_path: Path,
        *,
        default_league_slug: str | None = None,
        default_mode: str = "sc_trade",
    ) -> int:
        with gzip.open(bundle_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        builds = payload.get("builds") or []
        lslug = default_league_slug or normalize_league_slug(
            str(payload.get("league") or DEFAULT_LEAGUE)
        )
        mode = str(payload.get("mode") or default_mode)
        return self.upsert_many(builds, league_slug=lslug, mode=mode)

    def sync_from_remote(
        self,
        http_client: Poe2HttpClient,
        manifest_url: str,
        *,
        league_slug: str | None = None,
        mode: str | None = None,
        ttl_seconds: float = DEFAULT_BUILD_INDEX_SYNC_TTL_SECONDS,
        force_refresh: bool = False,
        now_fn: Callable[[], float] = time.time,
    ) -> BuildIndexSyncReport:
        """Synchronize remote build index slices via ETag conditional GET into SQLite."""
        now_ts = now_fn()
        last_checked_raw = self.get_meta("last_checked_at")
        cached_updated = self.get_meta("manifest_updated_at")
        cached_etag = self.get_meta("manifest_etag")
        existing_total = self.count_builds(league_slug=league_slug, mode=mode)

        if (
            not force_refresh
            and existing_total > 0
            and last_checked_raw is not None
            and (now_ts - float(last_checked_raw)) < ttl_seconds
        ):
            return BuildIndexSyncReport(
                status="cached_fresh",
                manifest_url=manifest_url,
                store_path=str(self.db_path),
                league=league_slug,
                mode=mode,
                etag=cached_etag,
                index_updated_at=cached_updated,
                synced_builds=0,
                total_store_builds=self.count_builds(),
            )

        headers: dict[str, str] = {"Accept": "application/json"}
        if cached_etag and not force_refresh and existing_total > 0:
            headers["If-None-Match"] = cached_etag

        notes: list[str] = []
        try:
            resp = http_client.sync_client.get(
                manifest_url, headers=headers, follow_redirects=True
            )
            if resp.status_code == 304:
                self.set_meta("last_checked_at", str(now_ts))
                return BuildIndexSyncReport(
                    status="not_modified",
                    manifest_url=manifest_url,
                    store_path=str(self.db_path),
                    league=league_slug,
                    mode=mode,
                    etag=cached_etag,
                    index_updated_at=cached_updated,
                    synced_builds=0,
                    total_store_builds=self.count_builds(),
                )
            resp.raise_for_status()
            manifest = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            notes.append(
                f"Remote build index manifest unavailable ({type(exc).__name__}); "
                f"using local cache ({existing_total} builds)."
            )
            return BuildIndexSyncReport(
                status="unavailable",
                manifest_url=manifest_url,
                store_path=str(self.db_path),
                league=league_slug,
                mode=mode,
                etag=cached_etag,
                index_updated_at=cached_updated,
                synced_builds=0,
                total_store_builds=self.count_builds(),
                notes=notes,
            )

        new_etag = resp.headers.get("ETag") or resp.headers.get("etag")
        manifest_updated = str(manifest.get("updated_at") or "") or utc_now_iso()
        slices = manifest.get("slices") or []
        if not isinstance(slices, list):
            slices = []

        # Filter to requested league_slug (and mode if available; else include sc_trade of league)
        selected: list[Mapping[str, Any]] = []
        for sl in slices:
            if not isinstance(sl, Mapping):
                continue
            sl_league = str(sl.get("league_slug") or "")
            sl_mode = str(sl.get("mode") or "sc_trade")
            if league_slug and sl_league and sl_league != league_slug:
                continue
            if mode and sl_mode != mode:
                continue
            selected.append(sl)

        if not selected and league_slug and mode and mode != "sc_trade":
            for sl in slices:
                if (
                    isinstance(sl, Mapping)
                    and str(sl.get("league_slug") or "") == league_slug
                    and str(sl.get("mode") or "sc_trade") == "sc_trade"
                ):
                    selected.append(sl)

        synced_count = 0
        for sl in selected:
            sl_league = str(
                sl.get("league_slug")
                or league_slug
                or normalize_league_slug(DEFAULT_LEAGUE)
            )
            sl_mode = str(sl.get("mode") or mode or "sc_trade")
            expected_sha = str(sl.get("sha256") or "").strip().lower()
            meta_sha_key = f"slice:{sl_league}:{sl_mode}:sha256"
            if (
                not force_refresh
                and expected_sha
                and self.get_meta(meta_sha_key) == expected_sha
                and self.count_builds(league_slug=sl_league, mode=sl_mode) > 0
            ):
                continue

            asset_url = str(sl.get("url") or "").strip()
            if not asset_url:
                asset_name = str(sl.get("asset") or "").strip()
                if not asset_name:
                    continue
                asset_url = urljoin(manifest_url, asset_name)

            try:
                s_resp = http_client.sync_client.get(asset_url, follow_redirects=True)
                s_resp.raise_for_status()
                raw_bytes = s_resp.content
            except httpx.HTTPError as exc:
                notes.append(
                    f"Failed to download build index slice {sl_league}/{sl_mode} "
                    f"({type(exc).__name__})."
                )
                continue

            actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
            if expected_sha and actual_sha != expected_sha:
                notes.append(
                    f"Checksum mismatch for build index slice {sl_league}/{sl_mode}; skipped."
                )
                continue

            try:
                decompressed = (
                    gzip.decompress(raw_bytes)
                    if raw_bytes[:2] == b"\x1f\x8b"
                    else raw_bytes
                )
                payload = json.loads(decompressed.decode("utf-8"))
                builds = payload.get("builds") or []
            except (OSError, ValueError) as exc:
                notes.append(
                    f"Corrupt build index slice {sl_league}/{sl_mode} ({type(exc).__name__})."
                )
                continue

            synced_count += self.upsert_many(builds, league_slug=sl_league, mode=sl_mode)
            self.set_meta(meta_sha_key, actual_sha)
            if sl.get("updated_at"):
                self.set_meta(f"slice:{sl_league}:{sl_mode}:updated_at", str(sl["updated_at"]))

        self.set_meta("last_checked_at", str(now_ts))
        self.set_meta("manifest_updated_at", manifest_updated)
        if new_etag:
            self.set_meta("manifest_etag", new_etag)

        return BuildIndexSyncReport(
            status="updated",
            manifest_url=manifest_url,
            store_path=str(self.db_path),
            league=league_slug,
            mode=mode,
            etag=new_etag or cached_etag,
            index_updated_at=manifest_updated,
            synced_builds=synced_count,
            total_store_builds=self.count_builds(),
            notes=notes,
        )
