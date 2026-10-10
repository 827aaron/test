from decimal import Decimal

import pytest

from engine.core.config import RiskCfg
from engine.core.safety import PauseReason, SafetyState
from engine.core.ticket import TakeProfit, TradeTicket
from engine.core.types import Side, Tier
from engine.risk.engine import (
    DailyLossGuard,
    OpenRisk,
    RiskContext,
    RiskEngine,
    expected_value,
    loss_per_unit,
)
from engine.risk.limits import HARD, EffectiveLimits, is_tightening

from .conftest import T0, make_inst

D = Decimal


def ticket(side=Side.LONG, entry=(D("99.9"), D("100.1")), stop=D(98), tp=D(104), p_win=None, **kw):
    return TradeTicket(ticket_id="t-abcdef123456", source="rule", symbol="ETHUSDT", side=side,
                       entry_low=entry[0], entry_high=entry[1], stop_loss=stop,
                       take_profits=[TakeProfit(price=tp, fraction=D(1))], created_ms=T0,
                       valid_until_ms=T0 + 180_000, confidence=D("0.6"), p_win=p_win, rationale="test", **kw)


def ctx(equity=D(10000), tier=Tier.A, open_risks=(), safety=None, daily=False, price=D(100), funding=D(0)):
    s = safety or SafetyState(reasons=set())
    return RiskContext(equity, equity, make_inst(), tier, price, funding, list(open_risks), s, daily)


def lim(**over):
    return EffectiveLimits.from_config(RiskCfg(**over))


def test_config_cannot_loosen_hard_limits():
    lm = lim(risk_per_trade_pct=5, max_leverage=20, daily_loss_halt_pct=10, ev_min_ratio=0)
    assert lm.risk_per_trade_pct == HARD.max_risk_per_trade_pct
    assert lm.max_leverage == 3
    assert lm.daily_loss_halt_pct == D("3.0")
    assert lm.ev_min_ratio == D("0.2")


def test_config_can_tighten():
    lm = lim(risk_per_trade_pct=0.5, max_leverage=2)
    assert lm.risk_per_trade_pct == D("0.5") and lm.max_leverage == 2


def test_is_tightening():
    assert is_tightening("risk_per_trade_pct", 1.0, 0.5)
    assert not is_tightening("risk_per_trade_pct", 0.5, 1.0)
    assert is_tightening("ev_min_ratio", 0.2, 0.3)
    assert not is_tightening("ev_min_ratio", 0.3, 0.2)


def test_max_loss_including_fees_and_slippage_within_one_percent():
    lm = lim()
    eng = RiskEngine(lm)
    t = ticket()
    d = eng.evaluate(t, ctx(), T0)
    assert d.approved, d.reasons
    # 止损触发时的真实亏损（最坏入场价、taker 手续费双边、滑点）
    actual = d.qty * loss_per_unit(t, lm)
    assert actual <= D(100)  # 10000 × 1%
    assert actual > D(95)  # 没有过度保守
    assert d.ev.max_loss_usd <= D(100)


def test_tier_b_half_size():
    eng = RiskEngine(lim())
    a = eng.evaluate(ticket(), ctx(tier=Tier.A), T0)
    b = eng.evaluate(ticket(), ctx(tier=Tier.B), T0)
    assert b.qty == pytest.approx(a.qty / 2, rel=0.01)
    assert b.verdict == "REDUCE"


def test_leverage_cap_limits_notional():
    eng = RiskEngine(lim())
    # 止损非常近 → 按风险算的仓位会很大，被 3 倍杠杆截住
    t = ticket(entry=(D("99.99"), D("100")), stop=D("99.98"), tp=D("101"))
    d = eng.evaluate(t, ctx(), T0)
    assert d.qty * t.worst_entry <= D(10000) * 3
    assert any("杠杆" in r for r in d.reasons)


def test_same_direction_exposure_cap():
    eng = RiskEngine(lim())
    risks = [OpenRisk("BTCUSDT", Side.LONG, D(100)), OpenRisk("SOLUSDT", Side.LONG, D(100))]
    d = eng.evaluate(ticket(), ctx(open_risks=risks), T0)
    full = eng.evaluate(ticket(), ctx(), T0)
    assert d.qty == full.qty  # 还剩 1R 空间，正好
    risks.append(OpenRisk("XRPUSDT", Side.SHORT, D(100)))
    risks[1] = OpenRisk("SOLUSDT", Side.LONG, D(150))
    d2 = eng.evaluate(ticket(), ctx(open_risks=risks), T0)
    assert d2.qty < full.qty


def test_same_direction_count_limit():
    eng = RiskEngine(lim())
    risks = [OpenRisk(s, Side.LONG, D(0)) for s in ("A", "B", "C")]
    d = eng.evaluate(ticket(), ctx(open_risks=risks), T0)
    assert d.verdict == "REJECT"


def test_reject_when_paused_or_daily_loss_or_expired():
    eng = RiskEngine(lim())
    s = SafetyState(reasons={PauseReason.USER})
    assert eng.evaluate(ticket(), ctx(safety=s), T0).verdict == "REJECT"
    assert eng.evaluate(ticket(), ctx(daily=True), T0).verdict == "REJECT"
    assert eng.evaluate(ticket(), ctx(), T0 + 180_000).verdict == "REJECT"


def test_reject_negative_ev():
    eng = RiskEngine(lim())
    # 盈亏比 1:1、胜率 45% → EV < 0
    d = eng.evaluate(ticket(tp=D("102.2")), ctx(), T0)
    assert d.verdict == "REJECT"
    assert any("EV" in r for r in d.reasons)


def test_ev_formula_and_funding():
    lm = lim()
    t = ticket(p_win=D("0.5"))
    no_f = expected_value(t, D(10), D("0.5"), D(0), lm)
    with_f = expected_value(t, D(10), D("0.5"), D("0.001"), lm)  # 多头付资金费
    assert with_f.ev_usd < no_f.ev_usd
    short_t = ticket(side=Side.SHORT, entry=(D("99.9"), D("100.1")), stop=D(102), tp=D(96))
    s_f = expected_value(short_t, D(10), D("0.5"), D("0.001"), lm)  # 空头收资金费
    assert s_f.funding_usd == 0
    assert no_f.win_usd == pytest.approx(D("39"), rel=D("0.01"))  # (104-100.1)×10


def test_size_hint_can_only_shrink():
    eng = RiskEngine(lim())
    big = eng.evaluate(ticket(size_hint_pct=D(5)), ctx(), T0)
    base = eng.evaluate(ticket(), ctx(), T0)
    small = eng.evaluate(ticket(size_hint_pct=D("0.5")), ctx(), T0)
    assert big.qty == base.qty
    assert small.qty < base.qty


def test_daily_loss_guard():
    g = DailyLossGuard(D(3))
    assert not g.update(D(1000), 0)
    assert not g.update(D(975), 0)
    assert g.update(D(970), 0)
    assert g.update(D(1000), 0)  # 当天不会自己恢复
    assert not g.update(D(970), 86_400_000)  # 新的一天重置基准
