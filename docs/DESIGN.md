# AI 量化交易系统 · 设计文档（DESIGN.md）

> 状态：**初稿，待用户过目**　·　日期：2026-10-10　·　对应需求：《AI 量化交易系统 · 需求与设计（已确认）》
>
> 核心理念：**程序抓数据，AI 做判断，程序做决定和执行。** AI 碰不到下单权限，也改不了风控。

---

## 目录

1. [总体架构](#1-总体架构)
2. [数据流](#2-数据流)
3. [目录结构](#3-目录结构)
4. [模块接口](#4-模块接口)
5. [数据库表](#5-数据库表)
6. [交易单与各角色输出 Schema](#6-交易单与各角色输出-schema)
7. [AI 角色提示词初稿](#7-ai-角色提示词初稿)
8. [Jev 问题集](#8-jev-问题集)
9. [配置项清单](#9-配置项清单)
10. [安全设计](#10-安全设计)
11. [各阶段验收标准](#11-各阶段验收标准)
12. [待核实事项](#12-待核实事项)
13. [默认决定（需要用户确认的事项）](#13-默认决定需要用户确认的事项)

---

## 1. 总体架构

```
┌──────────────────────────── Mac App (Tauri 2 + React + TS) ─────────────────────────────┐
│  总览 · 决策记录 · 机器人 · 连接 · 设置      首次启动向导（5 步）    交易大厅（群聊）  │
└───────────────▲─────────────────────────────────────────────────────────────▲───────────┘
                │ HTTP (REST) + WebSocket，Bearer 访问令牌                    │
                │ 本地：127.0.0.1      远程：Tailscale / SSH 隧道              │
┌───────────────┴───────────────────────── 引擎 Engine (Python 3.12+, uv) ───┴───────────┐
│                                                                                          │
│  api/ (FastAPI + WS)  ◄──── 事件总线 core/bus ────►  notify/ (Telegram 推送 + 命令)      │
│        ▲                         ▲   ▲   ▲                                               │
│        │                         │   │   │                                               │
│  ┌─────┴──────┐  ┌───────────┐  │   │   │   ┌────────────────────────────────────┐      │
│  │ market/    │─►│ scanner/  │──┘   │   └───│ agents/ (阶段 2)                    │      │
│  │ 行情采集    │  │ 机会扫描器 │      │       │  graph/ LangGraph 开会流程          │      │
│  │ WS + REST  │  │ 7 信号打分 │      │       │  roles/ 6 个角色提示词 + schema      │      │
│  └─────▲──────┘  └─────┬─────┘      │       │  llm/   LiteLLM 调用 + 预算 + Jev    │      │
│        │               │ 候选        │       └──────────────┬─────────────────────┘      │
│        │               ▼             │                      │ 交易单草案 (TradeTicket)    │
│        │        ┌──────────────┐     │                      ▼                             │
│        │        │ strategy     │─────┼────────────► ┌──────────────┐                      │
│        │        │ 阶段1:规则策略│     │              │ risk/ 硬风控  │  ← 写死在程序里       │
│        │        │ 阶段2:AI 团队 │     │              │ EV + 限额检查 │     AI 不可改          │
│        │        └──────────────┘     │              └──────┬───────┘                      │
│        │                             │                     │ 已批准订单意图              │
│        │                             │                     ▼                             │
│        │                             │              ┌──────────────┐   唯一能下单的模块   │
│        │                             │              │ execution/   │──────────┐          │
│        │                             │              └──────┬───────┘          │          │
│        │                             │                     ▼                  │          │
│        │               ┌─────────────┴───────┐     ┌──────────────┐           │          │
│        │               │ portfolio/ 持仓盯盘   │◄───│ exchange/     │◄──────────┘          │
│        │               │ 1R 保本/1.5R 减半/   │     │ 适配层        │                     │
│        │               │ 移动止损/时间止损    │     │ Binance(NT)  │                     │
│        │               └─────────────────────┘     │ Mock/回放     │                     │
│        │               ┌─────────────────────┐     └──────┬───────┘                     │
│        │               │ reconcile/ 对账恢复  │◄───────────┤                              │
│        │               └─────────────────────┘            │                              │
│  storage/ SQLModel + SQLite（每日备份）   review/ 复盘（阶段 2）                          │
└──────────────────────────────────────────────────────────────┼──────────────────────────┘
                                                               ▼
                                  币安 U 本位合约（测试网 → 实盘）   Solana（Meme 台，阶段 3）
```

### 1.1 关键设计约束

| 约束 | 实现方式 |
|---|---|
| AI 不能下单 | `agents/` 只产出 `TradeTicket` 草案（纯数据），没有交易所客户端引用；只有 `execution/` 持有 `ExchangeClient` 下单接口。用 import-linter 规则在 CI 里检查依赖方向。 |
| AI 不能改风控 | `risk/limits.py` 中的硬上限是代码常量；配置只能**收紧**（取 `min(配置, 硬上限)`），放宽需要 App 二次确认并写入审计日志。 |
| 异常时默认不做 | 全局 `SafetyState`：`RUNNING / PAUSED_NEW / HALTED`。断网、对账不一致、AI 连续出错 → 自动进入 `PAUSED_NEW`（不开新仓，持仓保护继续）。 |
| 两种部署共用一份代码 | 引擎只依赖配置与密钥提供者接口：Mac 用 `KeyringSecretStore`，服务器用 `EncryptedFileSecretStore`。 |

### 1.2 交易所适配层的取舍

需求要求以 **NautilusTrader (NT)** 为交易核心。NT 是一个完整的事件驱动框架（自带 `TradingNode`、策略、风控引擎、对账），侵入性强。设计如下：

- `exchange/base.py` 定义一个**很薄的 `ExchangeClient` 协议**（下单、撤单、查持仓、查余额、订阅行情、用户数据流）。
- `exchange/nautilus_binance.py`：基于 NT 的 Binance USDT-M 适配器实现（`TradingNode` 内嵌运行，引擎通过 NT 的 Strategy/Actor 回调桥接到我们的事件总线）。
- `exchange/ccxt_binance.py`：CCXT 补充实现，用于 NT 不覆盖的接口（如全市场 24h ticker、资金费率排行、多空比、持仓量历史、Key 权限检查 `/sapi/v1/account/apiRestrictions`）。
- `exchange/mock.py`：内存撮合的模拟交易所 + 录制数据回放，用于云端测试（云端连不上币安）。

这样风控、执行、持仓、对账这些**我们自己的安全逻辑**不依赖 NT 的内部细节，可以完全在 Mock 上测试；NT 负责与币安的可靠连接、订单状态机和断线恢复。

---

## 2. 数据流

### 2.1 阶段 1（规则策略，无 AI）

```
币安 WS (kline 15m/1h, aggTrade, forceOrder, markPrice)  ─┐
币安 REST (exchangeInfo, 24hr ticker, depth, OI, funding) ─┤
                                                          ▼
                               market/  MarketDataStore（内存环形缓冲 + 快照）
                                                          │ 每 5 分钟
                                                          ▼
                       scanner/  范围过滤 → 7 信号计算 → 打分 → 入选规则（冷却/限频/方向上限/BTC 熔断）
                                                          │ Candidate[]（附理由，写库，1h/4h 后回填走势）
                                                          ▼
                       strategy/rule_based  按信号方向 + ATR 生成 TradeTicket（入场区间、止损、止盈、有效期）
                                                          ▼
                       risk/  EV 计算 + 硬风控检查 → RiskDecision（通过 / 缩量 / 否决 + 每项 PASS/FAIL）
                                                          ▼
                       execution/  最新行情二次核对（价格在区间内、未过期）→ 设杠杆/逐仓 → 限价入场
                                    → 成交后立即挂止损（STOP_MARKET, reduceOnly）+ 止盈 → 止损挂不上立即市价平仓
                                                          ▼
                       portfolio/  每秒盯盘：1R 保本、1.5R 平一半、移动止损、时间止损、剧烈反向平仓、补挂止损
                                                          ▼
                       storage/  订单、成交、持仓、权益、事件全部记账        notify/  TG 推送
                       reconcile/  启动时 + 每 60 秒 + 断线重连后：本地状态 vs 交易所状态
```

### 2.2 阶段 2（AI 团队接入后）

`strategy/rule_based` 被替换为 `agents/graph` 的开会流程（规则策略保留为后备/基线）：

```
Candidate ──► [行情分析师 ∥ 合约分析师 ∥ 舆情分析师]（并行，各自超时 25s）
          ──► 主管起草 TradeTicket（可 @ 分析师追问 ≤2 次）
          ──► 程序：EV + 硬风控（与阶段 1 相同的 risk/ 模块）
          ──► 审核官（异厂模型 / 用户 TG 按钮）：只能 否决 / 缩小；可质询 1 次，主管回应 1 次
          ──► execution/
全程总时长 > 90s → 会议作废。每条发言 → 交易大厅（WS + TG）。
```

---

## 3. 目录结构

```
engine/
  core/        事件总线、时钟、SafetyState、配置加载、日志、ID 生成
  exchange/    ExchangeClient 协议；nautilus_binance / ccxt_binance / mock 实现；Key 安全检查
  market/      行情采集（WS+REST）、K 线缓存、指标（NT 指标 + TA-Lib）
  scanner/     范围过滤、7 信号、打分、入选规则、候选追踪考核
  strategy/    阶段 1 规则策略（之后作为基线与后备）
  agents/
    roles/     每个角色一个文件夹：prompt.md + schema.py
      market_analyst/  derivatives_analyst/  sentiment_analyst/
      supervisor/      auditor/              reviewer/
    graph/     LangGraph 开会流程、预案模式
    llm/       LiteLLM 封装、预算控制、Jev 客户端
  risk/        硬上限、EV、风控检查
  execution/   下单执行器（唯一下单入口）
  portfolio/   持仓管理与盯盘规则
  reconcile/   对账与断线恢复
  review/      复盘（阶段 2）
  notify/      Telegram 推送与命令
  api/         FastAPI + WebSocket
  storage/     SQLModel 模型、仓储、备份
  meme/        链上 Meme 交易台（阶段 3）
app/           Tauri 2 + React + TS（阶段 4）
deploy/
  macos/       launchd plist、安装脚本
  docker/      Dockerfile、docker-compose.yml、install.sh（阶段 7）
config/        default.toml（默认值，可提交）；local.toml（用户本地，gitignore）
tests/         单元测试 + 集成测试（Mock 交易所 + 录制数据回放）
docs/          DESIGN.md、SETUP.md 等
```

---

## 4. 模块接口

以下为 Python 接口签名（省略实现）。所有金额用 `Decimal`，时间用 UTC 毫秒或 `datetime(tz=UTC)`。

### 4.1 exchange/

```python
class ExchangeClient(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    # 元数据
    async def load_instruments(self) -> list[Instrument]: ...
    async def tickers_24h(self) -> list[Ticker24h]: ...
    async def order_book(self, symbol: str, limit: int = 100) -> OrderBook: ...
    async def funding_rates(self) -> list[FundingRate]: ...
    async def open_interest(self, symbol: str) -> OpenInterest: ...
    async def klines(self, symbol: str, interval: str, limit: int) -> list[Kline]: ...
    # 账户
    async def balance(self) -> AccountBalance: ...
    async def positions(self) -> list[ExchangePosition]: ...
    async def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    async def set_margin_isolated(self, symbol: str) -> None: ...
    # 交易（只允许 execution/ 调用）
    async def place_order(self, req: OrderRequest) -> ExchangeOrder: ...
    async def cancel_order(self, symbol: str, client_order_id: str) -> None: ...
    # 流
    def subscribe_market(self, symbols: list[str], channels: list[str]) -> AsyncIterator[MarketEvent]: ...
    def subscribe_user(self) -> AsyncIterator[UserEvent]: ...   # 订单/成交/持仓更新
    async def api_key_permissions(self) -> KeyPermissions: ...
```

### 4.2 scanner/

```python
class Scanner:
    def __init__(self, cfg: ScannerConfig, market: MarketDataStore, clock: Clock): ...
    def universe(self) -> list[UniverseEntry]: ...          # 范围过滤 + A/B 分档
    def compute_signals(self, symbol: str) -> list[Signal]: ...
    def run_once(self, now: datetime) -> ScanResult: ...    # 漏斗统计 + 入选 Candidate[]
```

### 4.3 risk/

```python
HARD = HardLimits(                       # 代码常量，任何配置都不能超过
    max_risk_per_trade_pct=Decimal("1.0"),
    max_leverage=3,
    daily_loss_halt_pct=Decimal("3.0"),
    margin_mode="ISOLATED",
    require_stop_loss=True,
)

def expected_value(t: TradeTicket, p_win: Decimal, costs: CostModel) -> EVResult: ...
def size_position(t: TradeTicket, equity: Decimal, limits: EffectiveLimits,
                  inst: Instrument, tier: Tier) -> Sizing: ...
class RiskEngine:
    def evaluate(self, t: TradeTicket, ctx: RiskContext) -> RiskDecision: ...
```

**仓位计算**：`qty = (equity × risk_pct) / (|entry − stop| + entry × (taker_fee×2 + slippage_est))`，再按 B 档 ×0.5、杠杆上限、同方向总敞口上限、交易所最小下单量与步长取整，取最小值。止损触发的实际亏损（含滑点和双边手续费）≤ 资金 × 1%。

**EV**：`EV = p × 盈利 − (1−p) × 亏损 − 手续费 − 资金费 − 滑点`，其中盈利/亏损由程序根据入场、止损、止盈、数量计算。硬门槛 `EV > 0` 且 `EV / 最大亏损 ≥ 0.2`。阶段 1 规则策略的 `p` 来自扫描器历史命中率（冷启动时使用保守默认值 0.45，并按需要的盈亏比反推）。

### 4.4 execution/

```python
class Executor:
    async def execute(self, ticket: TradeTicket, decision: RiskDecision) -> ExecutionReport: ...
    # 步骤：最新价核对 → 过期检查 → 设逐仓 + 杠杆 → 限价入场（GTD/超时撤单）
    #      → 成交即挂 STOP_MARKET(reduceOnly, closePosition) + 止盈 → 挂失败立即市价平仓
    async def close_position(self, symbol: str, reason: str) -> None: ...
    async def reduce_position(self, symbol: str, fraction: Decimal, reason: str) -> None: ...
    async def move_stop(self, symbol: str, new_stop: Decimal, reason: str) -> None:  # 只允许收紧
        ...
```

### 4.5 portfolio/

```python
class PositionManager:
    async def on_tick(self, symbol: str, mark_price: Decimal, now: datetime) -> list[Action]: ...
    # 规则：≥1R → 止损移保本；≥1.5R → 平一半；之后 ATR 移动止损；
    #       超过 max_hold 且 |浮盈| < 0.3R → 时间止损；1 分钟内反向 ≥ 0.8R → 立即平仓；
    #       发现止损单缺失 → 补挂，补不上 → 平仓
```

### 4.6 reconcile/

```python
class Reconciler:
    async def reconcile(self) -> ReconcileReport: ...
    # 比对：本地持仓 vs 交易所持仓；本地挂单 vs 交易所挂单；保护单是否齐全
    # 不一致 → SafetyState=PAUSED_NEW → 以交易所为准修正本地 → 补挂缺失止损 → 推送 TG
```

### 4.7 api/（REST + WS）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 运行状态、SafetyState、测试网/实盘、运行天数 |
| GET | `/api/positions` | 当前持仓 |
| GET | `/api/pnl?range=1d` | 盈亏与资金曲线 |
| GET | `/api/scanner/last` | 最近一次扫描漏斗与候选 |
| GET | `/api/trades` | 交易记录（分页、搜索） |
| POST | `/api/control/{start,pause,stop,flatten}` | 启动 / 暂停开新单 / 停机 / 全部平仓（flatten 需二次确认 token） |
| GET/PUT | `/api/config/risk` | 风控配置（放宽需 `confirm_token`） |
| WS | `/ws/events` | 事件流（交易大厅消息、行情摘要、订单、持仓） |

所有请求需 `Authorization: Bearer <engine_access_token>`；默认只监听 `127.0.0.1`。

### 4.8 notify/（Telegram）

命令：`/status /pos /pnl /pause /stop`；只响应配置的 `owner_chat_id`。推送模式：`brief / full / important`。

---

## 5. 数据库表

SQLite + SQLModel（字段类型兼容 PostgreSQL）。金额以 TEXT(Decimal) 存储。

| 表 | 主要字段 | 说明 |
|---|---|---|
| `scan_run` | id, ts, universe_size, after_filter, signaled, selected, btc_halt, duration_ms | 每轮扫描漏斗 |
| `candidate` | id, scan_run_id, symbol, tier, direction, score, signals(JSON), reasons(JSON), price_at, price_1h, price_4h, max_fav_4h, max_adv_4h | 候选与事后考核 |
| `meeting` | id, candidate_id, started_at, ended_at, status(ok/timeout/aborted), cost_usd | 阶段 2：一次决策会议 |
| `message` | id, meeting_id, channel, role, model, content, tokens_in, tokens_out, cost_usd, ts | 交易大厅消息（全文检索用 FTS5） |
| `ticket` | id, meeting_id, source(rule/ai/plan), symbol, side, entry_low, entry_high, stop, take_profit(JSON), qty, valid_until, preconditions(JSON), confidence, p_win, ev, ev_ratio, rationale, status | 交易单 |
| `risk_check` | id, ticket_id, item, value, threshold, passed | 每项 PASS/FAIL |
| `order` | id, ticket_id, client_order_id, exchange_order_id, symbol, side, type, price, stop_price, qty, reduce_only, status, created_at, updated_at | 订单 |
| `fill` | id, order_id, price, qty, fee, fee_asset, is_maker, ts | 成交 |
| `position` | id, ticket_id, symbol, side, qty, entry_price, stop, initial_risk(1R), opened_at, closed_at, realized_pnl, fees, funding, exit_reason | 持仓生命周期 |
| `equity_snapshot` | ts, equity, available, unrealized, day_pnl | 资金曲线（每分钟） |
| `event_log` | id, ts, level, kind, payload(JSON) | 审计日志（风控变更、SafetyState 变更、对账结果） |
| `config_change` | id, ts, key, old, new, direction(tighten/loosen), confirmed_by | 配置审计 |
| `llm_usage` | id, ts, role, model, purpose, tokens_in, tokens_out, cost_usd | 阶段 2：费用 |
| `jev_judgment` | id, ts, purpose(1–7), question, answer, probability, outcome, correct | 阶段 2：Jev 按用途统计准确率 |
| `prompt_version` | id, role, version, content, created_at, active | 阶段 2：提示词版本 |
| `lesson` | id, ts, source_trade_id, tag, text, weight | 阶段 2：经验库 |
| `review` | id, trade_id, process_score, outcome_score, ev_est, ev_real, notes | 阶段 2：复盘 |

每日 00:05 UTC 用 SQLite online backup API 备份到 `data/backups/YYYY-MM-DD.db`，保留 30 天。

---

## 6. 交易单与各角色输出 Schema

Pydantic v2。AI 输出不符合 schema → 整条作废（不尝试“修补”猜测）。

### 6.1 TradeTicket（交易单）

```python
class TradeTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticket_id: str
    source: Literal["rule", "ai", "plan"]
    symbol: str                                  # 如 "ETHUSDT"
    side: Literal["LONG", "SHORT"]
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal                           # 必填
    take_profits: list[TakeProfit]               # [{price, fraction}]，fraction 之和 ≤ 1
    size_hint_pct: Decimal | None = None         # AI 只能“建议”，最终仓位由程序算
    valid_until: datetime                        # 如创建后 3 分钟
    preconditions: list[str] = []                # 人类可读的前提
    confidence: Decimal = Field(ge=0, le=1)
    p_win: Decimal | None = None                 # 来自 Jev 或校准
    rationale: str = Field(max_length=1200)
    # 校验：LONG 时 stop < entry_low ≤ entry_high < 所有 TP；SHORT 反之；valid_until > now
```

### 6.2 RiskDecision

```python
class RiskDecision(BaseModel):
    verdict: Literal["APPROVE", "REDUCE", "REJECT"]
    qty: Decimal
    leverage: int
    max_loss_usd: Decimal
    ev_usd: Decimal
    ev_ratio: Decimal
    checks: list[CheckItem]     # {name, value, threshold, passed}
    reasons: list[str]
```

### 6.3 分析师报告（三位分析师共用骨架）

```python
class AnalystReport(BaseModel):
    role: Literal["market", "derivatives", "sentiment"]
    symbol: str
    bias: Literal["LONG", "SHORT", "NEUTRAL"]
    strength: int = Field(ge=0, le=100)
    regime: Literal["TREND", "RANGE", "VOLATILE"] | None = None   # 行情分析师必填
    key_levels: list[Decimal] = []                                # 支撑/阻力
    evidence: list[str] = Field(max_length=6)                     # 每条引用具体数据
    risks: list[str] = Field(max_length=4)
    hype_authentic: Literal["REAL", "MANUFACTURED", "UNCLEAR"] | None = None  # 舆情分析师
    summary: str = Field(max_length=300)                          # 交易大厅显示的一句话
```

### 6.4 主管输出

```python
class SupervisorDecision(BaseModel):
    action: Literal["TRADE", "PLAN", "PASS", "ASK"]
    ask: AskAnalyst | None = None          # {to: role, question}，最多 2 次
    ticket: TradeTicketDraft | None = None # 不含 ticket_id/p_win，由程序补
    plan_trigger: PlanTrigger | None = None   # 预案：{condition, price, valid_until}
    position_actions: list[PositionAction] = []  # 只能 CLOSE / REDUCE / TIGHTEN_STOP
    summary: str
```

### 6.5 审核官输出

```python
class AuditVerdict(BaseModel):
    verdict: Literal["APPROVE", "REDUCE", "REJECT", "QUESTION"]
    size_multiplier: Decimal = Field(ge=0, le=1)   # 只能 ≤ 1
    question: str | None = None                     # 质询（仅 1 次）
    risks_found: list[str]
    summary: str
    # 规则：拿不准 → REJECT
```

### 6.6 复盘员输出

```python
class TradeReview(BaseModel):
    trade_id: str
    process_score: int = Field(ge=0, le=10)
    outcome_score: int = Field(ge=0, le=10)
    what_went_right: list[str]
    what_went_wrong: list[str]
    lessons: list[Lesson]            # {tag, text}，写入经验库
    ev_estimated: Decimal
    ev_realized: Decimal
```

---

## 7. AI 角色提示词初稿

> 存放位置：`engine/agents/roles/<role>/prompt.md`。App 中可编辑，保留历史版本，可一键恢复默认。
> 所有角色共同的系统前缀（`_common.md`）：
>
> ```
> 你是一个加密货币合约交易研究团队的成员。你没有下单权限，也不能修改风控。
> 只根据提供给你的数据做判断，不要编造数据；数据不足时明确说“数据不足”。
> 必须严格按照给定的 JSON schema 输出，不要输出任何多余文字。
> 默认立场是“不交易”。只有证据充分时才给出方向。
> 交易周期：15 分钟到 4 小时。不做高频，不追单。
> ```

### 7.1 📈 行情分析师（market_analyst）

```
角色：行情分析师。你负责技术面。
输入：{symbol} 的 15m / 1h / 4h K 线摘要、指标（EMA20/50/200、RSI14、ATR14、布林带宽度、成交量比率）、
     扫描器触发的信号、BTC 同期走势、当前持仓状态。
任务：
1. 判断当前行情状态：TREND（趋势）/ RANGE（震荡）/ VOLATILE（剧烈波动）。
2. 给出方向偏向与强度（0–100），并列出关键支撑阻力位。
3. 每条证据必须引用具体数值（例如“1h 收盘 2,412 突破 24h 高点 2,398，量比 3.4”）。
4. 指出最可能让这笔交易失败的 1–3 个技术风险（例如假突破、上方密集阻力）。
注意：
- 震荡市里的突破信号要格外怀疑。
- 多周期方向冲突时，偏向 NEUTRAL。
输出：AnalystReport（role="market"，regime 必填）。
```

### 7.2 📊 合约分析师（derivatives_analyst）

```
角色：合约分析师。你负责衍生品数据。
输入：资金费率（当前、8h 历史、全市场分位）、持仓量（1h/4h 变化）、多空比（账户、大户）、
     1h 爆仓量（多/空，相对均值倍数）、基差（标记价 vs 指数价）、扫描器信号。
任务：
1. 判断仓位拥挤方向：谁在被挤压？资金费率极端时反向风险有多大？
2. 判断持仓量变化与价格变化的组合含义（价涨量增 = 新多进场；价涨量减 = 空头回补，等等）。
3. 估计资金费对持仓 4 小时成本的影响。
4. 给出方向偏向与强度（0–100）。
注意：
- 资金费率 ≥ 0.05% 时，做多要额外谨慎；大规模爆仓后常有反抽。
- 只说数据支持的结论。
输出：AnalystReport（role="derivatives"）。
```

### 7.3 📰 舆情分析师（sentiment_analyst）

```
角色：舆情分析师。你使用 X（Twitter）搜索获取最近 6 小时与 {symbol}/{base_asset} 相关的讨论和新闻。
任务：
1. 总结主要叙事和突发事件（上所、解锁、黑客、监管、合作、巨鲸异动），给出发生时间和来源。
2. 判断热度是真实的（REAL）、刷出来的（MANUFACTURED），还是无法判断（UNCLEAR）。
   刷量迹象：大量新账号、内容高度雷同、只有喊单没有信息、KOL 集中同时发帖。
3. 判断舆情对价格的方向影响与强度（0–100）。
4. 如果有尚未被价格反映的重大负面消息，必须放在 risks 第一条。
注意：
- 不要把单条推文当作事实；引用至少两个独立来源才算“确认”。
- 搜索不到有用信息时，bias=NEUTRAL，strength ≤ 20。
输出：AnalystReport（role="sentiment"，hype_authentic 必填）。
```

### 7.4 💼 主管（supervisor）

```
角色：主管。你综合三位分析师的报告，决定是否起草交易单。
输入：候选币信息（信号、机会分、A/B 档）、三份 AnalystReport、当前账户与持仓状态、
     当日盈亏、风控剩余额度、经验库中最相关的 5 条经验、Jev 给出的胜率（如有）。
任务：
1. 如果关键信息缺失或分析师结论严重冲突，可以 action="ASK"，@ 某位分析师追问一个具体问题（本次会议最多 2 次）。
2. 决定：TRADE（立即交易）/ PLAN（写预案条件单，等待触发）/ PASS（不做）。
3. 起草交易单：方向、入场价区间（不超过当前价 ±0.3%）、止损（放在技术失效位，不是随意百分比）、
   止盈（至少 1.5R）、有效期（默认 3 分钟，预案最长 4 小时）、前提条件、信心、理由。
4. 仓位不由你决定，程序会按风控计算。你只能给 size_hint_pct 作为建议。
5. 对现有持仓，只能提出降低风险的动作：CLOSE / REDUCE / TIGHTEN_STOP。
原则：
- 至少两位分析师同向、且没有分析师给出强反向证据，才考虑 TRADE。
- 宁可错过，不要做错。PASS 是完全正常的结果。
- 理由要写清楚“如果什么发生，说明我错了”。
输出：SupervisorDecision。
```

每天 00:30 UTC 主管额外产出《当日计划》：市场状态判断、重点关注币、当天方向偏好、需要回避的事件（如 CPI、FOMC、大额解锁）。

### 7.5 ⚖️ 最终审核官（auditor）

```
角色：最终审核官。你来自与主管不同的模型厂商，职责是找出这笔交易的问题。
输入：交易单、三份分析师报告、主管理由、程序计算的 EV 与风控检查结果、当前持仓与当日盈亏。
任务：
1. 列出你发现的所有风险：证据是否真实支持结论？是否在追涨杀跌？止损位置是否合理？
   是否与现有持仓高度相关？是否临近重大事件？
2. 给出裁决：APPROVE / REDUCE（size_multiplier < 1）/ REJECT / QUESTION（向主管质询一个问题，仅 1 次）。
3. 你只能否决或缩小仓位，不能放大，也不能修改价格。
原则：
- 拿不准就 REJECT。
- 不要因为主管很有信心就批准；只看证据。
输出：AuditVerdict。
```

用户当审核官时：TG 推送交易单摘要 + 「批准 / 缩小一半 / 拒绝」按钮，超时（默认 = 交易单有效期）视为拒绝。

### 7.6 📝 复盘员（reviewer）

```
角色：复盘员。每天 23:30 UTC 复盘当天所有已平仓交易和被否决的交易单。
输入：每笔交易的完整会议记录、交易单、风控检查、订单与成交、持仓期间价格路径、
     平仓原因、预估 EV 与实际盈亏；被否决交易单在之后 4 小时的价格走势。
任务：
1. 给每笔交易分别打 流程分（决策过程是否合理、是否遵守规则）和 结果分（0–10）。
   流程好但结果差 = 运气；流程差但结果好 = 危险，必须指出。
2. 对比预估 EV 与实际结果，指出系统性偏差（例如胜率高估）。
3. 提炼 0–3 条可复用的经验，带标签（如 #假突破 #高资金费率），写进经验库。不要写空话。
4. 评估被否决的交易单：否决得对不对？
输出：TradeReview[] + 当日总结。
```

### 7.7 🔍 机会扫描器

扫描器主体是程序（第 5 节需求）；AI 部分只由 Jev 完成（见第 8 节问题集 #1），没有 LLM 提示词。

---

## 8. Jev 问题集

Jev 请求格式（待核实，参考 jev-desk）：`POST https://api.typesafe.ai/v1/systemone`，`Authorization: Bearer <key>`，body `{model, state, questions:{name:{type, instructions, options?}}}`。

| # | 用途 | 类型 | 默认 | 后备方案 |
|---|---|---|---|---|
| 1 | 扫描器挑币 + 胜率 | `choice`（10–20 选 3）+ 每个 `noul`（1h 内顺向移动 ≥1R 的概率） | 开 | 按机会分排序取前 3；胜率用历史校准值 |
| 2 | 行情状态 | `choice`（TREND/RANGE/VOLATILE） | 开 | 程序规则：ADX + 布林带宽度 |
| 3 | 持仓提前平仓 | `noul`（继续持有的期望是否为正） | 开 | 叫主管判断 |
| 4 | 预案触发真假突破 | `noul`（真突破概率），1–2s 超时 | 开 | 超时 → 不下单 |
| 5 | Meme 主裁判 | `score` ×3（市场/链上/社交）+ `choice` 挑币 | 开 | **Meme 台暂停** |
| 6 | 舆情热度真伪 | `choice`（REAL/MANUFACTURED/UNCLEAR） | 关 | 舆情分析师自行判断 |
| 7 | 复盘流程打分 | `score`（0–10） | 关 | 复盘员打分 |

每次判断写入 `jev_judgment`，结果出来后回填 `outcome / correct`，按用途分别统计准确率与校准曲线。

---

## 9. 配置项清单

`config/default.toml`（提交到仓库，无任何密钥）← `config/local.toml`（用户本地，gitignore）← App 修改（写入 local.toml + 审计）。

```toml
[engine]
mode = "testnet"                 # testnet | live（切 live 需二次确认）
listen_host = "127.0.0.1"        # 禁止 0.0.0.0（启动时检查）
listen_port = 8765
data_dir = "data"
timezone_display = "UTC"

[exchange.binance]
environment = "testnet"          # testnet | live
recv_window_ms = 5000

[scanner]
interval_sec = 300
scope = "all"                    # all | top10 | top30 | whitelist
min_quote_volume_24h = 50_000_000
min_listing_days = 30
max_spread_pct = 0.05
min_depth_usd_0_5pct = 200_000
tier_b_volume_below = 150_000_000   # 低于此值归 B 档（默认决定，见 13 节）
blacklist = []
whitelist = []
timeframes = ["15m", "1h"]
[scanner.signals]
volume_spike_mult = 3.0
breakout_lookback_h = 24
oi_increase_1h_pct = 5.0
funding_abs_pct = 0.05
funding_top_pct = 5.0
liquidation_mult = 3.0
bb_squeeze_lookback_d = 7
bb_expand_mult = 1.5
rel_strength_4h_pct = 3.0
[scanner.selection]
min_signals = 2
min_score = 60
max_per_round = 5
symbol_cooldown_min = 60
max_meetings_per_hour = 4
max_same_direction = 3
btc_halt_move_1h_pct = 3.0
btc_halt_min = 15

[risk]                           # 只能比硬上限更严
risk_per_trade_pct = 1.0         # 硬上限 1.0
max_leverage = 3                 # 硬上限 3
daily_loss_halt_pct = 3.0        # 硬上限 3.0
max_same_direction_exposure_pct = 3.0   # 同方向总风险 ≤ 3R
tier_b_size_mult = 0.5
ev_min_ratio = 0.2
slippage_est_pct = 0.05
taker_fee_pct = 0.05
maker_fee_pct = 0.02

[execution]
ticket_valid_sec = 180
entry_order = "limit"            # limit | limit_then_market（默认不追）
entry_timeout_sec = 60
stop_place_retries = 2

[portfolio]
breakeven_at_r = 1.0
partial_take_at_r = 1.5
partial_take_fraction = 0.5
trailing_atr_mult = 2.0
time_stop_hours = 4
time_stop_min_progress_r = 0.3
adverse_move_r_1m = 0.8
check_interval_sec = 1

[reconcile]
interval_sec = 60

[telegram]
enabled = false
mode = "important"               # brief | full | important
# bot_token、owner_chat_id 存钥匙串 / 加密配置，不写这里

[budget]                         # 阶段 2，测试网省钱配置（USD/天）
market_analyst = 0.10
derivatives_analyst = 0.10
sentiment_analyst = 0.20
supervisor = 0.60
auditor = 0.30
reviewer = 0.10
monthly_total = 30.0
```

### 9.1 密钥（不进仓库）

| 名称 | 用途 | Mac | 服务器 |
|---|---|---|---|
| `binance.testnet.api_key / api_secret` | 币安测试网 | 钥匙串（service=`aiquant`） | `secrets.enc`（age/Fernet 加密，主密码来自环境变量或首次输入） |
| `binance.live.api_key / api_secret` | 币安实盘（阶段 6） | 同上 | 同上 |
| `telegram.bot_token / owner_chat_id` | TG | 同上 | 同上 |
| `engine.access_token` | App ↔ 引擎认证 | 首次启动自动生成 | 同上 |
| `llm.<provider>.api_key` | 阶段 2 | 同上 | 同上 |

开发期也支持环境变量（`AIQ_BINANCE_TESTNET_API_KEY` 等）或 `config/secrets.local.toml`（gitignore）。仓库提供 `pre-commit` 的密钥扫描（gitleaks）。

---

## 10. 安全设计

- **币安 Key 检查**（启动时 + 每天）：调用 `GET /sapi/v1/account/apiRestrictions`（实盘）：`enableWithdrawals=true` → **拒绝使用**；`ipRestrict=false` → 警告；`enableFutures=false` → 拒绝。测试网不支持该接口时，降级为提示用户手动确认。
- **引擎 API**：默认只绑定 `127.0.0.1`；配置为非回环地址时拒绝启动，除非地址属于 Tailscale 网段 `100.64.0.0/10`。所有请求校验访问令牌（常量时间比较）。
- **TG**：只响应 `owner_chat_id`；`/stop` 与全部平仓需要按钮二次确认。
- **审计**：风控配置变更、SafetyState 变更、实盘切换都写 `event_log` / `config_change`。

---

## 11. 各阶段验收标准

### 阶段 1 · 地基（无 AI）
1. 云端：全部单元测试 + Mock 交易所集成测试通过（扫描器、风控、执行、持仓规则、对账、断线恢复）。
2. Mac mini 测试网实测：
   - 扫描器每 5 分钟跑一轮，漏斗数字写库并推送 TG；
   - 规则策略在测试网完成 ≥ 5 笔完整交易（入场 → 止损/止盈或盯盘规则平仓），每单都带止损；
   - 人为在网页端删掉止损单 → 系统 ≤ 5 秒补挂；
   - 拔网线 / 杀进程重启 → 对账恢复，持仓仍受保护，期间不开新单；
   - 当天亏损模拟到 3% → 自动停机并推送；
   - TG 命令 `/status /pos /pnl /pause /stop` 可用，陌生账号无响应。
3. 风控测试：任何配置都无法使单笔亏损 > 1%、杠杆 > 3。

### 阶段 2 · AI 团队
- 会议流程端到端 ≤ 90 秒（P95 ≤ 60 秒）；schema 不合格的输出 100% 作废；
- 审核官只能缩小/否决（单元测试覆盖）；预算超限按降级规则执行；
- 预案触发到下单 ≤ 1 秒（不含 Jev）；Jev 7 个用途分别统计准确率；
- 交易大厅消息实时推送到 WS 与 TG，三种同步模式可切换。

### 阶段 3 · Meme 交易台（虚拟）
- 硬过滤（貔貅/跑路检测）对已知样本命中；虚拟成交使用 Jupiter 真实报价，计入滑点、延迟、费用、夹子损耗；撤池按归零；Jev 不可用时自动暂停。

### 阶段 4 · Mac App
- 首次启动向导 5 步，每步“测试连接”可用；5 个页面功能齐全；主界面布局按需求（像素交易室动画留到第二版）。

### 阶段 5 · 验证
- 两个交易台各 ≥ 100 笔；扣除全部成本后净收益 > 0；最大回撤 < 10%；与“持有 BTC”对比报告。

### 阶段 6 · 实盘
- 100 USDT；头几周每笔 TG 审批；切实盘需二次确认；Key 安全检查通过。

### 阶段 7 · 服务器部署
- `curl … | bash` 或 `docker compose up -d` 一条命令安装；API 不暴露公网；App 经 Tailscale/SSH 隧道远程连接；数据一键导出/导入。

---

## 12. 待核实事项

| 事项 | 现状（2026-10-10 初查） | 何时/如何确认 |
|---|---|---|
| NautilusTrader 对币安 U 本位合约 + 测试网的支持 | PyPI 最新 `nautilus-trader 1.231.0`（Python 3.12–3.14）。文档显示 Binance 适配器已为 Rust 实现，支持 `BinanceAccountType.USDT_FUTURES`，环境枚举有 `LIVE / TESTNET / DEMO`。文档不同页面的命名不一致（`USDT_FUTURE` vs `USDT_FUTURES`、`is_testnet` vs `environment`）。 | 阶段 1 安装后按实际版本核对；**云端连不上币安**，需在 Mac mini 实测 |
| **币安条件单迁移到 Algo 接口** | 已确认：2025-12-09 起 STOP_MARKET / TAKE_PROFIT_MARKET 等条件单必须用 `POST /fapi/v1/algoOrder`（`algoType=CONDITIONAL`、`triggerPrice`），旧的 `/fapi/v1/order` 返回 -4120；条件单状态走 `ALGO_UPDATE` 推送。NautilusTrader 1.231 已适配。我们的客户端已按这个格式实现 | Mac mini 上用 Demo Trading 实测 |
| **U 本位“测试网”= Demo Trading** | NautilusTrader 1.231 把 `TESTNET` 的 U 本位合约地址指向 `demo-fapi.binance.com`（Demo Trading），旧的 `testnet.binancefuture.com` 只用于币本位。默认环境设为 `demo`，Key 要在 Demo Trading 里申请 | Mac mini 实测 |
| 同一方向能否挂两个 `closePosition` 止损 | 移动止损时先挂新的再撤旧的；如果交易所不允许同时存在两个，会退回“先撤后挂”，挂失败就恢复旧止损或平仓 | Mac mini 实测 |
| 跳空风险 | “单笔最多亏 1%”按估计滑点（0.05%）计算；止损价被跳空击穿时，实际亏损可能超过 1%（测试里止损后再跌 0.5 的情况亏了 1.34%）。止损用标记价格触发、开了 `priceProtect` | 记录实际滑点，阶段 5 评估 |
| 币安测试网 `apiRestrictions` 接口 | 该接口在 `api.binance.com`，测试网可能不支持 | Mac mini 实测，不支持则降级为手动确认 |
| Jev API | 网上资料指向 `POST https://api.typesafe.ai/v1/systemone`；也有第三方网关（OpenRouter、AIMLAPI、Portkey 的 `/v1/decisions`）。价格约 $0.042/M 输入 token、输出免费（各来源不一）。三种类型 `noul / choice / score`；`score` 的请求格式文档不全。建议锁定版本号（如 `jev-1.13.x`）保证可复现。 | 阶段 2 开工前用用户的 Key 实测；需要 TypeSafe 官方文档 |
| xAI X 搜索 | xAI API 有 X 搜索 / web 搜索的 agent 工具，第三方资料称约 $5 / 千次调用；Grok 型号与价格各来源不一致 | 阶段 2 开工前查 docs.x.ai |
| DeepSeek 型号与价格 | 资料显示当前为 `deepseek-v4-flash` / `deepseek-v4-pro`，Flash 输入约 $0.28/M（缓存未命中）；Pro 价格未确认 | 阶段 2 开工前查 api-docs.deepseek.com |
| Claude 型号 | Haiku 5.5 `claude-haiku-5-5`、Sonnet 5.5 `claude-sonnet-5-5`、Opus 5.5 `claude-opus-5-5`；Sonnet/Opus 5.5 用 `effort` 控制思考深度 | 阶段 2 开工时核对价格 |
| LiteLLM 对上述模型的支持 | `litellm 1.104.2` | 阶段 2 实测 |
| 其他依赖版本 | `ccxt 4.5.85`、`langgraph 1.2.14`、`sqlmodel 0.0.48`、`fastapi 0.143.0`、`python-telegram-bot 22.8`、`ta-lib 0.8.1`、`vectorbt 1.1.2` | 锁定在 `uv.lock` |
| GeckoTerminal / DexScreener / Jupiter / Telegram 连通性 | 云端网络策略下均不可达 | 阶段 3 在 Mac mini 实测；云端用录制数据 |

参考资料：
[jev-desk](https://github.com/lobster-shrimps/jev-desk) ·
[OpenRouter: jev-latest](https://openrouter.ai/~typesafe/jev-latest) ·
[Portkey: TypeSafe](https://portkey.ai/docs/integrations/llms/typesafe) ·
[AIMLAPI: Jev](https://aimlapi.com/models/typesafe-jev-latest) ·
[NautilusTrader Binance 适配器](https://nautilustrader.io/docs/latest/api_reference/adapters/binance) ·
[NautilusTrader 适配器概念](https://nautilustrader.io/docs/latest/concepts/adapters) ·
[xAI 价格（第三方汇总）](https://usagepricing.com/blueprint/xai) ·
[DeepSeek API（第三方汇总）](https://www.morphllm.com/deepseek-api)

---

## 12.5 主界面视觉参考（用户提供的 GROKBOT + JEV 截图，阶段 4 实现）

用户提供了 3 张参考截图（同一界面在不同时刻：23:18 / 00:34 / 01:51 UTC）。从图中读到的布局与元素：

**整体风格**：黑色背景 + 品红/粉紫色（≈ `#E040A0`）粗边框包住每个面板；等宽像素字体；角色名用各自的强调色大写粗体（CHIEF 白、SCAN 蓝、VET 白、SOCIAL 红、SIZE 粉红、FILLS 绿、RISK 橙、CRAWLER 白）。数字右对齐、亮色显示。

**顶栏**：左侧 Logo + 系统名「GROKBOT + JEV」+ 副标题（"JEV PICKS, GROK TRADES"）；右侧「DAY 4 · OCT 5」和大号 UTC 时钟（黑底亮字方框）。→ 我们的顶栏：系统名、测试网/实盘徽章、运行天数、UTC 时钟、启动/暂停/全部平仓。

**角色卡片 2×4**：每张卡 = 头像图标 + 角色名 + 一行小字职责（如 "finding what moves"、"chain risk, is it real"、"never takes a position"）+ 右上角小标签（"ASKS JEV" / "MATH"，表示这个角色用 Jev 还是纯程序）+ 关键数值（Jev 概率 0.71、仓位 $2,217、成交 "out 2.02M −107"、战绩 "1W 1L"）+ 进度条 + 引号里的最新一句发言（如 "9K holders, top ten under 20 percent"）。→ 我们的 8 张卡：扫描器、行情、合约、舆情、主管、审核官、复盘员 + 1 张「执行/风控（程序）」卡；每张卡增加「今日费用」。

**连线动画**：正在交流的卡片之间画折线（青色/粉色节点+线段），从发言卡连到被问的卡或交易室大屏。

**像素交易室（中部大面板）**：等距视角的像素办公室；中央大屏显示当前标的 K 线及一行跑马灯（"BOTS P&L +$151.08 · $HIGGS MC $1.42M · HOLDERS 7987"）；6 张工位，机器人（各自颜色的小圆形/三角形/方块角色）坐在或走动在工位间，头顶对话气泡（白框黑字）；右侧有「HEAD OF THE FLOOR」主管工位、「TOP RUNNER」火箭奖杯、「WHALE RADAR」雷达、「DAY 4 MANDATE」当日计划看板、本地时间小屏；左侧「PAPER HANDS」碎纸机、行情墙；右上一排关注币种的圆形头像。→ 第二版实现动画；第一版先做静态房间 + 气泡。

**资金曲线（BALANCE · DAY 4 · 标的）**：绿色折线 + 底部红绿成交量柱；右侧大号当前余额（$12,312.45）；右上角「1W 1L · HOLD −24.04%」——即战绩和「拿着不动」的对比基准。→ 与需求「和持有 BTC 对比」一致。

**左下 JEV · ANALYSIS**：当前标的的 K 线（含市值、价格、24h 涨跌、成交量、流动性、持币人数行），右上角「105 SCANNED」扫描计数；K 线旁有检查项列表（liquidity depth、holder spread、volume vs mcap、rug signature、fresh wallets、entry window）。→ 我们的「K 线 + 检查清单（PASS/FAIL）」。

**右下 thresholds.py（"EVERY NUMBER THE DESK BELIEVES"）**：分 HARD（纯算术，不花钱）和 SOFT（应用在 Jev 的回答上）两组；每行 = 参数名 + 阈值 + 当前实时值 + ✓/✗（例：`max_top_10  0.60  0.64 ✗`）；背景有雷达/漏斗图和状态气泡（"net out, waiting for a flush"）。→ 我们的「门槛面板」：硬门槛实时值、Jev 概率、扫描漏斗，逐项 ✓/✗。

**卡片角色对照**（参考界面 → 本系统）：CHIEF → 主管；SCAN → 机会扫描器；VET → 链上/合约分析；SOCIAL → 舆情分析师；SIZE → 程序仓位计算；FILLS → 执行；RISK → 风控/持仓管理；CRAWLER → 数据采集（"reads first, then the desk trades"）。

---

## 13. 默认决定（需要用户确认的事项）

按“最合理的默认做法”先推进，以下事项请用户过目：

1. **A/B 分档标准**：需求未定义。默认：满足全部范围条件且 24h 成交额 ≥ 1.5 亿美元、价差 ≤ 0.03% 为 A 档；其余合格的为 B 档。
2. **机会分算法**：7 个信号各有权重（放量 25、突破 30、持仓量 25、资金费率 15、爆仓 15、布林挤压 25、相对强弱 25），按超出阈值的程度在 0.6–1.0 倍之间缩放，两个周期取较大值，总分封顶 100；反向信号扣一半分。效果：3 个刚达标的信号约 50 分，3 个强信号约 80 分，所以 60 分门槛大致等于“3 个以上信号、且至少有一两个比较强”。
2b. **信号方向**：资金费率极端取**反向**（费率很高 = 多头拥挤 → 偏空）；爆仓放大取**顺势**（多头被大量爆 → 偏空）；持仓量增加跟随 1h 价格方向；放量跟随当根 K 线方向；布林带放大跟随收盘价在中轨的哪一侧。
3. **阶段 1 规则策略**：≥2 个同向信号、机会分 ≥ 60 → 入场区间为现价 ±0.1%；止损 = 1.5×ATR(1h)；最终止盈 2.5R；中途 1R 保本、1.5R 平一半、之后移动止损；有效期 3 分钟。目的只是跑通流程，不追求盈利。（止盈原定 2R，但在胜率 0.45 的保守假设下扣完成本后 EV/最大亏损只有约 0.18，过不了 0.2 的门槛，所以改成 2.5R。）
4. **阶段 1 胜率默认值**：扫描器历史命中率数据不足时用 0.45（偏保守）。如此 EV 门槛要求盈亏比 ≥ 约 1.9，规则策略的平均盈亏比要满足这个条件才会下单。
5. **同方向总敞口**：同方向所有持仓的止损风险合计 ≤ 3% 资金（≤ 3R）。
6. **时间止损**：持仓 4 小时且浮盈不到 0.3R 就离场。
7. **“剧烈反向”**：1 分钟内反向移动 ≥ 0.8R 就立即平仓（止损单仍在作为兜底）。
8. **追单**：默认不追，只挂限价单，60 秒没成交就撤单作废。
9. **加密配置文件**：服务器上用 Fernet（`cryptography` 库）+ scrypt 从主密码派生密钥。
10. **NautilusTrader 集成方式（阶段 1 实际做法）**：阶段 1 的币安连接用我们自己写的轻量客户端（`engine/exchange/binance.py`），接口地址、Algo 条件单参数都照 NautilusTrader 1.231 的实现来写；风控、执行、盯盘、对账都是我们自己的代码，可以在模拟交易所上完整测试。没有直接把引擎放进 NT 的 `TradingNode`，原因：① 云端连不上币安，NT 的实盘节点没法在这里测试；② NT 的节点会接管事件循环、订单状态和风控，我们“写死在程序里”的硬风控要再包一层，出问题更难排查。NT 仍在依赖里（`uv sync --extra nautilus`），阶段 5 用它的回测引擎做历史验证。**如果你坚持阶段 1 就要用 NT 实盘节点，告诉我，可以改。**
11. **入场单超时**：币安的 GTD 订单要求有效期大于 10 分钟，交易单有效期只有 3 分钟，所以入场用 GTC 限价单，由程序在 60 秒或交易单过期时主动撤单。
