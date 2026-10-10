"""实盘/模拟盘交易循环：用全部历史训练模型，对最新 K 线生成目标仓位并调仓。"""

import logging

import pandas as pd

from .broker import Broker
from .config import Config
from .features import build_features, build_labels
from .model import SignalModel
from .risk import DrawdownGuard, apply_rebalance_band, volatility_target_weights
from .strategy import probability_to_signal

log = logging.getLogger(__name__)


class Trader:
    def __init__(self, symbol: str, broker: Broker, cfg: Config | None = None, min_trade_value: float = 100.0):
        self.symbol = symbol
        self.broker = broker
        self.cfg = cfg or Config()
        self.min_trade_value = min_trade_value
        self.guard = DrawdownGuard(self.cfg.risk)

    def target_weight(self, history: pd.DataFrame) -> tuple[float, float]:
        """返回 (上涨概率, 目标仓位)。history 的最后一根是刚收盘的 K 线。"""
        feats = build_features(history)
        labels = build_labels(history)
        valid = feats.notna().all(axis=1)
        train_y = labels[valid].dropna()
        if len(train_y) < self.cfg.model.min_train_size:
            raise ValueError(f"训练样本不足: {len(train_y)} < {self.cfg.model.min_train_size}")
        model = SignalModel(self.cfg.model).fit(feats.loc[train_y.index], train_y)
        latest = feats.iloc[[-1]]
        if latest.isna().any(axis=None):
            return float("nan"), 0.0
        proba = pd.Series(model.predict_proba(latest), index=latest.index)
        signal = probability_to_signal(proba, self.cfg.strategy)
        full_signal = pd.Series(0.0, index=history.index)
        full_signal.iloc[-1] = signal.iloc[-1]
        weight = volatility_target_weights(full_signal, history["close"], self.cfg.risk, self.cfg.periods_per_year)
        return float(proba.iloc[-1]), float(weight.iloc[-1])

    def step(self, history: pd.DataFrame) -> dict:
        price = float(history["close"].iloc[-1])
        equity = self.broker.equity({self.symbol: price})
        proba, weight = self.target_weight(history)
        if not self.guard.allow(equity):
            log.warning("触发回撤保护，强制空仓")
            weight = 0.0
        current_w = self.broker.position(self.symbol) * price / equity if equity > 0 else 0.0
        weight = apply_rebalance_band(current_w, weight, self.cfg.risk.rebalance_threshold)
        target_qty = weight * equity / price
        delta = target_qty - self.broker.position(self.symbol)
        fill = None
        if abs(delta) * price >= self.min_trade_value:
            fill = self.broker.submit_market_order(self.symbol, delta, price)
            log.info("成交 %s %.4f @ %.4f", self.symbol, fill.quantity, fill.price)
        return {"date": history.index[-1], "proba": proba, "weight": weight, "equity": equity, "fill": fill}
