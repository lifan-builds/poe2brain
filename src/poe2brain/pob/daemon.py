from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any

from poe2brain.pob.bridge import Pob2Bridge, Pob2BridgeError


class Pob2SocketDaemon:
    """Thread-safe local Unix-socket JSON-RPC proxy sharing one warm LuaJIT Pob2Bridge."""

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
            tmp_dir = Path(tempfile.mkdtemp(prefix="p2b_", dir="/tmp"))
            self._temp_dir: Path | None = tmp_dir
            self.socket_path = tmp_dir / "pob.sock"
        else:
            self._temp_dir = None
            self.socket_path = Path(socket_path)

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

    def start(self) -> Path:
        if self._server_sock is not None:
            return self.socket_path

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            srv.listen(32)
            srv.settimeout(0.5)
        except Exception:
            srv.close()
            raise

        if not self._lazy:
            self._bridge.ensure_started()

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
            if self.socket_path.exists():
                self.socket_path.unlink()
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
