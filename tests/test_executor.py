from decimal import Decimal

from engine.core.types import OrderType, Side
from engine.risk.engine import RiskDecision

from .conftest import make_inst
from .test_risk import ticket

D = Decimal


def approve(qty=D(10)):
    return RiskDecision("APPROVE", qty, 3, None)


async def test_open_places_stop_and_tp(executor, mock_ex):
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "FILLED", rep.notes
    pos = executor.portfolio.positions["ETHUSDT"]
    assert pos.qty == D(10) and pos.side is Side.LONG
    open_o = await mock_ex.open_orders("ETHUSDT")
    stops = [o for o in open_o if o.type is OrderType.STOP_MARKET]
    tps = [o for o in open_o if o.type is OrderType.TAKE_PROFIT_MARKET]
    assert len(stops) == 1 and stops[0].close_position and stops[0].stop_price == D(98)
    assert len(tps) == 1 and tps[0].stop_price == D(104)
    assert mock_ex.margin_type["ETHUSDT"] == "ISOLATED"
    assert mock_ex.leverage["ETHUSDT"] == 3


async def test_void_when_price_left_range(executor, mock_ex):
    mock_ex.set_price("ETHUSDT", D("100.5"))
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "VOID_PRICE"
    assert not mock_ex._pos  # 没有追单


async def test_void_when_expired(executor, clock):
    clock.advance(200_000)
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "VOID_EXPIRED"


async def test_void_when_paused(executor):
    executor.safety.pause(__import__("engine.core.safety", fromlist=["PauseReason"]).PauseReason.USER)
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "VOID_PAUSED"


async def test_stop_failure_closes_position_immediately(executor, mock_ex):
    mock_ex.fail_stop_orders = 10
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "UNPROTECTED_CLOSED"
    assert not mock_ex._pos
    assert "ETHUSDT" not in executor.portfolio.positions


async def test_unfilled_entry_is_canceled(executor, mock_ex):
    # 限价买单挂在 100.1 以下：把价格抬到区间上沿之上再回到区间内会成交；这里让入场单挂着不成交
    t = ticket(entry=(D("99.9"), D("100.1")))
    mock_ex.set_price("ETHUSDT", D("100.1"))
    # 入场限价取现价 100.1，会立刻成交；改用把价格放到区间内但让模拟交易所拒绝立即成交
    mock_ex.prices["ETHUSDT"] = D("100.05")
    orig = mock_ex._trigger_price
    mock_ex._trigger_price = staticmethod(lambda o, p: None if o.type is OrderType.LIMIT else orig(o, p))
    rep = await executor.execute(t, approve(), make_inst())
    assert rep.status == "NOT_FILLED"
    assert not [o for o in await mock_ex.open_orders() if o.type is OrderType.LIMIT]


async def test_move_stop_only_tightens(executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    pos = executor.portfolio.positions["ETHUSDT"]
    assert not await executor.move_stop(pos, D(97), "放宽")  # 拒绝
    assert pos.stop == D(98)
    assert await executor.move_stop(pos, D(99), "收紧")
    stops = [o for o in await mock_ex.open_orders("ETHUSDT") if o.type is OrderType.STOP_MARKET]
    assert len(stops) == 1 and stops[0].stop_price == D(99)


async def test_stop_triggers_and_closes(executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    mock_ex.set_price("ETHUSDT", D("97.9"))
    assert not mock_ex._pos
    # 剩余的止盈单随仓位一起失效
    assert not await mock_ex.open_orders("ETHUSDT")


async def test_reduce_and_close(executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    pos = executor.portfolio.positions["ETHUSDT"]
    assert await executor.reduce(pos, D("0.5"), make_inst(), "test")
    assert mock_ex._pos["ETHUSDT"].qty == D(5)
    assert await executor.close(pos, "test")
    assert not mock_ex._pos
    assert "ETHUSDT" not in executor.portfolio.positions
