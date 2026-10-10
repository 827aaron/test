"""持仓盯盘（每秒）：1R 保本、1.5R 平一半、移动止损、时间止损、剧烈反向立即平仓。

所有动作都只降低风险；具体下单交给执行器。
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal

from engine.core.clock import HOUR_MS
from engine.core.config import PortfolioCfg
from engine.core.types import Instrument, Side
from engine.execution.executor import Executor
from engine.market.indicators import atr
from engine.market.store import MarketDataStore
from engine.portfolio.state import ManagedPosition, Portfolio

log = logging.getLogger(__name__)
D = Decimal


class PositionManager:
    def __init__(self, cfg: PortfolioCfg, portfolio: Portfolio, executor: Executor, store: MarketDataStore,
                 taker_fee_pct: Decimal = D("0.05")) -> None:
        self.cfg = cfg
        self.portfolio = portfolio
        self.ex = executor
        self.store = store
        self.fee = taker_fee_pct / 100

    def _inst(self, symbol: str) -> Instrument:
        return self.store.instruments[symbol]

    async def on_tick(self, pos: ManagedPosition, price: Decimal, now_ms: int) -> list[str]:
        """返回本次执行的动作名称（便于测试和记录）。"""
        if pos.closing or pos.symbol not in self.portfolio.positions:
            return []
        cfg = self.cfg
        actions: list[str] = []
        sign = pos.side.sign
        r = pos.r_per_unit
        if r <= 0:
            return []

        # 更新最优价和最近价格窗口
        if pos.best_price is None or (price - pos.best_price) * sign > 0:
            pos.best_price = price
        pos.recent.append((now_ms, price))
        window_ms = cfg.adverse_window_sec * 1000
        while pos.recent and pos.recent[0][0] < now_ms - window_ms:
            pos.recent.popleft()

        # 1) 剧烈反向：窗口内从最好价位反向移动 ≥ adverse_move_r × R
        best_in_window = (max if pos.side is Side.LONG else min)(p for _, p in pos.recent)
        adverse_r = (best_in_window - price) * sign / r
        if adverse_r >= D(str(cfg.adverse_move_r)):
            await self.ex.close(pos, f"{cfg.adverse_window_sec} 秒内反向 {adverse_r:.2f}R，立即平仓")
            return ["adverse_close"]

        progress = pos.progress_r(price)

        # 2) 时间止损
        if (now_ms - pos.opened_ms >= cfg.time_stop_hours * HOUR_MS
                and progress < D(str(cfg.time_stop_min_progress_r))):
            await self.ex.close(pos, f"持仓 {cfg.time_stop_hours}h 无进展（{progress:.2f}R），时间止损")
            return ["time_stop"]

        inst = self._inst(pos.symbol)

        # 3) 1R → 止损移到保本（加上来回手续费）
        if not pos.breakeven_done and progress >= D(str(cfg.breakeven_at_r)):
            be = pos.entry_price * (1 + sign * 2 * self.fee)
            be = inst.round_price(be, up=pos.side is Side.LONG)
            if pos.is_tighter(be):
                if await self.ex.move_stop(pos, be, "浮盈 1R，止损移到保本"):
                    actions.append("breakeven")
            pos.breakeven_done = True

        # 4) 1.5R → 平一半
        if not pos.partial_done and progress >= D(str(cfg.partial_take_at_r)):
            if await self.ex.reduce(pos, D(str(cfg.partial_take_fraction)), inst, "浮盈 1.5R，平一半"):
                actions.append("partial")
            pos.partial_done = True

        # 5) 平一半之后：ATR 移动止损
        if pos.partial_done:
            a = atr(self.store.closed_klines(pos.symbol, "1h"), 14)
            if not math.isnan(a) and a > 0 and pos.best_price is not None:
                trail = pos.best_price - sign * D(str(a)) * D(str(cfg.trailing_atr_mult))
                trail = inst.round_price(trail, up=pos.side is Side.LONG)
                # 每次至少收紧 0.1R，避免频繁改单
                if pos.is_tighter(trail) and abs(trail - pos.stop) >= r * D("0.1"):
                    if await self.ex.move_stop(pos, trail, "移动止损"):
                        actions.append("trail")
        return actions

    async def tick_all(self, now_ms: int) -> None:
        for pos in list(self.portfolio.positions.values()):
            price = self.store.marks.get(pos.symbol)
            if price is None:
                continue
            try:
                await self.on_tick(pos, price, now_ms)
            except Exception:
                log.exception("盯盘出错 %s", pos.symbol)
