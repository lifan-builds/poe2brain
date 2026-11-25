"""Zero-auth local Path of Building (PoE2) Builds folder discovery and sync.

Lets Windows, macOS, and Linux players who already imported or saved a character
in desktop Path of Building 2 load it straight into poe2brain without OAuth or
waiting for poe.ninja indexing.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from poe2brain.models.common import Poe2Model
from poe2brain.ninja.xml_inspector import inspect_pob2_xml_or_code

POB2_BUILDS_DIR_ENV = "POE2BRAIN_POB2_BUILDS_DIR"


class LocalPobBuildSummary(Poe2Model):
    name: str
    path: str
    modified_at: str
    size_bytes: int
    class_name: str | None = None
    ascendancy_name: str | None = None
    level: int | None = None
    main_skill: str | None = None


class LocalPobBuildDiscovery(Poe2Model):
    searched_dirs: list[str] = Field(default_factory=list)
    existing_dirs: list[str] = Field(default_factory=list)
    builds: list[LocalPobBuildSummary] = Field(default_factory=list)


def candidate_local_pob2_build_dirs(
    *,
    platform_override: str | None = None,
    home_dir: Path | None = None,
) -> list[Path]:
    plat = (platform_override or sys.platform).lower()
    home = home_dir or Path.home()
    dirs: list[Path] = []

    env_dir = os.environ.get(POB2_BUILDS_DIR_ENV, "").strip()
    if env_dir:
        dirs.append(Path(env_dir).expanduser())

    if plat.startswith("win"):
        dirs.extend(
            [
                home / "Documents" / "Path of Building (PoE2)" / "Builds",
                home / "OneDrive" / "Documents" / "Path of Building (PoE2)" / "Builds",
            ]
        )
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            dirs.append(Path(appdata) / "Path of Building Community (PoE2)" / "Builds")
    elif plat.startswith("darwin") or plat == "macos":
        dirs.extend(
            [
                home / "Documents" / "Path of Building (PoE2)" / "Builds",
                home / "Library" / "Application Support" / "Path of Building (PoE2)" / "Builds",
                home
                / "Library"
                / "Application Support"
                / "Path of Building Community (PoE2)"
                / "Builds",
            ]
        )
    else:
        dirs.extend(
            [
                home / "Documents" / "Path of Building (PoE2)" / "Builds",
                home / ".local" / "share" / "Path of Building (PoE2)" / "Builds",
            ]
        )

    deduped: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            deduped.append(d)
    return deduped


def discover_local_pob2_builds(
    *,
    builds_dir: Path | None = None,
    limit: int = 30,
) -> LocalPobBuildDiscovery:
    searched = (
        [builds_dir.expanduser()]
        if builds_dir is not None
        else candidate_local_pob2_build_dirs()
    )
    existing = [d for d in searched if d.is_dir()]

    xml_files: list[Path] = []
    for d in existing:
        xml_files.extend(p for p in d.rglob("*.xml") if p.is_file())

    xml_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    summaries: list[LocalPobBuildSummary] = []
    for path in xml_files[: max(1, limit)]:
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat()
        cls_name: str | None = None
        asc_name: str | None = None
        level: int | None = None
        main_skill: str | None = None
        try:
            xml_text = path.read_text(encoding="utf-8", errors="replace")
            inspected = inspect_pob2_xml_or_code(xml_text)
            cls_name = inspected.class_name or None
            asc_name = inspected.ascendancy_name or None
            level = inspected.level or None
            main_skill = inspected.main_skill or None
        except Exception:
            pass
        summaries.append(
            LocalPobBuildSummary(
                name=path.stem,
                path=str(path.resolve()),
                modified_at=mtime,
                size_bytes=st.st_size,
                class_name=cls_name,
                ascendancy_name=asc_name,
                level=level,
                main_skill=main_skill,
            )
        )

    return LocalPobBuildDiscovery(
        searched_dirs=[str(d) for d in searched],
        existing_dirs=[str(d) for d in existing],
        builds=summaries,
    )


def resolve_local_pob2_build(
    name_or_path: str | None = None,
    *,
    builds_dir: Path | None = None,
) -> Path:
    if name_or_path:
        direct = Path(name_or_path).expanduser()
        if direct.is_file():
            return direct.resolve()

    discovery = discover_local_pob2_builds(builds_dir=builds_dir, limit=200)
    if not discovery.builds:
        searched = ", ".join(discovery.searched_dirs)
        raise FileNotFoundError(
            f"No local Path of Building (PoE2) .xml builds found in: {searched}. "
            "Save a build in PoB2 or pass --dir / an explicit XML path."
        )

    if not name_or_path:
        return Path(discovery.builds[0].path)

    query = name_or_path.strip().lower()
    if query.endswith(".xml"):
        query = query[:-4]

    for b in discovery.builds:
        if b.name.lower() == query:
            return Path(b.path)
    for b in discovery.builds:
        if query in b.name.lower():
            return Path(b.path)

    available = ", ".join(b.name for b in discovery.builds[:8])
    raise LookupError(
        f"No local PoB2 build matching '{name_or_path}'. Available recent builds: {available}"
    )
