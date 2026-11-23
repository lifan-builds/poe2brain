from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from poe2brain.config import REPO_ROOT, Pob2RuntimePaths, get_pob2_paths

BRIDGE_LUA_PATH: Path = Path(__file__).resolve().parent / "lua" / "pob2_bridge.lua"
BOOTSTRAP_SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "bootstrap_pob2.sh"


class Pob2BridgeError(RuntimeError):
    """Raised when the headless PathOfBuilding-PoE2 LuaJIT bridge fails."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class Pob2Bridge:
    """Persistent, thread-safe stdio JSON-RPC 2.0 client for PathOfBuilding-PoE2."""

    def __init__(
        self,
        *,
        pob2_dir: Path | None = None,
        bridge_lua_path: Path | None = None,
        auto_bootstrap: bool = True,
        request_timeout: float = 45.0,
        bootstrap_timeout: float = 180.0,
    ) -> None:
        self._explicit_pob2_dir = pob2_dir
        self.bridge_lua_path = bridge_lua_path or BRIDGE_LUA_PATH
        self.auto_bootstrap = auto_bootstrap
        self.request_timeout = request_timeout
        self.bootstrap_timeout = bootstrap_timeout
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0

    @property
    def paths(self) -> Pob2RuntimePaths:
        return get_pob2_paths(self._explicit_pob2_dir)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> Pob2Bridge:
        self.ensure_started()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def ensure_runtime_ready(self) -> Pob2RuntimePaths:
        paths = self.paths
        if paths.is_ready:
            return paths
        if not self.auto_bootstrap:
            raise Pob2BridgeError(
                f"Headless PoB2 runtime is not ready at {paths.root_dir}. "
                f"Run {BOOTSTRAP_SCRIPT_PATH} first."
            )
        if not BOOTSTRAP_SCRIPT_PATH.is_file():
            raise Pob2BridgeError(f"Missing PoB2 bootstrap script at {BOOTSTRAP_SCRIPT_PATH}")

        env = os.environ.copy()
        if self._explicit_pob2_dir is not None:
            env["POE2BRAIN_CACHE_DIR"] = str(self._explicit_pob2_dir.parent)

        proc = subprocess.run(
            ["bash", str(BOOTSTRAP_SCRIPT_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=self.bootstrap_timeout,
            check=False,
        )
        refreshed = self.paths
        if proc.returncode != 0 or not refreshed.is_ready:
            detail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise Pob2BridgeError(f"bootstrap_pob2.sh failed (rc={proc.returncode}): {detail}")
        return refreshed

    def ensure_started(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._kill_locked()
            paths = self.ensure_runtime_ready()
            if not self.bridge_lua_path.is_file():
                raise Pob2BridgeError(f"Missing pob2_bridge.lua at {self.bridge_lua_path}")

            env = os.environ.copy()
            existing_cpath = env.get("LUA_CPATH", "")
            env["LUA_CPATH"] = f"{paths.bin_dir}/?.so;{existing_cpath};;"
            env["POB2_SRC_DIR"] = str(paths.src_dir)

            self._proc = subprocess.Popen(
                [str(paths.luajit_bin), str(self.bridge_lua_path)],
                cwd=str(paths.src_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )

    def restart(self) -> dict[str, Any]:
        with self._lock:
            self._kill_locked()
            self.ensure_started()
            return self.ping()

    def close(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            if self._proc.poll() is None:
                try:
                    self._send_and_recv_locked("shutdown", {}, timeout=3.0)
                except Exception:
                    pass
            self._kill_locked()

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _read_stderr_tail(self, proc: subprocess.Popen[str]) -> str:
        if proc.stderr is None:
            return ""
        if proc.poll() is None:
            return ""
        try:
            text = proc.stderr.read()
        except OSError:
            return ""
        return text.strip()[-1000:]

    def _readline_with_timeout(self, proc: subprocess.Popen[str], timeout: float) -> str:
        assert proc.stdout is not None
        if os.name == "nt":
            return proc.stdout.readline()

        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"PoB2 bridge timed out after {timeout:.1f}s")
                events = sel.select(timeout=min(remaining, 0.5))
                if events:
                    return proc.stdout.readline()
                if proc.poll() is not None:
                    return proc.stdout.readline()

    def _send_and_recv_locked(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float,
    ) -> Any:
        assert self._proc is not None
        proc = self._proc
        if proc.stdin is None or proc.stdout is None:
            raise Pob2BridgeError("PoB2 bridge stdio pipes are closed")

        self._next_id += 1
        req_id = self._next_id
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": dict(params) if params is not None else {},
            },
            ensure_ascii=False,
        )
        try:
            proc.stdin.write(frame + "\n")
            proc.stdin.flush()
            line = self._readline_with_timeout(proc, timeout)
        except TimeoutError as exc:
            self._kill_locked()
            raise Pob2BridgeError(f"Timeout calling PoB2 method '{method}': {exc}") from exc
        except OSError as exc:
            stderr_tail = self._read_stderr_tail(proc)
            self._kill_locked()
            raise Pob2BridgeError(
                f"Broken pipe calling PoB2 method '{method}': {exc} {stderr_tail}".strip()
            ) from exc

        if not line:
            rc = proc.poll()
            stderr_tail = self._read_stderr_tail(proc)
            self._kill_locked()
            raise Pob2BridgeError(
                f"PoB2 bridge exited unexpectedly (rc={rc}) on '{method}'. {stderr_tail}".strip()
            )

        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Pob2BridgeError(
                f"Malformed JSON-RPC frame from PoB2 on '{method}': {line[:200]!r}"
            ) from exc

        if resp.get("id") != req_id:
            raise Pob2BridgeError(
                f"JSON-RPC id mismatch on '{method}': expected {req_id}, got {resp.get('id')!r}"
            )

        err = resp.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, Mapping) else None
            msg = err.get("message") if isinstance(err, Mapping) else str(err)
            raise Pob2BridgeError(str(msg), code=int(code) if isinstance(code, int) else None)

        return resp.get("result")

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        retry_on_crash: bool = True,
    ) -> Any:
        eff_timeout = self.request_timeout if timeout is None else timeout
        with self._lock:
            was_already_running = self._proc is not None and self._proc.poll() is None
            self.ensure_started()
            try:
                return self._send_and_recv_locked(method, params, timeout=eff_timeout)
            except Pob2BridgeError as exc:
                # Only auto-retry once if the underlying subprocess died/restarted cleanly
                # (never retry logical RPC validation errors where self._proc is still alive).
                if (
                    retry_on_crash
                    and was_already_running
                    and exc.code is None
                    and self._proc is None
                    and method != "shutdown"
                ):
                    self.ensure_started()
                    return self._send_and_recv_locked(method, params, timeout=eff_timeout)
                raise

    def ping(self) -> dict[str, Any]:
        res = self.call("ping")
        return dict(res) if isinstance(res, Mapping) else {"ok": False}

    def call_batch(self, calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        payload_calls = [
            {"method": str(c["method"]), "params": dict(c.get("params") or {})} for c in calls
        ]
        res = self.call("call_batch", {"calls": payload_calls})
        if isinstance(res, Mapping) and isinstance(res.get("results"), list):
            return list(res["results"])
        return []
