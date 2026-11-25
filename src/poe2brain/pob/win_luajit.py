# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Windows LuaJIT 2.1 runner hosted via PathOfBuilding-PoE2's bundled MIT x64 lua51.dll.

Usage:
    python -m poe2brain.pob.win_luajit <script.lua> [args...]
    python -m poe2brain.pob.win_luajit -e "<lua code>"
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from poe2brain.config import get_pob2_paths


def _configure_lua_api(dll: ctypes.CDLL) -> None:
    dll.luaL_newstate.argtypes = []
    dll.luaL_newstate.restype = ctypes.c_void_p

    dll.luaL_openlibs.argtypes = [ctypes.c_void_p]
    dll.luaL_openlibs.restype = None

    dll.lua_close.argtypes = [ctypes.c_void_p]
    dll.lua_close.restype = None

    dll.lua_createtable.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    dll.lua_createtable.restype = None

    dll.lua_pushstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.lua_pushstring.restype = None

    dll.lua_rawseti.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    dll.lua_rawseti.restype = None

    dll.lua_setfield.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    dll.lua_setfield.restype = None

    dll.luaL_loadfile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.luaL_loadfile.restype = ctypes.c_int

    dll.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.luaL_loadstring.restype = ctypes.c_int

    dll.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dll.lua_pcall.restype = ctypes.c_int

    dll.lua_tolstring.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    dll.lua_tolstring.restype = ctypes.c_char_p


def _resolve_runtime_dir() -> Path:
    explicit = os.environ.get("POB2_RUNTIME_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return get_pob2_paths().runtime_dir


def run_lua(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "Usage: python -m poe2brain.pob.win_luajit [-e code | script.lua] [args]\n"
        )
        return 2

    runtime_dir = _resolve_runtime_dir()
    lua_dll_path = runtime_dir / "lua51.dll"
    if not lua_dll_path.is_file():
        sys.stderr.write(f"[win_luajit] Missing {lua_dll_path}\n")
        return 1

    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(runtime_dir))
    os.environ["PATH"] = f"{runtime_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    dll = ctypes.CDLL(str(lua_dll_path))
    _configure_lua_api(dll)

    state = dll.luaL_newstate()
    if not state:
        sys.stderr.write("[win_luajit] luaL_newstate failed\n")
        return 1

    try:
        dll.luaL_openlibs(state)

        eval_mode = argv[0] == "-e"
        if eval_mode:
            if len(argv) < 2:
                sys.stderr.write("[win_luajit] -e requires Lua source string\n")
                return 2
            code_bytes = argv[1].encode("utf-8")
            script_args = argv[2:]
            script_label = "-e"
        else:
            script_path = Path(argv[0]).resolve()
            script_args = argv[1:]
            script_label = str(script_path)

        # Populate Lua global `arg` table (LUA_GLOBALSINDEX = -10002 in Lua 5.1 / LuaJIT)
        dll.lua_createtable(state, len(script_args), 1)
        dll.lua_pushstring(state, script_label.encode("utf-8"))
        dll.lua_rawseti(state, -2, 0)
        for idx, item in enumerate(script_args, start=1):
            dll.lua_pushstring(state, item.encode("utf-8"))
            dll.lua_rawseti(state, -2, idx)
        dll.lua_setfield(state, -10002, b"arg")

        if eval_mode:
            rc = dll.luaL_loadstring(state, code_bytes)
        else:
            rc = dll.luaL_loadfile(state, str(script_path).encode("utf-8"))

        if rc == 0:
            rc = dll.lua_pcall(state, 0, 0, 0)

        if rc != 0:
            size = ctypes.c_size_t(0)
            msg_ptr = dll.lua_tolstring(state, -1, ctypes.byref(size))
            msg = msg_ptr.decode("utf-8", errors="replace") if msg_ptr else f"Lua error code {rc}"
            sys.stderr.write(f"[win_luajit] {msg}\n")
            sys.stderr.flush()
            return 1
        return 0
    finally:
        dll.lua_close(state)


def main() -> None:
    raise SystemExit(run_lua(sys.argv[1:]))


if __name__ == "__main__":
    main()
