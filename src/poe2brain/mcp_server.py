from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from poe2brain import __version__
from poe2brain.config import (
    BUNDLED_DATA_DIR,
    DEFAULT_LEAGUE,
    POE2_PATCH,
    get_cache_dir,
    get_gamedata_db_path,
    get_pob2_paths,
)
from poe2brain.models.common import EngineStatus

mcp = FastMCP("poe2brain")


def collect_engine_status() -> EngineStatus:
    pob2 = get_pob2_paths()
    db_path = get_gamedata_db_path()
    return EngineStatus(
        version=__version__,
        patch=POE2_PATCH,
        default_league=DEFAULT_LEAGUE,
        cache_dir=str(get_cache_dir()),
        bundled_data_dir=str(BUNDLED_DATA_DIR),
        gamedata_db_ready=db_path.is_file(),
        pob2_runtime_ready=pob2.is_ready,
        pob2_luajit_path=str(pob2.luajit_bin) if pob2.luajit_bin.is_file() else None,
        pob2_src_dir=str(pob2.src_dir) if pob2.src_dir.is_dir() else None,
    )


@mcp.tool()
def poe2_status() -> dict[str, Any]:
    """Return poe2brain version, target PoE2 patch/league, and local gamedata/PoB2 readiness."""
    return collect_engine_status().model_dump()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
