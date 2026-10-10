"""EV 计算 + 仓位计算 + 硬风控检查。

盈利和亏损金额全部由程序计算；AI 或规则策略只提供价格和胜率。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from engine.core.safety import SafetyState
from engine.core.ticket import TradeTicket
from engine.core.types import Instrument, Side, Tier
from engine.risk.limits import EffectiveLimits

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class CheckItem:
    name: str
    value: str
    threshold: str
    passed: bool


@dataclass(frozen=True)
class OpenRisk:
    """一笔已有持仓在止损触发时的风险（美元），用来限制同方向总敞口。"""

    symbol: str
    side: Side
    risk_usd: Decimal


@dataclass(frozen=True)
class RiskContext:
    equity: Decimal
    available: Decimal
    instrument: Instrument
    tier: Tier
    last_price: Decimal
    funding_rate: Decimal  # 小数
    open_risks: list[OpenRisk]
    safety: SafetyState
    daily_loss_hit: bool
    max_same_direction_count: int = 3


@dataclass(frozen=True)
class EVResult:
    p_win: Decimal
    win_usd: Decimal
    loss_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    slippage_usd: Decimal
    ev_usd: Decimal
    max_loss_usd: Decimal  # 止损触发后的实际亏损：价差 + 手续费 + 滑点

    @property
    def ev_ratio(self) -> Decimal:
        return self.ev_usd / self.max_loss_usd if self.max_loss_usd > 0 else Decimal(0)


@dataclass(frozen=True)
class RiskDecision:
    verdict: Literal["APPROVE", "REDUCE", "REJECT"]
    qty: Decimal
    leverage: int
    ev: EVResult | None
    checks: list[CheckItem] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict != "REJECT" and self.qty > 0


def loss_per_unit(t: TradeTicket, lim: EffectiveLimits) -> Decimal:
    """每 1 个币在止损触发时的最坏亏损：价差 + 进出手续费（按 taker）+ 出场滑点。"""
    entry = t.worst_entry
    stop = t.stop_loss
    fee = lim.taker_fee_pct / HUNDRED
    slip = lim.slippage_est_pct / HUNDRED
    return abs(entry - stop) + entry * fee + stop * fee + stop * slip


def expected_value(t: TradeTicket, qty: Decimal, p_win: Decimal, funding_rate: Decimal,
                   lim: EffectiveLimits) -> EVResult:
    entry = t.worst_entry
    fee = lim.taker_fee_pct / HUNDRED
    entry_fee = lim.maker_fee_pct / HUNDRED  # 优先挂限价单
    slip = lim.slippage_est_pct / HUNDRED

    total_frac = sum((tp.fraction for tp in t.take_profits), Decimal(0))
    avg_tp = sum((tp.price * tp.fraction for tp in t.take_profits), Decimal(0))
    # 剩余没有指定止盈的部分，按最后一个止盈价估计
    avg_tp = (avg_tp + t.take_profits[-1].price * (1 - total_frac))
    win = abs(avg_tp - entry) * qty
    loss = abs(entry - t.stop_loss) * qty

    notional = entry * qty
    win_fees = notional * entry_fee + avg_tp * qty * fee
    loss_fees = notional * entry_fee + t.stop_loss * qty * fee
    fees = p_win * win_fees + (1 - p_win) * loss_fees
    slippage = t.stop_loss * qty * slip * (1 - p_win) + avg_tp * qty * slip * p_win

    # 资金费：多头在费率为正时付钱；空头在费率为负时付钱。按预计持仓时间估算，只计不利的一侧。
    pays = (funding_rate > 0 and t.side is Side.LONG) or (funding_rate < 0 and t.side is Side.SHORT)
    funding = abs(funding_rate) * notional * (lim.funding_hold_hours / 8) if pays else Decimal(0)

    ev = p_win * win - (1 - p_win) * loss - fees - funding - slippage
    max_loss = loss + notional * fee + t.stop_loss * qty * fee + t.stop_loss * qty * slip
    return EVResult(p_win, win, loss, fees, funding, slippage, ev, max_loss)


class RiskEngine:
    def __init__(self, limits: EffectiveLimits) -> None:
        self.lim = limits

    def size(self, t: TradeTicket, ctx: RiskContext) -> tuple[Decimal, list[str]]:
        lim = self.lim
        notes: list[str] = []
        risk_budget = ctx.equity * lim.risk_per_trade_pct / HUNDRED
        if ctx.tier is Tier.B:
            risk_budget *= lim.tier_b_size_mult
            notes.append(f"B 档币种，仓位 ×{lim.tier_b_size_mult}")

        # 同方向总风险上限
        same_dir = sum((r.risk_usd for r in ctx.open_risks if r.side is t.side), Decimal(0))
        dir_cap = ctx.equity * lim.max_same_direction_risk_pct / HUNDRED - same_dir
        if dir_cap < risk_budget:
            risk_budget = max(dir_cap, Decimal(0))
            notes.append("受同方向总敞口限制，仓位缩小")

        if t.size_hint_pct is not None:  # 建议仓位只能缩小，不能放大
            hint_budget = ctx.equity * min(t.size_hint_pct, lim.risk_per_trade_pct) / HUNDRED
            if hint_budget < risk_budget:
                risk_budget = hint_budget
                notes.append("按建议仓位缩小")

        per_unit = loss_per_unit(t, lim)
        qty = risk_budget / per_unit if per_unit > 0 else Decimal(0)

        # 杠杆上限：名义价值 ≤ 可用资金 × 杠杆
        max_notional = ctx.available * lim.max_leverage
        if qty * t.worst_entry > max_notional:
            qty = max_notional / t.worst_entry
            notes.append(f"受 {lim.max_leverage} 倍杠杆限制，仓位缩小")

        return ctx.instrument.round_qty(qty), notes

    def evaluate(self, t: TradeTicket, ctx: RiskContext, now_ms: int) -> RiskDecision:
        lim = self.lim
        checks: list[CheckItem] = []
        reasons: list[str] = []

        def check(name: str, value: object, threshold: object, ok: bool) -> None:
            checks.append(CheckItem(name, str(value), str(threshold), ok))
            if not ok:
                reasons.append(f"{name} 未通过：{value}（要求 {threshold}）")

        check("系统允许开新单", ctx.safety.level.value, "RUNNING", ctx.safety.can_open_new)
        check("当日亏损未触发停机", "已触发" if ctx.daily_loss_hit else "正常", "正常",
              not ctx.daily_loss_hit)
        check("交易单未过期", now_ms, f"< {t.valid_until_ms}", not t.is_expired(now_ms))
        check("止损已设置", t.stop_loss, "> 0", t.stop_loss > 0)
        check("合约可交易", ctx.instrument.trading, True, ctx.instrument.trading)
        same_count = sum(1 for r in ctx.open_risks if r.side is t.side)
        check("同方向持仓数", same_count, f"< {ctx.max_same_direction_count}",
              same_count < ctx.max_same_direction_count)
        check("同币种无持仓", t.symbol, "无持仓", all(r.symbol != t.symbol for r in ctx.open_risks))
        dev = abs(ctx.last_price - t.mid_entry) / t.mid_entry * HUNDRED
        check("现价偏离入场区间", f"{dev:.3f}%", "≤ 0.5%", dev <= Decimal("0.5"))

        qty, notes = self.size(t, ctx)
        reasons.extend(notes)
        inst = ctx.instrument
        check("数量 ≥ 最小下单量", qty, inst.min_qty, qty >= inst.min_qty)
        check("名义价值 ≥ 最小名义", qty * t.worst_entry, inst.min_notional,
              qty * t.worst_entry >= inst.min_notional)

        p_win = t.p_win if t.p_win is not None else lim.default_p_win
        ev = expected_value(t, qty, p_win, ctx.funding_rate, lim) if qty > 0 else None
        if ev is not None:
            cap = ctx.equity * lim.risk_per_trade_pct / HUNDRED
            check("单笔最大亏损", f"{ev.max_loss_usd:.4f}", f"≤ {cap:.4f}", ev.max_loss_usd <= cap)
            check("EV > 0", f"{ev.ev_usd:.4f}", "> 0", ev.ev_usd > 0)
            check("EV / 最大亏损", f"{ev.ev_ratio:.3f}", f"≥ {lim.ev_min_ratio}",
                  ev.ev_ratio >= lim.ev_min_ratio)
        else:
            check("EV 可计算", "数量为 0", "> 0", False)

        leverage = lim.max_leverage
        if not all(c.passed for c in checks):
            return RiskDecision("REJECT", Decimal(0), leverage, ev, checks, reasons)
        verdict: Literal["APPROVE", "REDUCE"] = "REDUCE" if notes else "APPROVE"
        return RiskDecision(verdict, qty, leverage, ev, checks, reasons)


class DailyLossGuard:
    """当天（UTC）亏损达到上限自动停机。以当天第一次看到的权益为基准。"""

    def __init__(self, halt_pct: Decimal) -> None:
        self.halt_pct = halt_pct
        self.day_start_ms: int | None = None
        self.start_equity: Decimal | None = None
        self.hit = False

    def update(self, equity: Decimal, day_start_ms: int) -> bool:
        if self.day_start_ms != day_start_ms:
            self.day_start_ms = day_start_ms
            self.start_equity = equity
            self.hit = False
        assert self.start_equity is not None
        if not self.hit and self.start_equity > 0:
            loss_pct = (self.start_equity - equity) / self.start_equity * HUNDRED
            if loss_pct >= self.halt_pct:
                self.hit = True
        return self.hit

    def day_pnl_pct(self, equity: Decimal) -> Decimal:
        if not self.start_equity:
            return Decimal(0)
        return (equity - self.start_equity) / self.start_equity * HUNDRED
