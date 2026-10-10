"""机会扫描器：范围过滤 → 七信号 → 打分 → 入选规则。全部是程序，阶段 2 再接 Jev 挑币。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from engine.core.clock import DAY_MS, HOUR_MS, MINUTE_MS
from engine.core.config import ScannerCfg
from engine.core.types import Side, Tier
from engine.market.store import MarketDataStore
from engine.scanner.signals import Signal, compute_all

D = Decimal


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    tier: Tier
    quote_volume: Decimal
    spread_pct: Decimal
    depth_usd: Decimal


@dataclass(frozen=True)
class Candidate:
    symbol: str
    tier: Tier
    side: Side
    score: float
    signals: list[Signal]
    reasons: list[str]
    price: Decimal
    ts_ms: int


@dataclass
class Funnel:
    total: int = 0
    after_basic: int = 0  # 合约类型/成交额/上线天数/黑白名单
    after_liquidity: int = 0  # 价差 + 深度
    with_signals: int = 0
    scored: int = 0
    selected: int = 0
    btc_halt: bool = False
    rejections: dict[str, int] = field(default_factory=dict)

    def reject(self, why: str) -> None:
        self.rejections[why] = self.rejections.get(why, 0) + 1


@dataclass(frozen=True)
class ScanResult:
    ts_ms: int
    funnel: Funnel
    universe: list[UniverseEntry]
    scored: list[Candidate]  # 所有达到最少信号数的币，按分数排序
    selected: list[Candidate]


class Scanner:
    def __init__(self, cfg: ScannerCfg, store: MarketDataStore, btc_symbol: str = "BTCUSDT") -> None:
        self.cfg = cfg
        self.store = store
        self.btc = btc_symbol
        self._last_pick_ms: dict[str, int] = {}
        self._meetings: deque[int] = deque()
        self._btc_halt_until = 0

    # ----- 范围 -----
    def basic_filter(self, now_ms: int, funnel: Funnel) -> list[str]:
        cfg = self.cfg
        rows = []
        for sym, inst in self.store.instruments.items():
            funnel.total += 1
            t = self.store.tickers.get(sym)
            if not inst.trading or inst.contract_type != "PERPETUAL" or inst.quote_asset != "USDT":
                funnel.reject("非 U 本位永续或非交易中")
                continue
            if sym in cfg.blacklist:
                funnel.reject("黑名单")
                continue
            if cfg.scope == "whitelist" and sym not in cfg.whitelist:
                funnel.reject("不在白名单")
                continue
            if t is None or float(t.quote_volume) < cfg.min_quote_volume_24h:
                funnel.reject("24h 成交额不足")
                continue
            if inst.onboard_ms and now_ms - inst.onboard_ms < cfg.min_listing_days * DAY_MS:
                funnel.reject("上线天数不足")
                continue
            rows.append((sym, t.quote_volume))
        rows.sort(key=lambda r: r[1], reverse=True)
        if cfg.scope == "top10":
            rows = rows[:10]
        elif cfg.scope == "top30":
            rows = rows[:30]
        funnel.after_basic = len(rows)
        return [r[0] for r in rows]

    def liquidity_filter(self, symbols: list[str], funnel: Funnel) -> list[UniverseEntry]:
        cfg = self.cfg
        out = []
        for sym in symbols:
            book = self.store.books.get(sym)
            if book is None or not book.bids or not book.asks:
                funnel.reject("无盘口数据")
                continue
            spread = book.top.spread_pct
            depth = book.depth_usd(D(str(cfg.depth_band_pct)))
            if spread > D(str(cfg.max_spread_pct)):
                funnel.reject("价差过大")
                continue
            if depth < D(str(cfg.min_depth_usd)):
                funnel.reject("深度不足")
                continue
            qv = self.store.tickers[sym].quote_volume
            tier = (Tier.A if float(qv) >= cfg.tier_a_min_quote_volume
                    and spread <= D(str(cfg.tier_a_max_spread_pct)) else Tier.B)
            out.append(UniverseEntry(sym, tier, qv, spread, depth))
        funnel.after_liquidity = len(out)
        return out

    # ----- BTC 熔断 -----
    def btc_halted(self, now_ms: int) -> bool:
        if now_ms < self._btc_halt_until:
            return True
        ks = self.store.closed_klines(self.btc, "15m")
        if len(ks) >= 4:
            window = ks[-4:]
            hi = max(k.high for k in window)
            lo = min(k.low for k in window)
            move = (hi - lo) / lo * 100
            if move > self.cfg.selection.btc_halt_move_1h_pct:
                self._btc_halt_until = now_ms + self.cfg.selection.btc_halt_min * MINUTE_MS
                return True
        return False

    # ----- 打分 -----
    @staticmethod
    def score(signals: list[Signal]) -> tuple[Side | None, float, list[Signal]]:
        """同一种信号在两个周期上取强的那个；按方向汇总，反向信号扣一半分。"""
        best: dict[tuple[str, Side], Signal] = {}
        for s in signals:
            k = (s.name, s.side)
            if k not in best or s.score > best[k].score:
                best[k] = s
        by_side: dict[Side, list[Signal]] = {Side.LONG: [], Side.SHORT: []}
        for (_, side), s in best.items():
            by_side[side].append(s)
        long_score = sum(s.score for s in by_side[Side.LONG])
        short_score = sum(s.score for s in by_side[Side.SHORT])
        if long_score == short_score:
            return None, 0.0, []
        side = Side.LONG if long_score > short_score else Side.SHORT
        main = by_side[side]
        opp = short_score if side is Side.LONG else long_score
        return side, min(100.0, max(0.0, sum(s.score for s in main) - 0.5 * opp)), main

    # ----- 主流程 -----
    def run_once(self, now_ms: int, open_sides: dict[str, Side] | None = None) -> ScanResult:
        open_sides = open_sides or {}
        sel = self.cfg.selection
        funnel = Funnel()
        symbols = self.basic_filter(now_ms, funnel)
        universe = self.liquidity_filter(symbols, funnel)

        scored: list[Candidate] = []
        for u in universe:
            sigs = compute_all(self.store, u.symbol, self.cfg.signals, self.cfg.timeframes, now_ms, self.btc)
            if sigs:
                funnel.with_signals += 1
            side, score, main = self.score(sigs)
            if side is None or len(main) < sel.min_signals:
                continue
            price = self.store.last_price(u.symbol) or D(0)
            reasons = [s.detail for s in sorted(main, key=lambda s: -s.score)]
            scored.append(Candidate(u.symbol, u.tier, side, round(float(score), 1), main, reasons, price, now_ms))
        scored.sort(key=lambda c: c.score, reverse=True)
        funnel.scored = len(scored)

        if self.btc_halted(now_ms):
            funnel.btc_halt = True
            return ScanResult(now_ms, funnel, universe, scored, [])

        while self._meetings and self._meetings[0] <= now_ms - HOUR_MS:
            self._meetings.popleft()
        selected: list[Candidate] = []
        dir_count = {Side.LONG: 0, Side.SHORT: 0}
        for s in open_sides.values():
            dir_count[s] += 1
        for c in scored:
            if len(selected) >= sel.max_per_round:
                break
            if c.score < sel.min_score:
                funnel.reject("机会分不足")
                continue
            if c.symbol in open_sides:
                funnel.reject("已有持仓")
                continue
            last = self._last_pick_ms.get(c.symbol)
            if last is not None and now_ms - last < sel.symbol_cooldown_min * MINUTE_MS:
                funnel.reject("冷却中")
                continue
            if len(self._meetings) >= sel.max_meetings_per_hour:
                funnel.reject("本小时开会次数已满")
                continue
            if dir_count[c.side] >= sel.max_same_direction:
                funnel.reject("同方向已满")
                continue
            selected.append(c)
            dir_count[c.side] += 1
            self._last_pick_ms[c.symbol] = now_ms
            self._meetings.append(now_ms)
        funnel.selected = len(selected)
        return ScanResult(now_ms, funnel, universe, scored, selected)
