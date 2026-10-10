"""端到端：扫描 → 规则策略交易单 → 风控 → 下单（限价入场 + 止损 + 止盈）→ 盯盘 → 止损触发 → 记账 → 推送。"""

import asyncio
from decimal import Decimal

import pytest

from engine.app import Engine
from engine.core.clock import HOUR_MS, MINUTE_MS
from engine.core.config import Config
from engine.core.safety import SafetyLevel
from engine.core.types import FundingRate, KeyPermissions, OpenInterest, OrderType, Ticker24h
from engine.exchange.mock import MockExchange
from engine.storage.db import Database

from .conftest import T0, breakout_klines, flat_klines, make_inst

D = Decimal
M15 = 15 * MINUTE_MS


def setup_market(ex: MockExchange) -> Decimal:
    ex.add_instrument(make_inst("BTCUSDT", tick="0.1"), D(60000))
    eth15 = breakout_klines(M15, 700, T0)
    eth1h = breakout_klines(HOUR_MS, 200, T0)
    price = D(str(round(eth15[-1].close, 2)))
    ex.add_instrument(make_inst("ETHUSDT"), price)
    ex.kline_data[("ETHUSDT", "15m")] = eth15
    ex.kline_data[("ETHUSDT", "1h")] = eth1h
    ex.kline_data[("BTCUSDT", "15m")] = flat_klines(M15, 700, T0, 60000)
    ex.kline_data[("BTCUSDT", "1h")] = flat_klines(HOUR_MS, 200, T0, 60000)
    ex.tickers = {
        "BTCUSDT": Ticker24h("BTCUSDT", D(60000), D("9000000000"), D(0), D(60500), D(59500)),
        "ETHUSDT": Ticker24h("ETHUSDT", price, D("900000000"), D(3), price, D(99)),
    }
    ex.funding = {
        "BTCUSDT": FundingRate("BTCUSDT", D("0.0001"), D(60000), 0),
        "ETHUSDT": FundingRate("ETHUSDT", D("0.0001"), price, 0),
    }
    ex.oi = {s: [OpenInterest(s, D(1000), T0 - i * 300_000) for i in range(13)][::-1] for s in ex.tickers}
    return price


@pytest.fixture
def engine(clock, tmp_path):
    ex = MockExchange(clock=clock)
    setup_market(ex)
    eng = Engine(Config(), ex, Database(tmp_path / "t.db"), clock=clock)
    events = []
    eng.bus.subscribe(events.append)
    eng.events = events
    return eng


def pump(eng: Engine) -> None:
    """把模拟交易所的用户数据流事件交给引擎（真实环境由 user_stream_loop 完成）。"""
    q = eng.ex._user_q
    while not q.empty():
        eng.tracker.on_event(q.get_nowait())


async def test_full_cycle_stop_loss(engine):
    ex: MockExchange = engine.ex
    assert await engine.start()
    assert engine.safety.level is SafetyLevel.RUNNING

    reports = await engine.scan_once()
    pump(engine)
    assert [r.status for r in reports] == ["FILLED"], [r.notes for r in reports]
    pos = engine.portfolio.positions["ETHUSDT"]
    kinds = [e.kind for e in engine.events]
    assert "scan" in kinds and "ticket" in kinds and "position_opened" in kinds

    # 每单都带止损；单笔最大亏损 ≤ 1%
    orders = await ex.open_orders("ETHUSDT")
    stop = next(o for o in orders if o.type is OrderType.STOP_MARKET)
    assert stop.close_position
    assert (pos.entry_price - stop.stop_price) * pos.qty <= D(100)

    # 价格跌破止损 → 交易所触发止损 → 引擎确认平仓并记账
    ex.set_price("ETHUSDT", stop.stop_price - D("0.01"))  # 触发时只滑一个 tick
    pump(engine)
    await asyncio.sleep(1.8)
    assert "ETHUSDT" not in engine.portfolio.positions
    rows = engine.db.closed_positions()
    assert len(rows) == 1 and rows[0].exit_reason == "止损触发"
    net = D(rows[0].realized_pnl) - D(rows[0].fees)
    assert D(-101) < net < D(-80)  # 约 -1R，含滑点和手续费也不超过权益的 1%
    closed = [e for e in engine.events if e.kind == "position_closed"]
    assert closed and closed[-1].payload["pnl"] is not None
    ticket_rows = engine.db.tickets()
    assert ticket_rows[0].exec_status == "FILLED" and ticket_rows[0].ev_usd is not None


async def test_profit_path_breakeven_partial(engine, clock):
    ex: MockExchange = engine.ex
    await engine.start()
    await engine.scan_once()
    pump(engine)
    pos = engine.portfolio.positions["ETHUSDT"]
    r = pos.r_per_unit
    for k in (D("1.1"), D("1.6")):
        clock.advance(5000)
        px = pos.entry_price + r * k
        ex.set_price("ETHUSDT", px)
        engine.store.update_mark("ETHUSDT", px, clock.now_ms())
        await engine.pm.tick_all(clock.now_ms())
        pump(engine)
    assert pos.breakeven_done and pos.partial_done
    assert pos.stop >= pos.entry_price
    assert ex._pos["ETHUSDT"].qty < pos.initial_qty


async def test_daily_loss_halts(engine):
    ex: MockExchange = engine.ex
    await engine.start()
    ex.wallet -= D(301)  # 当天亏 3.01%
    await engine.update_equity()
    assert engine.safety.level is SafetyLevel.HALTED
    assert any(e.kind == "daily_loss" for e in engine.events)
    reps = await engine.scan_once()  # 停机后交易单全部被风控否决，不会下单
    assert reps and all(r.status == "VOID_REJECTED" for r in reps)
    assert not ex._pos


async def test_withdraw_key_refused(clock, tmp_path):
    ex = MockExchange(clock=clock, permissions=KeyPermissions(True, True, True))
    setup_market(ex)
    eng = Engine(Config(), ex, Database(tmp_path / "t.db"), clock=clock)
    assert not await eng.start()
    assert eng.safety.level is SafetyLevel.HALTED


async def test_startup_adopts_existing_position(engine):
    from engine.core.types import Side

    engine.ex.force_position("ETHUSDT", Side.SHORT, D(3), D(103))
    await engine.start()
    assert "ETHUSDT" in engine.portfolio.positions
    stops = [o for o in await engine.ex.open_orders("ETHUSDT") if o.type is OrderType.STOP_MARKET]
    assert len(stops) == 1
    assert not engine.safety.can_open_new  # 有不一致：先暂停，下一次干净对账后恢复
    await engine.reconciler.reconcile()
    assert engine.safety.can_open_new


async def test_repeated_errors_pause(engine):
    await engine.start()
    engine.ex.fail_all_orders = True
    for _ in range(3):
        engine.scanner._last_pick_ms.clear()
        engine.scanner._meetings.clear()
        await engine.scan_once()
    assert not engine.safety.can_open_new


async def test_text_commands(engine):
    await engine.start()
    await engine.scan_once()
    assert "RUNNING" in await engine.text_status()
    assert "ETHUSDT" in await engine.text_positions()
    assert "今日已平仓" in await engine.text_pnl()
