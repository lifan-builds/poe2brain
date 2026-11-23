from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
BUNDLED_DATA_DIR: Path = REPO_ROOT / "data"

DEFAULT_LEAGUE: str = "Forbidden Rites"
POE2_PATCH: str = "0.5.5"
USER_AGENT: str = "poe2brain/0.1.0 (+https://github.com/lfan/poe2brain)"

GGG_TRADE2_BASE_URL: str = "https://www.pathofexile.com/api/trade2"
GGG_TRADE2_WEB_BASE_URL: str = "https://www.pathofexile.com/trade2/search/poe2"
POE2SCOUT_API_BASE_URL: str = "https://api.poe2scout.com"
POENINJA_POE2_BASE_URL: str = "https://poe.ninja/poe2/api"

LUAJIT_COMMIT: str = "2460b3ff93a1c955de3d62cfc825de7d68dc272e"
LUAUTF8_VERSION: str = "0.1.6"


@dataclass(frozen=True, slots=True)
class Pob2RuntimePaths:
    root_dir: Path
    bin_dir: Path
    luajit_bin: Path
    luautf8_lib: Path
    repo_dir: Path
    src_dir: Path
    runtime_lua_dir: Path

    @property
    def is_ready(self) -> bool:
        return (
            self.luajit_bin.is_file()
            and os.access(self.luajit_bin, os.X_OK)
            and self.luautf8_lib.is_file()
            and (self.src_dir / "HeadlessWrapper.lua").is_file()
            and (self.runtime_lua_dir / "dkjson.lua").is_file()
        )


def get_cache_dir() -> Path:
    override = os.environ.get("POE2BRAIN_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".cache" / "poe2brain").resolve()


CACHE_DIR: Path = get_cache_dir()


def get_pob2_dir() -> Path:
    return get_cache_dir() / "pob2"


def get_pob2_paths(pob2_dir: Path | None = None) -> Pob2RuntimePaths:
    root = pob2_dir if pob2_dir is not None else get_pob2_dir()
    bin_dir = root / "bin"
    repo_dir = root / "PathOfBuilding-PoE2"
    return Pob2RuntimePaths(
        root_dir=root,
        bin_dir=bin_dir,
        luajit_bin=bin_dir / "luajit",
        luautf8_lib=bin_dir / "lua-utf8.so",
        repo_dir=repo_dir,
        src_dir=repo_dir / "src",
        runtime_lua_dir=repo_dir / "runtime" / "lua",
    )


def get_http_cache_dir() -> Path:
    return get_cache_dir() / "http"


def get_gamedata_db_path() -> Path:
    return BUNDLED_DATA_DIR / f"poe2_{POE2_PATCH}.sqlite"
