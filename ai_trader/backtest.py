"""逐根 K 线回测引擎。

时序约定：第 t 根收盘后计算目标仓位并按收盘价调仓，持有到第 t+1 根收盘，
获得 close[t] -> close[t+1] 的收益。调仓成交额按手续费 + 滑点扣除。
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .features import build_features, build_labels
from .metrics import summarize
from .model import walk_forward_predict
from .risk import DrawdownGuard, apply_rebalance_band, volatility_target_weights
from .strategy import probability_to_signal


@dataclass
class BacktestResult:
    equity: pd.Series
    returns: pd.Series
    weights: pd.Series
    proba: pd.Series
    trades: pd.DataFrame
    metrics: dict
    benchmark_metrics: dict


def run_backtest_from_weights(close: pd.Series, target: pd.Series, cfg: Config) -> tuple:
    """给定每根 K 线的目标仓位，模拟资金曲线（含成本与回撤保护）。"""
    asset_ret = close.pct_change().shift(-1).fillna(0.0).values  # t -> t+1 的收益
    target = target.reindex(close.index).fillna(0.0).values
    cost_rate = cfg.costs.commission + cfg.costs.slippage
    guard = DrawdownGuard(cfg.risk)

    n = len(close)
    equity = np.empty(n)
    weights = np.zeros(n)
    port_ret = np.zeros(n)
    trades = []
    eq = cfg.initial_capital
    prev_w = 0.0
    for t in range(n):
        w = apply_rebalance_band(prev_w, target[t], cfg.risk.rebalance_threshold) if guard.allow(eq) else 0.0
        cost = abs(w - prev_w) * cost_rate
        if w != prev_w:
            trades.append({"date": close.index[t], "price": close.iloc[t], "from": prev_w, "to": w, "cost": cost * eq})
        eq *= 1 - cost
        r = w * asset_ret[t]
        eq *= 1 + r
        equity[t] = eq
        weights[t] = w
        port_ret[t] = (1 - cost) * (1 + r) - 1
        prev_w = w
    idx = close.index
    return (
        pd.Series(equity, index=idx, name="equity"),
        pd.Series(port_ret, index=idx, name="returns"),
        pd.Series(weights, index=idx, name="weight"),
        pd.DataFrame(trades, columns=["date", "price", "from", "to", "cost"]),
    )


def run_backtest(df: pd.DataFrame, cfg: Config | None = None) -> BacktestResult:
    cfg = cfg or Config()
    feats = build_features(df)
    labels = build_labels(df)
    proba = walk_forward_predict(feats, labels, cfg.model)

    # 只评估模型开始产生预测之后的区间
    start = proba.first_valid_index()
    if start is None:
        raise ValueError("数据太少，无法完成首次训练；请提供更多 K 线或调小 min_train_size")
    df = df.loc[start:]
    proba = proba.loc[start:]

    signal = probability_to_signal(proba, cfg.strategy)
    target = volatility_target_weights(signal, df["close"], cfg.risk, cfg.periods_per_year)
    equity, returns, weights, trades = run_backtest_from_weights(df["close"], target, cfg)

    bench_ret = df["close"].pct_change().shift(-1).fillna(0.0)
    bench_eq = cfg.initial_capital * (1 + bench_ret).cumprod()
    return BacktestResult(
        equity=equity,
        returns=returns,
        weights=weights,
        proba=proba,
        trades=trades,
        metrics=summarize(equity, returns, weights, cfg.periods_per_year),
        benchmark_metrics=summarize(bench_eq, bench_ret, pd.Series(1.0, index=df.index), cfg.periods_per_year),
    )
