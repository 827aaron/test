"""执行器：整个引擎里唯一能下单的模块。

流程：最新价核对 → 过期检查 → 逐仓 + 杠杆 → 限价入场（超时撤单，不追单）
     → 成交后立即挂止损（closePosition）→ 止损挂不上就立即市价平仓 → 挂止盈。
持仓期间只允许降低风险：平仓、减仓、收紧止损。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.bus import Event, EventBus
from engine.core.clock import Clock
from engine.core.safety import PauseReason, SafetyState
from engine.core.ticket import TradeTicket
from engine.core.types import ExchangeOrder, Instrument, OrderRequest, OrderStatus, OrderType, Side
from engine.exchange.base import ExchangeClient, ExchangeError, OrderRejected
from engine.execution.tracker import OrderTracker
from engine.portfolio.state import ManagedPosition, Portfolio
from engine.risk.engine import RiskDecision

log = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    ticket_id: str
    status: str  # FILLED | PARTIAL | VOID_EXPIRED | VOID_PRICE | VOID_PAUSED | NOT_FILLED | ERROR | UNPROTECTED_CLOSED
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    notes: list[str] = field(default_factory=list)
    latency_ms: int = 0


class Executor:
    def __init__(self, ex: ExchangeClient, tracker: OrderTracker, portfolio: Portfolio, safety: SafetyState,
                 bus: EventBus, clock: Clock, entry_timeout_sec: float = 60, stop_retries: int = 2,
                 poll_sec: float = 1.0, max_entry_deviation_pct: Decimal = Decimal("0.3")) -> None:
        self.ex = ex
        self.tracker = tracker
        self.portfolio = portfolio
        self.safety = safety
        self.bus = bus
        self.clock = clock
        self.entry_timeout = entry_timeout_sec
        self.stop_retries = stop_retries
        self.poll_sec = poll_sec
        self.max_dev = max_entry_deviation_pct
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(symbol, asyncio.Lock())

    def _emit(self, kind: str, important: bool = False, channel: str = "trading", **payload: object) -> None:
        self.bus.publish(Event(kind, dict(payload), self.clock.now_ms(), important, channel))

    # ------------------------------------------------------------------ 开仓
    async def execute(self, t: TradeTicket, decision: RiskDecision, inst: Instrument) -> ExecutionReport:
        start = self.clock.now_ms()
        rep = ExecutionReport(t.ticket_id, "ERROR")
        if not decision.approved:
            rep.status, rep.notes = "VOID_REJECTED", list(decision.reasons)
            return rep
        if not self.safety.can_open_new:
            rep.status = "VOID_PAUSED"
            return rep
        async with self.lock(t.symbol):
            if t.symbol in self.portfolio.positions:
                rep.status, rep.notes = "VOID_DUPLICATE", ["该币种已有持仓"]
                return rep
            # 1) 用最新行情再核对一遍
            price = await self.ex.mark_price(t.symbol)
            now = self.clock.now_ms()
            if t.is_expired(now):
                rep.status = "VOID_EXPIRED"
                return self._void(rep, "交易单已过期，不追单")
            if not t.price_in_range(price):
                rep.status = "VOID_PRICE"
                return self._void(rep, f"现价 {price} 已跑出入场区间 {t.entry_low}–{t.entry_high}，不追单")

            # 2) 逐仓 + 杠杆
            try:
                await self.ex.set_margin_isolated(t.symbol)
                await self.ex.set_leverage(t.symbol, decision.leverage)
            except ExchangeError as e:
                rep.status = "ERROR"
                return self._void(rep, f"设置逐仓/杠杆失败：{e}")

            # 3) 限价入场：价格取现价（在区间内），超时撤单
            limit = inst.round_price(price, up=t.side is Side.LONG)
            limit = min(max(limit, t.entry_low), t.entry_high)
            entry_id = f"aq-{t.ticket_id[-10:]}-in"
            try:
                o = await self.ex.place_order(OrderRequest(
                    t.symbol, t.side.entry_order_side, OrderType.LIMIT, decision.qty, entry_id, price=limit))
            except ExchangeError as e:
                rep.status = "ERROR"
                return self._void(rep, f"入场单被拒：{e}")
            self.tracker.update(o)
            o = await self._wait_entry(t, o, inst)
            if o.filled_qty <= 0:
                rep.status = "NOT_FILLED"
                return self._void(rep, "入场单未成交，已撤单作废")
            rep.filled_qty, rep.avg_price = o.filled_qty, o.avg_price or limit
            rep.status = "FILLED" if o.filled_qty >= decision.qty else "PARTIAL"

            # 4) 立即挂止损；挂不上就平仓
            pos = ManagedPosition(
                ticket_id=t.ticket_id, symbol=t.symbol, side=t.side, qty=o.filled_qty,
                initial_qty=o.filled_qty, entry_price=rep.avg_price, initial_stop=t.stop_loss,
                stop=t.stop_loss, take_profit=t.take_profits[-1].price, opened_ms=self.clock.now_ms(),
                best_price=rep.avg_price,
            )
            self.portfolio.positions[t.symbol] = pos
            if not await self._place_stop(pos, t.stop_loss):
                await self._emergency_close(pos, "止损单挂不上，立即平仓")
                rep.status = "UNPROTECTED_CLOSED"
                rep.notes.append("止损单挂不上，已立即平仓")
                return rep

            # 5) 止盈（失败不致命：持仓管理会按规则处理）
            await self._place_take_profits(pos, t, inst)

        rep.latency_ms = self.clock.now_ms() - start
        self._emit("position_opened", important=True, channel="positions", symbol=t.symbol,
                   side=t.side.value, qty=str(pos.qty), entry=str(pos.entry_price), stop=str(pos.stop),
                   take_profit=str(pos.take_profit), ticket_id=t.ticket_id, latency_ms=rep.latency_ms)
        return rep

    def _void(self, rep: ExecutionReport, why: str) -> ExecutionReport:
        rep.notes.append(why)
        self._emit("ticket_void", ticket_id=rep.ticket_id, status=rep.status, reason=why)
        return rep

    async def _wait_entry(self, t: TradeTicket, o: ExchangeOrder, inst: Instrument) -> ExchangeOrder:
        # 等待时长用真实时间计（time.monotonic），有效期用交易时钟计
        deadline = time.monotonic() + self.entry_timeout
        cur = o
        while (cur.status.is_open and time.monotonic() < deadline
               and not t.is_expired(self.clock.now_ms())):
            got = await self.tracker.wait_change(o.client_order_id, self.poll_sec)
            if got is not None:
                cur = got
        if cur.status.is_open:
            try:
                await self.ex.cancel_order(t.symbol, o.client_order_id)
            except OrderRejected:
                pass  # 可能刚好成交
            # 撤单后再确认一次最终成交数量（以交易所持仓为准）
            positions = {p.symbol: p for p in await self.ex.positions()}
            p = positions.get(t.symbol)
            filled = p.qty if p is not None and p.side is t.side else Decimal(0)
            last = self.tracker.orders.get(o.client_order_id, cur)
            cur = ExchangeOrder(**{**last.__dict__, "filled_qty": filled,
                                   "avg_price": p.entry_price if p else last.avg_price,
                                   "status": OrderStatus.CANCELED})
        return cur

    # ------------------------------------------------------------------ 保护单
    async def _place_stop(self, pos: ManagedPosition, stop: Decimal) -> bool:
        for attempt in range(self.stop_retries + 1):
            cid = pos.next_id("sl")
            try:
                o = await self.ex.place_order(OrderRequest(
                    pos.symbol, pos.side.exit_order_side, OrderType.STOP_MARKET, None, cid,
                    stop_price=stop, close_position=True))
                self.tracker.update(o)
                pos.stop_order_id = cid
                pos.stop = stop
                return True
            except OrderRejected as e:
                log.error("止损单被拒（第 %d 次）%s: %s", attempt + 1, pos.symbol, e)
                if "immediately trigger" in str(e):
                    return False  # 价格已穿过止损位，重试没有意义
            except ExchangeError as e:
                log.error("止损单下单失败（第 %d 次）%s: %s", attempt + 1, pos.symbol, e)
            await asyncio.sleep(0.2 * (attempt + 1))
        return False

    async def _place_take_profits(self, pos: ManagedPosition, t: TradeTicket, inst: Instrument) -> None:
        tps = t.take_profits
        for i, tp in enumerate(tps):
            last = i == len(tps) - 1
            cid = pos.next_id("tp")
            qty = None if last else inst.round_qty(pos.initial_qty * tp.fraction)
            try:
                o = await self.ex.place_order(OrderRequest(
                    pos.symbol, pos.side.exit_order_side, OrderType.TAKE_PROFIT_MARKET, qty, cid,
                    stop_price=tp.price, close_position=last, reduce_only=not last))
                self.tracker.update(o)
                pos.tp_order_ids.append(cid)
            except ExchangeError as e:
                log.warning("止盈单下单失败 %s: %s", pos.symbol, e)

    async def ensure_stop(self, pos: ManagedPosition) -> bool:
        """补挂止损（对账或盯盘发现止损丢失时调用）；补不上就平仓。"""
        async with self.lock(pos.symbol):
            if await self._place_stop(pos, pos.stop):
                self._emit("stop_restored", important=True, channel="positions", symbol=pos.symbol,
                           stop=str(pos.stop))
                return True
            await self._emergency_close(pos, "止损单丢失且补挂失败，立即平仓")
            return False

    # ------------------------------------------------------------------ 只降风险的动作
    async def move_stop(self, pos: ManagedPosition, new_stop: Decimal, reason: str) -> bool:
        """只允许收紧。先挂新止损，再撤旧止损，避免出现无保护的窗口。"""
        if not pos.is_tighter(new_stop):
            log.warning("拒绝放宽止损 %s: %s -> %s", pos.symbol, pos.stop, new_stop)
            return False
        async with self.lock(pos.symbol):
            old_id, old_stop = pos.stop_order_id, pos.stop
            if await self._place_stop(pos, new_stop):
                if old_id:
                    try:
                        await self.ex.cancel_order(pos.symbol, old_id)
                    except ExchangeError:
                        log.warning("撤旧止损失败 %s %s", pos.symbol, old_id)
                self._emit("stop_moved", channel="positions", symbol=pos.symbol, old=str(old_stop),
                           new=str(new_stop), reason=reason)
                return True
            # 新止损没挂上：旧止损还在就算了；旧的也不在了就平仓
            pos.stop, pos.stop_order_id = old_stop, old_id
            open_ids = {o.client_order_id for o in await self.ex.open_orders(pos.symbol)}
            if old_id not in open_ids:
                await self._emergency_close(pos, "移动止损失败且旧止损不存在，立即平仓")
            return False

    async def reduce(self, pos: ManagedPosition, fraction: Decimal, inst: Instrument, reason: str) -> bool:
        qty = inst.round_qty(pos.qty * fraction)
        if qty < inst.min_qty or qty >= pos.qty:
            return False
        async with self.lock(pos.symbol):
            try:
                o = await self.ex.place_order(OrderRequest(
                    pos.symbol, pos.side.exit_order_side, OrderType.MARKET, qty, pos.next_id("rd"),
                    reduce_only=True))
                self.tracker.update(o)
            except ExchangeError as e:
                log.error("减仓失败 %s: %s", pos.symbol, e)
                return False
            pos.qty -= o.filled_qty or qty
            self._emit("position_reduced", important=True, channel="positions", symbol=pos.symbol,
                       qty=str(qty), remaining=str(pos.qty), reason=reason, price=str(o.avg_price))
            return True

    async def close(self, pos: ManagedPosition, reason: str) -> bool:
        async with self.lock(pos.symbol):
            return await self._close_unlocked(pos, reason)

    async def _emergency_close(self, pos: ManagedPosition, reason: str) -> None:
        ok = await self._close_unlocked(pos, reason)
        self._emit("alert", important=True, symbol=pos.symbol, message=reason, closed=ok)

    async def _close_unlocked(self, pos: ManagedPosition, reason: str) -> bool:
        if pos.closing:
            return True
        pos.closing = True
        for attempt in range(3):
            try:
                o = await self.ex.place_order(OrderRequest(
                    pos.symbol, pos.side.exit_order_side, OrderType.MARKET, pos.qty, pos.next_id("x"),
                    reduce_only=True))
                self.tracker.update(o)
                break
            except OrderRejected as e:
                # 仓位可能已经被止损/止盈平掉了
                still = [p for p in await self.ex.positions() if p.symbol == pos.symbol]
                if not still:
                    break
                log.error("平仓被拒 %s: %s", pos.symbol, e)
            except ExchangeError as e:
                log.error("平仓失败（第 %d 次）%s: %s", attempt + 1, pos.symbol, e)
            await asyncio.sleep(0.5 * (attempt + 1))
        else:
            pos.closing = False
            self.safety.pause(PauseReason.REPEATED_ERRORS)
            self._emit("alert", important=True, symbol=pos.symbol, message=f"平仓失败！请立即人工处理：{reason}")
            return False
        await self.cancel_protection(pos)
        self.portfolio.close(pos.symbol)
        self.on_closed(pos, reason)
        return True

    def on_closed(self, pos: ManagedPosition, reason: str) -> None:
        """仓位平掉后的回调。引擎会替换它来记账并推送盈亏。"""
        self._emit("position_closed", important=True, channel="positions", symbol=pos.symbol,
                   reason=reason, ticket_id=pos.ticket_id)

    async def cancel_protection(self, pos: ManagedPosition) -> None:
        for oid in [pos.stop_order_id, *pos.tp_order_ids]:
            if oid:
                try:
                    await self.ex.cancel_order(pos.symbol, oid)
                except ExchangeError:
                    pass  # 已触发或已失效

    async def flatten_all(self, reason: str) -> None:
        for pos in list(self.portfolio.positions.values()):
            await self.close(pos, reason)
