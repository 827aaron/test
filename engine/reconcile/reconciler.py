"""对账与断线恢复：本地状态 vs 交易所状态，以交易所为准。

时机：启动时、每 60 秒、用户数据流重连后。
不一致 → 暂停开新单 → 修正本地 → 补挂缺失止损（补不上就平仓）→ 推送。
连续一次干净的对账后自动恢复开新单。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.bus import Event, EventBus
from engine.core.clock import Clock
from engine.core.safety import PauseReason, SafetyState
from engine.core.types import OrderType
from engine.exchange.base import ExchangeClient, ExchangeError
from engine.execution.executor import Executor
from engine.portfolio.state import ManagedPosition, Portfolio

log = logging.getLogger(__name__)
D = Decimal


@dataclass
class ReconcileReport:
    ok: bool = True
    adopted: list[str] = field(default_factory=list)  # 交易所有、本地没有
    vanished: list[str] = field(default_factory=list)  # 本地有、交易所没有（离线期间被平）
    qty_fixed: list[str] = field(default_factory=list)
    stops_restored: list[str] = field(default_factory=list)
    force_closed: list[str] = field(default_factory=list)
    orphan_orders_canceled: list[str] = field(default_factory=list)
    margin_warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def mismatch(self) -> bool:
        return bool(self.adopted or self.vanished or self.qty_fixed or self.stops_restored
                    or self.force_closed)


class Reconciler:
    def __init__(self, ex: ExchangeClient, portfolio: Portfolio, executor: Executor, safety: SafetyState,
                 bus: EventBus, clock: Clock, risk_per_trade_pct: Decimal = D("1.0")) -> None:
        self.ex = ex
        self.portfolio = portfolio
        self.executor = executor
        self.safety = safety
        self.bus = bus
        self.clock = clock
        self.risk_pct = risk_per_trade_pct

    async def reconcile(self) -> ReconcileReport:
        rep = ReconcileReport()
        try:
            positions = {p.symbol: p for p in await self.ex.positions()}
            orders = await self.ex.open_orders()
            balance = await self.ex.balance()
        except ExchangeError as e:
            rep.ok, rep.error = False, str(e)
            self.safety.pause(PauseReason.DISCONNECTED)
            self._emit(rep)
            return rep
        self.safety.clear(PauseReason.DISCONNECTED)

        stops_by_symbol: dict[str, list[str]] = {}
        for o in orders:
            if o.type is OrderType.STOP_MARKET and (o.close_position or o.reduce_only):
                stops_by_symbol.setdefault(o.symbol, []).append(o.client_order_id)

        # 1) 本地有、交易所没有：离线期间被止损/止盈/手动平掉
        for sym in list(self.portfolio.positions):
            if sym not in positions:
                local = self.portfolio.positions[sym]
                if local.closing:
                    continue
                await self.executor.cancel_protection(local)
                self.portfolio.close(sym)
                self.executor.on_closed(local, "交易所上已无持仓（止损/止盈触发或手动平仓）")
                rep.vanished.append(sym)

        # 2) 交易所有、本地没有：认领（例如重启前开的仓）
        for sym, xp in positions.items():
            local = self.portfolio.positions.get(sym)
            if local is None:
                stop_ids = stops_by_symbol.get(sym, [])
                stop_px = next((o.stop_price for o in orders if o.client_order_id in stop_ids
                                and o.stop_price), None)
                if stop_px is None:
                    # 没有止损：按“单笔最多亏 1%”反推一个保护止损
                    dist = balance.equity * self.risk_pct / 100 / xp.qty
                    stop_px = xp.entry_price - xp.side.sign * dist
                    if stop_px <= 0:
                        stop_px = xp.entry_price * D("0.5")
                local = ManagedPosition(
                    ticket_id=f"adopt-{sym}-{self.clock.now_ms()}", symbol=sym, side=xp.side, qty=xp.qty,
                    initial_qty=xp.qty, entry_price=xp.entry_price, initial_stop=stop_px, stop=stop_px,
                    take_profit=None, opened_ms=self.clock.now_ms(), best_price=xp.entry_price,
                    stop_order_id=stop_ids[0] if stop_ids else None, adopted=True,
                )
                self.portfolio.positions[sym] = local
                rep.adopted.append(sym)
            else:
                if local.side is not xp.side:
                    log.error("持仓方向不一致 %s：本地 %s，交易所 %s", sym, local.side, xp.side)
                    local.side = xp.side
                    rep.qty_fixed.append(sym)
                if local.qty != xp.qty:
                    local.qty = xp.qty
                    rep.qty_fixed.append(sym)
                    local.entry_price = xp.entry_price if local.adopted else local.entry_price
            if xp.margin_type != "ISOLATED":
                rep.margin_warnings.append(sym)

        # 3) 每个持仓都必须有止损
        for sym, local in list(self.portfolio.positions.items()):
            stop_ids = stops_by_symbol.get(sym, [])
            if local.stop_order_id in stop_ids:
                continue
            if stop_ids:  # 有止损单但 id 对不上（例如重启后）：认领
                local.stop_order_id = stop_ids[0]
                continue
            if await self.executor.ensure_stop(local):
                rep.stops_restored.append(sym)
            else:
                rep.force_closed.append(sym)

        # 4) 孤儿保护单：没有持仓的币种上剩下的只减仓单
        for o in orders:
            if o.symbol not in positions and (o.reduce_only or o.close_position):
                try:
                    await self.ex.cancel_order(o.symbol, o.client_order_id)
                    rep.orphan_orders_canceled.append(o.client_order_id)
                except ExchangeError:
                    pass

        if rep.mismatch:
            self.safety.pause(PauseReason.RECONCILE_MISMATCH)
        else:
            self.safety.clear(PauseReason.RECONCILE_MISMATCH)
        self._emit(rep)
        return rep

    def _emit(self, rep: ReconcileReport) -> None:
        important = rep.mismatch or not rep.ok
        self.bus.publish(Event("reconcile", {
            "ok": rep.ok, "adopted": rep.adopted, "vanished": rep.vanished, "qty_fixed": rep.qty_fixed,
            "stops_restored": rep.stops_restored, "force_closed": rep.force_closed,
            "orphans": rep.orphan_orders_canceled, "margin_warnings": rep.margin_warnings,
            "error": rep.error,
        }, self.clock.now_ms(), important, "positions"))
