from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    n_estimators: int = 100
    max_depth: int = 3
    learning_rate: float = 0.05
    min_train_size: int = 252  # 首次训练所需的最少样本数（约一年交易日）
    retrain_every: int = 21  # 每隔多少根 K 线重新训练（约一个月）
    random_state: int = 42


@dataclass
class StrategyConfig:
    long_threshold: float = 0.55  # 上涨概率高于此值时做多
    short_threshold: float = 0.45  # 上涨概率低于此值时做空
    allow_short: bool = False


@dataclass
class RiskConfig:
    target_volatility: float = 0.15  # 年化目标波动率
    vol_lookback: int = 20
    max_leverage: float = 1.0  # 最大仓位（占权益的比例）
    rebalance_threshold: float = 0.10  # 仓位变化小于此值且方向不变时不调仓，降低换手
    max_drawdown: float = 0.20  # 回撤超过此值时清仓并暂停交易
    cooldown_bars: int = 21  # 触发回撤保护后的暂停期


@dataclass
class CostConfig:
    commission: float = 0.0005  # 按成交额计的手续费率
    slippage: float = 0.0005  # 按成交额计的滑点


@dataclass
class Config:
    initial_capital: float = 100_000.0
    periods_per_year: int = 252
    model: ModelConfig = field(default_factory=ModelConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
