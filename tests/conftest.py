from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from poe2brain.pob import Pob2SocketDaemon


@pytest.fixture(scope="session")
def pob_daemon_socket() -> Iterator[Path]:
    """Share one warm local Unix-socket PoB2 LuaJIT daemon across cross-process tests."""
    with Pob2SocketDaemon(lazy=True) as daemon:
        yield daemon.socket_path
