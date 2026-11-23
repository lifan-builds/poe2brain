from __future__ import annotations

from typing import Any

from pydantic import Field

from poe2brain.config import POE2_PATCH
from poe2brain.models.common import Poe2Model


class KnowledgeRecord(Poe2Model):
    id: str
    kind: str
    title: str
    summary: str
    category: str | None = None
    patch: str = POE2_PATCH
    score: float | None = None
    source_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MechanicFact(Poe2Model):
    slug: str
    title: str
    category: str | None = None
    poe1_trap: str
    poe2_live_rule: str
    formula_expr: str | None = None
    source_ref: str
    patch_verified: str = POE2_PATCH


class GemSummary(Poe2Model):
    id: str
    name: str
    gem_kind: str
    support_tier: int | None = None
    max_copies_per_char: int | None = None
    spirit_cost: int | None = None
    tags: list[str] = Field(default_factory=list)
    stat_lines: list[str] = Field(default_factory=list)


class BaseItemSummary(Poe2Model):
    id: str
    name: str
    item_class: str
    poe2db_slug: str | None = None
    drop_level: int = 1
    max_prefixes: int = 3
    max_suffixes: int = 3
    tags: list[str] = Field(default_factory=list)
    implicits: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    is_live_poe2: bool = True


class UniqueSummary(Poe2Model):
    id: str
    name: str
    base_type: str
    item_class: str | None = None
    is_chanceable: bool | None = None
    implicits: list[str] = Field(default_factory=list)
    explicits: list[str] = Field(default_factory=list)
    price_exalted: float | None = None
