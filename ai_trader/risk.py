"""风险管理：波动率目标仓位、杠杆上限、最大回撤保护。"""

import numpy as np
import pandas as pd

from .config import RiskConfig


def volatility_target_weights(
    signal: pd.Series, close: pd.Series, cfg: RiskConfig, periods_per_year: int = 252
) -> pd.Series:
    """按目标波动率缩放方向信号，并限制在最大杠杆以内。"""
    realized = np.log(close).diff().rolling(cfg.vol_lookback).std() * np.sqrt(periods_per_year)
    scale = (cfg.target_volatility / realized).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return (signal * scale).clip(-cfg.max_leverage, cfg.max_leverage).rename("weight")


def apply_rebalance_band(current: float, target: float, threshold: float) -> float:
    """方向不变且变化不足 threshold 时保持当前仓位，避免因波动率微调而频繁交易。"""
    same_side = np.sign(current) == np.sign(target) and current != 0
    if same_side and abs(target - current) < threshold:
        return current
    return target


class DrawdownGuard:
    """权益从高点回撤超过阈值时强制空仓并暂停交易。

    冷却期结束后以当前权益为新的高点重新计算，因此这是"单段回撤"保护，
    累计回撤仍可能超过阈值。"""

    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.peak = None
        self.cooldown = 0

    def allow(self, equity: float) -> bool:
        if self.peak is None:
            self.peak = equity
        if self.cooldown > 0:
            self.cooldown -= 1
            if self.cooldown == 0:
                self.peak = equity
            return False
        self.peak = max(self.peak, equity)
        if equity < self.peak * (1 - self.cfg.max_drawdown):
            self.cooldown = self.cfg.cooldown_bars
            return False
        return True
