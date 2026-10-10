"""阶段 1 的最简单规则策略：只为跑通流程，不追求盈利。阶段 2 起作为基线和后备。

规则：扫描器入选的候选（≥2 个同向信号、机会分 ≥ 60）
  → 入场区间 = 现价 ± entry_band_pct%
  → 止损 = 现价 ∓ atr_stop_mult × ATR(1h)
  → 止盈 = take_profit_r × R（最终目标）；1R 保本、1.5R 平一半、移动止损由持仓管理负责
  → 有效期 = ticket_valid_sec
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal

from engine.core.config import ExecutionCfg, StrategyCfg
from engine.core.ticket import TakeProfit, TradeTicket
from engine.core.types import Instrument, Side
from engine.market.indicators import atr
from engine.market.store import MarketDataStore
from engine.scanner.scanner import Candidate

D = Decimal


def build_ticket(c: Candidate, inst: Instrument, store: MarketDataStore, scfg: StrategyCfg,
                 ecfg: ExecutionCfg, now_ms: int) -> TradeTicket | None:
    price = store.last_price(c.symbol)
    a = atr(store.closed_klines(c.symbol, "1h"), 14)
    if price is None or math.isnan(a) or a <= 0:
        return None
    band = price * D(str(scfg.entry_band_pct)) / 100
    stop_dist = D(str(a)) * D(str(scfg.atr_stop_mult))
    sign = c.side.sign
    entry_low = inst.round_price(price - band)
    entry_high = inst.round_price(price + band, up=True)
    if c.side is Side.LONG:
        stop = inst.round_price(entry_low - stop_dist)
        r = entry_high - stop
        tp = inst.round_price(entry_high + r * D(str(scfg.take_profit_r)), up=True)
    else:
        stop = inst.round_price(entry_high + stop_dist, up=True)
        r = stop - entry_low
        tp = inst.round_price(entry_low - r * D(str(scfg.take_profit_r)))
    if stop <= 0 or tp <= 0 or sign == 0:
        return None
    try:
        return TradeTicket(
            ticket_id=f"t-{uuid.uuid4().hex[:12]}", source="rule", symbol=c.symbol, side=c.side,
            entry_low=entry_low, entry_high=entry_high, stop_loss=stop,
            take_profits=[TakeProfit(price=tp, fraction=D("1"))],
            created_ms=now_ms, valid_until_ms=now_ms + ecfg.ticket_valid_sec * 1000,
            preconditions=[f"价格保持在 {entry_low}–{entry_high}"],
            confidence=D(str(min(c.score / 100, 1))),
            rationale="规则策略：" + "；".join(c.reasons[:4]),
        )
    except ValueError:
        return None
