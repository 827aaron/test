from decimal import Decimal

import pytest

from engine.core.clock import HOUR_MS, MINUTE_MS
from engine.core.config import ScannerCfg, SignalsCfg
from engine.core.types import (
    FundingRate,
    Liquidation,
    OpenInterest,
    OrderBook,
    OrderSide,
    Side,
    Ticker24h,
    Tier,
)
from engine.market.store import MarketDataStore
from engine.scanner import signals as sg
from engine.scanner.scanner import Scanner

from .conftest import T0, breakout_klines, flat_klines, make_inst

D = Decimal
M15 = 15 * MINUTE_MS


def book(symbol, price, spread="0.01", depth_qty="5000"):
    p = D(str(price))
    s = D(spread)
    return OrderBook(symbol, [(p - s * i, D(depth_qty)) for i in range(1, 30)],
                     [(p + s * i, D(depth_qty)) for i in range(1, 30)])


def add_symbol(st: MarketDataStore, sym: str, qv="500000000", price=100.0, kl15=None, kl1h=None, **inst_kw):
    st.instruments[sym] = make_inst(sym, **inst_kw)
    st.tickers[sym] = Ticker24h(sym, D(str(price)), D(qv), D(0), D(str(price * 1.02)), D(str(price * 0.98)))
    st.books[sym] = book(sym, price)
    st.klines[(sym, "15m")] = kl15 if kl15 is not None else flat_klines(M15, 700, T0, price)
    st.klines[(sym, "1h")] = kl1h if kl1h is not None else flat_klines(HOUR_MS, 200, T0, price)
    st.update_mark(sym, D(str(price)), T0)


@pytest.fixture
def st():
    s = MarketDataStore()
    add_symbol(s, "BTCUSDT", price=60000.0)
    s.books["BTCUSDT"] = book("BTCUSDT", 60000, "0.1", "10")
    return s


def test_volume_spike_and_breakout_signals():
    ks = breakout_klines(M15, 200, T0)
    cfg = SignalsCfg()
    v = sg.volume_spike(ks, "15m", cfg)
    b = sg.breakout(ks, "15m", cfg)
    assert v and v.side is Side.LONG and v.ratio >= 1
    assert b and b.side is Side.LONG
    assert sg.volume_spike(flat_klines(M15, 200, T0), "15m", cfg) is None
    assert sg.breakout(flat_klines(M15, 200, T0), "15m", cfg) is None
    down = breakout_klines(M15, 200, T0, up=False)
    assert sg.breakout(down, "15m", cfg).side is Side.SHORT


def test_bb_squeeze_expand():
    ks = flat_klines(M15, 700, T0 - 5 * M15, wiggle=0.01)
    tight = flat_klines(M15, 40, T0 - 5 * M15, wiggle=0.0005)
    # 最后一段先收窄，再放量拉升
    ks = ks[:-40] + tight
    last = ks[-1].close
    from engine.core.types import Kline
    for i in range(5):
        c = last * (1 + 0.006 * (i + 1))
        ks.append(Kline(T0 - (5 - i) * M15, c * 0.995, c * 1.001, c * 0.994, c, 3000, 3000 * c,
                        T0 - (4 - i) * M15 - 1))
    s = sg.bb_squeeze_expand(ks, "15m", SignalsCfg())
    assert s is not None and s.side is Side.LONG


def test_relative_strength():
    btc = flat_klines(HOUR_MS, 50, T0, 60000)
    alt = breakout_klines(HOUR_MS, 50, T0, jump=0.05)
    s = sg.relative_strength(alt, btc, "1h", SignalsCfg())
    assert s and s.side is Side.LONG


def test_oi_funding_liquidation(st):
    add_symbol(st, "ETHUSDT", kl15=breakout_klines(M15, 700, T0))
    st.oi_hist["ETHUSDT"] = [OpenInterest("ETHUSDT", D(1000 + i * 10), T0 - (12 - i) * 5 * MINUTE_MS)
                             for i in range(13)]
    s = sg.oi_increase(st, "ETHUSDT", SignalsCfg(), T0)
    assert s and s.side is Side.LONG  # 价涨 + 持仓量涨 12%

    st.funding = {f"S{i}USDT": FundingRate(f"S{i}USDT", D("0.0001"), D(1), 0) for i in range(30)}
    st.funding["ETHUSDT"] = FundingRate("ETHUSDT", D("0.0008"), D(100), 0)
    f = sg.funding_extreme(st, "ETHUSDT", SignalsCfg())
    assert f and f.side is Side.SHORT  # 多头拥挤 → 偏空

    for h in range(1, 7 * 24):
        st.add_liquidation(Liquidation("ETHUSDT", OrderSide.SELL, D(100), D(10), T0 - h * HOUR_MS - 1))
    for _ in range(10):
        st.add_liquidation(Liquidation("ETHUSDT", OrderSide.SELL, D(100), D(10), T0 - 60_000))
    liq = sg.liquidation_spike(st, "ETHUSDT", SignalsCfg(), T0)
    assert liq and liq.side is Side.SHORT and liq.ratio > 1


def test_universe_filters(st):
    add_symbol(st, "ETHUSDT")
    add_symbol(st, "LOWVOLUSDT", qv="1000000")
    add_symbol(st, "NEWUSDT", onboard_ms=T0 - 5 * 24 * HOUR_MS)
    add_symbol(st, "WIDEUSDT")
    st.books["WIDEUSDT"] = book("WIDEUSDT", 100, spread="0.2")
    add_symbol(st, "THINUSDT")
    st.books["THINUSDT"] = book("THINUSDT", 100, depth_qty="1")
    add_symbol(st, "BUSDT", qv="80000000")
    add_symbol(st, "BANNEDUSDT")
    sc = Scanner(ScannerCfg(blacklist=["BANNEDUSDT"]), st)
    res = sc.run_once(T0)
    syms = {u.symbol: u.tier for u in res.universe}
    assert set(syms) == {"BTCUSDT", "ETHUSDT", "BUSDT"}
    assert syms["ETHUSDT"] is Tier.A and syms["BUSDT"] is Tier.B
    r = res.funnel.rejections
    assert r["24h 成交额不足"] == 1 and r["上线天数不足"] == 1 and r["价差过大"] == 1
    assert r["深度不足"] == 1 and r["黑名单"] == 1


def _hot_symbols(st, n, up=True):
    for i in range(n):
        sym = f"HOT{i}USDT"
        add_symbol(st, sym, kl15=breakout_klines(M15, 700, T0, up=up),
                   kl1h=breakout_klines(HOUR_MS, 200, T0, up=up))


def test_selection_rules(st):
    _hot_symbols(st, 8)
    sc = Scanner(ScannerCfg(), st)
    res = sc.run_once(T0)
    assert len(res.scored) == 8
    assert all(c.score >= 60 and len(c.signals) >= 2 for c in res.scored)
    assert len(res.selected) == 3  # 同方向最多 3 个
    assert all(c.reasons for c in res.selected)
    # 冷却：同一批币 1 小时内不会再入选
    res2 = sc.run_once(T0 + 5 * MINUTE_MS)
    assert {c.symbol for c in res2.selected}.isdisjoint({c.symbol for c in res.selected})


def test_max_per_round_and_meetings_per_hour(st):
    _hot_symbols(st, 4, up=True)
    for i in range(4):
        sym = f"COLD{i}USDT"
        add_symbol(st, sym, kl15=breakout_klines(M15, 700, T0, up=False),
                   kl1h=breakout_klines(HOUR_MS, 200, T0, up=False))
    sc = Scanner(ScannerCfg(), st)
    res = sc.run_once(T0)
    assert len(res.selected) == 4  # 每小时最多 4 次会
    assert sc.run_once(T0 + 5 * MINUTE_MS).selected == []
    cfg = ScannerCfg()
    cfg.selection.max_meetings_per_hour = 10
    sc2 = Scanner(cfg, st)
    assert len(sc2.run_once(T0).selected) == 5  # 每轮最多 5 个


def test_open_position_excluded_and_counts_direction(st):
    _hot_symbols(st, 4)
    sc = Scanner(ScannerCfg(), st)
    res = sc.run_once(T0, open_sides={"HOT0USDT": Side.LONG, "XUSDT": Side.LONG})
    assert "HOT0USDT" not in {c.symbol for c in res.selected}
    assert len(res.selected) == 1  # 已有 2 个多单，只剩 1 个名额


def test_btc_halt(st):
    _hot_symbols(st, 2)
    st.klines[("BTCUSDT", "15m")] = breakout_klines(M15, 700, T0, price=60000, jump=0.05)
    sc = Scanner(ScannerCfg(), st)
    res = sc.run_once(T0)
    assert res.funnel.btc_halt and res.selected == []
    st.klines[("BTCUSDT", "15m")] = flat_klines(M15, 700, T0, 60000)
    assert sc.run_once(T0 + 10 * MINUTE_MS).selected == []  # 15 分钟内仍暂停
    assert sc.run_once(T0 + 16 * MINUTE_MS).selected != []


def test_scope_top10_and_whitelist(st):
    for i in range(15):
        add_symbol(st, f"C{i:02d}USDT", qv=str(60_000_000 + i * 1_000_000))
    assert len(Scanner(ScannerCfg(scope="top10"), st).run_once(T0).universe) == 10
    wl = Scanner(ScannerCfg(scope="whitelist", whitelist=["C01USDT"]), st).run_once(T0)
    assert [u.symbol for u in wl.universe] == ["C01USDT"]
