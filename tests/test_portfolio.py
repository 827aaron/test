from decimal import Decimal

import pytest

from engine.core.clock import HOUR_MS
from engine.core.config import PortfolioCfg
from engine.core.types import OrderType
from engine.portfolio.manager import PositionManager

from .conftest import make_inst
from .test_executor import approve
from .test_risk import ticket

D = Decimal


@pytest.fixture
async def opened(executor, mock_ex, store_with_eth):
    # 入场 100，止损 98 → 1R = 2
    rep = await executor.execute(ticket(), approve(), make_inst())
    assert rep.status == "FILLED"
    pm = PositionManager(PortfolioCfg(), executor.portfolio, executor, store_with_eth)
    return pm, executor.portfolio.positions["ETHUSDT"]


async def _stop_px(mock_ex):
    stops = [o for o in await mock_ex.open_orders("ETHUSDT") if o.type is OrderType.STOP_MARKET]
    assert len(stops) == 1
    return stops[0].stop_price


async def test_breakeven_at_1r(opened, mock_ex, clock):
    pm, pos = opened
    entry = pos.entry_price
    clock.advance(1000)
    mock_ex.set_price("ETHUSDT", entry + 2)
    acts = await pm.on_tick(pos, entry + 2, clock.now_ms())
    assert "breakeven" in acts
    assert await _stop_px(mock_ex) >= entry


async def test_partial_at_1_5r_then_trailing(opened, mock_ex, clock):
    pm, pos = opened
    entry = pos.entry_price
    for px in (entry + 2, entry + 3):
        clock.advance(10_000)
        mock_ex.set_price("ETHUSDT", px)
        await pm.on_tick(pos, px, clock.now_ms())
    assert pos.partial_done
    assert mock_ex._pos["ETHUSDT"].qty == D(5)
    # 价格继续上涨，移动止损跟上
    before = await _stop_px(mock_ex)
    clock.advance(30_000)
    px = entry + 8
    mock_ex.set_price("ETHUSDT", D(px))
    acts = await pm.on_tick(pos, D(px), clock.now_ms())
    assert "trail" in acts
    assert await _stop_px(mock_ex) > before


async def test_time_stop(opened, mock_ex, clock):
    pm, pos = opened
    clock.advance(4 * HOUR_MS + 1)
    acts = await pm.on_tick(pos, pos.entry_price + D("0.1"), clock.now_ms())
    assert acts == ["time_stop"]
    assert not mock_ex._pos


async def test_adverse_move_closes(opened, mock_ex, clock):
    pm, pos = opened
    entry = pos.entry_price
    await pm.on_tick(pos, entry + 1, clock.now_ms())
    clock.advance(20_000)
    # 20 秒内从 +0.5R 跌到 -0.4R：反向 0.9R ≥ 0.8R
    acts = await pm.on_tick(pos, entry - D("0.8"), clock.now_ms())
    assert acts == ["adverse_close"]
    assert not mock_ex._pos


async def test_no_action_in_normal_range(opened, clock):
    pm, pos = opened
    acts = await pm.on_tick(pos, pos.entry_price + D("0.5"), clock.now_ms())
    assert acts == []
