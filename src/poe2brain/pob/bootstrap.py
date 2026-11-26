# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Cross-platform PathOfBuilding-PoE2 runtime bootstrapper (Windows, macOS, Linux).

On Windows:
  - Zero C compiler required: uses MIT PathOfBuilding-PoE2's bundled x64
    `runtime/lua51.dll` + `runtime/lua-utf8.dll` + `runtime/zlib1.dll` hosted via
    `python -m poe2brain.pob.win_luajit`.
  - Auto-reuses an existing local Path of Building Community (PoE2) installation
    if present, or downloads the pinned MIT PathOfBuilding-PoE2 source archive.

On macOS / Linux:
  - Runs `scripts/bootstrap_pob2.sh` when available (fast native LuaJIT 2.1 +
    luautf8 build), or falls back to a self-contained Python POSIX builder so
    wheel/PyPI installs without `scripts/` work out of the box.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

from poe2brain.config import (
    LUAJIT_COMMIT,
    LUAUTF8_VERSION,
    POB2_PINNED_COMMIT,
    REPO_ROOT,
    USER_AGENT,
    Pob2RuntimePaths,
    get_pob2_paths,
)

POB2_UPSTREAM_REPO = "https://github.com/PathOfBuildingCommunity/PathOfBuilding-PoE2"
POB2_UPSTREAM_GIT = f"{POB2_UPSTREAM_REPO}.git"
BOOTSTRAP_SCRIPT_PATH: Path = REPO_ROOT / "scripts" / "bootstrap_pob2.sh"

_SMOKE_LUA = r"""
local count = 1
count += 41
assert(count == 42, "LuaJIT += operator missing")
local s = "po"
s ..= "b2"
assert(s == "pob2", "LuaJIT ..= operator missing")
local utf8 = require("lua-utf8")
assert(utf8.len("流放之路2") == 5, "lua-utf8 failed")
local ffi = require("ffi")
local z = nil
for _, name in ipairs({"zlib1", "z", "libz.dylib", "libz.so.1"}) do
  local ok, lib = pcall(ffi.load, name)
  if ok and lib then z = lib; break end
end
assert(z ~= nil, "zlib FFI load failed")
ffi.cdef("const char *zlibVersion(void);")
assert(#ffi.string(z.zlibVersion()) > 0, "zlib FFI failed")
"""


def _windows_existing_pob2_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("POE2BRAIN_POB2_APP_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for env_key in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env_key, "").strip()
        if not base:
            continue
        b = Path(base)
        candidates.extend(
            [
                b / "Programs" / "Path of Building Community (PoE2)",
                b / "Path of Building Community (PoE2)",
                b / "Path of Building (PoE2)",
            ]
        )
    return candidates


def _is_valid_windows_pob2_tree(root: Path) -> bool:
    return (
        (root / "src" / "HeadlessWrapper.lua").is_file()
        and (root / "runtime" / "lua" / "dkjson.lua").is_file()
        and (root / "runtime" / "lua51.dll").is_file()
        and (root / "runtime" / "lua-utf8.dll").is_file()
    )


def _download_pob2_archive(dest_repo_dir: Path, ref: str) -> str:
    url = f"{POB2_UPSTREAM_REPO}/archive/{ref}.zip"
    with tempfile.TemporaryDirectory(prefix="p2b_pob2_dl_") as td:
        zip_path = Path(td) / "pob2.zip"
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=180.0,
        ) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with zip_path.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=131072):
                        fh.write(chunk)

        extract_root = Path(td) / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_root)

        top_dirs = [p for p in extract_root.iterdir() if p.is_dir()]
        if not top_dirs:
            raise RuntimeError(f"Downloaded PoB2 archive from {url} contained no root directory")
        src_root = top_dirs[0]

        if dest_repo_dir.exists():
            shutil.rmtree(dest_repo_dir)
        dest_repo_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_root), str(dest_repo_dir))
    return ref


def _smoke_test_windows_runtime(paths: Pob2RuntimePaths) -> bool:
    if not paths.is_ready:
        return False
    env = os.environ.copy()
    env["POB2_RUNTIME_DIR"] = str(paths.runtime_dir)
    env["LUA_CPATH"] = f"{paths.runtime_dir}\\?.dll;{paths.bin_dir}\\?.dll;;"
    cmd = (
        [str(paths.luajit_bin), "-e", _SMOKE_LUA]
        if paths.luajit_bin.is_file()
        else [sys.executable, "-m", "poe2brain.pob.win_luajit", "-e", _SMOKE_LUA]
    )
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, check=False)
    return proc.returncode == 0


def _smoke_test_posix_runtime(paths: Pob2RuntimePaths) -> bool:
    if not (paths.luajit_bin.is_file() and paths.luautf8_lib.is_file()):
        return False
    env = os.environ.copy()
    env["LUA_CPATH"] = f"{paths.bin_dir}/?.so;;"
    proc = subprocess.run(
        [str(paths.luajit_bin), "-e", _SMOKE_LUA],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return proc.returncode == 0


def _bootstrap_windows(paths: Pob2RuntimePaths, *, force: bool = False) -> Pob2RuntimePaths:
    paths.root_dir.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)

    if not force and _smoke_test_windows_runtime(paths):
        return paths

    ref = os.environ.get("POE2BRAIN_POB2_REF", POB2_PINNED_COMMIT).strip() or POB2_PINNED_COMMIT
    source_note = "github_archive"

    if force and paths.repo_dir.exists():
        shutil.rmtree(paths.repo_dir)

    if not _is_valid_windows_pob2_tree(paths.repo_dir):
        reused = False
        for cand in _windows_existing_pob2_candidates():
            if _is_valid_windows_pob2_tree(cand):
                shutil.copytree(cand, paths.repo_dir, dirs_exist_ok=True)
                source_note = f"local_pob2:{cand}"
                reused = True
                break
        if not reused:
            _download_pob2_archive(paths.repo_dir, ref)

    # Mirror DLLs into bin_dir for consistent layout checks
    for dll_name in ("lua51.dll", "lua-utf8.dll", "zlib1.dll"):
        src_dll = paths.runtime_dir / dll_name
        if src_dll.is_file():
            shutil.copy2(src_dll, paths.bin_dir / dll_name)

    if not _smoke_test_windows_runtime(paths):
        raise RuntimeError(
            f"Windows PoB2 runtime smoke test failed at {paths.repo_dir} "
            f"(lua51.dll={paths.win_lua_dll.is_file()}, "
            f"lua-utf8.dll={paths.win_luautf8_dll.is_file()})"
        )

    manifest = {
        "platform": "windows",
        "luajit_host": "win_luajit_ctypes_lua51_dll",
        "pob2_upstream": POB2_UPSTREAM_GIT,
        "pob2_license": "MIT",
        "pob2_ref": ref,
        "source": source_note,
        "pob2_repo_dir": str(paths.repo_dir),
        "pob2_src_dir": str(paths.src_dir),
        "pob2_runtime_dir": str(paths.runtime_dir),
    }
    (paths.root_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return paths


def _bootstrap_posix_python(
    paths: Pob2RuntimePaths, *, force: bool = False, timeout: float = 240.0
) -> Pob2RuntimePaths:
    paths.root_dir.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)

    if force or not _smoke_test_posix_runtime(paths):
        with tempfile.TemporaryDirectory(prefix="p2b_luajit_") as td:
            td_path = Path(td)
            lj_tar = td_path / "luajit.tar.gz"
            u8_tar = td_path / "luautf8.tar.gz"
            with httpx.Client(
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=120.0,
            ) as client:
                for url, dst in (
                    (f"https://github.com/LuaJIT/LuaJIT/archive/{LUAJIT_COMMIT}.tar.gz", lj_tar),
                    (
                        f"https://github.com/starwing/luautf8/archive/refs/tags/{LUAUTF8_VERSION}.tar.gz",
                        u8_tar,
                    ),
                ):
                    r = client.get(url)
                    r.raise_for_status()
                    dst.write_bytes(r.content)

            lj_dir = td_path / "luajit"
            u8_dir = td_path / "luautf8"
            lj_dir.mkdir()
            u8_dir.mkdir()
            with tarfile.open(lj_tar) as tf:
                tf.extractall(lj_dir)
            with tarfile.open(u8_tar) as tf:
                tf.extractall(u8_dir)
            lj_src = next(lj_dir.iterdir()) / "src"
            u8_src = next(u8_dir.iterdir())

            env = os.environ.copy()
            jobs = str(os.cpu_count() or 4)
            if sys.platform == "darwin":
                env.setdefault("MACOSX_DEPLOYMENT_TARGET", "11.0")
            subprocess.run(
                ["make", "-C", str(lj_src), f"-j{jobs}", "BUILDMODE=static", "amalg"],
                env=env,
                check=True,
                timeout=timeout,
            )
            shutil.copy2(lj_src / "luajit", paths.luajit_bin)
            os.chmod(paths.luajit_bin, 0o755)

            cc = os.environ.get("CC", "cc")
            shared_flags = (
                ["-bundle", "-undefined", "dynamic_lookup"]
                if sys.platform == "darwin"
                else ["-shared"]
            )
            subprocess.run(
                [
                    cc,
                    "-O2",
                    "-fPIC",
                    *shared_flags,
                    f"-I{lj_src}",
                    str(u8_src / "lutf8lib.c"),
                    "-o",
                    str(paths.luautf8_lib),
                ],
                env=env,
                check=True,
                timeout=timeout,
            )

    ref = os.environ.get("POE2BRAIN_POB2_REF", POB2_PINNED_COMMIT).strip() or POB2_PINNED_COMMIT
    if force and paths.repo_dir.exists():
        shutil.rmtree(paths.repo_dir)
    if not (paths.src_dir / "HeadlessWrapper.lua").is_file():
        _download_pob2_archive(paths.repo_dir, ref)

    if not paths.is_ready:
        raise RuntimeError(
            f"POSIX PoB2 bootstrap did not produce a ready runtime at {paths.root_dir}"
        )
    return paths


def bootstrap_pob2_runtime(
    *,
    pob2_dir: Path | None = None,
    force: bool = False,
    timeout: float = 240.0,
) -> Pob2RuntimePaths:
    """Provision the headless PathOfBuilding-PoE2 runtime on Windows, macOS, or Linux."""
    paths = get_pob2_paths(pob2_dir)
    if not force and paths.is_ready:
        return paths

    if os.name == "nt":
        return _bootstrap_windows(paths, force=force)

    if BOOTSTRAP_SCRIPT_PATH.is_file():
        env = os.environ.copy()
        if pob2_dir is not None:
            env["POE2BRAIN_CACHE_DIR"] = str(pob2_dir.parent)
        args = ["bash", str(BOOTSTRAP_SCRIPT_PATH)]
        if force:
            args.append("--force")
        proc = subprocess.run(
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        refreshed = get_pob2_paths(pob2_dir)
        if proc.returncode != 0 or not refreshed.is_ready:
            detail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise RuntimeError(f"bootstrap_pob2.sh failed (rc={proc.returncode}): {detail}")
        return refreshed

    return _bootstrap_posix_python(paths, force=force, timeout=timeout)
