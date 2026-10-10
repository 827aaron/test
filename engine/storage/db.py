"""数据库访问与每日备份。"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from engine.core.bus import Event
from engine.core.ticket import TradeTicket
from engine.core.types import ExchangeOrder, UserEvent
from engine.portfolio.state import ManagedPosition
from engine.risk.engine import RiskDecision
from engine.scanner.scanner import ScanResult
from engine.storage import models as m

log = logging.getLogger(__name__)


def _s(x: Decimal | None) -> str | None:
    return None if x is None else format(x, "f")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if path != ":memory:" else None
        url = "sqlite://" if self.path is None else f"sqlite:///{self.path}"
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine)

    # ----- 写入 -----
    def save_scan(self, res: ScanResult, duration_ms: int) -> int:
        f = res.funnel
        with self.session() as s:
            run = m.ScanRun(ts_ms=res.ts_ms, total=f.total, after_basic=f.after_basic,
                            after_liquidity=f.after_liquidity, with_signals=f.with_signals, scored=f.scored,
                            selected=f.selected, btc_halt=f.btc_halt, duration_ms=duration_ms,
                            rejections=f.rejections)
            s.add(run)
            s.commit()
            s.refresh(run)
            assert run.id is not None
            chosen = {c.symbol for c in res.selected}
            for c in res.scored:
                s.add(m.CandidateRow(
                    scan_run_id=run.id, ts_ms=c.ts_ms, symbol=c.symbol, tier=c.tier.value, side=c.side.value,
                    score=c.score, selected=c.symbol in chosen,
                    signals=[{"name": x.name, "tf": x.timeframe, "side": x.side.value, "ratio": round(x.ratio, 3)}
                             for x in c.signals],
                    reasons=c.reasons, price_at=_s(c.price) or "0"))
            s.commit()
            return run.id

    def pending_candidate_followups(self, now_ms: int) -> list[m.CandidateRow]:
        with self.session() as s:
            rows = s.exec(select(m.CandidateRow).where(
                (m.CandidateRow.price_4h.is_(None)) & (m.CandidateRow.ts_ms <= now_ms - 3_600_000))).all()
            return list(rows)

    def update_candidate(self, row: m.CandidateRow) -> None:
        with self.session() as s:
            s.merge(row)
            s.commit()

    def save_ticket(self, t: TradeTicket, d: RiskDecision | None = None, exec_status: str | None = None,
                    notes: list[str] | None = None) -> None:
        with self.session() as s:
            row = m.TicketRow(
                id=t.ticket_id, ts_ms=t.created_ms, source=t.source, symbol=t.symbol, side=t.side.value,
                entry_low=_s(t.entry_low), entry_high=_s(t.entry_high), stop_loss=_s(t.stop_loss),
                take_profits=[{"price": _s(tp.price), "fraction": _s(tp.fraction)} for tp in t.take_profits],
                valid_until_ms=t.valid_until_ms, confidence=_s(t.confidence), p_win=_s(t.p_win),
                rationale=t.rationale, candidate_id=t.candidate_id,
                verdict=d.verdict if d else None, qty=_s(d.qty) if d else None,
                ev_usd=_s(d.ev.ev_usd) if d and d.ev else None,
                ev_ratio=_s(d.ev.ev_ratio) if d and d.ev else None,
                max_loss_usd=_s(d.ev.max_loss_usd) if d and d.ev else None,
                exec_status=exec_status, exec_notes=notes or [])
            s.merge(row)
            if d is not None:
                for c in d.checks:
                    s.add(m.RiskCheckRow(ticket_id=t.ticket_id, name=c.name, value=c.value,
                                         threshold=c.threshold, passed=c.passed))
            s.commit()

    def save_order(self, o: ExchangeOrder, ticket_id: str | None = None) -> None:
        with self.session() as s:
            s.merge(m.OrderRow(
                client_order_id=o.client_order_id, exchange_order_id=o.exchange_order_id, ticket_id=ticket_id,
                symbol=o.symbol, side=o.side.value, type=o.type.value, status=o.status.value, qty=_s(o.qty),
                filled_qty=_s(o.filled_qty), avg_price=_s(o.avg_price), price=_s(o.price),
                stop_price=_s(o.stop_price), reduce_only=o.reduce_only, close_position=o.close_position,
                update_ms=o.update_ms))
            s.commit()

    def save_fill(self, ev: UserEvent) -> None:
        if ev.order is None or ev.last_fill_qty <= 0:
            return
        with self.session() as s:
            s.add(m.FillRow(client_order_id=ev.order.client_order_id, symbol=ev.order.symbol,
                            side=ev.order.side.value, price=_s(ev.last_fill_price), qty=_s(ev.last_fill_qty),
                            fee=_s(ev.fee), realized_pnl=_s(ev.realized_pnl), is_maker=ev.is_maker,
                            ts_ms=ev.ts_ms))
            s.commit()

    def open_position(self, p: ManagedPosition) -> None:
        with self.session() as s:
            s.add(m.PositionRow(ticket_id=p.ticket_id, symbol=p.symbol, side=p.side.value, qty=_s(p.initial_qty),
                                entry_price=_s(p.entry_price), initial_stop=_s(p.initial_stop),
                                opened_ms=p.opened_ms, adopted=p.adopted))
            s.commit()

    def close_position(self, ticket_id: str, closed_ms: int, reason: str) -> m.PositionRow | None:
        """平仓时根据成交记录汇总已实现盈亏和手续费。"""
        with self.session() as s:
            row = s.exec(select(m.PositionRow).where(m.PositionRow.ticket_id == ticket_id,
                                                     m.PositionRow.closed_ms.is_(None))).first()
            if row is None:
                return None
            prefix = f"aq-{ticket_id[-10:]}-"
            fills = s.exec(select(m.FillRow).where(m.FillRow.client_order_id.startswith(prefix))).all()
            pnl = sum((Decimal(f.realized_pnl) for f in fills), Decimal(0))
            fees = sum((Decimal(f.fee) for f in fills), Decimal(0))
            row.closed_ms, row.exit_reason = closed_ms, reason
            row.realized_pnl, row.fees = _s(pnl), _s(fees)
            risk = abs(Decimal(row.entry_price) - Decimal(row.initial_stop)) * Decimal(row.qty)
            row.r_multiple = float((pnl - fees) / risk) if risk > 0 else None
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def save_equity(self, ts_ms: int, equity: Decimal, wallet: Decimal, available: Decimal,
                    unrealized: Decimal) -> None:
        with self.session() as s:
            s.merge(m.EquitySnapshot(ts_ms=ts_ms, equity=_s(equity), wallet=_s(wallet),
                                     available=_s(available), unrealized=_s(unrealized)))
            s.commit()

    def log_event(self, ev: Event) -> None:
        with self.session() as s:
            s.add(m.EventLog(ts_ms=ev.ts_ms, kind=ev.kind, channel=ev.channel, important=ev.important,
                             payload=_jsonable(ev.payload)))
            s.commit()

    # ----- 查询 -----
    def closed_positions(self, since_ms: int = 0) -> list[m.PositionRow]:
        with self.session() as s:
            return list(s.exec(select(m.PositionRow).where(m.PositionRow.closed_ms.is_not(None),
                                                           m.PositionRow.closed_ms >= since_ms)).all())

    def equity_series(self, since_ms: int = 0) -> list[m.EquitySnapshot]:
        with self.session() as s:
            return list(s.exec(select(m.EquitySnapshot).where(m.EquitySnapshot.ts_ms >= since_ms)
                               .order_by(m.EquitySnapshot.ts_ms)).all())

    def last_scan(self) -> m.ScanRun | None:
        with self.session() as s:
            return s.exec(select(m.ScanRun).order_by(m.ScanRun.ts_ms.desc())).first()

    def candidates_of(self, scan_run_id: int) -> list[m.CandidateRow]:
        with self.session() as s:
            return list(s.exec(select(m.CandidateRow).where(m.CandidateRow.scan_run_id == scan_run_id)).all())

    def tickets(self, limit: int = 50) -> list[m.TicketRow]:
        with self.session() as s:
            return list(s.exec(select(m.TicketRow).order_by(m.TicketRow.ts_ms.desc()).limit(limit)).all())

    # ----- 备份 -----
    def backup(self, backup_dir: Path, keep_days: int = 30) -> Path | None:
        if self.path is None:
            return None
        backup_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        dest = backup_dir / f"{day}.db"
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        backups = sorted(backup_dir.glob("*.db"))
        for old in backups[:-keep_days]:
            old.unlink()
        return dest


def _jsonable(x: object) -> object:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set):
        return [_jsonable(v) for v in x]
    if isinstance(x, Decimal):
        return format(x, "f")
    if isinstance(x, str | int | float | bool) or x is None:
        return x
    return str(x)
