"""券商接口抽象与模拟盘实现。接入真实券商时实现 Broker 接口即可。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Fill:
    symbol: str
    quantity: float  # 正数买入，负数卖出
    price: float
    commission: float


class Broker(ABC):
    @abstractmethod
    def cash(self) -> float: ...

    @abstractmethod
    def position(self, symbol: str) -> float: ...

    @abstractmethod
    def submit_market_order(self, symbol: str, quantity: float, price: float) -> Fill: ...

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash() + sum(self.position(s) * p for s, p in prices.items())


@dataclass
class PaperBroker(Broker):
    """内存模拟盘：按给定价格加滑点成交，扣除手续费。"""

    starting_cash: float = 100_000.0
    commission: float = 0.0005
    slippage: float = 0.0005
    _cash: float = field(init=False)
    _positions: dict = field(default_factory=dict, init=False)
    fills: list = field(default_factory=list, init=False)

    def __post_init__(self):
        self._cash = self.starting_cash

    def cash(self) -> float:
        return self._cash

    def position(self, symbol: str) -> float:
        return self._positions.get(symbol, 0.0)

    def submit_market_order(self, symbol: str, quantity: float, price: float) -> Fill:
        if quantity == 0:
            raise ValueError("下单数量不能为 0")
        exec_price = price * (1 + self.slippage if quantity > 0 else 1 - self.slippage)
        commission = abs(quantity) * exec_price * self.commission
        self._cash -= quantity * exec_price + commission
        self._positions[symbol] = self.position(symbol) + quantity
        fill = Fill(symbol, quantity, exec_price, commission)
        self.fills.append(fill)
        return fill
