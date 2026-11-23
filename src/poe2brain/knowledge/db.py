from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import Field

from poe2brain.config import POE2_PATCH, REPO_ROOT, get_gamedata_db_path
from poe2brain.models.common import Poe2Model

GAMEDATA_TABLES: tuple[str, ...] = (
    "keywords",
    "mechanics_facts",
    "gems",
    "uniques",
    "base_items",
    "augments",
    "mod_tiers",
    "crafting_levers",
    "runeforge_recipes",
    "knowledge_fts",
)


class GamedataHealth(Poe2Model):
    db_path: str
    patch: str
    client_version: str
    generated_at: str | None = None
    is_healthy: bool
    table_counts: dict[str, int] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)


class GamedataDB:
    """Read-only SQLite + FTS5 connection wrapper for PoE2 0.5.5 gamedata."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        auto_sync: bool = True,
        sync_script: Path | None = None,
    ) -> None:
        resolved = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else get_gamedata_db_path().resolve()
        )
        self.db_path: Path = resolved
        self.auto_sync: bool = auto_sync
        self.sync_script: Path = (
            sync_script.expanduser().resolve()
            if sync_script is not None
            else (REPO_ROOT / "scripts" / "sync_gamedata.py")
        )
        self._conn: sqlite3.Connection | None = None

    def ensure_db(self) -> Path:
        if self.db_path.is_file():
            return self.db_path
        if self.auto_sync:
            self._run_sync()
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"PoE2 gamedata SQLite DB not found at {self.db_path} "
                f"(sync script: {self.sync_script})"
            )
        return self.db_path

    def _run_sync(self) -> None:
        if not self.sync_script.is_file():
            raise FileNotFoundError(
                f"PoE2 gamedata DB missing at {self.db_path} and sync script "
                f"not found at {self.sync_script}"
            )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(self.sync_script), "--out", str(self.db_path)],
            cwd=str(REPO_ROOT),
            check=True,
        )

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            path = self.ensure_db()
            uri = f"{path.as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def connect(self) -> sqlite3.Connection:
        return self.connection

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def get_meta(self) -> dict[str, str]:
        rows = self.query_all("SELECT key, value FROM meta")
        return {str(r["key"]): str(r["value"]) for r in rows}

    def health_check(self) -> GamedataHealth:
        meta = self.get_meta()
        counts: dict[str, int] = {}
        for table in GAMEDATA_TABLES:
            row = self.query_one(f"SELECT COUNT(*) AS c FROM {table}")
            counts[table] = int(row["c"]) if row is not None else 0

        patch = meta.get("patch", "")
        client_version = meta.get("client_version", "")
        generated_at = meta.get("generated_at")
        is_healthy = (
            patch == POE2_PATCH
            and bool(client_version)
            and all(counts.get(t, 0) > 0 for t in GAMEDATA_TABLES)
        )
        return GamedataHealth(
            db_path=str(self.db_path),
            patch=patch,
            client_version=client_version,
            generated_at=generated_at,
            is_healthy=is_healthy,
            table_counts=counts,
            meta=meta,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> GamedataDB:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
