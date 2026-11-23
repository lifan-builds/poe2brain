from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Poe2Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class AffixSide(StrEnum):
    PREFIX = "prefix"
    SUFFIX = "suffix"
    IMPLICIT = "implicit"
    ENCHANT = "enchant"
    SOCKETABLE = "socketable"
    CORRUPTED = "corrupted"
    SKILL = "skill"
    NONE = "none"


class SaleType(StrEnum):
    SECURABLE = "securable"
    WHISPER = "whisper"
    UNKNOWN = "unknown"


class StatRollRange(Poe2Model):
    min: float
    max: float


class PriceAmount(Poe2Model):
    amount: float
    currency: str
    exalted_equiv: float | None = None
    divine_equiv: float | None = None


class EngineStatus(Poe2Model):
    version: str
    patch: str
    default_league: str
    cache_dir: str
    bundled_data_dir: str
    gamedata_db_ready: bool
    pob2_runtime_ready: bool
    pob2_luajit_path: str | None = None
    pob2_src_dir: str | None = None
