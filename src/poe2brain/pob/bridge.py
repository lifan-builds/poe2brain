from __future__ import annotations

import atexit
import json
import os
import selectors
import socket
import subprocess
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from poe2brain.config import REPO_ROOT, Pob2RuntimePaths, get_pob2_paths

BRIDGE_LUA_PATH: Path = Path(__file__).resolve().parent / "lua" / "pob2_bridge.lua"
BOOTSTRAP_SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "bootstrap_pob2.sh"
POB_SOCKET_ENV_VAR: str = "POE2BRAIN_POB_SOCKET"


class Pob2BridgeError(RuntimeError):
    """Raised when the headless PathOfBuilding-PoE2 LuaJIT bridge fails."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class Pob2Bridge:
    """Thread-safe JSON-RPC 2.0 client for PathOfBuilding-PoE2 (stdio or Unix socket)."""

    def __init__(
        self,
        *,
        pob2_dir: Path | None = None,
        bridge_lua_path: Path | None = None,
        auto_bootstrap: bool = True,
        request_timeout: float = 45.0,
        bootstrap_timeout: float = 180.0,
        socket_path: Path | str | None = None,
        use_daemon: bool = True,
    ) -> None:
        self._explicit_pob2_dir = pob2_dir
        self.bridge_lua_path = bridge_lua_path or BRIDGE_LUA_PATH
        self.auto_bootstrap = auto_bootstrap
        self.request_timeout = request_timeout
        self.bootstrap_timeout = bootstrap_timeout
        self._explicit_socket_path = Path(socket_path) if socket_path is not None else None
        self.use_daemon = use_daemon
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._sock: socket.socket | None = None
        self._sock_rfile: TextIO | None = None
        self._next_id = 0
        _LIVE_BRIDGES.add(self)

    @property
    def paths(self) -> Pob2RuntimePaths:
        return get_pob2_paths(self._explicit_pob2_dir)

    @property
    def socket_path(self) -> Path | None:
        if not self.use_daemon:
            return None
        if self._explicit_socket_path is not None:
            return self._explicit_socket_path
        if self._explicit_pob2_dir is not None:
            return None
        env_sock = os.environ.get(POB_SOCKET_ENV_VAR, "").strip()
        return Path(env_sock) if env_sock else None

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._sock is not None:
                return True
            return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> Pob2Bridge:
        self.ensure_started()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._close_sock_locked()
            self._kill_locked()
        except Exception:
            pass

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
            if self._sock is not None:
                return
            if self._proc is not None and self._proc.poll() is None:
                return

            sock_path = self.socket_path
            if sock_path is not None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.settimeout(self.request_timeout)
                    sock.connect(str(sock_path))
                    self._sock = sock
                    self._sock_rfile = sock.makefile("r", encoding="utf-8")
                    return
                except OSError as exc:
                    sock.close()
                    raise Pob2BridgeError(
                        f"Failed to connect to PoB2 daemon socket {sock_path}: {exc}"
                    ) from exc

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
            if self.socket_path is not None:
                self._close_sock_locked()
                self.ensure_started()
                return self.ping()
            self._kill_locked()
            self.ensure_started()
            return self.ping()

    def close(self) -> None:
        with self._lock:
            self._close_sock_locked()
            if self._proc is None:
                return
            if self._proc.poll() is None:
                try:
                    self._send_and_recv_locked("shutdown", {}, timeout=3.0)
                except Exception:
                    pass
            self._kill_locked()

    def _close_sock_locked(self) -> None:
        rfile = self._sock_rfile
        sock = self._sock
        self._sock_rfile = None
        self._sock = None
        if rfile is not None:
            try:
                rfile.close()
            except OSError:
                pass
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

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

    def _parse_response_frame(self, line: str, *, method: str, req_id: int) -> Any:
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

    def _send_and_recv_socket_locked(
        self,
        method: str,
        frame: str,
        *,
        req_id: int,
        timeout: float,
    ) -> Any:
        assert self._sock is not None and self._sock_rfile is not None
        sock = self._sock
        rfile = self._sock_rfile
        try:
            sock.settimeout(timeout)
            sock.sendall((frame + "\n").encode("utf-8"))
            line = rfile.readline()
        except TimeoutError as exc:
            self._close_sock_locked()
            raise Pob2BridgeError(
                f"Timeout calling PoB2 daemon method '{method}': {exc}"
            ) from exc
        except OSError as exc:
            self._close_sock_locked()
            raise Pob2BridgeError(
                f"Broken socket calling PoB2 daemon method '{method}': {exc}"
            ) from exc

        if not line:
            self._close_sock_locked()
            raise Pob2BridgeError(f"PoB2 daemon closed connection unexpectedly on '{method}'.")

        return self._parse_response_frame(line, method=method, req_id=req_id)

    def _send_and_recv_locked(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        timeout: float,
    ) -> Any:
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

        if self._sock is not None:
            return self._send_and_recv_socket_locked(method, frame, req_id=req_id, timeout=timeout)

        assert self._proc is not None
        proc = self._proc
        if proc.stdin is None or proc.stdout is None:
            raise Pob2BridgeError("PoB2 bridge stdio pipes are closed")

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

        return self._parse_response_frame(line, method=method, req_id=req_id)

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
            was_already_running = self._sock is not None or (
                self._proc is not None and self._proc.poll() is None
            )
            self.ensure_started()
            try:
                return self._send_and_recv_locked(method, params, timeout=eff_timeout)
            except Pob2BridgeError as exc:
                # Only auto-retry once if the underlying subprocess/socket died cleanly
                # (never retry logical RPC validation errors where the transport is still alive).
                if (
                    retry_on_crash
                    and was_already_running
                    and exc.code is None
                    and self._proc is None
                    and self._sock is None
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


_LIVE_BRIDGES: weakref.WeakSet[Pob2Bridge] = weakref.WeakSet()


def _close_live_bridges() -> None:
    for bridge in list(_LIVE_BRIDGES):
        try:
            bridge.close()
        except Exception:
            pass


atexit.register(_close_live_bridges)
