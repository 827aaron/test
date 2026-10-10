"""行情快照存储：扫描器每轮读取一次，盯盘每秒读取标记价格。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.clock import HOUR_MS
from engine.core.types import (
    FundingRate,
    Instrument,
    Kline,
    Liquidation,
    OpenInterest,
    OrderBook,
    Ticker24h,
)


@dataclass
class MarketDataStore:
    instruments: dict[str, Instrument] = field(default_factory=dict)
    tickers: dict[str, Ticker24h] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    funding: dict[str, FundingRate] = field(default_factory=dict)
    oi_hist: dict[str, list[OpenInterest]] = field(default_factory=dict)  # 5 分钟粒度
    klines: dict[tuple[str, str], list[Kline]] = field(default_factory=dict)
    marks: dict[str, Decimal] = field(default_factory=dict)
    mark_ts_ms: dict[str, int] = field(default_factory=dict)
    # 爆仓：保留 8 天，用于计算“平时”的小时均值
    liquidations: dict[str, deque[Liquidation]] = field(default_factory=dict)
    liq_keep_ms: int = 8 * 24 * HOUR_MS

    def add_liquidation(self, liq: Liquidation) -> None:
        q = self.liquidations.setdefault(liq.symbol, deque())
        q.append(liq)
        while q and q[0].ts_ms < liq.ts_ms - self.liq_keep_ms:
            q.popleft()

    def liq_notional(self, symbol: str, start_ms: int, end_ms: int) -> tuple[Decimal, Decimal]:
        """返回 (多头被爆金额, 空头被爆金额)。"""
        longs = shorts = Decimal(0)
        for liq in self.liquidations.get(symbol, ()):
            if start_ms <= liq.ts_ms < end_ms:
                if liq.side.value == "SELL":
                    longs += liq.notional
                else:
                    shorts += liq.notional
        return longs, shorts

    def update_mark(self, symbol: str, price: Decimal, ts_ms: int) -> None:
        self.marks[symbol] = price
        self.mark_ts_ms[symbol] = ts_ms

    def last_price(self, symbol: str) -> Decimal | None:
        if symbol in self.marks:
            return self.marks[symbol]
        t = self.tickers.get(symbol)
        return t.last_price if t else None

    def closed_klines(self, symbol: str, interval: str) -> list[Kline]:
        return [k for k in self.klines.get((symbol, interval), []) if k.closed]
