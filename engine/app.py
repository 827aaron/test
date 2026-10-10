"""引擎主程序：把行情、扫描、策略、风控、执行、盯盘、对账、记账、推送串起来。"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

from engine.core.bus import Event, EventBus
from engine.core.clock import DAY_MS, HOUR_MS, Clock, SystemClock, utc_day_start
from engine.core.config import Config
from engine.core.safety import PauseReason, SafetyLevel, SafetyState
from engine.core.ticket import TradeTicket
from engine.core.types import OrderStatus, Tier, UserEvent
from engine.exchange.base import ExchangeClient, ExchangeError
from engine.execution.executor import ExecutionReport, Executor
from engine.execution.tracker import OrderTracker
from engine.market.feed import MarketFeed
from engine.market.store import MarketDataStore
from engine.portfolio.manager import PositionManager
from engine.portfolio.state import ManagedPosition, Portfolio
from engine.reconcile.reconciler import Reconciler
from engine.risk.engine import DailyLossGuard, OpenRisk, RiskContext, RiskEngine
from engine.risk.limits import EffectiveLimits
from engine.scanner.scanner import Candidate, Funnel, Scanner
from engine.storage.db import Database
from engine.strategy.rule_based import build_ticket

log = logging.getLogger(__name__)
D = Decimal


class Engine:
    def __init__(self, cfg: Config, ex: ExchangeClient, db: Database, clock: Clock | None = None,
                 bus: EventBus | None = None) -> None:
        self.cfg = cfg
        self.ex = ex
        self.db = db
        self.clock = clock or SystemClock()
        self.bus = bus or EventBus()
        self.started_ms = self.clock.now_ms()

        self.limits = EffectiveLimits.from_config(cfg.risk)
        self.safety = SafetyState()
        self.safety.on_change(self._on_safety_change)
        self.store = MarketDataStore()
        self.feed = MarketFeed(ex, self.store, self.clock)
        self.scanner = Scanner(cfg.scanner, self.store)
        self.risk = RiskEngine(self.limits)
        self.daily = DailyLossGuard(self.limits.daily_loss_halt_pct)
        self.portfolio = Portfolio()
        self.tracker = OrderTracker()
        self.executor = Executor(ex, self.tracker, self.portfolio, self.safety, self.bus, self.clock,
                                 entry_timeout_sec=cfg.execution.entry_timeout_sec,
                                 stop_retries=cfg.execution.stop_place_retries,
                                 max_entry_deviation_pct=D(str(cfg.execution.max_entry_deviation_pct)))
        self.executor.on_closed = self._on_position_closed  # type: ignore[method-assign]
        self.pm = PositionManager(cfg.portfolio, self.portfolio, self.executor, self.store,
                                  self.limits.taker_fee_pct)
        self.reconciler = Reconciler(ex, self.portfolio, self.executor, self.safety, self.bus, self.clock,
                                     self.limits.risk_per_trade_pct)
        self.bus.subscribe(self._persist_event)
        self._tasks: list[asyncio.Task] = []
        self._consecutive_errors = 0
        self.last_equity: D | None = None

    # ------------------------------------------------------------------ 事件
    def emit(self, kind: str, important: bool = False, channel: str = "trading", **payload: object) -> None:
        self.bus.publish(Event(kind, dict(payload), self.clock.now_ms(), important, channel))

    def _persist_event(self, ev: Event) -> None:
        try:
            self.db.log_event(ev)
        except Exception:
            log.exception("写事件日志失败")

    def _on_safety_change(self, before: SafetyLevel, after: SafetyLevel, why: str) -> None:
        log.warning("系统状态 %s -> %s (%s)", before, after, why)
        self.emit("safety", important=True, before=before.value, after=after.value, why=why,
                  reasons=sorted(r.value for r in self.safety.reasons))

    def _on_position_closed(self, pos: ManagedPosition, reason: str) -> None:
        async def finalize() -> None:
            await asyncio.sleep(1.5)  # 等最后的成交回报到达
            row = self.db.close_position(pos.ticket_id, self.clock.now_ms(), reason)
            pnl = None
            r = None
            if row is not None and row.realized_pnl is not None:
                pnl = f"{D(row.realized_pnl) - D(row.fees or '0'):.2f}"
                r = f"{row.r_multiple:.2f}" if row.r_multiple is not None else None
            self.emit("position_closed", important=True, channel="positions", symbol=pos.symbol,
                      reason=reason, ticket_id=pos.ticket_id, pnl=pnl, r=r)

        try:
            asyncio.get_running_loop().create_task(finalize())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ 启动
    async def start(self) -> bool:
        await self.ex.connect()
        if not await self.check_key():
            return False
        self.tracker.listeners.append(self._on_user_event)
        rep = await self.reconciler.reconcile()
        if rep.ok:
            self.safety.clear(PauseReason.STARTUP)
        await self.update_equity()
        self.emit("started", important=True, mode=self.cfg.engine.mode,
                  environment=self.cfg.exchange.environment, safety=self.safety.level.value)
        return True

    async def check_key(self) -> bool:
        perms = await self.ex.api_key_permissions()
        if perms.can_withdraw:
            self.safety.pause(PauseReason.KEY_UNSAFE)
            self.safety.halt("API Key 开了提现权限")
            self.emit("alert", important=True, message="❌ 这个币安 API Key 开启了提现权限，拒绝使用。"
                      "请到币安 API 管理里关闭“允许提现”，或新建一个只开合约交易的 Key。")
            return False
        if not perms.can_trade_futures:
            self.safety.pause(PauseReason.KEY_UNSAFE)
            self.safety.halt("API Key 没有合约权限")
            self.emit("alert", important=True, message="❌ 这个 API Key 没有开通合约交易权限。")
            return False
        if not perms.checked:
            self.emit("alert", important=False, message="ℹ️ 当前环境无法自动检查 Key 权限，"
                      "请自己确认：没开提现权限、已开合约权限。")
        elif not perms.ip_restricted:
            self.emit("alert", important=True, message="⚠️ 这个 API Key 没有绑定 IP 白名单，建议绑定。")
        return True

    # ------------------------------------------------------------------ 用户数据流
    def _on_user_event(self, ev: UserEvent) -> None:
        o = ev.order
        if o is None:
            return
        ticket_id = None
        if o.client_order_id.startswith("aq-"):
            ticket_id = next((p.ticket_id for p in [*self.portfolio.positions.values(), *self.portfolio.closed]
                              if p.ticket_id.endswith(o.client_order_id.split("-")[1])), None)
        try:
            self.db.save_order(o, ticket_id)
            self.db.save_fill(ev)
        except Exception:
            log.exception("记账失败")
        pos = self.portfolio.positions.get(o.symbol)
        if pos is None:
            return
        if ev.last_fill_qty > 0:
            pos.fees += ev.fee
            pos.realized_pnl += ev.realized_pnl
        # 止损/止盈触发：交易所已经平仓
        if (o.status is OrderStatus.FILLED and (o.close_position or o.reduce_only)
                and o.client_order_id in {pos.stop_order_id, *pos.tp_order_ids}):
            asyncio.get_running_loop().create_task(self._confirm_closed(pos, o.client_order_id))

    async def _confirm_closed(self, pos: ManagedPosition, order_id: str) -> None:
        try:
            still = [p for p in await self.ex.positions() if p.symbol == pos.symbol]
        except ExchangeError:
            return  # 交给下一次对账
        if still:
            pos.qty = still[0].qty
            return
        if self.portfolio.positions.get(pos.symbol) is pos:
            await self.executor.cancel_protection(pos)
            self.portfolio.close(pos.symbol)
            why = "止损触发" if order_id == pos.stop_order_id else "止盈触发"
            self._on_position_closed(pos, why)

    async def user_stream_loop(self) -> None:
        while True:
            try:
                async for ev in self.ex.user_events():
                    self.tracker.on_event(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("用户数据流断开，暂停开新单并对账", exc_info=True)
            self.safety.pause(PauseReason.DISCONNECTED)
            await asyncio.sleep(3)
            await self.reconciler.reconcile()

    # ------------------------------------------------------------------ 扫描 → 交易
    async def scan_once(self) -> list[ExecutionReport]:
        t0 = time.monotonic()
        now = self.clock.now_ms()
        await self.feed.refresh_overview()
        pre = self.scanner.basic_filter(now, Funnel())
        await self.feed.refresh_books(pre)
        uni = self.scanner.liquidity_filter(pre, Funnel())
        detail_syms = sorted({u.symbol for u in uni} | {self.scanner.btc})
        await self.feed.refresh_details(detail_syms, self.cfg.scanner.timeframes)

        res = self.scanner.run_once(now, self.portfolio.open_sides())
        dur = int((time.monotonic() - t0) * 1000)
        try:
            self.db.save_scan(res, dur)
        except Exception:
            log.exception("保存扫描结果失败")
        f = res.funnel
        self.emit("scan", total=f.total, after_basic=f.after_basic, after_liquidity=f.after_liquidity,
                  with_signals=f.with_signals, scored=f.scored, btc_halt=f.btc_halt, duration_ms=dur,
                  selected=[{"symbol": c.symbol, "side": c.side.value, "score": c.score,
                             "tier": c.tier.value, "reason": "；".join(c.reasons[:3])} for c in res.selected])
        reports = []
        for c in res.selected:
            rep = await self.trade_candidate(c)
            if rep is not None:
                reports.append(rep)
        return reports

    async def trade_candidate(self, c: Candidate) -> ExecutionReport | None:
        inst = self.store.instruments.get(c.symbol)
        if inst is None:
            return None
        now = self.clock.now_ms()
        t = build_ticket(c, inst, self.store, self.cfg.strategy, self.cfg.execution, now)
        if t is None:
            return None
        return await self.submit_ticket(t, c.tier)

    async def submit_ticket(self, t: TradeTicket, tier: Tier) -> ExecutionReport:
        inst = self.store.instruments[t.symbol]
        bal = await self.ex.balance()
        fr = self.store.funding.get(t.symbol)
        last = self.store.last_price(t.symbol) or t.mid_entry
        ctx = RiskContext(
            equity=bal.equity, available=bal.available, instrument=inst, tier=tier, last_price=last,
            funding_rate=fr.rate if fr else D(0),
            open_risks=[OpenRisk(p.symbol, p.side, p.risk_usd()) for p in self.portfolio.positions.values()],
            safety=self.safety, daily_loss_hit=self.daily.hit,
            max_same_direction_count=self.cfg.scanner.selection.max_same_direction,
        )
        decision = self.risk.evaluate(t, ctx, self.clock.now_ms())
        failed = [f"{c.name}：{c.value}" for c in decision.checks if not c.passed]
        self.emit("ticket", ticket_id=t.ticket_id, symbol=t.symbol, side=t.side.value,
                  entry_low=str(t.entry_low), entry_high=str(t.entry_high), stop=str(t.stop_loss),
                  tp=str(t.take_profits[-1].price), verdict=decision.verdict, qty=str(decision.qty),
                  ev=f"{decision.ev.ev_usd:.2f}" if decision.ev else None,
                  ev_ratio=f"{decision.ev.ev_ratio:.2f}" if decision.ev else None, failed=failed,
                  checks=[{"name": c.name, "value": c.value, "threshold": c.threshold, "passed": c.passed}
                          for c in decision.checks])
        if not decision.approved:
            rep = ExecutionReport(t.ticket_id, "VOID_REJECTED", notes=decision.reasons)
        else:
            rep = await self.executor.execute(t, decision, inst)
            if rep.status in ("FILLED", "PARTIAL") and t.symbol in self.portfolio.positions:
                self.db.open_position(self.portfolio.positions[t.symbol])
            self._track_errors(rep)
        self.db.save_ticket(t, decision, rep.status, rep.notes)
        return rep

    def _track_errors(self, rep: ExecutionReport) -> None:
        if rep.status == "ERROR":
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self.safety.pause(PauseReason.REPEATED_ERRORS)
                self.emit("alert", important=True, message="连续 3 次下单出错，已暂停开新单，请检查")
        else:
            self._consecutive_errors = 0

    # ------------------------------------------------------------------ 定时任务
    async def update_equity(self) -> None:
        bal = await self.ex.balance()
        now = self.clock.now_ms()
        self.last_equity = bal.equity
        self.db.save_equity(now, bal.equity, bal.wallet, bal.available, bal.unrealized)
        day = utc_day_start(now)
        was_hit = self.daily.hit
        new_day = self.daily.day_start_ms is not None and self.daily.day_start_ms != day
        hit = self.daily.update(bal.equity, day)
        if new_day and PauseReason.DAILY_LOSS in self.safety.reasons:
            self.safety.clear(PauseReason.DAILY_LOSS)
            self.safety.halted = False
            self.emit("alert", important=True, message="新的一天，当日亏损停机已解除")
        if hit and not was_hit:
            self.safety.pause(PauseReason.DAILY_LOSS)
            self.safety.halt("当日亏损达到上限")
            self.emit("daily_loss", important=True,
                      message=f"当天亏损达到 {self.limits.daily_loss_halt_pct}%，自动停机到 UTC 0 点。"
                              "已有持仓继续受止损保护。")

    async def followup_candidates(self) -> None:
        """记录候选币之后 1h / 4h 的走势，用来考核扫描器。"""
        now = self.clock.now_ms()
        for row in self.db.pending_candidate_followups(now):
            px = self.store.last_price(row.symbol)
            if px is None:
                continue
            age = now - row.ts_ms
            if row.price_1h is None and age >= HOUR_MS:
                row.price_1h = format(px, "f")
            if age >= 4 * HOUR_MS:
                row.price_4h = format(px, "f")
                ks = [k for k in self.store.closed_klines(row.symbol, "15m")
                      if row.ts_ms <= k.open_ms < row.ts_ms + 4 * HOUR_MS]
                if ks:
                    base = float(row.price_at)
                    hi = max(k.high for k in ks) / base - 1
                    lo = min(k.low for k in ks) / base - 1
                    fav, adv = (hi, lo) if row.side == "LONG" else (-lo, -hi)
                    row.max_fav_4h_pct, row.max_adv_4h_pct = round(fav * 100, 3), round(adv * 100, 3)
            self.db.update_candidate(row)

    async def refresh_position_marks(self) -> None:
        """没有标记价格 WS 时（或 WS 太久没更新），用 REST 补拉持仓币种的价格。"""
        now = self.clock.now_ms()
        for sym in list(self.portfolio.positions):
            if now - self.store.mark_ts_ms.get(sym, 0) > 3000:
                try:
                    self.store.update_mark(sym, await self.ex.mark_price(sym), now)
                except ExchangeError:
                    log.warning("拉取标记价格失败 %s", sym)

    async def _every(self, seconds: float, fn, name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except ExchangeError as e:
                log.warning("%s 失败：%s", name, e)
            except Exception:
                log.exception("%s 出错", name)
            await asyncio.sleep(seconds)

    async def monitor_once(self) -> None:
        await self.refresh_position_marks()
        await self.pm.tick_all(self.clock.now_ms())

    async def backup_once(self) -> None:
        self.db.backup(Path(self.cfg.engine.data_dir) / "backups")

    def run_background(self) -> None:
        c = self.cfg
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self.user_stream_loop()),
            loop.create_task(self.feed.run_liquidations()),
            loop.create_task(self.feed.run_mark_prices()),
            loop.create_task(self._every(c.portfolio.check_interval_sec, self.monitor_once, "盯盘")),
            loop.create_task(self._every(c.reconcile.interval_sec, self.reconciler.reconcile, "对账")),
            loop.create_task(self._every(60, self.update_equity, "权益")),
            loop.create_task(self._every(300, self.followup_candidates, "候选考核")),
            loop.create_task(self._every(c.scanner.interval_sec, self.scan_once, "扫描")),
            loop.create_task(self._every(DAY_MS / 1000, self.backup_once, "备份")),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.ex.close()

    # ------------------------------------------------------------------ 控制（API / TG）
    def pause_by_user(self) -> None:
        self.safety.pause(PauseReason.USER)

    def resume_by_user(self) -> None:
        self.safety.resume_by_user()

    def halt_by_user(self) -> None:
        self.safety.halt("用户停机")

    async def flatten_all(self) -> None:
        self.safety.pause(PauseReason.USER)
        await self.executor.flatten_all("用户全部平仓")

    # ------------------------------------------------------------------ 文字摘要（TG 命令）
    def uptime_days(self) -> float:
        return (self.clock.now_ms() - self.started_ms) / DAY_MS

    async def text_status(self) -> str:
        reasons = "、".join(sorted(r.value for r in self.safety.reasons)) or "无"
        eq = f"{self.last_equity:.2f}" if self.last_equity is not None else "?"
        return (f"环境：{self.cfg.exchange.environment}（{self.cfg.engine.mode}）\n"
                f"状态：{self.safety.level.value}（原因：{reasons}）\n"
                f"权益：{eq} USDT，持仓 {len(self.portfolio.positions)} 个\n"
                f"运行 {self.uptime_days():.2f} 天")

    async def text_positions(self) -> str:
        if not self.portfolio.positions:
            return "当前没有持仓"
        lines = []
        for p in self.portfolio.positions.values():
            px = self.store.marks.get(p.symbol)
            prog = f"{p.progress_r(px):+.2f}R" if px else "?"
            lines.append(f"{p.symbol} {p.side.value} {p.qty} @ {p.entry_price} 止损 {p.stop} 现价 {px} {prog}")
        return "\n".join(lines)

    async def text_pnl(self) -> str:
        now = self.clock.now_ms()
        rows = self.db.closed_positions(utc_day_start(now))
        net = sum((D(r.realized_pnl or "0") - D(r.fees or "0") for r in rows), D(0))
        wins = sum(1 for r in rows if D(r.realized_pnl or "0") - D(r.fees or "0") > 0)
        eq = self.last_equity or D(0)
        return (f"今日已平仓 {len(rows)} 笔（赢 {wins}），净盈亏 {net:.2f} USDT\n"
                f"今日权益变化 {self.daily.day_pnl_pct(eq):+.2f}%（停机线 -{self.limits.daily_loss_halt_pct}%）")
