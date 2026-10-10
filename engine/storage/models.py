"""数据库表（SQLModel）。金额用字符串保存 Decimal，避免浮点误差；以后可以换 PostgreSQL。"""

from __future__ import annotations

from sqlmodel import JSON, Column, Field, SQLModel


class ScanRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts_ms: int = Field(index=True)
    total: int
    after_basic: int
    after_liquidity: int
    with_signals: int
    scored: int
    selected: int
    btc_halt: bool
    duration_ms: int
    rejections: dict = Field(default_factory=dict, sa_column=Column(JSON))


class CandidateRow(SQLModel, table=True):
    __tablename__ = "candidate"
    id: int | None = Field(default=None, primary_key=True)
    scan_run_id: int = Field(index=True)
    ts_ms: int = Field(index=True)
    symbol: str = Field(index=True)
    tier: str
    side: str
    score: float
    selected: bool
    signals: list = Field(default_factory=list, sa_column=Column(JSON))
    reasons: list = Field(default_factory=list, sa_column=Column(JSON))
    price_at: str
    price_1h: str | None = None
    price_4h: str | None = None
    max_fav_4h_pct: float | None = None
    max_adv_4h_pct: float | None = None


class TicketRow(SQLModel, table=True):
    __tablename__ = "ticket"
    id: str = Field(primary_key=True)
    ts_ms: int = Field(index=True)
    source: str
    symbol: str = Field(index=True)
    side: str
    entry_low: str
    entry_high: str
    stop_loss: str
    take_profits: list = Field(default_factory=list, sa_column=Column(JSON))
    valid_until_ms: int
    confidence: str
    p_win: str | None = None
    rationale: str
    candidate_id: int | None = None
    verdict: str | None = None
    qty: str | None = None
    ev_usd: str | None = None
    ev_ratio: str | None = None
    max_loss_usd: str | None = None
    exec_status: str | None = None
    exec_notes: list = Field(default_factory=list, sa_column=Column(JSON))


class RiskCheckRow(SQLModel, table=True):
    __tablename__ = "risk_check"
    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(index=True)
    name: str
    value: str
    threshold: str
    passed: bool


class OrderRow(SQLModel, table=True):
    __tablename__ = "order"
    client_order_id: str = Field(primary_key=True)
    exchange_order_id: str
    ticket_id: str | None = Field(default=None, index=True)
    symbol: str = Field(index=True)
    side: str
    type: str
    status: str
    qty: str
    filled_qty: str
    avg_price: str
    price: str | None = None
    stop_price: str | None = None
    reduce_only: bool = False
    close_position: bool = False
    update_ms: int


class FillRow(SQLModel, table=True):
    __tablename__ = "fill"
    id: int | None = Field(default=None, primary_key=True)
    client_order_id: str = Field(index=True)
    symbol: str
    side: str
    price: str
    qty: str
    fee: str
    realized_pnl: str
    is_maker: bool
    ts_ms: int = Field(index=True)


class PositionRow(SQLModel, table=True):
    __tablename__ = "position"
    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(index=True)
    symbol: str = Field(index=True)
    side: str
    qty: str
    entry_price: str
    initial_stop: str
    opened_ms: int
    closed_ms: int | None = None
    realized_pnl: str | None = None
    fees: str | None = None
    exit_reason: str | None = None
    r_multiple: float | None = None
    adopted: bool = False


class EquitySnapshot(SQLModel, table=True):
    __tablename__ = "equity_snapshot"
    ts_ms: int = Field(primary_key=True)
    equity: str
    wallet: str
    available: str
    unrealized: str


class EventLog(SQLModel, table=True):
    __tablename__ = "event_log"
    id: int | None = Field(default=None, primary_key=True)
    ts_ms: int = Field(index=True)
    kind: str = Field(index=True)
    channel: str
    important: bool
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))


class ConfigChange(SQLModel, table=True):
    __tablename__ = "config_change"
    id: int | None = Field(default=None, primary_key=True)
    ts_ms: int
    key: str
    old: str
    new: str
    direction: str  # tighten | loosen
    confirmed: bool
