"""机器学习信号模型与滚动（walk-forward）训练。"""

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from .config import ModelConfig


class SignalModel:
    """预测下一根 K 线上涨概率的梯度提升分类器。"""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.clf = None
        self._constant = 0.5

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "SignalModel":
        if y.nunique() < 2:
            self.clf = None
            self._constant = float(y.iloc[0]) if len(y) else 0.5
            return self
        self.clf = HistGradientBoostingClassifier(
            max_iter=self.cfg.n_estimators,
            max_depth=self.cfg.max_depth,
            learning_rate=self.cfg.learning_rate,
            l2_regularization=1.0,
            early_stopping=False,
            random_state=self.cfg.random_state,
        )
        self.clf.fit(X.values, y.values.astype(int))
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self.clf is None:
            return np.full(len(X), self._constant)
        return self.clf.predict_proba(X.values)[:, 1]


def walk_forward_predict(features: pd.DataFrame, labels: pd.Series, cfg: ModelConfig) -> pd.Series:
    """滚动训练并给出样本外概率。

    在第 t 根 K 线收盘时，只有标签索引 <= t-1 的样本是已知的（标签 t-1 依赖第 t 根收盘价），
    因此模型只用这些样本训练，然后对第 t 根的特征做预测，保证没有未来信息泄露。
    """
    valid = features.notna().all(axis=1)
    X = features[valid]
    y = labels.reindex(X.index)
    proba = pd.Series(np.nan, index=features.index, name="proba")

    for pos in range(cfg.min_train_size + 1, len(X), cfg.retrain_every):
        train_y = y.iloc[:pos].dropna()  # 位置 < pos 的标签都已实现
        model = SignalModel(cfg).fit(X.loc[train_y.index], train_y)
        block = X.iloc[pos : pos + cfg.retrain_every]
        proba.loc[block.index] = model.predict_proba(block)
    return proba
