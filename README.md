# AI 交易系统 (ai-trader)

一个用 Python 写的、模块化的 AI 量化交易框架：用机器学习模型预测下一根 K 线涨跌，再经过风险管理换算成仓位，可以回测，也可以接券商做模拟盘/实盘。

> ⚠️ 仅供学习研究，不构成投资建议。历史回测表现不代表未来收益，实盘前请充分验证。

## 功能

- **数据**：加载 OHLCV CSV，或生成带趋势/震荡状态切换的合成数据（离线演示用）
- **特征工程**：多周期收益率、均线偏离、波动率、RSI、MACD、振幅、成交量 Z 值
- **AI 模型**：梯度提升分类器（`HistGradientBoostingClassifier`），**滚动训练（walk-forward）**，所有预测都是样本外的
- **策略**：上涨概率超过阈值做多，低于阈值做空（可选）
- **风险管理**：目标波动率仓位、杠杆上限、调仓阈值（降低换手）、最大回撤保护与冷却期
- **回测**：逐根 K 线撮合，计入手续费和滑点，并与买入持有基准对比
- **绩效指标**：总收益、年化收益、波动率、Sharpe、Sortino、最大回撤、Calmar、胜率、持仓占比、换手率
- **交易执行**：`Broker` 抽象接口 + `PaperBroker` 模拟盘；接真实券商时实现 `Broker` 即可

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 用合成数据回测
python -m ai_trader backtest --synthetic

# 用自己的数据回测（CSV 需要 date,open,high,low,close,volume 列）
python -m ai_trader backtest --csv data/your_stock.csv --allow-short --out output

# 在最近 30 根 K 线上逐根回放，模拟实盘交易循环
python -m ai_trader paper --synthetic --steps 30

# 运行测试
pytest
```

常用参数：`--long-threshold`（做多概率阈值，默认 0.55）、`--target-vol`（年化目标波动率，默认 0.15）、`--max-leverage`（最大仓位，默认 1.0）、`--capital`（初始资金）。更多参数在 `ai_trader/config.py`。

## 项目结构

```
ai_trader/
├── config.py     # 所有可调参数（模型、策略、风控、成本）
├── data.py       # 数据加载与合成
├── features.py   # 特征与标签
├── model.py      # ML 模型与滚动训练
├── strategy.py   # 概率 → 方向信号
├── risk.py       # 仓位管理与风控
├── backtest.py   # 回测引擎
├── metrics.py    # 绩效指标
├── broker.py     # 券商接口与模拟盘
├── trader.py     # 实盘/模拟盘交易循环
└── cli.py        # 命令行入口
tests/            # 单元测试（含未来函数检查）
```

## 时序与防止未来函数

- 第 t 根 K 线收盘后，用截至 t 的特征计算目标仓位，按收盘价调仓，持有到 t+1 收盘。
- 标签是"下一根是否上涨"，所以第 t 根时只有 t-1 及之前的标签是已知的，模型只用这些样本训练。
- 测试里有专门的检查：篡改未来数据后，之前的特征和预测必须完全不变。

## 后续可扩展方向

- 接入真实数据源和券商 API（实现 `Broker` 接口）
- 多标的组合与资产配置
- 更多模型（LSTM / Transformer）、超参数搜索、特征重要性分析
- 定时调度、交易日志与监控告警
