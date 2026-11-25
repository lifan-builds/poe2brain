from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from poe2brain import mcp_server as mcp_mod
from poe2brain.cli import app
from poe2brain.config import REPO_ROOT, get_pob2_paths
from poe2brain.filters import (
    extract_build_filter_context,
    extract_build_filter_context_from_xml,
)
from poe2brain.ninja import NinjaBuildsService, decode_pob2_code_to_xml
from poe2brain.pob import (
    BuildService,
    Pob2Bridge,
    Pob2BridgeError,
    close_default_pob2_bridge,
    get_default_build_service,
    get_default_pob2_bridge,
)


def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _rss_mb(pid: int) -> float:
    if os.name == "nt":
        import ctypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            return 0.0
        try:
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / (1024.0 * 1024.0)
            return 0.0
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True).strip()
    return int(out) / 1024.0


def _load_sample_corpus_builds(count: int = 4) -> list[dict[str, object]]:
    corpus_path = REPO_ROOT / "data" / "ninja" / "forbidden_rites_0.5.5_builds.json.gz"
    with gzip.open(corpus_path, "rt", encoding="utf-8") as handle:
        builds = [b for b in json.load(handle)["builds"] if b.get("pob_code")]
    ranked = sorted(
        builds,
        key=lambda b: len(b.get("active_skills") or []) + len(b.get("support_gems") or []),
        reverse=True,
    )
    return ranked[:count]


def test_singleton_bridge_reuse_across_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POE2BRAIN_POB_SOCKET", raising=False)
    mcp_mod.close_services()
    close_default_pob2_bridge()
    try:
        b_default = get_default_pob2_bridge()
        svc_default = get_default_build_service()
        svc_direct = BuildService()
        svc_mcp = mcp_mod.get_build_service()
        ninja = NinjaBuildsService()
        try:
            svc_ninja = ninja._ensure_build_service()
            assert svc_default.bridge is b_default
            assert svc_direct.bridge is b_default
            assert svc_mcp.bridge is b_default
            assert svc_ninja.bridge is b_default

            # Closing non-owning BuildService wrappers must not kill the shared bridge
            b_default.ping()
            assert b_default._proc is not None
            shared_pid = b_default._proc.pid
            svc_direct.close()
            assert b_default.is_running is True
            assert b_default._proc is not None
            assert b_default._proc.pid == shared_pid
        finally:
            ninja.close()
    finally:
        mcp_mod.close_services()
        close_default_pob2_bridge()


def test_four_slot_single_build_rss_and_fast_audit() -> None:
    sample = _load_sample_corpus_builds(4)
    assert len(sample) == 4

    with Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.0) as bridge:
        svc = BuildService(bridge=bridge)
        assert bridge._proc is not None
        pid = bridge._proc.pid

        svc.load_build(str(sample[0]["pob_code"]), slot="main")
        rss_one_slot = _rss_mb(pid)

        t0 = time.perf_counter()
        audit = svc.audit_build(slot="main")
        audit_sec = time.perf_counter() - t0
        assert audit_sec < 4.0, f"Expected audit_build < 4.0s, took {audit_sec:.2f}s"
        assert audit.sustainable_pinnacle_stats.level >= 1

        svc.load_build(str(sample[1]["pob_code"]), slot="early")
        svc.load_build(str(sample[2]["pob_code"]), slot="ref")
        svc.load_build(str(sample[3]["pob_code"]), slot="candidate")
        rss_four_slots = _rss_mb(pid)

        # Inactive slots are stored as compressed XML rather than live Build graphs:
        # 3 additional 20+ skill builds should add < 40 MB RSS, not 3x 100+ MB!
        assert (rss_four_slots - rss_one_slot) < 40.0, (
            f"Expected 4-slot RSS delta < 40 MB over 1-slot ({rss_one_slot:.1f} MB), "
            f"got {rss_four_slots:.1f} MB"
        )
        assert rss_four_slots < 460.0

        # Verify undo stack cap <= 5
        for lvl in range(80, 88):
            svc.reset_slot("candidate", class_name="Sorceress", level=lvl)
        slots_meta = {s["slot"]: s for s in svc.list_slots()["slots"]}
        assert slots_meta["candidate"]["undoDepth"] <= 5


def test_idle_auto_hibernation_and_transparent_slot_rehydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POE2BRAIN_POB_IDLE_TIMEOUT", "0.8")
    with Pob2Bridge(use_daemon=False) as bridge:
        svc = BuildService(bridge=bridge)
        s_main = svc.create_character("Sorceress", "Stormweaver", level=88, slot="main")
        assert s_main.level == 88
        svc.clone_slot("main", "candidate", activate=True)
        svc.reset_slot("candidate", class_name="Ranger", ascendancy="Deadeye", level=92)
        svc.reset_slot("candidate", class_name="Ranger", ascendancy="Deadeye", level=94)

        assert bridge._proc is not None
        first_pid = bridge._proc.pid
        assert _is_pid_alive(first_pid)

        # Wait for automatic idle hibernation to snapshot slots and reap LuaJIT
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and bridge.is_running:
            time.sleep(0.1)

        assert bridge.is_running is False
        assert bridge.is_hibernated is True
        assert not _is_pid_alive(first_pid)

        # Next RPC transparently boots a new LuaJIT VM and restores all slots + undo stack
        cand_stats = svc.get_stats("candidate")
        assert bridge.is_running is True
        assert bridge.is_hibernated is False
        assert bridge._proc is not None
        second_pid = bridge._proc.pid
        assert second_pid != first_pid
        assert cand_stats.class_name == "Ranger"
        assert cand_stats.ascendancy_name == "Deadeye"
        assert cand_stats.level == 94

        undone = svc.undo("candidate")
        assert undone.level == 92

        main_stats = svc.get_stats("main")
        assert main_stats.class_name == "Sorceress"
        assert main_stats.ascendancy_name == "Stormweaver"
        assert main_stats.level == 88

    assert not _is_pid_alive(second_pid)


def test_timeout_triggers_hard_sigkill_and_reaps_child(tmp_path: Path) -> None:
    hang_lua = tmp_path / "hang_bridge.lua"
    hang_lua.write_text(
        "local line = io.stdin:read('*l')\nwhile true do end\n",
        encoding="utf-8",
    )
    bridge = Pob2Bridge(
        bridge_lua_path=hang_lua,
        use_daemon=False,
        request_timeout=0.3,
        idle_timeout_seconds=0.0,
    )
    bridge.ensure_started()
    assert bridge._proc is not None
    hung_pid = bridge._proc.pid
    assert _is_pid_alive(hung_pid)

    with pytest.raises(Pob2BridgeError, match="Timeout"):
        bridge.call("ping", retry_on_crash=False)

    assert bridge.is_running is False
    assert not _is_pid_alive(hung_pid)
    bridge.close()


def test_parent_sigkill_triggers_luajit_getppid_self_termination() -> None:
    # Keep a duplicate pipe or let a child Python process spawn Pob2Bridge, then SIGKILL
    # the child Python process and verify LuaJIT exits within < 1.0s with 0 orphans!
    helper_code = (
        "import sys, time\n"
        "from poe2brain.pob.bridge import Pob2Bridge\n"
        "b = Pob2Bridge(use_daemon=False, idle_timeout_seconds=0.0)\n"
        "b.ping()\n"
        "print(b._proc.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    py_proc = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert py_proc.stdout is not None
        luajit_pid_line = py_proc.stdout.readline().strip()
        assert luajit_pid_line.isdigit(), f"Unexpected child output: {luajit_pid_line!r}"
        luajit_pid = int(luajit_pid_line)
        assert _is_pid_alive(luajit_pid)

        # Hard-kill parent Python process (simulating killed agent/pytest worker)
        if os.name == "nt":
            py_proc.kill()
        else:
            os.kill(py_proc.pid, signal.SIGKILL)
        py_proc.wait(timeout=3.0)

        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and _is_pid_alive(luajit_pid):
            time.sleep(0.05)

        assert not _is_pid_alive(luajit_pid), (
            f"Orphan LuaJIT process {luajit_pid} survived parent SIGKILL!"
        )
    finally:
        if py_proc.poll() is None:
            py_proc.kill()
            py_proc.wait(timeout=2.0)


def test_zero_luajit_xml_fast_path_for_filter_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_code = str(_load_sample_corpus_builds(1)[0]["pob_code"])
    sample_xml = decode_pob2_code_to_xml(sample_code)

    real_pob2 = get_pob2_paths().root_dir
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POE2BRAIN_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("POE2BRAIN_POB2_DIR", str(real_pob2))
    monkeypatch.delenv("POE2BRAIN_POB_SOCKET", raising=False)
    mcp_mod.close_services()
    close_default_pob2_bridge()

    # Forbid Pob2Bridge.ensure_started completely to prove 0 LuaJIT processes are spawned!
    def _forbid_luajit_boot(self: Pob2Bridge) -> None:
        raise AssertionError("LuaJIT Pob2Bridge.ensure_started must NOT be called on XML fast path")

    monkeypatch.setattr(Pob2Bridge, "ensure_started", _forbid_luajit_boot)

    # 1. Direct extract_build_filter_context with pob_code & None service
    ctx_cold = extract_build_filter_context(None, pob_code=sample_code)
    t0 = time.perf_counter()
    ctx_direct = extract_build_filter_context(None, pob_code=sample_code)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 15.0
    assert ctx_direct == ctx_cold
    assert ctx_direct == extract_build_filter_context_from_xml(sample_xml)
    assert ctx_direct.class_name is not None
    assert len(ctx_direct.weapon_filter_classes) >= 1

    # 2. CLI `poe2 filter generate --pob <code> --slot main --offline --json`
    runner = CliRunner()
    out_filter_1 = tmp_path / "fast_pob.filter"
    res_pob = runner.invoke(
        app,
        [
            "filter",
            "generate",
            "--pob",
            sample_code,
            "--slot",
            "main",
            "--offline",
            "--out",
            str(out_filter_1),
            "--json",
        ],
    )
    assert res_pob.exit_code == 0, res_pob.output
    payload_pob = json.loads(res_pob.stdout)
    assert payload_pob["validation"]["valid"] is True
    assert (cache_dir / "pob_slots" / "main.xml").is_file()

    # 3. CLI `poe2 filter generate --slot main --offline --json` (persisted slot XML fast path)
    out_filter_2 = tmp_path / "fast_slot.filter"
    res_slot = runner.invoke(
        app,
        [
            "filter",
            "generate",
            "--slot",
            "main",
            "--offline",
            "--out",
            str(out_filter_2),
            "--json",
        ],
    )
    assert res_slot.exit_code == 0, res_slot.output
    payload_slot = json.loads(res_slot.stdout)
    assert payload_slot["validation"]["valid"] is True
    assert payload_slot["build_context"]["class_name"] == ctx_direct.class_name
