"""指标计算（numpy 实现，结果与 TA-Lib 的常见定义一致）。

阶段 1 不强制依赖 TA-Lib（需要系统 C 库），装了 TA-Lib 后可以替换。
"""

from __future__ import annotations

import numpy as np

from engine.core.types import Kline


def arrays(ks: list[Kline]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    o = np.array([k.open for k in ks], dtype=float)
    h = np.array([k.high for k in ks], dtype=float)
    lo = np.array([k.low for k in ks], dtype=float)
    c = np.array([k.close for k in ks], dtype=float)
    v = np.array([k.volume for k in ks], dtype=float)
    return o, h, lo, c, v


def atr(ks: list[Kline], period: int = 14) -> float:
    """Wilder ATR，返回最后一个值。"""
    if len(ks) < period + 1:
        return float("nan")
    _, h, lo, c, _ = arrays(ks)
    prev = c[:-1]
    tr = np.maximum(h[1:] - lo[1:], np.maximum(abs(h[1:] - prev), abs(lo[1:] - prev)))
    a = tr[:period].mean()
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return float(a)


def bb_width_series(close: np.ndarray, period: int = 20, k: float = 2.0) -> np.ndarray:
    """布林带宽度 (上轨-下轨)/中轨，长度 = len(close)-period+1。"""
    if len(close) < period:
        return np.array([])
    win = np.lib.stride_tricks.sliding_window_view(close, period)
    mid = win.mean(axis=1)
    sd = win.std(axis=1)
    return (2 * k * sd) / mid


def ema(x: np.ndarray, period: int) -> np.ndarray:
    alpha = 2 / (period + 1)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out
