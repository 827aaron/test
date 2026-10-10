"""绩效指标。"""

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1).min())


def summarize(equity: pd.Series, returns: pd.Series, weights: pd.Series, periods_per_year: int = 252) -> dict:
    n = len(returns)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    years = n / periods_per_year
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else float("nan")
    vol = returns.std() * np.sqrt(periods_per_year)
    sharpe = returns.mean() / returns.std() * np.sqrt(periods_per_year) if returns.std() > 0 else 0.0
    downside = returns[returns < 0].std()
    sortino = returns.mean() / downside * np.sqrt(periods_per_year) if downside and downside > 0 else 0.0
    mdd = max_drawdown(equity)
    active = returns[weights != 0]  # returns[t] 是持有 weights[t] 获得的收益
    return {
        "total_return": float(total),
        "cagr": float(cagr),
        "volatility": float(vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": mdd,
        "calmar": float(cagr / abs(mdd)) if mdd < 0 else 0.0,
        "win_rate": float((active > 0).mean()) if len(active) else 0.0,
        "exposure": float((weights != 0).mean()),
        "turnover": float(weights.diff().abs().sum() / years) if years > 0 else 0.0,
    }
