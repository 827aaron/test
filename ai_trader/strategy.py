"""把模型概率转换成方向信号。"""

import pandas as pd

from .config import StrategyConfig


def probability_to_signal(proba: pd.Series, cfg: StrategyConfig) -> pd.Series:
    """返回 -1 / 0 / +1 方向信号；概率缺失时不持仓。"""
    signal = pd.Series(0.0, index=proba.index, name="signal")
    signal[proba > cfg.long_threshold] = 1.0
    if cfg.allow_short:
        signal[proba < cfg.short_threshold] = -1.0
    signal[proba.isna()] = 0.0
    return signal

