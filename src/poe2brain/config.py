# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_PACKAGE_DATA_DIR: Path = Path(__file__).resolve().parent / "_data"
_DEV_DATA_DIR: Path = REPO_ROOT / "data"
BUNDLED_DATA_DIR: Path = _DEV_DATA_DIR if _DEV_DATA_DIR.is_dir() else _PACKAGE_DATA_DIR

DEFAULT_LEAGUE: str = os.environ.get("POE2BRAIN_LEAGUE", "Forbidden Rites")
POE2_PATCH: str = os.environ.get("POE2BRAIN_PATCH", "0.5.5")
CONTACT_EMAIL: str = "lifan.builds@gmail.com"
REPO_URL: str = "https://github.com/lifan-builds/poe2brain"
USER_AGENT: str = f"poe2brain/0.1.0 (+{REPO_URL}; contact: {CONTACT_EMAIL})"

GGG_OAUTH_CLIENT_ID_ENV: str = "POE2BRAIN_OAUTH_CLIENT_ID"
DEFAULT_GGG_OAUTH_CLIENT_ID: str = "poe2brain"
GGG_OAUTH_AUTHORIZE_URL: str = "https://www.pathofexile.com/oauth/authorize"
GGG_OAUTH_TOKEN_URL: str = "https://www.pathofexile.com/oauth/token"
GGG_API_BASE_URL: str = "https://api.pathofexile.com"

GGG_TRADE2_BASE_URL: str = "https://www.pathofexile.com/api/trade2"
GGG_TRADE2_WEB_BASE_URL: str = "https://www.pathofexile.com/trade2/search/poe2"
POE2SCOUT_API_BASE_URL: str = "https://api.poe2scout.com"
POENINJA_POE2_BASE_URL: str = "https://poe.ninja/poe2/api"

LUAJIT_COMMIT: str = "2460b3ff93a1c955de3d62cfc825de7d68dc272e"
LUAUTF8_VERSION: str = "0.1.6"
POB2_PINNED_COMMIT: str = "601768bd105869618c3d6581b883e5d7aeec91b2"


@dataclass(frozen=True, slots=True)
class Pob2RuntimePaths:
    root_dir: Path
    bin_dir: Path
    luajit_bin: Path
    luautf8_lib: Path
    repo_dir: Path
    src_dir: Path
    runtime_dir: Path
    runtime_lua_dir: Path

    @property
    def win_lua_dll(self) -> Path:
        return self.runtime_dir / "lua51.dll"

    @property
    def win_luautf8_dll(self) -> Path:
        return self.runtime_dir / "lua-utf8.dll"

    @property
    def uses_windows_dll_host(self) -> bool:
        return (
            os.name == "nt"
            and not self.luajit_bin.is_file()
            and self.win_lua_dll.is_file()
            and self.win_luautf8_dll.is_file()
        )

    @property
    def is_ready(self) -> bool:
        pob_ready = (
            (self.src_dir / "HeadlessWrapper.lua").is_file()
            and (self.runtime_lua_dir / "dkjson.lua").is_file()
        )
        if not pob_ready:
            return False
        if os.name == "nt":
            native_ok = self.luajit_bin.is_file() and self.luautf8_lib.is_file()
            dll_ok = self.win_lua_dll.is_file() and self.win_luautf8_dll.is_file()
            return native_ok or dll_ok
        return (
            self.luajit_bin.is_file()
            and os.access(self.luajit_bin, os.X_OK)
            and self.luautf8_lib.is_file()
        )


def get_cache_dir() -> Path:
    override = os.environ.get("POE2BRAIN_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".cache" / "poe2brain").resolve()


CACHE_DIR: Path = get_cache_dir()


def get_pob2_dir() -> Path:
    override = os.environ.get("POE2BRAIN_POB2_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return get_cache_dir() / "pob2"


def get_pob2_paths(pob2_dir: Path | None = None) -> Pob2RuntimePaths:
    root = pob2_dir if pob2_dir is not None else get_pob2_dir()
    bin_dir = root / "bin"
    repo_dir = root / "PathOfBuilding-PoE2"
    runtime_dir = repo_dir / "runtime"
    luajit_name = "luajit.exe" if os.name == "nt" else "luajit"
    luautf8_name = "lua-utf8.dll" if os.name == "nt" else "lua-utf8.so"
    return Pob2RuntimePaths(
        root_dir=root,
        bin_dir=bin_dir,
        luajit_bin=bin_dir / luajit_name,
        luautf8_lib=bin_dir / luautf8_name,
        repo_dir=repo_dir,
        src_dir=repo_dir / "src",
        runtime_dir=runtime_dir,
        runtime_lua_dir=runtime_dir / "lua",
    )


def get_http_cache_dir() -> Path:
    return get_cache_dir() / "http"


def get_gamedata_db_path() -> Path:
    exact = BUNDLED_DATA_DIR / f"poe2_{POE2_PATCH}.sqlite"
    if exact.is_file():
        return exact
    candidates = sorted(BUNDLED_DATA_DIR.glob("poe2_*.sqlite"), reverse=True)
    return candidates[0] if candidates else exact
