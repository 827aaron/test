"""技术指标特征工程。所有特征只使用当根及之前的数据，避免未来函数。"""

import numpy as np
import pandas as pd


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    log_ret = np.log(close).diff()
    feats = pd.DataFrame(index=df.index)
    for n in (1, 5, 10, 20):
        feats[f"ret_{n}"] = np.log(close).diff(n)
    for n in (10, 20, 50):
        feats[f"sma_ratio_{n}"] = close / close.rolling(n).mean() - 1
    feats["vol_10"] = log_ret.rolling(10).std()
    feats["vol_20"] = log_ret.rolling(20).std()
    feats["vol_ratio"] = feats["vol_10"] / feats["vol_20"]
    feats["rsi_14"] = rsi(close) / 100
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    feats["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / close
    feats["hl_range"] = (df["high"] - df["low"]) / close
    feats["volume_z"] = (df["volume"] - df["volume"].rolling(20).mean()) / df["volume"].rolling(20).std()
    return feats.replace([np.inf, -np.inf], np.nan)


def build_labels(df: pd.DataFrame) -> pd.Series:
    """标签：下一根 K 线是否上涨（1/0）。最后一根没有标签，为 NaN。"""
    fwd = df["close"].shift(-1) / df["close"] - 1
    return (fwd > 0).astype(float).where(fwd.notna())
