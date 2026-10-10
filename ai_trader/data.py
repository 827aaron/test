"""行情数据加载与合成。"""

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


def load_csv(path: str) -> pd.DataFrame:
    """加载 OHLCV CSV 文件。需包含 date 列及 open/high/low/close/volume（不区分大小写）。"""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns:
        raise ValueError("CSV 缺少 date 列")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV 缺少列: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return validate(df[REQUIRED_COLUMNS].astype(float))


def validate(df: pd.DataFrame) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].dropna()
    if (df["close"] <= 0).any():
        raise ValueError("收盘价必须为正数")
    return df


def generate_synthetic(n_bars: int = 1500, seed: int = 0, start: str = "2018-01-01") -> pd.DataFrame:
    """生成带有趋势/震荡状态切换的合成 OHLCV 数据，便于离线演示与测试。"""
    rng = np.random.default_rng(seed)
    drift = np.empty(n_bars)
    regime = 0
    for i in range(n_bars):
        if rng.random() < 0.02:
            regime = rng.integers(0, 3)
        drift[i] = (0.0008, -0.0006, 0.0)[regime]
    # 带少量动量的收益序列，使模型有可学习的结构
    noise = rng.normal(0, 0.012, n_bars)
    rets = np.empty(n_bars)
    prev = 0.0
    for i in range(n_bars):
        prev = drift[i] + 0.1 * prev + noise[i]
        rets[i] = prev
    close = 100 * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, 0.003, n_bars))
    spread = np.abs(rng.normal(0, 0.006, n_bars))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = rng.lognormal(13, 0.4, n_bars)
    index = pd.bdate_range(start=start, periods=n_bars, name="date")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    )
