"""交易所无关的核心数据类型。金额一律 Decimal，时间一律 UTC 毫秒（int）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG

    @property
    def entry_order_side(self) -> OrderSide:
        return OrderSide.BUY if self is Side.LONG else OrderSide.SELL

    @property
    def exit_order_side(self) -> OrderSide:
        return OrderSide.SELL if self is Side.LONG else OrderSide.BUY


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class Tier(StrEnum):
    A = "A"
    B = "B"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    onboard_ms: int
    trading: bool = True
    contract_type: str = "PERPETUAL"

    def round_price(self, price: Decimal, up: bool = False) -> Decimal:
        q = (price / self.tick_size).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
        return (q * self.tick_size).normalize()

    def round_qty(self, qty: Decimal) -> Decimal:
        q = (qty / self.step_size).to_integral_value(rounding=ROUND_DOWN)
        return (q * self.step_size).normalize()


@dataclass(frozen=True)
class Ticker24h:
    symbol: str
    last_price: Decimal
    quote_volume: Decimal
    price_change_pct: Decimal
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class BookTop:
    symbol: str
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 100


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: list[tuple[Decimal, Decimal]]  # 价格从高到低
    asks: list[tuple[Decimal, Decimal]]  # 价格从低到高

    @property
    def top(self) -> BookTop:
        return BookTop(self.symbol, self.bids[0][0], self.asks[0][0])

    def depth_usd(self, pct: Decimal) -> Decimal:
        """现价上下 pct% 范围内的挂单总额（两侧之和）。"""
        mid = self.top.mid
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        total = sum((p * q for p, q in self.bids if p >= lo), Decimal(0))
        total += sum((p * q for p, q in self.asks if p <= hi), Decimal(0))
        return total


@dataclass(frozen=True)
class Kline:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    close_ms: int
    closed: bool = True


@dataclass(frozen=True)
class FundingRate:
    symbol: str
    rate: Decimal  # 小数，0.0005 = 0.05%
    mark_price: Decimal
    next_funding_ms: int


@dataclass(frozen=True)
class OpenInterest:
    symbol: str
    value: Decimal  # 合约张数（以币计）
    ts_ms: int


@dataclass(frozen=True)
class Liquidation:
    symbol: str
    side: OrderSide  # 被强平订单的方向：SELL = 多头被爆
    price: Decimal
    qty: Decimal
    ts_ms: int

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    type: OrderType
    qty: Decimal | None
    client_order_id: str
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    good_till_ms: int | None = None
    post_only: bool = False


@dataclass
class ExchangeOrder:
    symbol: str
    client_order_id: str
    exchange_order_id: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    qty: Decimal
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    price: Decimal | None = None
    stop_price: Decimal | None = None
    reduce_only: bool = False
    close_position: bool = False
    update_ms: int = 0


@dataclass
class ExchangePosition:
    symbol: str
    side: Side
    qty: Decimal  # 恒为正
    entry_price: Decimal
    leverage: int
    margin_type: str = "ISOLATED"
    unrealized_pnl: Decimal = Decimal(0)


@dataclass(frozen=True)
class AccountBalance:
    equity: Decimal  # 钱包余额 + 未实现盈亏
    wallet: Decimal
    available: Decimal
    unrealized: Decimal


@dataclass(frozen=True)
class KeyPermissions:
    can_trade_futures: bool
    can_withdraw: bool
    ip_restricted: bool
    checked: bool = True  # False 表示接口不可用（测试网），需要用户手动确认


@dataclass(frozen=True)
class UserEvent:
    """用户数据流事件：订单更新 / 成交 / 账户更新。"""

    kind: str  # "order" | "account"
    order: ExchangeOrder | None = None
    last_fill_qty: Decimal = Decimal(0)
    last_fill_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    is_maker: bool = False
    ts_ms: int = 0
    extra: dict = field(default_factory=dict)
