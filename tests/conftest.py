from __future__ import annotations

import math
from decimal import Decimal

import pytest

from engine.core.bus import EventBus
from engine.core.clock import HOUR_MS, ManualClock
from engine.core.config import Config
from engine.core.safety import PauseReason, SafetyState
from engine.core.types import Instrument, Kline, Ticker24h
from engine.exchange.mock import MockExchange
from engine.execution.executor import Executor
from engine.execution.tracker import OrderTracker
from engine.market.store import MarketDataStore
from engine.portfolio.state import Portfolio

D = Decimal
T0 = 1_760_000_000_000 - (1_760_000_000_000 % HOUR_MS)  # 整点


def make_inst(symbol: str = "ETHUSDT", tick: str = "0.01", step: str = "0.001",
              onboard_ms: int = T0 - 400 * 24 * HOUR_MS) -> Instrument:
    return Instrument(symbol, symbol.removesuffix("USDT"), "USDT", D(tick), D(step), D(step), D("5"),
                      onboard_ms)


def flat_klines(tf_ms: int, n: int, end_ms: int, price: float = 100.0, vol: float = 1000.0,
                wiggle: float = 0.002) -> list[Kline]:
    """一段窄幅震荡的 K 线，结束于 end_ms。"""
    out = []
    start = end_ms - n * tf_ms
    for i in range(n):
        o = price * (1 + wiggle * math.sin(i / 3))
        c = price * (1 + wiggle * math.sin((i + 1) / 3))
        hi, lo = max(o, c) * (1 + wiggle / 2), min(o, c) * (1 - wiggle / 2)
        out.append(Kline(start + i * tf_ms, o, hi, lo, c, vol, vol * c, start + (i + 1) * tf_ms - 1))
    return out


def breakout_klines(tf_ms: int, n: int, end_ms: int, price: float = 100.0, jump: float = 0.03,
                    vol_mult: float = 6.0, up: bool = True) -> list[Kline]:
    """前面窄幅震荡，最后一根放量突破。"""
    ks = flat_klines(tf_ms, n - 1, end_ms - tf_ms, price)
    last_close = ks[-1].close
    c = last_close * (1 + jump if up else 1 - jump)
    o = last_close
    hi, lo = max(o, c) * 1.001, min(o, c) * 0.999
    ks.append(Kline(end_ms - tf_ms, o, hi, lo, c, 1000 * vol_mult, 1000 * vol_mult * c, end_ms - 1))
    return ks


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def mock_ex(clock: ManualClock) -> MockExchange:
    ex = MockExchange(clock=clock)
    ex.add_instrument(make_inst(), D("100"))
    ex.tickers["ETHUSDT"] = Ticker24h("ETHUSDT", D(100), D("500000000"), D(1), D(103), D(97))
    return ex


@pytest.fixture
def running_safety() -> SafetyState:
    s = SafetyState()
    s.clear(PauseReason.STARTUP)
    return s


@pytest.fixture
def executor(mock_ex, running_safety, bus, clock):
    pf = Portfolio()
    tr = OrderTracker()
    return Executor(mock_ex, tr, pf, running_safety, bus, clock, entry_timeout_sec=0.05, poll_sec=0.01)


@pytest.fixture
def store_with_eth(mock_ex) -> MarketDataStore:
    st = MarketDataStore()
    st.instruments = dict(mock_ex.instruments)
    st.tickers = dict(mock_ex.tickers)
    st.update_mark("ETHUSDT", D(100), T0)
    st.klines[("ETHUSDT", "1h")] = flat_klines(HOUR_MS, 100, T0)
    return st
