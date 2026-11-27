# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import TextIO
from unittest.mock import Mock

import pytest

from poe2brain.pob.bridge import (
    Pob2Bridge,
    Pob2BridgeError,
    close_default_pob2_bridge,
    get_default_pob2_bridge,
)
from poe2brain.pob.daemon import Pob2SocketDaemon


@pytest.mark.parametrize(
    "failure",
    [
        Pob2BridgeError("snapshot failed"),
        {},
        {"slots": []},
        {"slots": {"candidate": {"xml": ""}}},
    ],
)
def test_failed_hibernation_preserves_build(
    monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    snapshot = (
        Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    )
    with Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.0) as bridge:
        bridge.call("switch_slot", {"slot": "candidate"})
        proc = bridge._proc
        activity = bridge._last_activity_ts - 1.0
        bridge._last_activity_ts = activity
        epoch = bridge.state_epoch
        with monkeypatch.context() as patch:
            patch.setattr(bridge, "_send_and_recv_locked", snapshot)
            assert bridge.hibernate() is False
        assert bridge._proc is proc
        assert bridge.is_running
        assert bridge.state_epoch == epoch
        assert bridge._last_activity_ts > activity
        assert bridge.ping()["activeSlot"] == "candidate"


def test_snapshot_timeout_drains_late_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    with Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.0) as bridge:
        bridge.call("switch_slot", {"slot": "candidate"})
        proc = bridge._proc
        assert proc is not None and proc.stdout is not None
        readline = proc.stdout.readline
        read_with_timeout = bridge._readline_with_timeout
        release = threading.Event()

        def delayed_readline() -> str:
            assert release.wait(timeout=5.0)
            return readline()

        with monkeypatch.context() as patch:
            patch.setattr(proc.stdout, "readline", delayed_readline)
            patch.setattr(
                bridge, "_readline_with_timeout", lambda child, _: read_with_timeout(child, 0)
            )
            try:
                assert bridge.hibernate() is False
                with pytest.raises(Pob2BridgeError, match="Timeout"):
                    bridge.call("ping", timeout=0, retry_on_crash=False)
            finally:
                release.set()

        assert bridge._proc is proc
        assert bridge.ping()["activeSlot"] == "candidate"


def test_idle_watchdog_retries_failed_hibernation(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.05)
    snapshot = Mock(side_effect=[Pob2BridgeError("snapshot failed"), {"slots": {}}])
    monkeypatch.setattr(bridge, "_send_and_recv_locked", snapshot)
    with bridge:
        assert bridge._idle_thread is not None
        bridge._idle_thread.join(timeout=3.0)
        assert snapshot.call_count == 2
        assert bridge.is_hibernated


def test_hibernate_cleans_up_exited_child() -> None:
    with Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.0) as bridge:
        proc = bridge._proc
        assert proc is not None
        proc.kill()
        proc.wait(timeout=3.0)
        assert bridge.hibernate() is False
        assert bridge._proc is None


def test_default_bridge_routes_to_selected_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    first = Pob2SocketDaemon(socket_path=tmp_path / "first.json")
    second = Pob2SocketDaemon(socket_path=tmp_path / "second.json")
    first._use_tcp = second._use_tcp = True
    monkeypatch.setattr(first.bridge, "call", Mock(return_value={"daemon": "first"}))
    monkeypatch.setattr(second.bridge, "call", Mock(return_value={"daemon": "second"}))
    with first, second:
        try:
            monkeypatch.setenv("POE2BRAIN_POB_SOCKET", str(first.socket_path))
            previous = get_default_pob2_bridge()
            assert previous.ping() == {"daemon": "first"}
            monkeypatch.setenv("POE2BRAIN_POB_SOCKET", str(second.socket_path))
            current = get_default_pob2_bridge()
            assert current.ping() == {"daemon": "second"}
            assert not previous.is_running
            monkeypatch.setenv("POE2BRAIN_POB_SOCKET", second.socket_path.name)
            assert current is get_default_pob2_bridge()
            monkeypatch.delenv("POE2BRAIN_POB_SOCKET")
            assert get_default_pob2_bridge().socket_path is None
            assert not current.is_running
        finally:
            close_default_pob2_bridge()


def test_default_bridge_tracks_resolved_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POE2BRAIN_POB_SOCKET", raising=False)
    monkeypatch.setenv("POE2BRAIN_POB2_DIR", "one")
    try:
        previous = get_default_pob2_bridge()
        monkeypatch.setenv("POE2BRAIN_POB2_DIR", str(tmp_path / "one"))
        assert get_default_pob2_bridge() is previous
        monkeypatch.setenv("POE2BRAIN_POB2_DIR", "two")
        current = get_default_pob2_bridge()
        assert current is not previous
        assert current.paths.root_dir == (tmp_path / "two").resolve()
        assert previous.paths.root_dir == (tmp_path / "one").resolve()
    finally:
        close_default_pob2_bridge()


def test_tcp_descriptor_published_privately_and_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = tmp_path / "daemon.json"
    descriptor.write_text("old descriptor", encoding="utf-8")
    old_inode = descriptor.stat().st_ino
    daemon = Pob2SocketDaemon(socket_path=descriptor)
    daemon._use_tcp = True
    dump = json.dump
    replace = os.replace
    write_modes = []
    publications = []

    def check_dump(data: object, handle: TextIO) -> None:
        write_modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        dump(data, handle)

    def check_replace(source: str | Path, target: str | Path) -> None:
        pending = Path(source)
        assert pending.parent == descriptor.parent
        assert Path(target) == descriptor
        assert descriptor.stat().st_ino == old_inode
        if os.name != "nt":
            assert stat.S_IMODE(pending.stat().st_mode) == 0o600
        publications.append(pending)
        replace(source, target)

    monkeypatch.setattr(json, "dump", check_dump)
    monkeypatch.setattr(os, "replace", check_replace)
    with daemon:
        if os.name != "nt":
            assert write_modes == [0o600]
        assert len(publications) == 1
        assert list(tmp_path.iterdir()) == [descriptor]


@pytest.mark.parametrize("transport", ["tcp"] if os.name == "nt" else ["tcp", "unix"])
def test_eager_start_failure_rolls_back_listener_and_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    monkeypatch.chdir(tmp_path)
    daemon = Pob2SocketDaemon(socket_path="daemon", lazy=False)
    daemon._use_tcp = transport == "tcp"
    listener = socket.socket(socket.AF_INET if daemon._use_tcp else socket.AF_UNIX)
    monkeypatch.setattr(socket, "socket", Mock(return_value=listener))

    def fail_start() -> None:
        assert daemon.socket_path.exists()
        raise Pob2BridgeError("startup failed")

    monkeypatch.setattr(daemon.bridge, "ensure_started", fail_start)
    with pytest.raises(Pob2BridgeError, match="startup failed"):
        daemon.start()
    assert listener.fileno() == -1
    assert list(tmp_path.iterdir()) == []
    assert daemon._accept_thread is None
    assert not daemon.is_running
