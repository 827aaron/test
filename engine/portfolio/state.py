"""本地持仓登记表（引擎视角）。以交易所为准，由对账模块修正。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.types import Side


@dataclass
class ManagedPosition:
    ticket_id: str
    symbol: str
    side: Side
    qty: Decimal
    initial_qty: Decimal
    entry_price: Decimal
    initial_stop: Decimal
    stop: Decimal
    take_profit: Decimal | None
    opened_ms: int
    stop_order_id: str | None = None
    tp_order_ids: list[str] = field(default_factory=list)
    breakeven_done: bool = False
    partial_done: bool = False
    best_price: Decimal | None = None
    recent: deque[tuple[int, Decimal]] = field(default_factory=deque)
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    adopted: bool = False  # 对账时从交易所“认领”的仓位（例如重启前开的）
    closing: bool = False
    seq: int = 0  # 用来生成唯一的 clientOrderId

    @property
    def r_per_unit(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop)

    def progress_r(self, price: Decimal) -> Decimal:
        r = self.r_per_unit
        return (price - self.entry_price) * self.side.sign / r if r > 0 else Decimal(0)

    def risk_usd(self) -> Decimal:
        """当前止损触发时的亏损（止损移到保本后为 0 或负数）。"""
        return max((self.entry_price - self.stop) * self.side.sign * self.qty, Decimal(0))

    def is_tighter(self, new_stop: Decimal) -> bool:
        return (new_stop > self.stop) if self.side is Side.LONG else (new_stop < self.stop)

    def next_id(self, kind: str) -> str:
        self.seq += 1
        return f"aq-{self.ticket_id[-10:]}-{kind}{self.seq}"[:36]


@dataclass
class Portfolio:
    positions: dict[str, ManagedPosition] = field(default_factory=dict)  # symbol -> position
    closed: list[ManagedPosition] = field(default_factory=list)

    def open_sides(self) -> dict[str, Side]:
        return {s: p.side for s, p in self.positions.items()}

    def close(self, symbol: str) -> ManagedPosition | None:
        p = self.positions.pop(symbol, None)
        if p is not None:
            self.closed.append(p)
        return p
