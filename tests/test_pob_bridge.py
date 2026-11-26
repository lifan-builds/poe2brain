# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from poe2brain.pob.bridge import Pob2Bridge, Pob2BridgeError


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
        activity = bridge._last_activity_ts
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
