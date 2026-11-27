#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Incremental PoE2 build-index publisher for GitHub Actions + rolling GitHub Releases.

Ingests builds from sustainable live sources:
  1. Previous published `build-index` manifest/slices (incremental diffing — skips unchanged builds)
  2. Official GGG OAuth 2.1 PoE2 Ladder API (`service:leagues:ladder` across SC/HC/SSF/HCSSF)
     when `GGG_OAUTH_CLIENT_ID` & `GGG_OAUTH_CLIENT_SECRET` are configured
  3. Polite live refresh via `poe.ninja` `HTTP 200` `/poe2/pob/raw/overview/code`
  4. Opt-in community submissions feed (public `pobb.in` / `poe.ninja` / `poe2db` / `maxroll` URLs)
  5. Optional one-time bootstrap seed (`--bootstrap-from-file`) when starting from an empty index

Runs headless LuaJIT Path of Building 2 dewatering only for newly added or changed `pob_hash`
entries, and writes release-ready artifacts:
  - `manifest.json`
  - `builds_<league_slug>_<mode>.json.gz`
  - `web_builds.json` (lightweight edge payload for poe2brain.clifan.com)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from poe2brain.config import (
    DEFAULT_BUILD_INDEX_MANIFEST_URL,
    DEFAULT_LEAGUE,
    GGG_API_BASE_URL,
    GGG_OAUTH_TOKEN_URL,
    POE2_PATCH,
    USER_AGENT,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.ninja.service import (
    POENINJA_CHAR_POB_RAW_URL,
    POENINJA_ORIGIN,
    NinjaBuildsService,
    _derive_mobility_and_chase,
    _league_family_specs,
)
from poe2brain.ninja.store import compute_pob_hash, normalize_league_slug, utc_now_iso
from poe2brain.ninja.xml_inspector import inspect_pob2_xml_or_code


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _write_deterministic_gzip_json(path: Path, payload: Mapping[str, Any]) -> str:
    raw_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw_json)
    blob = buf.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def _load_previous_from_dir(prev_dir: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    slices: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not prev_dir.is_dir():
        return slices
    for gz_path in sorted(prev_dir.glob("*.json.gz")):
        try:
            with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        builds = payload.get("builds") or []
        if not isinstance(builds, list):
            continue
        lslug = str(payload.get("league_slug") or "") or normalize_league_slug(
            str(payload.get("league") or DEFAULT_LEAGUE)
        )
        mode = str(payload.get("mode") or "sc_trade")
        slices.setdefault((lslug, mode), []).extend(builds)
    return slices


def _load_previous_from_manifest_url(
    client: Poe2HttpClient, manifest_url: str
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    slices: dict[tuple[str, str], list[dict[str, Any]]] = {}
    try:
        resp = client.sync_client.get(manifest_url, follow_redirects=True)
        if resp.status_code == 404:
            return slices
        resp.raise_for_status()
        manifest = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"[sync_build_index] Previous manifest unavailable "
            f"({type(exc).__name__}); starting clean."
        )
        return slices

    for sl in manifest.get("slices") or []:
        if not isinstance(sl, Mapping):
            continue
        asset = str(sl.get("url") or sl.get("asset") or "").strip()
        if not asset:
            continue
        url = asset if "://" in asset else urljoin(manifest_url, asset)
        try:
            sr = client.sync_client.get(url, follow_redirects=True)
            sr.raise_for_status()
            payload = json.loads(gzip.decompress(sr.content).decode("utf-8"))
        except (httpx.HTTPError, OSError, ValueError) as exc:
            print(f"[sync_build_index] Skipped previous slice {asset}: {exc}")
            continue
        lslug = str(
            sl.get("league_slug")
            or normalize_league_slug(str(payload.get("league") or DEFAULT_LEAGUE))
        )
        mode = str(sl.get("mode") or payload.get("mode") or "sc_trade")
        builds = payload.get("builds") or []
        if isinstance(builds, list):
            slices.setdefault((lslug, mode), []).extend(builds)
    return slices


def _fetch_ggg_client_token(
    client: Poe2HttpClient, client_id: str, client_secret: str
) -> str | None:
    try:
        resp = client.sync_client.post(
            GGG_OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": "service:leagues:ladder",
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        token = str(resp.json().get("access_token") or "").strip()
        return token or None
    except (httpx.HTTPError, ValueError) as exc:
        print(
            f"[sync_build_index] GGG OAuth client_credentials failed "
            f"({type(exc).__name__}): {exc}"
        )
        return None


def _fetch_ggg_ladder_entries(
    client: Poe2HttpClient,
    *,
    league_name: str,
    access_token: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch top PoE2 ladder identities from official GGG OAuth API (`service:leagues:ladder`)."""
    out: list[dict[str, Any]] = []
    enc_league = quote(league_name, safe="")
    page_size = 200
    offset = 0
    while offset < limit:
        batch_limit = min(page_size, limit - offset)
        url = f"{GGG_API_BASE_URL}/league/{enc_league}/ladder"
        try:
            resp = client.sync_client.get(
                url,
                params={"realm": "poe2", "limit": str(batch_limit), "offset": str(offset)},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[sync_build_index] GGG ladder fetch failed for {league_name!r}: {exc}")
            break
        entries = (data.get("ladder") or {}).get("entries") or data.get("entries") or []
        if not entries:
            break
        for ent in entries:
            char = ent.get("character") or {}
            acct = ent.get("account") or {}
            acct_name = str(acct.get("name") or "").strip().replace("#", "-")
            char_name = str(char.get("name") or "").strip()
            if not acct_name or not char_name:
                continue
            out.append(
                {
                    "account": acct_name,
                    "character": char_name,
                    "level": int(char.get("level") or 1),
                    "experience": int(char.get("experience") or 0),
                    "rank": int(ent.get("rank") or 0) or None,
                    "class_name": str(char.get("class") or "Unknown"),
                }
            )
        if len(entries) < batch_limit:
            break
        offset += batch_limit
        time.sleep(0.5)
    return out


def _load_community_submissions(
    client: Poe2HttpClient, feed: str | None
) -> list[dict[str, Any]]:
    if not feed or not feed.strip():
        return []
    raw_feed = feed.strip()
    try:
        if raw_feed.startswith(("http://", "https://")):
            r = client.sync_client.get(raw_feed, follow_redirects=True)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
        else:
            p = Path(raw_feed)
            if not p.is_file():
                return []
            data = json.loads(p.read_text(encoding="utf-8"))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        print(f"[sync_build_index] Community feed skipped ({type(exc).__name__}): {exc}")
        return []

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, Mapping) and isinstance(data.get("submissions"), list):
        return [x for x in data["submissions"] if isinstance(x, dict)]
    return []


def _hydrate_pob_record(
    *,
    code: str,
    account: str,
    character: str,
    league: str,
    mode: str,
    ninja_url: str,
    source: str,
    live_refreshed: bool,
    existing: Mapping[str, Any] | None = None,
    experience: int | None = None,
) -> dict[str, Any]:
    insp = inspect_pob2_xml_or_code(code)
    pob_hash = compute_pob_hash(code)
    rec: dict[str, Any] = dict(existing or {})
    rec.update(
        {
            "account": account,
            "character": character,
            "league": league,
            "mode": mode,
            "fetched_at": utc_now_iso(),
            "audit_patch": POE2_PATCH,
            "source": source,
            "live_refreshed": live_refreshed,
            "class_name": insp.class_name,
            "ascendancy_name": insp.ascendancy_name or rec.get("ascendancy_name"),
            "level": insp.level,
            "main_skill": insp.main_skill or rec.get("main_skill"),
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
            "ninja_url": ninja_url,
            "pob_hash": pob_hash,
            "pob_code": code,
        }
    )
    if experience is not None:
        rec["experience"] = experience
    mob_tags, chase_uniques = _derive_mobility_and_chase(rec)
    rec["mobility_tags"] = mob_tags
    rec["t0_chase_uniques"] = chase_uniques
    # Invalidate cached PoB2 audit if the underlying PoB code changed
    if existing is not None and existing.get("pob_hash") != pob_hash:
        for k in (
            "dewatered_dps",
            "dps_inflation_pct",
            "dewatered_ehp",
            "phys_max_hit",
            "chaos_max_hit",
            "chaos_immune",
            "movement_speed_mod",
            "movement_speed_while_using_skill",
        ):
            rec.pop(k, None)
    return rec


def _enrich_records_with_pob2(
    records: list[dict[str, Any]],
    *,
    max_audits: int,
    force_reaudit: bool = False,
) -> int:
    needs_audit = [
        r
        for r in records
        if r.get("pob_code")
        and (
            force_reaudit
            or r.get("dewatered_dps") is None
            or r.get("audit_patch") != POE2_PATCH
        )
    ]
    if not needs_audit or max_audits <= 0:
        return 0

    from poe2brain.pob.service import BuildService

    audited = 0
    code_cache: dict[str, dict[str, Any]] = {}
    svc = BuildService()
    try:
        for rec in needs_audit[:max_audits]:
            code = str(rec.get("pob_code") or "").strip()
            if not code:
                continue
            cached = code_cache.get(code)
            if cached is None:
                try:
                    svc.load_build(code, slot="index_enrich")
                    audit = svc.audit_build(slot="index_enrich")
                    sus = audit.sustainable_pinnacle_stats
                    cached = {
                        "dewatered_dps": round(float(sus.effective_dps), 2),
                        "dps_inflation_pct": round(float(audit.dps_inflation_pct), 2),
                        "dewatered_ehp": round(float(sus.total_ehp), 2),
                        "phys_max_hit": round(float(sus.phys_max_hit), 1),
                        "chaos_max_hit": round(float(sus.chaos_max_hit), 1),
                        "chaos_immune": bool(sus.chaos_immune),
                        "movement_speed_mod": round(float(sus.movement_speed_mod), 4),
                        "movement_speed_while_using_skill": round(
                            float(sus.movement_speed_while_using_skill), 4
                        ),
                        "audit_patch": POE2_PATCH,
                    }
                    audited += 1
                except Exception as exc:
                    print(
                        f"[sync_build_index] PoB2 audit skipped for "
                        f"{rec.get('account')}/{rec.get('character')}: {exc}"
                    )
                    cached = {}
                code_cache[code] = cached
            rec.update(cached)
            if audited > 0 and audited % 10 == 0:
                total_to_audit = min(len(needs_audit), max_audits)
                print(
                    f"[sync_build_index] PoB2 audited {audited}/{total_to_audit} changed builds...",
                    flush=True,
                )
    finally:
        svc.close()
    return audited


def _build_web_payload(
    *,
    league: str,
    updated_at: str,
    primary_slice_path: Path,
    sources: list[str],
    modes_summary: dict[str, int],
) -> dict[str, Any]:
    """Generate the compact, PoB-code-free `web_builds.json` for poe2brain.clifan.com."""
    with tempfile.TemporaryDirectory() as td:
        with NinjaBuildsService(
            corpus_path=primary_slice_path,
            cache_dir=Path(td),
            auto_sync_index=False,
        ) as ns:
            presets_out: dict[str, list[str]] = {}
            score_by_key: dict[str, float | None] = {}
            for web_key, preset_name, sort_key, ssf_flag in (
                ("sleeper", "sleeper", "sleeper", False),
                ("boss", "boss_killer", "dps", False),
                ("mapper", "zoom_mapper", "mobility", False),
                ("tanky", "hc_tank", "ehp", False),
                ("ssf", "league_start", "dps", True),
            ):
                pres = ns.search_builds(
                    league=league,
                    preset=preset_name,
                    sort_by=sort_key,
                    ssf_viable_first=ssf_flag,
                    limit=100,
                )
                presets_out[web_key] = [f"{b.account}|{b.character}" for b in pres.builds]
                if web_key == "sleeper":
                    for b in pres.builds:
                        score_by_key[f"{b.account}|{b.character}"] = b.sleeper_score

            _, all_rows = ns._ensure_corpus_loaded()
            all_entries = [ns._record_to_entry(r, include_pob_code=False) for r in all_rows]
            all_entries.sort(
                key=lambda b: float(b.dewatered_dps or b.effective_dps or 0.0),
                reverse=True,
            )

            web_builds: list[dict[str, Any]] = []
            for b in all_entries:
                key = f"{b.account}|{b.character}"
                s_score = score_by_key.get(key, b.sleeper_score)
                web_builds.append(
                    {
                        "k": key,
                        "account": b.account,
                        "character": b.character,
                        "class_name": b.class_name,
                        "ascendancy": b.ascendancy_name,
                        "mode": b.mode,
                        "level": b.level,
                        "main_skill": b.main_skill,
                        "active_skills": b.active_skills[:8],
                        "raw_dps": round(b.effective_dps or 0),
                        "dewatered_dps": round(b.dewatered_dps or b.effective_dps or 0),
                        "dps_inflation_pct": b.dps_inflation_pct,
                        "ehp": round(b.dewatered_ehp or b.total_ehp or 0),
                        "life": round(b.life or 0),
                        "es": round(b.energy_shield or 0),
                        "move_speed": (
                            round(b.movement_speed_mod, 2) if b.movement_speed_mod else None
                        ),
                        "sleeper_score": round(s_score, 1) if s_score else None,
                        "chaos_immune": b.chaos_immune,
                        "unique_items": b.unique_items[:6],
                        "keystones": b.keystones[:5],
                        "ninja_url": b.ninja_url,
                        "source": b.source,
                        "fetched_at": b.fetched_at,
                    }
                )

    return {
        "schema_version": 1,
        "patch": POE2_PATCH,
        "league": league,
        "updated_at": updated_at,
        "sources": sources,
        "modes": modes_summary,
        "presets": presets_out,
        "builds": web_builds,
    }


def sync_build_index(
    *,
    league: str = DEFAULT_LEAGUE,
    out_dir: Path,
    previous_dir: Path | None = None,
    previous_manifest_url: str | None = None,
    bootstrap_file: Path | None = None,
    community_feed: str | None = None,
    ggg_limit: int = 200,
    max_live_fetches: int = 120,
    stale_hours: float = 72.0,
    max_pob_audits: int = 300,
    skip_pob_audit: bool = False,
    force_reaudit: bool = False,
    pace_seconds: float = 0.65,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _, family = _league_family_specs(league)
    base_league = family["sc_trade"][0]
    league_slug = normalize_league_slug(base_league)
    updated_at = utc_now_iso()
    stale_cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)

    client = Poe2HttpClient()
    try:
        # 1. Load previous incremental state
        slices: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if previous_dir is not None:
            slices.update(_load_previous_from_dir(previous_dir))
        elif previous_manifest_url:
            slices.update(_load_previous_from_manifest_url(client, previous_manifest_url))

        if not slices and bootstrap_file is not None and bootstrap_file.is_file():
            with gzip.open(bootstrap_file, "rt", encoding="utf-8") as fh:
                b_payload = json.load(fh)
            b_patch = str(b_payload.get("patch") or POE2_PATCH)
            b_rows = []
            for rec in b_payload.get("builds") or []:
                row = dict(rec)
                if row.get("dewatered_dps") is not None and not row.get("audit_patch"):
                    row["audit_patch"] = b_patch
                b_rows.append(row)
            slices[(league_slug, "sc_trade")] = b_rows
            print(f"[sync_build_index] Bootstrapped {len(b_rows)} records from {bootstrap_file}.")

        # Index existing records by (league_slug, mode) -> {(acct_low, char_low): rec}
        by_slice: dict[tuple[str, str], dict[tuple[str, str], dict[str, Any]]] = {}
        for key, rows in slices.items():
            bucket = by_slice.setdefault(key, {})
            for rec in rows:
                acct = str(rec.get("account") or "").strip()
                char = str(rec.get("character") or "").strip()
                if not acct or not char:
                    continue
                ident = (acct.lower(), char.lower())
                existing = bucket.get(ident)
                if existing is None or str(rec.get("fetched_at") or "") >= str(
                    existing.get("fetched_at") or ""
                ):
                    clean = dict(rec)
                    clean.setdefault("mode", key[1])
                    clean.setdefault("pob_hash", compute_pob_hash(clean.get("pob_code")))
                    bucket[ident] = clean

        # Fetch live snapshot-versions slugs per mode from poe.ninja
        with NinjaBuildsService(http_client=client, auto_sync_index=False) as nsvc:
            snap_state = nsvc.get_snapshot_state(base_league, force_refresh=True)

        # 2. Official GGG OAuth 2.1 Ladder ingestion across all 4 modes (when credentials exist)
        ggg_id = os.environ.get("GGG_OAUTH_CLIENT_ID", "").strip()
        ggg_secret = os.environ.get("GGG_OAUTH_CLIENT_SECRET", "").strip()
        live_fetches_used = 0

        if ggg_id and ggg_secret:
            token = _fetch_ggg_client_token(client, ggg_id, ggg_secret)
            if token:
                for mode_key, (disp_name, lurl, default_snap) in family.items():
                    m_snap = snap_state.modes.get(mode_key)
                    ov_slug = (m_snap.snapshot_name if m_snap else None) or default_snap
                    ladder_rows = _fetch_ggg_ladder_entries(
                        client, league_name=disp_name, access_token=token, limit=ggg_limit
                    )
                    print(
                        f"[sync_build_index] GGG ladder {disp_name!r} ({mode_key}): "
                        f"{len(ladder_rows)} entries"
                    )
                    bucket = by_slice.setdefault((league_slug, mode_key), {})
                    for lrow in ladder_rows:
                        if live_fetches_used >= max_live_fetches:
                            break
                        ident = (lrow["account"].lower(), lrow["character"].lower())
                        prev = bucket.get(ident)
                        if (
                            prev is not None
                            and prev.get("experience") == lrow["experience"]
                            and prev.get("level") == lrow["level"]
                            and prev.get("pob_code")
                        ):
                            continue
                        try:
                            r = client.sync_client.get(
                                POENINJA_CHAR_POB_RAW_URL,
                                params={
                                    "overview": ov_slug,
                                    "type": "exp",
                                    "account": lrow["account"],
                                    "name": lrow["character"],
                                },
                                headers={"Accept": "text/plain, */*"},
                            )
                            live_fetches_used += 1
                            time.sleep(pace_seconds)
                            if r.status_code != 200 or not r.text.strip():
                                continue
                            acct_q = quote(lrow["account"], safe="")
                            char_q = quote(lrow["character"], safe="")
                            ninja_url = (
                                f"{POENINJA_ORIGIN}/poe2/builds/{lurl}/character/"
                                f"{acct_q}/{char_q}"
                            )
                            bucket[ident] = _hydrate_pob_record(
                                code=r.text.strip(),
                                account=lrow["account"],
                                character=lrow["character"],
                                league=disp_name,
                                mode=mode_key,
                                ninja_url=ninja_url,
                                source="ggg_ladder_live",
                                live_refreshed=True,
                                existing=prev,
                                experience=lrow["experience"],
                            )
                        except (httpx.HTTPError, ValueError):
                            continue

        # 3. Polite live refresh of stalest existing records via /poe2/pob/raw/overview/code
        stale_candidates: list[tuple[str, tuple[str, str], tuple[str, str], dict[str, Any]]] = []
        for s_key, bucket in by_slice.items():
            for ident, rec in bucket.items():
                dt = _parse_iso_utc(str(rec.get("fetched_at") or ""))
                if dt is None or dt < stale_cutoff:
                    stale_candidates.append((str(rec.get("fetched_at") or ""), s_key, ident, rec))
        stale_candidates.sort(key=lambda item: item[0])

        refreshed_count = 0
        for _, s_key, ident, rec in stale_candidates:
            if live_fetches_used >= max_live_fetches:
                break
            mode_key = s_key[1]
            disp_name, lurl, default_snap = family.get(mode_key, family["sc_trade"])
            m_snap = snap_state.modes.get(mode_key)
            ov_slug = (m_snap.snapshot_name if m_snap else None) or default_snap
            try:
                r = client.sync_client.get(
                    POENINJA_CHAR_POB_RAW_URL,
                    params={
                        "overview": ov_slug,
                        "type": "exp",
                        "account": rec["account"],
                        "name": rec["character"],
                    },
                    headers={"Accept": "text/plain, */*"},
                )
                live_fetches_used += 1
                time.sleep(pace_seconds)
                if r.status_code == 200 and r.text.strip():
                    ninja_url = str(rec.get("ninja_url") or "") or (
                        f"{POENINJA_ORIGIN}/poe2/builds/{lurl}/character/"
                        f"{quote(rec['account'], safe='')}/{quote(rec['character'], safe='')}"
                    )
                    by_slice[s_key][ident] = _hydrate_pob_record(
                        code=r.text.strip(),
                        account=rec["account"],
                        character=rec["character"],
                        league=disp_name,
                        mode=mode_key,
                        ninja_url=ninja_url,
                        source="poeninja_live_pob",
                        live_refreshed=True,
                        existing=rec,
                    )
                    refreshed_count += 1
            except (httpx.HTTPError, ValueError):
                continue

        print(
            f"[sync_build_index] Live fetches used: {live_fetches_used}/{max_live_fetches} "
            f"(refreshed {refreshed_count} existing builds)."
        )

        # 4. Community opt-in submissions feed
        submissions = _load_community_submissions(client, community_feed)
        if submissions:
            with NinjaBuildsService(
                http_client=client,
                auto_sync_index=False,
                min_uncached_char_interval_seconds=pace_seconds,
            ) as c_nsvc:
                bucket = by_slice.setdefault((league_slug, "sc_trade"), {})
                added_comm = 0
                for sub in submissions[:50]:
                    src_url = str(sub.get("url") or "").strip()
                    if not src_url:
                        continue
                    try:
                        imp = c_nsvc.fetch_character(src_url)
                        acct = imp.account or "community"
                        char = imp.character or f"build-{compute_pob_hash(imp.pob_code)[:8]}"
                        ident = (acct.lower(), char.lower())
                        bucket[ident] = _hydrate_pob_record(
                            code=imp.pob_code,
                            account=acct,
                            character=char,
                            league=imp.league or base_league,
                            mode="sc_trade",
                            ninja_url=imp.ninja_url or src_url,
                            source="community_pob",
                            live_refreshed=True,
                            existing=bucket.get(ident),
                        )
                        added_comm += 1
                    except Exception:
                        continue
                print(f"[sync_build_index] Merged {added_comm} community submissions.")

        # 5. Headless PoB2 dewatering audit (only changed/unaudited builds)
        all_records_flat = [
            rec for bucket in by_slice.values() for rec in bucket.values()
        ]
        if not skip_pob_audit:
            audited = _enrich_records_with_pob2(
                all_records_flat,
                max_audits=max_pob_audits,
                force_reaudit=force_reaudit,
            )
            print(f"[sync_build_index] PoB2 dewatered {audited} new/changed builds.")

        # 6. Write deterministic slice archives, manifest.json, and web_builds.json
        manifest_slices: list[dict[str, Any]] = []
        modes_summary: dict[str, int] = {}
        all_sources: set[str] = set()
        primary_slice_path: Path | None = None

        for (sl_league, sl_mode), bucket in sorted(by_slice.items()):
            records = sorted(
                bucket.values(),
                key=lambda r: (
                    str(r.get("ascendancy_name") or ""),
                    -float(r.get("dewatered_dps") or r.get("effective_dps") or 0.0),
                    str(r.get("account") or "").lower(),
                    str(r.get("character") or "").lower(),
                ),
            )
            if not records:
                continue
            disp_league = family.get(sl_mode, family["sc_trade"])[0]
            slice_sources = sorted({str(r.get("source") or "build_index") for r in records})
            all_sources.update(slice_sources)
            modes_summary[sl_mode] = len(records)

            asset_name = f"builds_{sl_league}_{sl_mode}.json.gz"
            asset_path = out_dir / asset_name
            slice_payload = {
                "schema_version": 1,
                "patch": POE2_PATCH,
                "league": disp_league,
                "league_slug": sl_league,
                "mode": sl_mode,
                "updated_at": updated_at,
                "build_count": len(records),
                "sources": slice_sources,
                "builds": records,
            }
            sha = _write_deterministic_gzip_json(asset_path, slice_payload)
            if sl_league == league_slug and sl_mode == "sc_trade":
                primary_slice_path = asset_path
            elif primary_slice_path is None:
                primary_slice_path = asset_path

            manifest_slices.append(
                {
                    "league": disp_league,
                    "league_slug": sl_league,
                    "mode": sl_mode,
                    "asset": asset_name,
                    "sha256": sha,
                    "build_count": len(records),
                    "live_refreshed_count": sum(1 for r in records if r.get("live_refreshed")),
                    "pob2_audited_count": sum(
                        1 for r in records if r.get("dewatered_dps") is not None
                    ),
                    "sources": slice_sources,
                    "updated_at": updated_at,
                }
            )

        if primary_slice_path is not None:
            web_payload = _build_web_payload(
                league=base_league,
                updated_at=updated_at,
                primary_slice_path=primary_slice_path,
                sources=sorted(all_sources),
                modes_summary=modes_summary,
            )
            web_path = out_dir / "web_builds.json"
            web_path.write_text(
                json.dumps(web_payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            print(
                f"[sync_build_index] Wrote {web_path} "
                f"({web_path.stat().st_size / 1024:.1f} KB, {len(web_payload['builds'])} builds)."
            )

        manifest = {
            "schema_version": 1,
            "patch": POE2_PATCH,
            "updated_at": updated_at,
            "default_league": base_league,
            "default_league_slug": league_slug,
            "web_index_asset": "web_builds.json",
            "slices": manifest_slices,
        }
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[sync_build_index] Wrote {manifest_path} ({len(manifest_slices)} slices).")
        return manifest_path
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", default=DEFAULT_LEAGUE)
    parser.add_argument("--out-dir", type=Path, default=Path("dist/build-index"))
    parser.add_argument("--previous-dir", type=Path, default=None)
    parser.add_argument(
        "--previous-manifest-url",
        default=DEFAULT_BUILD_INDEX_MANIFEST_URL,
        help="Remote manifest URL to load incremental baseline from (pass '' to disable).",
    )
    parser.add_argument("--bootstrap-file", type=Path, default=None)
    parser.add_argument("--community-feed", default=None)
    parser.add_argument("--ggg-limit", type=int, default=200)
    parser.add_argument("--max-live-fetches", type=int, default=120)
    parser.add_argument("--stale-hours", type=float, default=72.0)
    parser.add_argument("--max-pob-audits", type=int, default=300)
    parser.add_argument("--skip-pob-audit", action="store_true")
    parser.add_argument("--force-reaudit", action="store_true")
    parser.add_argument("--pace-seconds", type=float, default=0.65)
    args = parser.parse_args()

    sync_build_index(
        league=args.league,
        out_dir=args.out_dir,
        previous_dir=args.previous_dir,
        previous_manifest_url=args.previous_manifest_url or None,
        bootstrap_file=args.bootstrap_file,
        community_feed=args.community_feed,
        ggg_limit=args.ggg_limit,
        max_live_fetches=args.max_live_fetches,
        stale_hours=args.stale_hours,
        max_pob_audits=args.max_pob_audits,
        skip_pob_audit=args.skip_pob_audit,
        force_reaudit=args.force_reaudit,
        pace_seconds=args.pace_seconds,
    )


if __name__ == "__main__":
    main()
