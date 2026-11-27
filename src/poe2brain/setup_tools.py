# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Zero-JSON MCP setup, domestic mirror helpers, environment doctor, update & uninstall."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from poe2brain.config import (
    DEFAULT_LEAGUE,
    POE2_PATCH,
    REPO_ROOT,
    USER_AGENT,
    get_cache_dir,
    get_gamedata_db_path,
    get_pob2_paths,
)

CN_PYPI_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
CN_PYPI_FALLBACK_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
CN_PYTHON_INSTALL_MIRROR = (
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/"
)
CN_GITHUB_MIRRORS: tuple[str, ...] = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)

# 17 highest-frequency tools covering all 6 pillars (~62% smaller schema for
# DeepSeek / Qwen / Doubao / open-weights models on Cherry Studio & Trae).
CORE_MCP_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "poe2_status",
        "poe2_search_knowledge",
        "poe2_explain_mechanic",
        "poe2_waystone_regex",
        "poe2_pob_import_local",
        "poe2_pob_load",
        "poe2_pob_audit",
        "poe2_pob_supports",
        "poe2_pob_optimize_runes",
        "poe2_currency_price",
        "poe2_parse_item",
        "poe2_find_upgrades",
        "poe2_triage_dump",
        "poe2_craft_plan",
        "poe2_ninja_search",
        "poe2_filter_generate",
        "poe2_filter_validate",
    }
)

SUPPORTED_MCP_CLIENTS: tuple[str, ...] = ("cursor", "trae", "claude", "codex", "cherry")
SUPPORTED_MCP_PROFILES: tuple[str, ...] = ("full", "core")


def resolve_uv_executable(explicit: str | None = None) -> str:
    """Resolve an absolute `uv` executable path so GUI MCP clients never hit ENOENT."""
    if explicit:
        return explicit
    found = shutil.which("uv")
    if found:
        return Path(found).resolve().as_posix() if os.name != "nt" else str(Path(found).resolve())

    home = Path.home()
    candidates = (
        [
            home / ".local" / "bin" / "uv.exe",
            home / ".cargo" / "bin" / "uv.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "uv.exe",
        ]
        if os.name == "nt"
        else [
            home / ".local" / "bin" / "uv",
            home / ".cargo" / "bin" / "uv",
            Path("/opt/homebrew/bin/uv"),
            Path("/usr/local/bin/uv"),
        ]
    )
    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())
    return "uv"


def _posix_path_str(path: Path) -> str:
    """Return forward-slash absolute path (safe inside JSON/TOML on Windows & POSIX)."""
    return path.resolve().as_posix()


def build_mcp_server_entry(
    repo_dir: Path | None = None,
    *,
    profile: str = "full",
    uv_path: str | None = None,
) -> dict[str, Any]:
    """Build a canonical STDIO MCP server definition for `poe2brain`."""
    if profile not in SUPPORTED_MCP_PROFILES:
        raise ValueError(
            f"Unsupported MCP profile {profile!r}; choose from {SUPPORTED_MCP_PROFILES}"
        )
    root = (repo_dir or REPO_ROOT).resolve()
    uv_cmd = resolve_uv_executable(uv_path)
    if os.name == "nt":
        uv_cmd = Path(uv_cmd).as_posix() if "\\" in uv_cmd else uv_cmd
    args = ["--directory", _posix_path_str(root), "run", "poe2brain-mcp"]
    if profile != "full":
        args.extend(["--profile", profile])
    return {
        "command": uv_cmd,
        "args": args,
    }


def _client_config_paths(home: Path | None = None) -> dict[str, list[Path]]:
    """Return known MCP config file paths per AI client on the current OS."""
    h = (home or Path.home()).resolve()
    is_win = os.name == "nt"
    is_mac = sys.platform == "darwin"
    appdata = Path(os.environ.get("APPDATA", str(h / "AppData" / "Roaming")))

    if is_win:
        trae_paths = [
            appdata / "Trae" / "User" / "mcp.json",
            appdata / "Trae CN" / "User" / "mcp.json",
        ]
    elif is_mac:
        app_sup = h / "Library" / "Application Support"
        trae_paths = [
            app_sup / "Trae" / "User" / "mcp.json",
            app_sup / "Trae CN" / "User" / "mcp.json",
        ]
    else:
        trae_paths = [
            h / ".config" / "Trae" / "User" / "mcp.json",
            h / ".config" / "Trae CN" / "User" / "mcp.json",
        ]

    return {
        "cursor": [h / ".cursor" / "mcp.json"],
        "trae": trae_paths,
        "claude": [h / ".claude.json"],
        "codex": [h / ".codex" / "config.toml"],
    }


def _merge_json_mcp_file(
    path: Path, entry: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"{path} is not a JSON object")
            existing = parsed

    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    servers["poe2brain"] = entry

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": str(path), "written": not dry_run, "format": "json"}


def _remove_json_mcp_entry(path: Path, *, dry_run: bool = False) -> bool:
    if not path.is_file():
        return False
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "poe2brain" not in servers:
        return False
    del servers["poe2brain"]
    if not dry_run:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


_CODEX_SECTION_RE = re.compile(r"(?ms)^\[mcp_servers\.poe2brain\]\r?\n(?:^(?!\[).*?(?:\r?\n|\Z))*")


def _format_codex_toml_block(entry: dict[str, Any]) -> str:
    cmd = json.dumps(entry["command"])
    args = ", ".join(json.dumps(a) for a in entry["args"])
    return f"[mcp_servers.poe2brain]\ncommand = {cmd}\nargs = [{args}]\n"


def _merge_codex_toml_file(
    path: Path, entry: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    block = _format_codex_toml_block(entry)
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _CODEX_SECTION_RE.search(content):
        updated = _CODEX_SECTION_RE.sub(block, content, count=1)
    else:
        sep = "\n" if content and not content.endswith("\n") else ""
        blank = "\n" if content.strip() else ""
        updated = f"{content}{sep}{blank}{block}"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "written": not dry_run, "format": "toml"}


def _remove_codex_toml_entry(path: Path, *, dry_run: bool = False) -> bool:
    if not path.is_file():
        return False
    content = path.read_text(encoding="utf-8")
    if not _CODEX_SECTION_RE.search(content):
        return False
    updated = _CODEX_SECTION_RE.sub("", content).strip()
    if updated:
        updated += "\n"
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return True


def _copy_to_clipboard(text: str) -> bool:
    if os.name == "nt":
        cmd = ["clip"]
    elif sys.platform == "darwin":
        cmd = ["pbcopy"]
    elif shutil.which("wl-copy"):
        cmd = ["wl-copy"]
    elif shutil.which("xclip"):
        cmd = ["xclip", "-selection", "clipboard"]
    else:
        return False
    try:
        proc = subprocess.run(cmd, input=text, text=True, check=False, timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


def detect_installed_clients(home: Path | None = None) -> dict[str, dict[str, Any]]:
    """Detect which AI clients have config directories present on this machine."""
    paths_map = _client_config_paths(home)
    out: dict[str, dict[str, Any]] = {}
    for client, paths in paths_map.items():
        matched = [str(p) for p in paths if p.is_file() or p.parent.is_dir()]
        configured = False
        for p in paths:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if "poe2brain" in text:
                configured = True
                break
        out[client] = {
            "detected": bool(matched),
            "configured": configured,
            "paths": matched or [str(paths[0])],
        }
    return out


def setup_mcp_clients(
    clients: list[str],
    *,
    profile: str = "full",
    repo_dir: Path | None = None,
    uv_path: str | None = None,
    home: Path | None = None,
    dry_run: bool = False,
    copy_clipboard: bool = False,
) -> dict[str, Any]:
    """Configure one or more AI clients to connect to `poe2brain-mcp` with zero manual JSON."""
    normalized: list[str] = []
    for raw in clients:
        name = raw.strip().lower()
        if name == "cherry-studio":
            name = "cherry"
        if name == "all":
            normalized.extend(["cursor", "trae", "claude", "codex", "cherry"])
        elif name in SUPPORTED_MCP_CLIENTS:
            normalized.append(name)
        else:
            raise ValueError(
                f"Unknown client {raw!r}. Supported: {', '.join(SUPPORTED_MCP_CLIENTS)}, all"
            )
    deduped = list(dict.fromkeys(normalized))
    if not deduped:
        deduped = ["cursor", "cherry"]

    entry = build_mcp_server_entry(repo_dir=repo_dir, profile=profile, uv_path=uv_path)
    json_snippet = json.dumps({"mcpServers": {"poe2brain": entry}}, indent=2, ensure_ascii=False)
    paths_map = _client_config_paths(home)
    results: dict[str, Any] = {}

    for client in deduped:
        if client == "cherry":
            copied = _copy_to_clipboard(json_snippet) if (copy_clipboard and not dry_run) else False
            results["cherry"] = {
                "mode": "paste_json_import",
                "copied_to_clipboard": copied,
                "steps_zh": [
                    "打开 Cherry Studio → 设置 → MCP 服务器 → 添加服务器 → 从 JSON 导入",
                    "粘贴下方（或已复制的）JSON 并保存开启开关即可",
                ],
                "json": json_snippet,
            }
            continue

        target_paths = paths_map[client]
        if client == "trae":
            existing_dirs = [p for p in target_paths if p.parent.is_dir() or p.is_file()]
            chosen = existing_dirs or [target_paths[0]]
        else:
            chosen = [target_paths[0]]

        written_list = []
        for p in chosen:
            if client == "codex":
                written_list.append(_merge_codex_toml_file(p, entry, dry_run=dry_run))
            else:
                written_list.append(_merge_json_mcp_file(p, entry, dry_run=dry_run))
        results[client] = written_list[0] if len(written_list) == 1 else written_list

    clipboard_ok = False
    if copy_clipboard and not dry_run and "cherry" not in results:
        clipboard_ok = _copy_to_clipboard(json_snippet)

    return {
        "ok": True,
        "profile": profile,
        "tool_count": len(CORE_MCP_TOOL_NAMES) if profile == "core" else 45,
        "dry_run": dry_run,
        "entry": entry,
        "json_snippet": json_snippet,
        "copied_to_clipboard": clipboard_ok
        or results.get("cherry", {}).get("copied_to_clipboard", False),
        "clients": results,
    }


def _probe_url(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            r = client.head(url)
            return {"ok": r.status_code < 500, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def run_doctor(
    *,
    repo_dir: Path | None = None,
    check_network: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Diagnose local poe2brain paths, uv, PoB2 runtime, AI clients, and optional CN network."""
    root = (repo_dir or REPO_ROOT).resolve()
    cwd = Path.cwd().resolve()
    cache_dir = get_cache_dir()
    db_path = get_gamedata_db_path()
    pob_paths = get_pob2_paths()
    uv_exe = resolve_uv_executable()
    uv_found = uv_exe != "uv" or shutil.which("uv") is not None

    issues: list[dict[str, str]] = []
    if not (cwd / "pyproject.toml").is_file() and (root / "pyproject.toml").is_file():
        issues.append(
            {
                "code": "cwd_not_repo_root",
                "message": f"Current directory ({cwd}) is not the poe2brain root ({root}).",
                "fix": f'cd "{root}"  # or use uv --directory "{root}" run poe2 ...',
            }
        )
    if not uv_found:
        issues.append(
            {
                "code": "uv_missing",
                "message": "`uv` executable was not found on PATH.",
                "fix": f"python -m pip install -i {CN_PYPI_INDEX_URL} uv",
            }
        )
    if not db_path.is_file():
        issues.append(
            {
                "code": "gamedata_missing",
                "message": f"Gamedata SQLite DB missing at {db_path}.",
                "fix": "uv run poe2 sync gamedata",
            }
        )
    if not pob_paths.is_ready:
        issues.append(
            {
                "code": "pob2_missing",
                "message": f"Headless PoB2 runtime not initialized at {pob_paths.root_dir}.",
                "fix": "uv run poe2 bootstrap pob2 --cn",
            }
        )

    network: dict[str, Any] | None = None
    if check_network:
        network = {
            "github": _probe_url("https://github.com/lifan-builds/poe2brain"),
            "aliyun_pypi": _probe_url(CN_PYPI_INDEX_URL),
            "nju_python_mirror": _probe_url(CN_PYTHON_INSTALL_MIRROR),
        }
        if not network["github"]["ok"]:
            issues.append(
                {
                    "code": "github_unreachable",
                    "message": "Direct access to github.com timed out or failed.",
                    "fix": (
                        f"Set UV_INDEX_URL={CN_PYPI_INDEX_URL} & "
                        f"UV_PYTHON_INSTALL_MIRROR={CN_PYTHON_INSTALL_MIRROR}, "
                        "then run: uv run poe2 bootstrap pob2 --cn"
                    ),
                }
            )

    return {
        "ok": len(issues) == 0,
        "patch": POE2_PATCH,
        "default_league": DEFAULT_LEAGUE,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "repo_dir": str(root),
        "cwd": str(cwd),
        "pyproject_in_cwd": (cwd / "pyproject.toml").is_file(),
        "uv_path": uv_exe if uv_found else None,
        "gamedata_db_ready": db_path.is_file(),
        "pob2_runtime_ready": pob_paths.is_ready,
        "cache_dir": str(cache_dir),
        "cache_size_mb": round(_dir_size_bytes(cache_dir) / (1024 * 1024), 2),
        "clients": detect_installed_clients(home=home),
        "cn_mirrors": {
            "pypi": CN_PYPI_INDEX_URL,
            "python_install": CN_PYTHON_INSTALL_MIRROR,
        },
        "network": network,
        "issues": issues,
    }


def run_update(
    *,
    repo_dir: Path | None = None,
    cn: bool = False,
    mirror: str | None = None,
    force_pob2: bool = False,
) -> dict[str, Any]:
    """One-command upgrade (`git pull --ff-only`, `uv sync`, `bootstrap pob2`) for patch bumps."""
    from poe2brain.pob.bootstrap import bootstrap_pob2_runtime

    root = (repo_dir or REPO_ROOT).resolve()
    steps: list[dict[str, Any]] = []

    if (root / ".git").exists() and shutil.which("git"):
        pull = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        steps.append(
            {
                "step": "git_pull",
                "ok": pull.returncode == 0,
                "stdout": pull.stdout.strip(),
                "stderr": pull.stderr.strip(),
            }
        )
        if pull.returncode != 0:
            return {
                "ok": False,
                "repo_dir": str(root),
                "steps": steps,
                "error": "git pull --ff-only failed; commit or stash local edits first.",
            }
    else:
        steps.append({"step": "git_pull", "skipped": True, "reason": "not a git checkout"})

    uv_exe = resolve_uv_executable()
    env = os.environ.copy()
    if cn:
        env.setdefault("UV_INDEX_URL", CN_PYPI_INDEX_URL)
        env.setdefault("UV_PYTHON_INSTALL_MIRROR", CN_PYTHON_INSTALL_MIRROR)

    sync = subprocess.run(
        [uv_exe, "--directory", str(root), "sync"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    steps.append(
        {
            "step": "uv_sync",
            "ok": sync.returncode == 0,
            "stdout": sync.stdout.strip()[-400:],
            "stderr": sync.stderr.strip()[-400:],
        }
    )
    if sync.returncode != 0:
        return {
            "ok": False,
            "repo_dir": str(root),
            "steps": steps,
            "error": "uv sync failed; retry with --cn for Aliyun/NJU mirrors.",
        }

    paths = bootstrap_pob2_runtime(force=force_pob2, cn=cn, mirror=mirror)
    steps.append(
        {
            "step": "bootstrap_pob2",
            "ok": paths.is_ready,
            "pob2_dir": str(paths.root_dir),
        }
    )
    return {
        "ok": paths.is_ready,
        "patch": POE2_PATCH,
        "repo_dir": str(root),
        "cn": cn,
        "steps": steps,
    }


def run_uninstall(
    *,
    remove_cache: bool = True,
    remove_mcp: bool = True,
    repo_dir: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Cleanly remove `poe2brain` MCP entries and local cache (never touches Windows registry)."""
    root = (repo_dir or REPO_ROOT).resolve()
    removed_mcp: list[str] = []
    if remove_mcp:
        paths_map = _client_config_paths(home)
        for client, paths in paths_map.items():
            for p in paths:
                changed = (
                    _remove_codex_toml_entry(p, dry_run=dry_run)
                    if client == "codex"
                    else _remove_json_mcp_entry(p, dry_run=dry_run)
                )
                if changed:
                    removed_mcp.append(str(p))

    cache_dir = get_cache_dir()
    cache_existed = cache_dir.exists()
    cache_removed = False
    if remove_cache and cache_existed:
        if not dry_run:
            shutil.rmtree(cache_dir, ignore_errors=True)
        cache_removed = not dry_run

    delete_cmd = f'Remove-Item -Recurse -Force "{root}"' if os.name == "nt" else f'rm -rf "{root}"'
    return {
        "ok": True,
        "dry_run": dry_run,
        "removed_mcp_configs": removed_mcp,
        "cache_dir": str(cache_dir),
        "cache_existed": cache_existed,
        "cache_removed": cache_removed,
        "final_step_delete_repo": delete_cmd,
    }
