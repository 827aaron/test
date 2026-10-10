"""交易单（TradeTicket）：规则策略和 AI 主管都只能产出它，不能直接下单。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.core.types import Side


class TakeProfit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    price: Decimal = Field(gt=0)
    fraction: Decimal = Field(gt=0, le=1)


class TradeTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: str
    source: Literal["rule", "ai", "plan"]
    symbol: str
    side: Side
    entry_low: Decimal = Field(gt=0)
    entry_high: Decimal = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    take_profits: list[TakeProfit] = Field(min_length=1)
    created_ms: int
    valid_until_ms: int
    preconditions: list[str] = Field(default_factory=list)
    confidence: Decimal = Field(ge=0, le=1)
    p_win: Decimal | None = Field(default=None, ge=0, le=1)
    size_hint_pct: Decimal | None = Field(default=None, gt=0)
    rationale: str = Field(max_length=1200)
    candidate_id: int | None = None

    @model_validator(mode="after")
    def _check_geometry(self) -> TradeTicket:
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low 不能大于 entry_high")
        if self.valid_until_ms <= self.created_ms:
            raise ValueError("有效期必须晚于创建时间")
        if sum(tp.fraction for tp in self.take_profits) > 1:
            raise ValueError("止盈比例之和不能超过 1")
        if self.side is Side.LONG:
            if not self.stop_loss < self.entry_low:
                raise ValueError("做多：止损必须低于入场区间")
            if any(tp.price <= self.entry_high for tp in self.take_profits):
                raise ValueError("做多：止盈必须高于入场区间")
        else:
            if not self.stop_loss > self.entry_high:
                raise ValueError("做空：止损必须高于入场区间")
            if any(tp.price >= self.entry_low for tp in self.take_profits):
                raise ValueError("做空：止盈必须低于入场区间")
        return self

    @property
    def worst_entry(self) -> Decimal:
        """离止损最远的入场价，用来算最大亏损。"""
        return self.entry_high if self.side is Side.LONG else self.entry_low

    @property
    def mid_entry(self) -> Decimal:
        return (self.entry_low + self.entry_high) / 2

    def is_expired(self, now_ms: int) -> bool:
        return now_ms >= self.valid_until_ms

    def price_in_range(self, price: Decimal) -> bool:
        return self.entry_low <= price <= self.entry_high
