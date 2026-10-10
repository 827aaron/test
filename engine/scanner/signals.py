"""七种信号。每个信号返回方向和强度（实际值 / 阈值）。只使用已收盘的 K 线。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from engine.core.clock import HOUR_MS
from engine.core.config import SignalsCfg
from engine.core.types import Kline, Side
from engine.market.indicators import bb_width_series
from engine.market.store import MarketDataStore

TF_MS = {"5m": 5 * 60_000, "15m": 15 * 60_000, "1h": HOUR_MS, "4h": 4 * HOUR_MS}

# 权重（默认决定，见 DESIGN.md 第 13 节）：按强度在 0.6–1.0 倍之间缩放，总分封顶 100。
# 刻意让总权重大于 100：大约 3 个刚达标的信号 ≈ 50 分，3 个强信号 ≈ 80 分，门槛 60 分。
WEIGHTS = {
    "volume_spike": 25,
    "breakout": 30,
    "oi_increase": 25,
    "funding_extreme": 15,
    "liquidation_spike": 15,
    "bb_squeeze_expand": 25,
    "relative_strength": 25,
}


@dataclass(frozen=True)
class Signal:
    name: str
    timeframe: str
    side: Side
    ratio: float  # 实际值 / 阈值，≥ 1 才算触发
    detail: str

    @property
    def score(self) -> float:
        factor = 0.6 + 0.4 * min(max(self.ratio - 1.0, 0.0), 1.0)
        return WEIGHTS[self.name] * factor


def _bars_per(tf: str, hours: float) -> int:
    return int(hours * HOUR_MS / TF_MS[tf])


def volume_spike(ks: list[Kline], tf: str, cfg: SignalsCfg) -> Signal | None:
    n = _bars_per(tf, 24)
    if len(ks) < n + 1:
        return None
    last = ks[-1]
    avg = np.mean([k.volume for k in ks[-n - 1:-1]])
    if avg <= 0:
        return None
    ratio = last.volume / avg / cfg.volume_spike_mult
    if ratio < 1:
        return None
    side = Side.LONG if last.close >= last.open else Side.SHORT
    return Signal("volume_spike", tf, side, ratio, f"{tf} 成交量是 24h 均量的 {last.volume / avg:.1f} 倍")


def breakout(ks: list[Kline], tf: str, cfg: SignalsCfg) -> Signal | None:
    n = _bars_per(tf, cfg.breakout_lookback_h)
    if len(ks) < n + 1:
        return None
    last = ks[-1]
    prev = ks[-n - 1:-1]
    hi = max(k.high for k in prev)
    lo = min(k.low for k in prev)
    if last.close > hi:
        return Signal("breakout", tf, Side.LONG, 1 + (last.close - hi) / hi * 100,
                      f"{tf} 收盘 {last.close:g} 突破 24h 高点 {hi:g}")
    if last.close < lo:
        return Signal("breakout", tf, Side.SHORT, 1 + (lo - last.close) / lo * 100,
                      f"{tf} 收盘 {last.close:g} 跌破 24h 低点 {lo:g}")
    return None


def bb_squeeze_expand(ks: list[Kline], tf: str, cfg: SignalsCfg, period: int = 20) -> Signal | None:
    n = _bars_per(tf, cfg.bb_squeeze_lookback_d * 24)
    if len(ks) < period + 10:
        return None
    close = np.array([k.close for k in ks[-(n + period):]], dtype=float)
    w = bb_width_series(close, period)
    if len(w) < 10:
        return None
    recent = w[-10:]  # 最近 10 根内出现过“7 天最窄”
    min_idx = int(np.argmin(w))
    if min_idx < len(w) - 10:
        return None
    squeeze = float(recent.min())
    if squeeze <= 0:
        return None
    ratio = float(w[-1]) / squeeze / cfg.bb_expand_mult
    if ratio < 1:
        return None
    mid = close[-period:].mean()
    side = Side.LONG if close[-1] >= mid else Side.SHORT
    return Signal("bb_squeeze_expand", tf, side, ratio,
                  f"{tf} 布林带从 7 天最窄放大到 {w[-1] / squeeze:.2f} 倍")


def relative_strength(ks: list[Kline], btc: list[Kline], tf: str, cfg: SignalsCfg) -> Signal | None:
    n = _bars_per(tf, 4)
    if len(ks) < n + 1 or len(btc) < n + 1:
        return None
    r = ks[-1].close / ks[-n - 1].close - 1
    rb = btc[-1].close / btc[-n - 1].close - 1
    diff = (r - rb) * 100
    ratio = abs(diff) / cfg.rel_strength_4h_pct
    if ratio < 1:
        return None
    side = Side.LONG if diff > 0 else Side.SHORT
    return Signal("relative_strength", tf, side, ratio, f"4h 相对 BTC {'强' if diff > 0 else '弱'} {diff:+.2f}%")


def oi_increase(store: MarketDataStore, symbol: str, cfg: SignalsCfg, now_ms: int) -> Signal | None:
    hist = store.oi_hist.get(symbol, [])
    if len(hist) < 2:
        return None
    last = hist[-1]
    base = next((h for h in reversed(hist) if h.ts_ms <= last.ts_ms - HOUR_MS), None)
    if base is None or base.value <= 0:
        return None
    pct = float((last.value - base.value) / base.value * 100)
    ratio = pct / cfg.oi_increase_1h_pct
    if ratio < 1:
        return None
    ks = store.closed_klines(symbol, "15m")
    if len(ks) < 5:
        return None
    side = Side.LONG if ks[-1].close >= ks[-5].close else Side.SHORT  # 跟随 1h 价格方向
    return Signal("oi_increase", "1h", side, ratio, f"1h 持仓量 +{pct:.1f}%，价格{'涨' if side is Side.LONG else '跌'}")


def funding_extreme(store: MarketDataStore, symbol: str, cfg: SignalsCfg) -> Signal | None:
    fr = store.funding.get(symbol)
    if fr is None:
        return None
    rate_pct = float(fr.rate) * 100
    all_abs = sorted(abs(float(f.rate)) for f in store.funding.values())
    top_cut = all_abs[int(len(all_abs) * (1 - cfg.funding_top_pct / 100))] if len(all_abs) >= 20 else None
    by_abs = abs(rate_pct) / cfg.funding_abs_pct
    in_top = top_cut is not None and abs(float(fr.rate)) >= top_cut and fr.rate != 0
    if by_abs < 1 and not in_top:
        return None
    # 反向：资金费率极高 = 多头拥挤 → 偏空（默认决定，见 DESIGN.md 第 13 节）
    side = Side.SHORT if fr.rate > 0 else Side.LONG
    return Signal("funding_extreme", "8h", side, max(by_abs, 1.0),
                  f"资金费率 {rate_pct:+.4f}%{'（全市场前 5%）' if in_top else ''}")


def liquidation_spike(store: MarketDataStore, symbol: str, cfg: SignalsCfg, now_ms: int) -> Signal | None:
    longs, shorts = store.liq_notional(symbol, now_ms - HOUR_MS, now_ms)
    total = longs + shorts
    if total <= 0:
        return None
    hist_l, hist_s = store.liq_notional(symbol, now_ms - 7 * 24 * HOUR_MS, now_ms - HOUR_MS)
    hours = Decimal(7 * 24 - 1)
    normal = (hist_l + hist_s) / hours
    if normal <= 0:
        return None
    ratio = float(total / normal) / cfg.liquidation_mult
    if ratio < 1:
        return None
    # 顺势：多头被大量爆仓 → 价格在被砸 → 偏空（默认决定，见 DESIGN.md 第 13 节）
    side = Side.SHORT if longs > shorts else Side.LONG
    return Signal("liquidation_spike", "1h", side, ratio,
                  f"1h 爆仓 ${float(total):,.0f}，是平时的 {float(total / normal):.1f} 倍")


def compute_all(store: MarketDataStore, symbol: str, cfg: SignalsCfg, timeframes: list[str],
                now_ms: int, btc_symbol: str = "BTCUSDT") -> list[Signal]:
    out: list[Signal] = []
    for tf in timeframes:
        ks = store.closed_klines(symbol, tf)
        for fn in (volume_spike, breakout, bb_squeeze_expand):
            s = fn(ks, tf, cfg)
            if s:
                out.append(s)
        if symbol != btc_symbol:
            s = relative_strength(ks, store.closed_klines(btc_symbol, tf), tf, cfg)
            if s:
                out.append(s)
    for s in (oi_increase(store, symbol, cfg, now_ms), funding_extreme(store, symbol, cfg),
              liquidation_spike(store, symbol, cfg, now_ms)):
        if s:
            out.append(s)
    return out
