# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from poe2brain import mcp_server as mcp_mod
from poe2brain.pob import Pob2SocketDaemon
from poe2brain.pob.bridge import _close_live_bridges, close_default_pob2_bridge
from poe2brain.pob.daemon import _close_live_daemons
from poe2brain.pob.service import _close_default_build_service


@pytest.fixture(scope="session")
def pob_daemon_socket() -> Iterator[Path]:
    """Share one warm local Unix-socket PoB2 LuaJIT daemon across cross-process tests."""
    with Pob2SocketDaemon(lazy=True) as daemon:
        yield daemon.socket_path


@pytest.fixture(scope="session", autouse=True)
def _reap_all_pob2_processes_at_session_end() -> Iterator[None]:
    """Ensure zero LuaJIT processes remain running when pytest finishes."""
    try:
        yield
    finally:
        mcp_mod.close_services()
        _close_default_build_service()
        close_default_pob2_bridge()
        _close_live_daemons()
        _close_live_bridges()
