from __future__ import annotations

import atexit
import json
import os
import queue
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from poe2brain.config import REPO_ROOT, Pob2RuntimePaths, get_pob2_paths
from poe2brain.pob.bootstrap import bootstrap_pob2_runtime

BRIDGE_LUA_PATH: Path = Path(__file__).resolve().parent / "lua" / "pob2_bridge.lua"
BOOTSTRAP_SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "bootstrap_pob2.sh"
POB_SOCKET_ENV_VAR: str = "POE2BRAIN_POB_SOCKET"
POB_IDLE_TIMEOUT_ENV_VAR: str = "POE2BRAIN_POB_IDLE_TIMEOUT"
DEFAULT_IDLE_TIMEOUT_SECONDS: float = 60.0


class Pob2BridgeError(RuntimeError):
    """Raised when the headless PathOfBuilding-PoE2 LuaJIT bridge fails."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _reap_popen(proc: subprocess.Popen[str]) -> None:
    """Unconditionally close stdio pipes and hard-SIGKILL + reap a LuaJIT subprocess."""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        if os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _idle_watchdog_entry(
    bridge_ref: weakref.ReferenceType[Pob2Bridge],
    proc: subprocess.Popen[str],
) -> None:
    while True:
        bridge = bridge_ref()
        if bridge is None or proc.poll() is not None:
            return
        timeout = bridge.idle_timeout_seconds
        if timeout <= 0:
            return
        poll_step = min(0.25, max(0.05, timeout / 4.0))
        bridge._idle_wake_event.wait(timeout=poll_step)

        bridge = bridge_ref()
        if bridge is None or proc.poll() is not None:
            return
        if not bridge._lock.acquire(blocking=False):
            continue
        try:
            if bridge._proc is not proc or proc.poll() is not None:
                return
            eff_timeout = bridge.idle_timeout_seconds
            if eff_timeout <= 0:
                return
            if (
                bridge._active_calls == 0
                and (time.monotonic() - bridge._last_activity_ts) >= eff_timeout
            ):
                bridge._hibernate_locked()
                return
        finally:
            bridge._lock.release()


class Pob2Bridge:
    """Thread-safe JSON-RPC 2.0 client for PathOfBuilding-PoE2 (stdio or Unix socket)."""

    def __init__(
        self,
        *,
        pob2_dir: Path | None = None,
        bridge_lua_path: Path | None = None,
        auto_bootstrap: bool = True,
        request_timeout: float = 120.0,
        bootstrap_timeout: float = 180.0,
        idle_timeout_seconds: float | None = None,
        socket_path: Path | str | None = None,
        use_daemon: bool = True,
    ) -> None:
        self._explicit_pob2_dir = pob2_dir
        self.bridge_lua_path = bridge_lua_path or BRIDGE_LUA_PATH
        self.auto_bootstrap = auto_bootstrap
        self.request_timeout = request_timeout
        self.bootstrap_timeout = bootstrap_timeout
        self._explicit_idle_timeout = idle_timeout_seconds
        self._explicit_socket_path = Path(socket_path) if socket_path is not None else None
        self.use_daemon = use_daemon
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._proc_finalizer: weakref.finalize[Any, subprocess.Popen[str]] | None = None
        self._sock: socket.socket | None = None
        self._sock_rfile: TextIO | None = None
        self._next_id = 0
        self._hibernated_snapshot: dict[str, Any] | None = None
        self._last_activity_ts = time.monotonic()
        self._active_calls = 0
        self._state_epoch = 0
        self._idle_wake_event = threading.Event()
        self._idle_thread: threading.Thread | None = None
        _LIVE_BRIDGES.add(self)

    @property
    def paths(self) -> Pob2RuntimePaths:
        return get_pob2_paths(self._explicit_pob2_dir)

    @property
    def idle_timeout_seconds(self) -> float:
        if self._explicit_idle_timeout is not None:
            return float(self._explicit_idle_timeout)
        raw = os.environ.get(POB_IDLE_TIMEOUT_ENV_VAR, "").strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                return DEFAULT_IDLE_TIMEOUT_SECONDS
        return DEFAULT_IDLE_TIMEOUT_SECONDS

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

    @property
    def is_hibernated(self) -> bool:
        with self._lock:
            return self._hibernated_snapshot is not None and not self.is_running

    @property
    def state_epoch(self) -> int:
        return self._state_epoch

    def __enter__(self) -> Pob2Bridge:
        self.ensure_started()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._close_sock_locked()
            self._kill_locked(preserve_snapshot=False)
        except Exception:
            pass

    def ensure_runtime_ready(self) -> Pob2RuntimePaths:
        paths = self.paths
        if paths.is_ready:
            return paths
        if not self.auto_bootstrap:
            raise Pob2BridgeError(
                f"Headless PoB2 runtime is not ready at {paths.root_dir}. "
                "Run 'poe2 bootstrap pob2' first."
            )
        try:
            return bootstrap_pob2_runtime(
                pob2_dir=self._explicit_pob2_dir,
                timeout=self.bootstrap_timeout,
            )
        except Exception as exc:
            raise Pob2BridgeError(f"Failed to bootstrap headless PoB2 runtime: {exc}") from exc

    def _start_idle_watchdog_locked(self, proc: subprocess.Popen[str]) -> None:
        if self.idle_timeout_seconds <= 0:
            return
        self._idle_wake_event.clear()
        thread = threading.Thread(
            target=_idle_watchdog_entry,
            args=(weakref.ref(self), proc),
            name="poe2brain-pob-idle-watchdog",
            daemon=True,
        )
        self._idle_thread = thread
        thread.start()

    def ensure_started(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            if self._proc is not None and self._proc.poll() is None:
                return

            sock_path = self.socket_path
            if sock_path is not None:
                tcp_desc: dict[str, Any] | None = None
                if sock_path.is_file():
                    try:
                        raw = sock_path.read_text(encoding="utf-8").strip()
                        if raw.startswith("{"):
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict) and parsed.get("transport") == "tcp":
                                tcp_desc = parsed
                    except (OSError, json.JSONDecodeError):
                        tcp_desc = None

                if tcp_desc is not None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.settimeout(self.request_timeout)
                        sock.connect((str(tcp_desc["host"]), int(tcp_desc["port"])))
                        auth_frame = json.dumps({"auth": str(tcp_desc["token"])}) + "\n"
                        sock.sendall(auth_frame.encode("utf-8"))
                        self._sock = sock
                        self._sock_rfile = sock.makefile("r", encoding="utf-8")
                        self._last_activity_ts = time.monotonic()
                        return
                    except OSError as exc:
                        sock.close()
                        raise Pob2BridgeError(
                            f"Failed to connect to PoB2 TCP daemon {sock_path}: {exc}"
                        ) from exc

                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.settimeout(self.request_timeout)
                    sock.connect(str(sock_path))
                    self._sock = sock
                    self._sock_rfile = sock.makefile("r", encoding="utf-8")
                    self._last_activity_ts = time.monotonic()
                    return
                except OSError as exc:
                    sock.close()
                    raise Pob2BridgeError(
                        f"Failed to connect to PoB2 daemon socket {sock_path}: {exc}"
                    ) from exc

            saved_snap = self._hibernated_snapshot
            self._kill_locked(preserve_snapshot=saved_snap is not None)
            paths = self.ensure_runtime_ready()
            if not self.bridge_lua_path.is_file():
                raise Pob2BridgeError(f"Missing pob2_bridge.lua at {self.bridge_lua_path}")

            env = os.environ.copy()
            existing_cpath = env.get("LUA_CPATH", "")
            if os.name == "nt":
                env["LUA_CPATH"] = (
                    f"{paths.runtime_dir}\\?.dll;{paths.bin_dir}\\?.dll;{existing_cpath};;"
                )
                env["POB2_RUNTIME_DIR"] = str(paths.runtime_dir)
                env["POE2BRAIN_PARENT_PID"] = str(os.getpid())
                env["PYTHONUTF8"] = "1"
            else:
                env["LUA_CPATH"] = f"{paths.bin_dir}/?.so;{existing_cpath};;"
            env["POB2_SRC_DIR"] = str(paths.src_dir)

            cmd = (
                [sys.executable, "-m", "poe2brain.pob.win_luajit", str(self.bridge_lua_path)]
                if paths.uses_windows_dll_host
                else [str(paths.luajit_bin), str(self.bridge_lua_path)]
            )
            popen_kwargs: dict[str, Any] = {
                "cwd": str(paths.src_dir),
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "bufsize": 1,
                "env": env,
                "start_new_session": (os.name != "nt"),
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(cmd, **popen_kwargs)
            self._proc = proc
            self._proc_finalizer = weakref.finalize(self, _reap_popen, proc)
            self._last_activity_ts = time.monotonic()

            if saved_snap is not None:
                self._hibernated_snapshot = None
                try:
                    self._send_and_recv_locked(
                        "restore_all_slots",
                        saved_snap,
                        timeout=self.request_timeout,
                    )
                except Exception:
                    self._hibernated_snapshot = saved_snap
                    self._kill_locked(preserve_snapshot=True)
                    raise

            self._last_activity_ts = time.monotonic()
            self._start_idle_watchdog_locked(proc)

    def hibernate(self) -> bool:
        """Snapshot all build slots in Python memory and terminate the idle LuaJIT child."""
        with self._lock:
            return self._hibernate_locked()

    def _hibernate_locked(self) -> bool:
        if self._sock is not None or self._proc is None:
            return False
        if self._proc.poll() is None:
            try:
                snap = self._send_and_recv_locked("snapshot_all_slots", {}, timeout=5.0)
                if isinstance(snap, Mapping) and "slots" in snap:
                    self._hibernated_snapshot = dict(snap)
            except Exception:
                pass
        self._kill_locked(preserve_snapshot=self._hibernated_snapshot is not None)
        return self._hibernated_snapshot is not None

    def restart(self) -> dict[str, Any]:
        with self._lock:
            self._hibernated_snapshot = None
            if self.socket_path is not None:
                self._close_sock_locked()
                self._state_epoch += 1
                self.ensure_started()
                return self.ping()
            self._kill_locked(preserve_snapshot=False)
            self.ensure_started()
            return self.ping()

    def close(self) -> None:
        with self._lock:
            self._hibernated_snapshot = None
            self._close_sock_locked()
            self._kill_locked(preserve_snapshot=False)

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

    def _kill_locked(self, *, preserve_snapshot: bool = False) -> None:
        proc = self._proc
        fin = self._proc_finalizer
        self._proc = None
        self._proc_finalizer = None
        self._idle_wake_event.set()
        if not preserve_snapshot:
            self._hibernated_snapshot = None
            if proc is not None:
                self._state_epoch += 1
        if fin is not None:
            fin.detach()
        if proc is not None:
            _reap_popen(proc)

    def _read_stderr_tail(self, proc: subprocess.Popen[str]) -> str:
        if proc.stderr is None or proc.stderr.closed:
            return ""
        if proc.poll() is None:
            return ""
        try:
            text = proc.stderr.read()
        except (OSError, ValueError):
            return ""
        return text.strip()[-1000:]

    def _readline_with_timeout(self, proc: subprocess.Popen[str], timeout: float) -> str:
        assert proc.stdout is not None
        if os.name == "nt":
            out_q: queue.Queue[tuple[str | None, BaseException | None]] = queue.Queue(maxsize=1)

            def _reader() -> None:
                try:
                    assert proc.stdout is not None
                    out_q.put((proc.stdout.readline(), None))
                except BaseException as exc:
                    out_q.put((None, exc))

            t = threading.Thread(target=_reader, name="poe2brain-win-readline", daemon=True)
            t.start()
            try:
                line, err = out_q.get(timeout=timeout)
            except queue.Empty as exc:
                raise TimeoutError(f"PoB2 bridge timed out after {timeout:.1f}s") from exc
            if err is not None:
                raise err
            return line or ""

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
            self._state_epoch += 1
            raise Pob2BridgeError(f"Timeout calling PoB2 daemon method '{method}': {exc}") from exc
        except OSError as exc:
            self._close_sock_locked()
            self._state_epoch += 1
            raise Pob2BridgeError(
                f"Broken socket calling PoB2 daemon method '{method}': {exc}"
            ) from exc

        if not line:
            self._close_sock_locked()
            self._state_epoch += 1
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
            self._kill_locked(preserve_snapshot=False)
            raise Pob2BridgeError(f"Timeout calling PoB2 method '{method}': {exc}") from exc
        except OSError as exc:
            stderr_tail = self._read_stderr_tail(proc)
            self._kill_locked(preserve_snapshot=False)
            raise Pob2BridgeError(
                f"Broken pipe calling PoB2 method '{method}': {exc} {stderr_tail}".strip()
            ) from exc

        if not line:
            rc = proc.poll()
            stderr_tail = self._read_stderr_tail(proc)
            self._kill_locked(preserve_snapshot=False)
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
            self._active_calls += 1
            self._last_activity_ts = time.monotonic()
            try:
                was_already_running = self._sock is not None or (
                    self._proc is not None and self._proc.poll() is None
                )
                self.ensure_started()
                try:
                    return self._send_and_recv_locked(method, params, timeout=eff_timeout)
                except Pob2BridgeError as exc:
                    # Only auto-retry once if the underlying subprocess/socket died cleanly
                    # (never retry logical RPC errors where the transport is still alive).
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
            finally:
                self._last_activity_ts = time.monotonic()
                self._active_calls = max(0, self._active_calls - 1)

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
_DEFAULT_BRIDGE_LOCK = threading.Lock()
_DEFAULT_BRIDGE: Pob2Bridge | None = None


def get_default_pob2_bridge() -> Pob2Bridge:
    """Return the process-wide shared Pob2Bridge singleton."""
    global _DEFAULT_BRIDGE
    target_sock = os.environ.get(POB_SOCKET_ENV_VAR, "").strip() or None
    target_pob_dir = str(get_pob2_paths().root_dir)
    with _DEFAULT_BRIDGE_LOCK:
        if _DEFAULT_BRIDGE is not None:
            cur_sock = (
                str(_DEFAULT_BRIDGE.socket_path)
                if _DEFAULT_BRIDGE.socket_path is not None
                else None
            )
            cur_dir = str(_DEFAULT_BRIDGE.paths.root_dir)
            if cur_sock != target_sock or cur_dir != target_pob_dir:
                _DEFAULT_BRIDGE.close()
                _DEFAULT_BRIDGE = None
        if _DEFAULT_BRIDGE is None:
            _DEFAULT_BRIDGE = Pob2Bridge(request_timeout=120.0)
        return _DEFAULT_BRIDGE


def close_default_pob2_bridge() -> None:
    """Close and reset the process-wide shared Pob2Bridge singleton."""
    global _DEFAULT_BRIDGE
    with _DEFAULT_BRIDGE_LOCK:
        if _DEFAULT_BRIDGE is not None:
            try:
                _DEFAULT_BRIDGE.close()
            except Exception:
                pass
            _DEFAULT_BRIDGE = None


def _close_live_bridges() -> None:
    close_default_pob2_bridge()
    for bridge in list(_LIVE_BRIDGES):
        try:
            bridge.close()
        except Exception:
            pass


atexit.register(_close_live_bridges)
