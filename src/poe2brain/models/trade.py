# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from __future__ import annotations

from pydantic import Field, model_validator

from poe2brain.config import DEFAULT_LEAGUE
from poe2brain.models.common import Poe2Model, SaleType


class CurrencyQuote(Poe2Model):
    id: str
    name: str
    category: str = "currency"
    league: str = DEFAULT_LEAGUE
    exalted: float
    chaos: float | None = None
    divine: float | None = None
    volume_primary: float | None = None
    change_7d_pct: float | None = None
    source: str = "poe2scout"


class TradeListing(Poe2Model):
    listing_id: str
    item_name: str | None = None
    base_type: str
    item_level: int | None = None
    corrupted: bool = False
    price_amount: float
    price_currency: str
    price_exalted: float
    sale_type: SaleType = SaleType.UNKNOWN
    fee: int | None = None
    whisper: str | None = None
    account_name: str
    online: bool = False
    indexed_at: str | None = None
    pob_item_text: str | None = None
    mod_lines: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infer_sale_type(self) -> TradeListing:
        if self.sale_type == SaleType.UNKNOWN:
            if self.fee is not None:
                self.sale_type = SaleType.SECURABLE
            elif self.whisper:
                self.sale_type = SaleType.WHISPER
        return self

    @property
    def is_instant_buyout(self) -> bool:
        return self.sale_type == SaleType.SECURABLE or self.fee is not None


class PriceDistribution(Poe2Model):
    count_total: int = 0
    count_deduplicated: int = 0
    count_securable: int = 0
    count_whisper: int = 0
    min_exalted: float | None = None
    p25_exalted: float | None = None
    median_exalted: float | None = None
    p75_exalted: float | None = None
    securable_min_exalted: float | None = None
    securable_median_exalted: float | None = None


class PriceCheckResult(Poe2Model):
    league: str = DEFAULT_LEAGUE
    query_id: str | None = None
    trade_url: str | None = None
    total_matches: int = 0
    estimated_exalted: float | None = None
    estimated_divine: float | None = None
    distribution: PriceDistribution = Field(default_factory=PriceDistribution)
    listings: list[TradeListing] = Field(default_factory=list)
    fixer_warnings: list[str] = Field(default_factory=list)
