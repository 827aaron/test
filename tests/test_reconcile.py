from decimal import Decimal

import pytest

from engine.core.safety import PauseReason
from engine.core.types import OrderType, Side
from engine.reconcile.reconciler import Reconciler

from .conftest import make_inst
from .test_executor import approve
from .test_risk import ticket

D = Decimal


@pytest.fixture
def rec(executor, mock_ex, running_safety, bus, clock):
    return Reconciler(mock_ex, executor.portfolio, executor, running_safety, bus, clock)


async def stops(mock_ex):
    return [o for o in await mock_ex.open_orders("ETHUSDT") if o.type is OrderType.STOP_MARKET]


async def test_clean_reconcile(rec, executor):
    await executor.execute(ticket(), approve(), make_inst())
    rep = await rec.reconcile()
    assert rep.ok and not rep.mismatch
    assert rec.safety.can_open_new


async def test_adopt_unknown_position_and_protect_it(rec, mock_ex):
    mock_ex.force_position("ETHUSDT", Side.LONG, D(10), D(100))
    rep = await rec.reconcile()
    assert rep.adopted == ["ETHUSDT"] and rep.stops_restored == ["ETHUSDT"]
    s = await stops(mock_ex)
    assert len(s) == 1
    # 保护止损：亏损 ≤ 权益 1%
    assert (D(100) - s[0].stop_price) * 10 <= D(100) + D("0.01")
    assert not rec.safety.can_open_new  # 对不上 → 暂停开新单
    rep2 = await rec.reconcile()
    assert not rep2.mismatch and rec.safety.can_open_new  # 下一次干净的对账后恢复


async def test_missing_stop_is_restored(rec, executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    pos = executor.portfolio.positions["ETHUSDT"]
    mock_ex.drop_order(pos.stop_order_id)  # 用户在网页端删掉了止损
    rep = await rec.reconcile()
    assert rep.stops_restored == ["ETHUSDT"]
    assert len(await stops(mock_ex)) == 1


async def test_missing_stop_unrecoverable_closes(rec, executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    pos = executor.portfolio.positions["ETHUSDT"]
    mock_ex.drop_order(pos.stop_order_id)
    mock_ex.fail_stop_orders = 10
    rep = await rec.reconcile()
    assert rep.force_closed == ["ETHUSDT"]
    assert not mock_ex._pos


async def test_vanished_position(rec, executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    mock_ex._pos.clear()  # 离线期间被平
    rep = await rec.reconcile()
    assert rep.vanished == ["ETHUSDT"]
    assert "ETHUSDT" not in executor.portfolio.positions


async def test_qty_mismatch_fixed(rec, executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    mock_ex._pos["ETHUSDT"].qty = D(7)
    rep = await rec.reconcile()
    assert rep.qty_fixed == ["ETHUSDT"]
    assert executor.portfolio.positions["ETHUSDT"].qty == D(7)


async def test_disconnect_pauses(rec, mock_ex):
    mock_ex.connected = False
    rep = await rec.reconcile()
    assert not rep.ok
    assert PauseReason.DISCONNECTED in rec.safety.reasons
    mock_ex.connected = True
    await rec.reconcile()
    assert rec.safety.can_open_new


async def test_orphan_protective_orders_canceled(rec, executor, mock_ex):
    await executor.execute(ticket(), approve(), make_inst())
    # 仓位被外部平掉，但保护单还挂着（模拟交易所未自动撤）
    mock_ex._pos.clear()
    rep = await rec.reconcile()
    assert not await mock_ex.open_orders("ETHUSDT")
    assert rep.vanished == ["ETHUSDT"]
