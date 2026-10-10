"""配置加载：config/default.toml ← config/local.toml。

风控相关的值在 risk/limits.py 里再与硬上限比较，这里只负责读取和类型校验。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "default.toml"
LOCAL_PATH = ROOT / "config" / "local.toml"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineCfg(_M):
    mode: Literal["testnet", "live"] = "testnet"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    data_dir: str = "data"


class ExchangeCfg(_M):
    environment: Literal["demo", "testnet", "live"] = "demo"
    recv_window_ms: int = 5000


class SignalsCfg(_M):
    volume_spike_mult: float = 3.0
    breakout_lookback_h: int = 24
    oi_increase_1h_pct: float = 5.0
    funding_abs_pct: float = 0.05
    funding_top_pct: float = 5.0
    liquidation_mult: float = 3.0
    bb_squeeze_lookback_d: int = 7
    bb_expand_mult: float = 1.5
    rel_strength_4h_pct: float = 3.0


class SelectionCfg(_M):
    min_signals: int = 2
    min_score: float = 60
    max_per_round: int = 5
    symbol_cooldown_min: int = 60
    max_meetings_per_hour: int = 4
    max_same_direction: int = 3
    btc_halt_move_1h_pct: float = 3.0
    btc_halt_min: int = 15


class ScannerCfg(_M):
    interval_sec: int = 300
    scope: Literal["all", "top10", "top30", "whitelist"] = "all"
    min_quote_volume_24h: float = 50_000_000
    min_listing_days: int = 30
    max_spread_pct: float = 0.05
    min_depth_usd: float = 200_000
    depth_band_pct: float = 0.5
    tier_a_min_quote_volume: float = 150_000_000
    tier_a_max_spread_pct: float = 0.03
    blacklist: list[str] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h"])
    signals: SignalsCfg = Field(default_factory=SignalsCfg)
    selection: SelectionCfg = Field(default_factory=SelectionCfg)


class RiskCfg(_M):
    risk_per_trade_pct: float = 1.0
    max_leverage: int = 3
    daily_loss_halt_pct: float = 3.0
    max_same_direction_risk_pct: float = 3.0
    tier_b_size_mult: float = 0.5
    ev_min_ratio: float = 0.2
    slippage_est_pct: float = 0.05
    taker_fee_pct: float = 0.05
    maker_fee_pct: float = 0.02
    default_p_win: float = 0.45
    funding_hold_hours: float = 4


class ExecutionCfg(_M):
    ticket_valid_sec: int = 180
    entry_timeout_sec: int = 60
    max_entry_deviation_pct: float = 0.3
    stop_place_retries: int = 2


class PortfolioCfg(_M):
    breakeven_at_r: float = 1.0
    partial_take_at_r: float = 1.5
    partial_take_fraction: float = 0.5
    trailing_atr_mult: float = 2.0
    time_stop_hours: float = 4
    time_stop_min_progress_r: float = 0.3
    adverse_move_r: float = 0.8
    adverse_window_sec: int = 60
    check_interval_sec: float = 1


class ReconcileCfg(_M):
    interval_sec: int = 60


class StrategyCfg(_M):
    atr_stop_mult: float = 1.5
    take_profit_r: float = 2.5
    entry_band_pct: float = 0.1


class TelegramCfg(_M):
    enabled: bool = False
    mode: Literal["brief", "full", "important"] = "important"


class Config(_M):
    engine: EngineCfg = Field(default_factory=EngineCfg)
    exchange: ExchangeCfg = Field(default_factory=ExchangeCfg)
    scanner: ScannerCfg = Field(default_factory=ScannerCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    execution: ExecutionCfg = Field(default_factory=ExecutionCfg)
    portfolio: PortfolioCfg = Field(default_factory=PortfolioCfg)
    reconcile: ReconcileCfg = Field(default_factory=ReconcileCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(default_path: Path = DEFAULT_PATH, local_path: Path = LOCAL_PATH) -> Config:
    data: dict[str, Any] = {}
    if default_path.exists():
        data = tomllib.loads(default_path.read_text(encoding="utf-8"))
    if local_path.exists():
        data = _deep_merge(data, tomllib.loads(local_path.read_text(encoding="utf-8")))
    return Config.model_validate(data)
