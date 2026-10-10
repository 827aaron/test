"""硬性风控上限。写死在代码里，配置、AI、提示词都改不了。

配置只能把限额调严：生效值 = min(配置, 硬上限)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from engine.core.config import RiskCfg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HardLimits:
    max_risk_per_trade_pct: Decimal = Decimal("1.0")
    max_leverage: int = 3
    max_daily_loss_pct: Decimal = Decimal("3.0")
    max_same_direction_risk_pct: Decimal = Decimal("3.0")
    max_tier_b_size_mult: Decimal = Decimal("0.5")
    min_ev_ratio: Decimal = Decimal("0.2")
    margin_type: str = "ISOLATED"


HARD = HardLimits()


@dataclass(frozen=True)
class EffectiveLimits:
    risk_per_trade_pct: Decimal
    max_leverage: int
    daily_loss_halt_pct: Decimal
    max_same_direction_risk_pct: Decimal
    tier_b_size_mult: Decimal
    ev_min_ratio: Decimal
    slippage_est_pct: Decimal
    taker_fee_pct: Decimal
    maker_fee_pct: Decimal
    default_p_win: Decimal
    funding_hold_hours: Decimal

    @classmethod
    def from_config(cls, cfg: RiskCfg, hard: HardLimits = HARD) -> EffectiveLimits:
        def tighter_max(name: str, value: Decimal, cap: Decimal) -> Decimal:
            if value > cap:
                log.warning("风控配置 %s=%s 超过硬上限 %s，已按硬上限执行", name, value, cap)
                return cap
            return value

        def tighter_min(name: str, value: Decimal, floor: Decimal) -> Decimal:
            if value < floor:
                log.warning("风控配置 %s=%s 低于硬下限 %s，已按硬下限执行", name, value, floor)
                return floor
            return value

        d = lambda x: Decimal(str(x))  # noqa: E731
        lev = cfg.max_leverage
        if lev > hard.max_leverage:
            log.warning("风控配置 max_leverage=%s 超过硬上限 %s", lev, hard.max_leverage)
            lev = hard.max_leverage
        return cls(
            risk_per_trade_pct=tighter_max("risk_per_trade_pct", d(cfg.risk_per_trade_pct),
                                           hard.max_risk_per_trade_pct),
            max_leverage=max(1, lev),
            daily_loss_halt_pct=tighter_max("daily_loss_halt_pct", d(cfg.daily_loss_halt_pct),
                                            hard.max_daily_loss_pct),
            max_same_direction_risk_pct=tighter_max(
                "max_same_direction_risk_pct", d(cfg.max_same_direction_risk_pct),
                hard.max_same_direction_risk_pct),
            tier_b_size_mult=tighter_max("tier_b_size_mult", d(cfg.tier_b_size_mult),
                                         hard.max_tier_b_size_mult),
            ev_min_ratio=tighter_min("ev_min_ratio", d(cfg.ev_min_ratio), hard.min_ev_ratio),
            # 成本估计只能往保守（更高）方向调
            slippage_est_pct=max(d(cfg.slippage_est_pct), Decimal("0.02")),
            taker_fee_pct=max(d(cfg.taker_fee_pct), Decimal("0.04")),
            maker_fee_pct=max(d(cfg.maker_fee_pct), Decimal("0.0")),
            default_p_win=min(d(cfg.default_p_win), Decimal("0.5")),
            funding_hold_hours=max(d(cfg.funding_hold_hours), Decimal("1")),
        )


def is_tightening(key: str, old: float, new: float) -> bool:
    """判断一次配置修改是收紧还是放宽。放宽需要用户二次确认。"""
    higher_is_looser = {
        "risk_per_trade_pct", "max_leverage", "daily_loss_halt_pct",
        "max_same_direction_risk_pct", "tier_b_size_mult", "default_p_win",
    }
    lower_is_looser = {"ev_min_ratio", "slippage_est_pct", "taker_fee_pct", "maker_fee_pct",
                       "funding_hold_hours"}
    if key in higher_is_looser:
        return new <= old
    if key in lower_is_looser:
        return new >= old
    raise KeyError(f"未知风控配置项：{key}")
