import numpy as np
import pandas as pd
import pytest

from ai_trader.backtest import run_backtest, run_backtest_from_weights
from ai_trader.broker import PaperBroker
from ai_trader.config import Config
from ai_trader.data import generate_synthetic, load_csv
from ai_trader.features import build_features, build_labels
from ai_trader.model import walk_forward_predict
from ai_trader.risk import DrawdownGuard, apply_rebalance_band
from ai_trader.trader import Trader


@pytest.fixture(scope="module")
def df():
    return generate_synthetic(n_bars=500, seed=1)


def small_config():
    cfg = Config()
    cfg.model.min_train_size = 150
    cfg.model.n_estimators = 30
    return cfg


def test_features_have_no_lookahead(df):
    full = build_features(df)
    truncated = build_features(df.iloc[:300])
    pd.testing.assert_frame_equal(full.iloc[:300], truncated)


def test_labels_last_bar_is_nan(df):
    labels = build_labels(df)
    assert np.isnan(labels.iloc[-1])
    assert set(labels.dropna().unique()) <= {0.0, 1.0}


def test_walk_forward_does_not_use_future(df):
    cfg = small_config()
    feats, labels = build_features(df), build_labels(df)
    proba_full = walk_forward_predict(feats, labels, cfg.model)
    # 篡改未来数据后，之前的预测不应改变
    tampered = df.copy()
    tampered.iloc[400:, tampered.columns.get_loc("close")] *= 3
    proba_tampered = walk_forward_predict(build_features(tampered), build_labels(tampered), cfg.model)
    pd.testing.assert_series_equal(proba_full.iloc[:400], proba_tampered.iloc[:400])
    valid = proba_full.dropna()
    assert len(valid) > 0 and valid.between(0, 1).all()


def test_backtest_costs_and_accounting():
    close = pd.Series([100.0, 110.0, 99.0], index=pd.bdate_range("2024-01-01", periods=3))
    cfg = Config()
    cfg.costs.commission, cfg.costs.slippage = 0.001, 0.0
    cfg.risk.rebalance_threshold = 0.0
    equity, returns, weights, trades = run_backtest_from_weights(close, pd.Series(1.0, index=close.index), cfg)
    # 第 0 根建仓付 0.1% 成本，然后持有两段：+10%、-10%
    expected = 100_000 * 0.999 * 1.10 * 0.90
    assert equity.iloc[-1] == pytest.approx(expected)
    assert len(trades) == 1


def test_run_backtest_end_to_end(df):
    result = run_backtest(df, small_config())
    assert result.equity.notna().all()
    assert (result.weights.abs() <= 1.0 + 1e-9).all()
    for key in ("sharpe", "max_drawdown", "cagr"):
        assert key in result.metrics


def test_long_only_by_default(df):
    result = run_backtest(df, small_config())
    assert (result.weights >= 0).all()


def test_drawdown_guard_triggers_and_recovers():
    cfg = Config().risk
    cfg.max_drawdown, cfg.cooldown_bars = 0.1, 2
    guard = DrawdownGuard(cfg)
    assert guard.allow(100)
    assert not guard.allow(85)  # 回撤 15% 触发
    assert not guard.allow(85)
    assert not guard.allow(85)  # 冷却结束，以 85 为新高点
    assert guard.allow(85)


def test_rebalance_band():
    assert apply_rebalance_band(0.5, 0.55, 0.1) == 0.5
    assert apply_rebalance_band(0.5, 0.7, 0.1) == 0.7
    assert apply_rebalance_band(0.0, 0.05, 0.1) == 0.05
    assert apply_rebalance_band(0.5, -0.5, 0.1) == -0.5


def test_paper_broker_accounting():
    broker = PaperBroker(starting_cash=10_000, commission=0.001, slippage=0.0)
    broker.submit_market_order("X", 10, 100.0)
    assert broker.position("X") == 10
    assert broker.cash() == pytest.approx(10_000 - 1000 - 1)
    assert broker.equity({"X": 110.0}) == pytest.approx(10_000 - 1 + 100)


def test_trader_step_respects_leverage(df):
    cfg = small_config()
    broker = PaperBroker(starting_cash=cfg.initial_capital)
    trader = Trader("DEMO", broker, cfg)
    for end in range(len(df) - 3, len(df)):
        r = trader.step(df.iloc[: end + 1])
        assert 0.0 <= r["weight"] <= cfg.risk.max_leverage
    price = df["close"].iloc[-1]
    assert broker.position("DEMO") * price <= broker.equity({"DEMO": price}) * 1.01


def test_load_csv(tmp_path, df):
    path = tmp_path / "data.csv"
    out = df.reset_index().rename(columns={"close": "Close"})
    out.to_csv(path, index=False)
    loaded = load_csv(str(path))
    assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
    assert len(loaded) == len(df)
