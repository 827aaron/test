"""命令行入口。

示例：
    python -m ai_trader backtest --synthetic
    python -m ai_trader backtest --csv data/AAPL.csv --allow-short --out output
    python -m ai_trader paper --synthetic --steps 60
"""

import argparse
import json
import logging
import os

from .backtest import run_backtest
from .broker import PaperBroker
from .config import Config
from .data import generate_synthetic, load_csv
from .trader import Trader


def _load(args):
    if args.csv:
        return load_csv(args.csv)
    return generate_synthetic(n_bars=args.bars, seed=args.seed)


def _config(args) -> Config:
    cfg = Config(initial_capital=args.capital)
    cfg.strategy.allow_short = args.allow_short
    cfg.strategy.long_threshold = args.long_threshold
    cfg.strategy.short_threshold = 1 - args.long_threshold
    cfg.risk.target_volatility = args.target_vol
    cfg.risk.max_leverage = args.max_leverage
    return cfg


def _fmt(metrics: dict) -> str:
    pct = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate", "exposure"}
    return "\n".join(f"  {k:<14}{v:>10.2%}" if k in pct else f"  {k:<14}{v:>10.2f}" for k, v in metrics.items())


def cmd_backtest(args):
    df = _load(args)
    result = run_backtest(df, _config(args))
    print(f"回测区间: {result.equity.index[0].date()} ~ {result.equity.index[-1].date()}，交易 {len(result.trades)} 次")
    print("策略:\n" + _fmt(result.metrics))
    print("买入持有基准:\n" + _fmt(result.benchmark_metrics))
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        result.equity.to_frame().join(result.weights).join(result.proba).to_csv(os.path.join(args.out, "equity.csv"))
        result.trades.to_csv(os.path.join(args.out, "trades.csv"), index=False)
        with open(os.path.join(args.out, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump({"strategy": result.metrics, "benchmark": result.benchmark_metrics}, f, indent=2)
        print(f"结果已保存到 {args.out}/")


def cmd_paper(args):
    """在历史数据的最后 N 根 K 线上逐根回放，模拟实盘交易循环。"""
    df = _load(args)
    cfg = _config(args)
    broker = PaperBroker(starting_cash=cfg.initial_capital, commission=cfg.costs.commission, slippage=cfg.costs.slippage)
    trader = Trader(args.symbol, broker, cfg)
    for end in range(len(df) - args.steps, len(df)):
        r = trader.step(df.iloc[: end + 1])
        action = f"{r['fill'].quantity:+.2f} @ {r['fill'].price:.2f}" if r["fill"] else "-"
        print(f"{r['date'].date()}  p(up)={r['proba']:.3f}  weight={r['weight']:+.2f}  equity={r['equity']:,.2f}  {action}")
    final = broker.equity({args.symbol: float(df["close"].iloc[-1])})
    print(f"最终权益: {final:,.2f}（初始 {cfg.initial_capital:,.2f}）")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ai-trader", description="AI 交易系统")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("backtest", cmd_backtest), ("paper", cmd_paper)):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--csv", help="OHLCV CSV 文件路径")
        src.add_argument("--synthetic", action="store_true", help="使用合成数据")
        p.add_argument("--bars", type=int, default=1500, help="合成数据的 K 线数量")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--capital", type=float, default=100_000.0)
        p.add_argument("--allow-short", action="store_true")
        p.add_argument("--long-threshold", type=float, default=0.55)
        p.add_argument("--target-vol", type=float, default=0.15)
        p.add_argument("--max-leverage", type=float, default=1.0)
        p.set_defaults(func=fn)
        if name == "backtest":
            p.add_argument("--out", help="保存结果的目录")
        else:
            p.add_argument("--symbol", default="DEMO")
            p.add_argument("--steps", type=int, default=30, help="回放的 K 线数量")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
