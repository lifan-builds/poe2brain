# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from poe2brain.config import USER_AGENT, get_http_cache_dir

RATE_LIMIT_SAFETY_BUFFER_SECONDS: float = 1.0

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True, slots=True)
class RateLimitWindow:
    rule: str
    max_hits: int
    window_seconds: float
    ban_penalty_seconds: float
    current_hits: int = 0
    active_ban_seconds: float = 0.0

    @property
    def remaining_hits(self) -> int:
        return max(0, self.max_hits - self.current_hits)

    @property
    def is_exhausted(self) -> bool:
        return self.current_hits >= self.max_hits or self.active_ban_seconds > 0

    def required_delay(
        self,
        *,
        safety_buffer: float = RATE_LIMIT_SAFETY_BUFFER_SECONDS,
        window_elapsed_seconds: float | None = None,
    ) -> float:
        if self.active_ban_seconds > 0:
            return self.active_ban_seconds + safety_buffer
        if self.current_hits >= self.max_hits:
            remaining_window = self.window_seconds
            if window_elapsed_seconds is not None:
                remaining_window = max(0.0, self.window_seconds - window_elapsed_seconds)
            return remaining_window + safety_buffer
        return 0.0


def _parse_triplets(raw: str) -> list[tuple[int, float, float]]:
    out: list[tuple[int, float, float]] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid GGG rate-limit triplet: {token!r}")
        hits = int(parts[0])
        window_s = float(parts[1])
        ban_s = float(parts[2])
        out.append((hits, window_s, ban_s))
    return out


def parse_rate_limit_headers(headers: Mapping[str, str]) -> list[RateLimitWindow]:
    lower = {k.lower(): v for k, v in headers.items()}
    rules_raw = lower.get("x-rate-limit-rules", "")
    rules = [r.strip() for r in rules_raw.split(",") if r.strip()]
    if not rules:
        if "x-rate-limit-ip" in lower or "x-rate-limit-ip-state" in lower:
            rules = ["Ip"]
        else:
            return []

    windows: list[RateLimitWindow] = []
    for rule in rules:
        rule_key = rule.lower()
        limit_raw = lower.get(f"x-rate-limit-{rule_key}", "")
        state_raw = lower.get(f"x-rate-limit-{rule_key}-state", "")
        if not limit_raw:
            continue

        limits = _parse_triplets(limit_raw)
        states = _parse_triplets(state_raw) if state_raw else []
        state_by_window = {win_s: (cur_hits, active_ban) for cur_hits, win_s, active_ban in states}

        for idx, (max_hits, win_s, ban_penalty_s) in enumerate(limits):
            if idx < len(states) and states[idx][1] == win_s:
                cur_hits, _, active_ban_s = states[idx]
            elif win_s in state_by_window:
                cur_hits, active_ban_s = state_by_window[win_s]
            else:
                cur_hits, active_ban_s = 0, 0.0
            windows.append(
                RateLimitWindow(
                    rule=rule,
                    max_hits=max_hits,
                    window_seconds=win_s,
                    ban_penalty_seconds=ban_penalty_s,
                    current_hits=cur_hits,
                    active_ban_seconds=active_ban_s,
                )
            )
    return windows


def compute_rate_limit_delay(
    headers: Mapping[str, str],
    *,
    safety_buffer: float = RATE_LIMIT_SAFETY_BUFFER_SECONDS,
) -> float:
    lower = {k.lower(): v for k, v in headers.items()}
    delay = 0.0
    retry_after = lower.get("retry-after")
    if retry_after:
        try:
            delay = max(delay, float(retry_after.strip()) + safety_buffer)
        except ValueError:
            pass

    for win in parse_rate_limit_headers(headers):
        delay = max(delay, win.required_delay(safety_buffer=safety_buffer))
    return delay


def infer_ggg_bucket(url: str, headers: Mapping[str, str] | None = None) -> str:
    if headers:
        for k, v in headers.items():
            if k.lower() == "x-rate-limit-policy" and v.strip():
                return v.strip()
    path = urlparse(url).path.lower()
    if "/api/trade2/search/" in path:
        return "trade-search-request-limit"
    if "/api/trade2/fetch/" in path:
        return "trade-fetch-request-limit"
    if "/api/trade2/exchange/" in path:
        return "trade-exchange-request-limit"
    if "/api/trade2/data/" in path:
        return "trade-data"
    return "ggg-default"


@dataclass
class _BucketState:
    windows: list[RateLimitWindow] = field(default_factory=list)
    blocked_until: float = 0.0
    timestamps: deque[float] = field(default_factory=deque)


class GGGRateLimiter:
    def __init__(
        self,
        *,
        safety_buffer: float = RATE_LIMIT_SAFETY_BUFFER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep_sync: Callable[[float], None] = time.sleep,
        sleep_async: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.safety_buffer = safety_buffer
        self._clock = clock
        self._sleep_sync = sleep_sync
        self._sleep_async = sleep_async
        self._buckets: dict[str, _BucketState] = {}
        self._sync_lock = threading.Lock()
        self._async_locks: dict[str, asyncio.Lock] = {}

    def _bucket(self, name: str) -> _BucketState:
        state = self._buckets.get(name)
        if state is None:
            state = _BucketState()
            self._buckets[name] = state
        return state

    def _prune(self, state: _BucketState, now: float) -> None:
        max_win = max((w.window_seconds for w in state.windows), default=21600.0)
        cutoff = now - max_win - self.safety_buffer
        while state.timestamps and state.timestamps[0] < cutoff:
            state.timestamps.popleft()

    def record_request(self, bucket: str, now: float | None = None) -> None:
        ts = self._clock() if now is None else now
        with self._sync_lock:
            state = self._bucket(bucket)
            self._prune(state, ts)
            state.timestamps.append(ts)

    def pending_delay(self, bucket: str, now: float | None = None) -> float:
        ts = self._clock() if now is None else now
        with self._sync_lock:
            state = self._bucket(bucket)
            self._prune(state, ts)
            delay = max(0.0, state.blocked_until - ts)
            for win in state.windows:
                if win.max_hits <= 0:
                    continue
                in_win = [t for t in state.timestamps if ts - t < win.window_seconds]
                if len(in_win) >= win.max_hits:
                    oldest_relevant = in_win[-win.max_hits]
                    reset_in = (oldest_relevant + win.window_seconds - ts) + self.safety_buffer
                    delay = max(delay, max(0.0, reset_in))
            return delay

    def update_from_headers(
        self,
        headers: Mapping[str, str],
        *,
        url: str = "",
        bucket: str | None = None,
        now: float | None = None,
    ) -> float:
        ts = self._clock() if now is None else now
        resolved_bucket = bucket or infer_ggg_bucket(url, headers)
        windows = parse_rate_limit_headers(headers)
        lower = {k.lower(): v for k, v in headers.items()}

        with self._sync_lock:
            state = self._bucket(resolved_bucket)
            if windows:
                state.windows = windows
            self._prune(state, ts)

            delay = 0.0
            retry_after = lower.get("retry-after")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after.strip()) + self.safety_buffer)
                except ValueError:
                    pass

            for win in windows:
                if win.active_ban_seconds > 0:
                    delay = max(delay, win.active_ban_seconds + self.safety_buffer)
                elif win.current_hits >= win.max_hits and win.max_hits > 0:
                    in_win = [t for t in state.timestamps if ts - t < win.window_seconds]
                    if len(in_win) >= win.max_hits:
                        elapsed = ts - in_win[-win.max_hits]
                    else:
                        elapsed = None
                    delay = max(
                        delay,
                        win.required_delay(
                            safety_buffer=self.safety_buffer,
                            window_elapsed_seconds=elapsed,
                        ),
                    )

            if delay > 0:
                state.blocked_until = max(state.blocked_until, ts + delay)
            return delay

    def acquire_sync(self, bucket: str) -> float:
        total_slept = 0.0
        while True:
            wait_s = self.pending_delay(bucket)
            if wait_s <= 0:
                self.record_request(bucket)
                return total_slept
            self._sleep_sync(wait_s)
            total_slept += wait_s

    async def acquire_async(self, bucket: str) -> float:
        lock = self._async_locks.setdefault(bucket, asyncio.Lock())
        total_slept = 0.0
        async with lock:
            while True:
                wait_s = self.pending_delay(bucket)
                if wait_s <= 0:
                    self.record_request(bucket)
                    return total_slept
                await self._sleep_async(wait_s)
                total_slept += wait_s

    def snapshot(self, bucket: str) -> list[RateLimitWindow]:
        with self._sync_lock:
            return list(self._bucket(bucket).windows)


DEFAULT_GGG_RATE_LIMITER = GGGRateLimiter()


def build_default_headers(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    merged: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if extra:
        for k, v in extra.items():
            if k.lower() == "user-agent" and not v.strip():
                continue
            merged[k] = v
    merged.setdefault("User-Agent", USER_AGENT)
    return merged


def create_sync_client(
    *,
    timeout: float = 20.0,
    headers: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers=build_default_headers(headers),
        follow_redirects=True,
        transport=transport,
    )


def create_async_client(
    *,
    timeout: float = 20.0,
    headers: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers=build_default_headers(headers),
        follow_redirects=True,
        transport=transport,
    )


def _is_ggg_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "pathofexile.com" or host.endswith(".pathofexile.com")


class Poe2HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        rate_limiter: GGGRateLimiter | None = None,
        sync_transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout = timeout
        self.rate_limiter = rate_limiter or DEFAULT_GGG_RATE_LIMITER
        self._sync_transport = sync_transport
        self._async_transport = async_transport
        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = create_sync_client(
                timeout=self.timeout, transport=self._sync_transport
            )
        return self._sync_client

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = create_async_client(
                timeout=self.timeout, transport=self._async_transport
            )
        return self._async_client

    def close(self) -> None:
        if self._sync_client is not None and not self._sync_client.is_closed:
            self._sync_client.close()

    async def aclose(self) -> None:
        if self._async_client is not None and not self._async_client.is_closed:
            await self._async_client.aclose()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        retry_on_429: bool = True,
    ) -> Any:
        is_ggg = _is_ggg_url(url)
        bucket = infer_ggg_bucket(url) if is_ggg else ""
        attempts = 2 if retry_on_429 else 1

        for attempt in range(attempts):
            if is_ggg:
                self.rate_limiter.acquire_sync(bucket)
            try:
                resp = self.sync_client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.RemoteProtocolError):
                if method.upper() == "GET" and attempt + 1 < attempts:
                    self.rate_limiter._sleep_sync(0.5)
                    continue
                raise
            if is_ggg:
                self.rate_limiter.update_from_headers(resp.headers, url=url, bucket=bucket)
            if resp.status_code == 429 and attempt + 1 < attempts:
                if not is_ggg:
                    wait_s = compute_rate_limit_delay(
                        resp.headers, safety_buffer=self.rate_limiter.safety_buffer
                    )
                    if wait_s > 0:
                        self.rate_limiter._sleep_sync(wait_s)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Unreachable HTTP retry state for {url}")

    async def arequest_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        retry_on_429: bool = True,
    ) -> Any:
        is_ggg = _is_ggg_url(url)
        bucket = infer_ggg_bucket(url) if is_ggg else ""
        attempts = 2 if retry_on_429 else 1

        for attempt in range(attempts):
            if is_ggg:
                await self.rate_limiter.acquire_async(bucket)
            try:
                resp = await self.async_client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.RemoteProtocolError):
                if method.upper() == "GET" and attempt + 1 < attempts:
                    await self.rate_limiter._sleep_async(0.5)
                    continue
                raise
            if is_ggg:
                self.rate_limiter.update_from_headers(resp.headers, url=url, bucket=bucket)
            if resp.status_code == 429 and attempt + 1 < attempts:
                if not is_ggg:
                    wait_s = compute_rate_limit_delay(
                        resp.headers, safety_buffer=self.rate_limiter.safety_buffer
                    )
                    if wait_s > 0:
                        await self.rate_limiter._sleep_async(wait_s)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Unreachable HTTP retry state for {url}")

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self.request_json("GET", url, params=params, headers=headers)

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ttl_seconds: float | None = None,
        cache_dir: Path | None = None,
        force_refresh: bool = False,
        retry_on_429: bool = True,
    ) -> str:
        req_headers = {"Accept": "text/plain, */*"}
        if headers:
            req_headers.update(headers)

        def _fetch_text() -> str:
            is_ggg = _is_ggg_url(url)
            bucket = infer_ggg_bucket(url) if is_ggg else ""
            attempts = 2 if retry_on_429 else 1
            for attempt in range(attempts):
                if is_ggg:
                    self.rate_limiter.acquire_sync(bucket)
                try:
                    resp = self.sync_client.request(
                        "GET",
                        url,
                        params=params,
                        headers=req_headers,
                    )
                except (httpx.TimeoutException, httpx.RemoteProtocolError):
                    if attempt + 1 < attempts:
                        self.rate_limiter._sleep_sync(0.5)
                        continue
                    raise
                if is_ggg:
                    self.rate_limiter.update_from_headers(resp.headers, url=url, bucket=bucket)
                if resp.status_code == 429 and attempt + 1 < attempts:
                    if not is_ggg:
                        wait_s = compute_rate_limit_delay(
                            resp.headers, safety_buffer=self.rate_limiter.safety_buffer
                        )
                        if wait_s > 0:
                            self.rate_limiter._sleep_sync(wait_s)
                    continue
                resp.raise_for_status()
                return resp.text
            raise RuntimeError(f"Unreachable HTTP retry state for {url}")

        if ttl_seconds is not None and (self._sync_transport is None or cache_dir is not None):
            envelope = get_cached_json(
                url,
                params=params,
                ttl_seconds=ttl_seconds,
                fetcher=lambda: {"text": _fetch_text()},
                cache_dir=cache_dir,
                client=self,
                force_refresh=force_refresh,
            )
            if isinstance(envelope, Mapping) and isinstance(envelope.get("text"), str):
                return envelope["text"]

        return _fetch_text()

    def post_json(
        self,
        url: str,
        *,
        json_body: Any,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return self.request_json("POST", url, params=params, json_body=json_body, headers=headers)

    async def aget_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return await self.arequest_json("GET", url, params=params, headers=headers)

    async def apost_json(
        self,
        url: str,
        *,
        json_body: Any,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        return await self.arequest_json(
            "POST", url, params=params, json_body=json_body, headers=headers
        )


def _cache_file_path(key: str, cache_dir: Path | None = None) -> Path:
    root = cache_dir if cache_dir is not None else get_http_cache_dir()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    prefix = _SLUG_RE.sub("_", key).strip("_")[:48] or "entry"
    return root / f"{prefix}_{digest}.json"


def _canonical_cache_key(key_or_url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return key_or_url
    encoded = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
    sep = "&" if "?" in key_or_url else "?"
    return f"{key_or_url}{sep}{encoded}"


def _read_cache_envelope(path: Path) -> tuple[float, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return float(raw["cached_at"]), raw["data"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache_envelope(path: Path, key: str, data: Any, now_epoch: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"cached_at": now_epoch, "key": key, "data": data}, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def get_cached_json(
    key_or_url: str,
    *,
    ttl_seconds: float = 900.0,
    fetcher: Callable[[], Any] | None = None,
    params: Mapping[str, Any] | None = None,
    cache_dir: Path | None = None,
    client: Poe2HttpClient | None = None,
    force_refresh: bool = False,
    allow_stale_on_error: bool = True,
    now_fn: Callable[[], float] = time.time,
) -> Any:
    use_disk_cache = cache_dir is not None or (
        client is None or (client._sync_transport is None and client._async_transport is None)
    )
    cache_key = _canonical_cache_key(key_or_url, params)
    cache_path = _cache_file_path(cache_key, cache_dir=cache_dir) if use_disk_cache else None
    now_epoch = now_fn()

    existing = (
        None
        if (force_refresh or cache_path is None)
        else _read_cache_envelope(cache_path)
    )
    if existing is not None:
        cached_at, data = existing
        if now_epoch - cached_at <= ttl_seconds:
            return data

    try:
        if fetcher is not None:
            fresh = fetcher()
        else:
            owned = client is None
            http_client = client or Poe2HttpClient()
            try:
                fresh = http_client.get_json(key_or_url, params=params)
            finally:
                if owned:
                    http_client.close()
    except Exception:
        if allow_stale_on_error and cache_path is not None:
            stale = _read_cache_envelope(cache_path)
            if stale is not None:
                return stale[1]
        raise

    if cache_path is not None:
        _write_cache_envelope(cache_path, cache_key, fresh, now_fn())
    return fresh


async def aget_cached_json(
    key_or_url: str,
    *,
    ttl_seconds: float = 900.0,
    fetcher: Callable[[], Awaitable[Any]] | None = None,
    params: Mapping[str, Any] | None = None,
    cache_dir: Path | None = None,
    client: Poe2HttpClient | None = None,
    force_refresh: bool = False,
    allow_stale_on_error: bool = True,
    now_fn: Callable[[], float] = time.time,
) -> Any:
    use_disk_cache = cache_dir is not None or (
        client is None or (client._sync_transport is None and client._async_transport is None)
    )
    cache_key = _canonical_cache_key(key_or_url, params)
    cache_path = _cache_file_path(cache_key, cache_dir=cache_dir) if use_disk_cache else None
    now_epoch = now_fn()

    existing = (
        None
        if (force_refresh or cache_path is None)
        else _read_cache_envelope(cache_path)
    )
    if existing is not None:
        cached_at, data = existing
        if now_epoch - cached_at <= ttl_seconds:
            return data

    try:
        if fetcher is not None:
            fresh = await fetcher()
        else:
            owned = client is None
            http_client = client or Poe2HttpClient()
            try:
                fresh = await http_client.aget_json(key_or_url, params=params)
            finally:
                if owned:
                    await http_client.aclose()
    except Exception:
        if allow_stale_on_error and cache_path is not None:
            stale = _read_cache_envelope(cache_path)
            if stale is not None:
                return stale[1]
        raise

    if cache_path is not None:
        _write_cache_envelope(cache_path, cache_key, fresh, now_fn())
    return fresh
