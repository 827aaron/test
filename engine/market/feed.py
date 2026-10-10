"""行情采集：定时 REST 拉取（扫描用）+ WebSocket（标记价格、爆仓）。"""

from __future__ import annotations

import asyncio
import logging

from engine.core.clock import Clock
from engine.exchange.base import MarketData
from engine.market.store import MarketDataStore

log = logging.getLogger(__name__)

KLINE_LIMITS = {"15m": 700, "1h": 200}  # 15m 需要 7 天（672 根）算布林带最窄


class MarketFeed:
    def __init__(self, ex: MarketData, store: MarketDataStore, clock: Clock, concurrency: int = 8) -> None:
        self.ex = ex
        self.store = store
        self.clock = clock
        self._sem = asyncio.Semaphore(concurrency)
        self._instruments_at = 0

    async def refresh_overview(self) -> None:
        """全市场一次性数据：合约列表（每小时）、24h 行情、资金费率。"""
        now = self.clock.now_ms()
        if now - self._instruments_at > 3_600_000 or not self.store.instruments:
            self.store.instruments = {i.symbol: i for i in await self.ex.load_instruments()}
            self._instruments_at = now
        self.store.tickers = {t.symbol: t for t in await self.ex.tickers_24h()}
        self.store.funding = {f.symbol: f for f in await self.ex.funding_rates()}
        for f in self.store.funding.values():
            self.store.update_mark(f.symbol, f.mark_price, now)

    async def refresh_books(self, symbols: list[str]) -> None:
        async def one(sym: str) -> None:
            async with self._sem:
                try:
                    self.store.books[sym] = await self.ex.order_book(sym, 100)
                except Exception:
                    log.warning("拉取盘口失败 %s", sym, exc_info=True)

        await asyncio.gather(*(one(s) for s in symbols))

    async def refresh_details(self, symbols: list[str], timeframes: list[str]) -> None:
        """候选范围内的币：K 线和持仓量历史。"""

        async def one(sym: str) -> None:
            async with self._sem:
                try:
                    for tf in timeframes:
                        self.store.klines[(sym, tf)] = await self.ex.klines(sym, tf, KLINE_LIMITS.get(tf, 200))
                    self.store.oi_hist[sym] = await self.ex.open_interest_hist(sym, "5m", 13)
                except Exception:
                    log.warning("拉取 K 线/持仓量失败 %s", sym, exc_info=True)

        await asyncio.gather(*(one(s) for s in symbols))

    async def run_liquidations(self) -> None:
        while True:
            try:
                async for liq in self.ex.liquidations():
                    self.store.add_liquidation(liq)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("爆仓流断开，5 秒后重连", exc_info=True)
                await asyncio.sleep(5)

    async def run_mark_prices(self) -> None:
        stream = getattr(self.ex, "mark_prices", None)
        if stream is None:
            return
        while True:
            try:
                async for batch in stream():
                    now = self.clock.now_ms()
                    for sym, px in batch.items():
                        self.store.update_mark(sym, px, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("标记价格流断开，3 秒后重连", exc_info=True)
                await asyncio.sleep(3)
