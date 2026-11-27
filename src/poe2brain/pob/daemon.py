# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import weakref
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from poe2brain.pob.bridge import Pob2Bridge, Pob2BridgeError


class Pob2SocketDaemon:
    """Thread-safe local JSON-RPC proxy sharing one warm LuaJIT Pob2Bridge.

    Uses AF_UNIX on POSIX and token-authenticated 127.0.0.1 TCP loopback
    (descriptor written to `socket_path`) on Windows.
    """

    def __init__(
        self,
        *,
        socket_path: Path | str | None = None,
        pob2_dir: Path | None = None,
        auto_bootstrap: bool = True,
        request_timeout: float = 120.0,
        lazy: bool = True,
    ) -> None:
        self._owns_temp_dir = socket_path is None
        if socket_path is None:
            base_tmp = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
            tmp_dir = Path(tempfile.mkdtemp(prefix="p2b_", dir=base_tmp))
            self._temp_dir: Path | None = tmp_dir
            self.socket_path = tmp_dir / ("pob.tcp.json" if os.name == "nt" else "pob.sock")
        else:
            self._temp_dir = None
            self.socket_path = Path(socket_path)

        self._use_tcp = os.name == "nt" or not hasattr(socket, "AF_UNIX")
        self._auth_token: str | None = None

        self._bridge = Pob2Bridge(
            pob2_dir=pob2_dir,
            auto_bootstrap=auto_bootstrap,
            request_timeout=request_timeout,
            use_daemon=False,
        )
        self._lazy = lazy
        self._stop_event = threading.Event()
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._clients_lock = threading.Lock()
        _LIVE_DAEMONS.add(self)

    @property
    def bridge(self) -> Pob2Bridge:
        return self._bridge

    @property
    def is_running(self) -> bool:
        return self._server_sock is not None and not self._stop_event.is_set()

    def _publish_tcp_descriptor(self, port: int, token: str) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=self.socket_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.chmod(tmp_name, 0o600)
                json.dump(
                    {"transport": "tcp", "host": "127.0.0.1", "port": port, "token": token},
                    handle,
                )
            os.replace(tmp_name, self.socket_path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def start(self) -> Path:
        if self._server_sock is not None:
            return self.socket_path

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        family = socket.AF_INET if self._use_tcp else socket.AF_UNIX
        token = None
        with ExitStack() as rollback:
            srv = socket.socket(family, socket.SOCK_STREAM)
            rollback.callback(srv.close)
            if self._use_tcp:
                srv.bind(("127.0.0.1", 0))
            else:
                self.socket_path.unlink(missing_ok=True)
                srv.bind(str(self.socket_path))
                rollback.callback(self.socket_path.unlink, missing_ok=True)
                self.socket_path.chmod(0o600)
            srv.listen(32)
            srv.settimeout(0.5)
            if self._use_tcp:
                token = secrets.token_hex(24)
                self._publish_tcp_descriptor(srv.getsockname()[1], token)
                rollback.callback(self.socket_path.unlink, missing_ok=True)
            if not self._lazy:
                self._bridge.ensure_started()
            rollback.pop_all()

        self._auth_token = token
        self._stop_event.clear()
        self._server_sock = srv
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="poe2brain-pob-daemon",
            daemon=True,
        )
        self._accept_thread.start()
        return self.socket_path

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop_event.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_client,
                args=(conn,),
                name="poe2brain-pob-client",
                daemon=True,
            )
            with self._clients_lock:
                self._client_threads.add(t)
            t.start()

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            with conn:
                rfile = conn.makefile("r", encoding="utf-8", errors="replace")
                try:
                    if self._auth_token is not None:
                        auth_line = rfile.readline()
                        if not auth_line:
                            return
                        try:
                            auth_obj = json.loads(auth_line)
                        except json.JSONDecodeError:
                            return
                        if (
                            not isinstance(auth_obj, dict)
                            or auth_obj.get("auth") != self._auth_token
                        ):
                            return
                    for raw_line in rfile:
                        if self._stop_event.is_set():
                            break
                        line = raw_line.strip()
                        if not line:
                            continue
                        resp = self._dispatch_line(line)
                        payload = (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")
                        conn.sendall(payload)
                finally:
                    try:
                        rfile.close()
                    except OSError:
                        pass
        except OSError:
            pass
        finally:
            cur = threading.current_thread()
            with self._clients_lock:
                self._client_threads.discard(cur)

    def _dispatch_line(self, line: str) -> dict[str, Any]:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }

        if not isinstance(req, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params")

        if not isinstance(method, str) or not method:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method!r}"},
            }

        if method == "shutdown":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": "Remote shutdown is disabled on shared daemon",
                },
            }

        try:
            result = self._bridge.call(
                method,
                params if isinstance(params, dict) else {},
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Pob2BridgeError as exc:
            err_obj: dict[str, Any] = {"message": str(exc)}
            if exc.code is not None:
                err_obj["code"] = exc.code
            return {"jsonrpc": "2.0", "id": req_id, "error": err_obj}
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def close(self) -> None:
        self._stop_event.set()
        srv = self._server_sock
        self._server_sock = None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None

        self._bridge.close()

        try:
            if srv is not None:
                self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._owns_temp_dir and self._temp_dir is not None and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self) -> Pob2SocketDaemon:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


_LIVE_DAEMONS: weakref.WeakSet[Pob2SocketDaemon] = weakref.WeakSet()


def _close_live_daemons() -> None:
    for daemon in list(_LIVE_DAEMONS):
        try:
            daemon.close()
        except Exception:
            pass


atexit.register(_close_live_daemons)
