"""内存模拟交易所：U 本位永续、单向持仓、逐仓。

用于云端测试（连不上币安）和录制数据回放。支持故障注入：断线、止损单下单失败、手动删单。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.clock import Clock, ManualClock
from engine.core.types import (
    AccountBalance,
    ExchangeOrder,
    ExchangePosition,
    FundingRate,
    Instrument,
    KeyPermissions,
    Kline,
    Liquidation,
    OpenInterest,
    OrderBook,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Side,
    Ticker24h,
    UserEvent,
)
from engine.exchange.base import ExchangeDisconnected, OrderRejected


@dataclass
class _Pos:
    side: Side
    qty: Decimal
    entry: Decimal


@dataclass
class MockExchange:
    clock: Clock = field(default_factory=ManualClock)
    wallet: Decimal = Decimal("10000")
    maker_fee: Decimal = Decimal("0.0002")
    taker_fee: Decimal = Decimal("0.0005")
    market_slippage: Decimal = Decimal("0.0002")
    permissions: KeyPermissions = field(
        default_factory=lambda: KeyPermissions(True, False, True, True))

    instruments: dict[str, Instrument] = field(default_factory=dict)
    prices: dict[str, Decimal] = field(default_factory=dict)
    tickers: dict[str, Ticker24h] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    funding: dict[str, FundingRate] = field(default_factory=dict)
    oi: dict[str, list[OpenInterest]] = field(default_factory=dict)
    kline_data: dict[tuple[str, str], list[Kline]] = field(default_factory=dict)

    leverage: dict[str, int] = field(default_factory=dict)
    margin_type: dict[str, str] = field(default_factory=dict)
    orders: dict[str, ExchangeOrder] = field(default_factory=dict)
    _good_till: dict[str, int] = field(default_factory=dict)
    _pos: dict[str, _Pos] = field(default_factory=dict)

    # 故障注入
    connected: bool = True
    fail_stop_orders: int = 0  # 接下来 N 个止损单下单失败
    fail_all_orders: bool = False

    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _user_q: asyncio.Queue[UserEvent] = field(default_factory=asyncio.Queue)
    _liq_q: asyncio.Queue[Liquidation] = field(default_factory=asyncio.Queue)
    fills: list[UserEvent] = field(default_factory=list)

    # ---------- 测试辅助 ----------
    def add_instrument(self, inst: Instrument, price: Decimal) -> None:
        self.instruments[inst.symbol] = inst
        self.prices[inst.symbol] = price
        spread = inst.tick_size
        self.books[inst.symbol] = OrderBook(
            inst.symbol,
            bids=[(price - spread * i, Decimal(1000)) for i in range(1, 50)],
            asks=[(price + spread * i, Decimal(1000)) for i in range(1, 50)],
        )

    def set_price(self, symbol: str, price: Decimal) -> list[UserEvent]:
        """更新最新价并撮合挂单/触发条件单。返回本次产生的成交事件。"""
        self.prices[symbol] = price
        inst = self.instruments[symbol]
        self.books[symbol] = OrderBook(
            symbol,
            bids=[(price - inst.tick_size * i, Decimal(1000)) for i in range(1, 50)],
            asks=[(price + inst.tick_size * i, Decimal(1000)) for i in range(1, 50)],
        )
        events: list[UserEvent] = []
        now = self.clock.now_ms()
        for o in list(self.orders.values()):
            if o.symbol != symbol or not o.status.is_open:
                continue
            gt = self._good_till.get(o.client_order_id)
            if gt is not None and now >= gt:
                o.status = OrderStatus.EXPIRED
                events.append(self._emit_order(o))
                continue
            fill_px = self._trigger_price(o, price)
            if fill_px is not None:
                events.extend(self._fill(o, fill_px, maker=o.type is OrderType.LIMIT))
        return events

    def drop_order(self, client_order_id: str) -> None:
        """模拟用户在网页端手动撤掉订单（例如删掉止损单）。"""
        o = self.orders[client_order_id]
        o.status = OrderStatus.CANCELED

    def force_position(self, symbol: str, side: Side, qty: Decimal, entry: Decimal) -> None:
        self._pos[symbol] = _Pos(side, qty, entry)

    # ---------- 撮合 ----------
    @staticmethod
    def _trigger_price(o: ExchangeOrder, price: Decimal) -> Decimal | None:
        if o.type is OrderType.LIMIT:
            assert o.price is not None
            if o.side is OrderSide.BUY and price <= o.price:
                return o.price
            if o.side is OrderSide.SELL and price >= o.price:
                return o.price
            return None
        if o.type is OrderType.STOP_MARKET:
            assert o.stop_price is not None
            if o.side is OrderSide.SELL and price <= o.stop_price:
                return price
            if o.side is OrderSide.BUY and price >= o.stop_price:
                return price
            return None
        if o.type is OrderType.TAKE_PROFIT_MARKET:
            assert o.stop_price is not None
            if o.side is OrderSide.SELL and price >= o.stop_price:
                return price
            if o.side is OrderSide.BUY and price <= o.stop_price:
                return price
            return None
        return None

    def _fill(self, o: ExchangeOrder, px: Decimal, maker: bool) -> list[UserEvent]:
        pos = self._pos.get(o.symbol)
        qty = o.qty - o.filled_qty
        if o.close_position or o.reduce_only:
            if pos is None or pos.side.exit_order_side is not o.side:
                o.status = OrderStatus.EXPIRED  # 没有可减的仓位
                return [self._emit_order(o)]
            qty = pos.qty if o.close_position else min(qty, pos.qty)
        if o.type is not OrderType.LIMIT:
            slip = px * self.market_slippage
            px = px + slip if o.side is OrderSide.BUY else px - slip
        fee_rate = self.maker_fee if maker else self.taker_fee
        fee = px * qty * fee_rate
        realized = self._apply_fill(o.symbol, o.side, qty, px)
        self.wallet += realized - fee
        o.avg_price = (o.avg_price * o.filled_qty + px * qty) / (o.filled_qty + qty)
        o.filled_qty += qty
        if o.close_position:
            o.qty = o.filled_qty
        o.status = OrderStatus.FILLED if o.filled_qty >= o.qty else OrderStatus.PARTIALLY_FILLED
        o.update_ms = self.clock.now_ms()
        ev = UserEvent("order", order=_copy(o), last_fill_qty=qty, last_fill_price=px, fee=fee,
                       realized_pnl=realized, is_maker=maker, ts_ms=o.update_ms)
        self.fills.append(ev)
        self._user_q.put_nowait(ev)
        events = [ev]
        # 仓位平掉后，同币种剩余的只减仓单自动失效（与币安行为一致）
        if o.symbol not in self._pos:
            for other in self.orders.values():
                if (other.symbol == o.symbol and other.status.is_open
                        and (other.reduce_only or other.close_position)):
                    other.status = OrderStatus.EXPIRED
                    events.append(self._emit_order(other))
        return events

    def _apply_fill(self, symbol: str, side: OrderSide, qty: Decimal, px: Decimal) -> Decimal:
        pos = self._pos.get(symbol)
        fill_side = Side.LONG if side is OrderSide.BUY else Side.SHORT
        if pos is None:
            self._pos[symbol] = _Pos(fill_side, qty, px)
            return Decimal(0)
        if pos.side is fill_side:
            pos.entry = (pos.entry * pos.qty + px * qty) / (pos.qty + qty)
            pos.qty += qty
            return Decimal(0)
        closed = min(qty, pos.qty)
        realized = (px - pos.entry) * closed * pos.side.sign
        pos.qty -= closed
        rest = qty - closed
        if pos.qty == 0:
            del self._pos[symbol]
            if rest > 0:
                self._pos[symbol] = _Pos(fill_side, rest, px)
        return realized

    def _emit_order(self, o: ExchangeOrder) -> UserEvent:
        o.update_ms = self.clock.now_ms()
        ev = UserEvent("order", order=_copy(o), ts_ms=o.update_ms)
        self._user_q.put_nowait(ev)
        return ev

    def _check(self) -> None:
        if not self.connected:
            raise ExchangeDisconnected("mock: disconnected")

    # ---------- ExchangeClient ----------
    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        pass

    async def load_instruments(self) -> list[Instrument]:
        self._check()
        return list(self.instruments.values())

    async def tickers_24h(self) -> list[Ticker24h]:
        self._check()
        return list(self.tickers.values())

    async def order_book(self, symbol: str, limit: int = 100) -> OrderBook:
        self._check()
        return self.books[symbol]

    async def funding_rates(self) -> list[FundingRate]:
        self._check()
        return list(self.funding.values())

    async def open_interest(self, symbol: str) -> OpenInterest:
        self._check()
        return self.oi[symbol][-1]

    async def open_interest_hist(self, symbol: str, period: str, limit: int) -> list[OpenInterest]:
        self._check()
        return self.oi.get(symbol, [])[-limit:]

    async def klines(self, symbol: str, interval: str, limit: int) -> list[Kline]:
        self._check()
        return self.kline_data.get((symbol, interval), [])[-limit:]

    async def mark_price(self, symbol: str) -> Decimal:
        self._check()
        return self.prices[symbol]

    async def liquidations(self) -> AsyncIterator[Liquidation]:
        while True:
            yield await self._liq_q.get()

    def push_liquidation(self, liq: Liquidation) -> None:
        self._liq_q.put_nowait(liq)

    async def balance(self) -> AccountBalance:
        self._check()
        unreal = sum(((self.prices[s] - p.entry) * p.qty * p.side.sign for s, p in self._pos.items()),
                     Decimal(0))
        return AccountBalance(self.wallet + unreal, self.wallet, self.wallet, unreal)

    async def positions(self) -> list[ExchangePosition]:
        self._check()
        return [
            ExchangePosition(s, p.side, p.qty, p.entry, self.leverage.get(s, 20),
                             self.margin_type.get(s, "CROSSED"),
                             (self.prices[s] - p.entry) * p.qty * p.side.sign)
            for s, p in self._pos.items()
        ]

    async def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self._check()
        return [_copy(o) for o in self.orders.values()
                if o.status.is_open and (symbol is None or o.symbol == symbol)]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._check()
        self.leverage[symbol] = leverage

    async def set_margin_isolated(self, symbol: str) -> None:
        self._check()
        self.margin_type[symbol] = "ISOLATED"

    async def place_order(self, req: OrderRequest) -> ExchangeOrder:
        self._check()
        if self.fail_all_orders:
            raise OrderRejected("mock: all orders rejected")
        if req.type is OrderType.STOP_MARKET and self.fail_stop_orders > 0:
            self.fail_stop_orders -= 1
            raise OrderRejected("mock: stop order rejected")
        if req.client_order_id in self.orders:
            raise OrderRejected("duplicate clientOrderId")
        price = self.prices[req.symbol]
        qty = req.qty if req.qty is not None else Decimal(0)
        if req.close_position:
            pos = self._pos.get(req.symbol)
            qty = pos.qty if pos else Decimal(0)
        o = ExchangeOrder(
            symbol=req.symbol, client_order_id=req.client_order_id,
            exchange_order_id=str(next(self._ids)), side=req.side, type=req.type,
            status=OrderStatus.NEW, qty=qty, price=req.price, stop_price=req.stop_price,
            reduce_only=req.reduce_only, close_position=req.close_position,
            update_ms=self.clock.now_ms(),
        )
        # 条件单：触发价已被穿越时，币安会直接拒单（"Order would immediately trigger"）
        if req.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if self._trigger_price(o, price) is not None:
                raise OrderRejected("Order would immediately trigger.")
        if req.post_only and req.type is OrderType.LIMIT and self._trigger_price(o, price) is not None:
            raise OrderRejected("post only order would match")
        self.orders[o.client_order_id] = o
        if req.good_till_ms is not None:
            self._good_till[o.client_order_id] = req.good_till_ms
        if req.type is OrderType.MARKET:
            self._fill(o, price, maker=False)
        elif req.type is OrderType.LIMIT and self._trigger_price(o, price) is not None:
            self._fill(o, price, maker=False)  # 可立即成交的限价单按现价吃单
        return _copy(o)

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        self._check()
        o = self.orders.get(client_order_id)
        if o is None or not o.status.is_open:
            raise OrderRejected("Unknown order sent.")
        o.status = OrderStatus.CANCELED
        self._emit_order(o)

    async def api_key_permissions(self) -> KeyPermissions:
        return self.permissions

    async def user_events(self) -> AsyncIterator[UserEvent]:
        while True:
            yield await self._user_q.get()


def _copy(o: ExchangeOrder) -> ExchangeOrder:
    return ExchangeOrder(**o.__dict__)
