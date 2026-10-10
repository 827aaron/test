"""引擎 API（FastAPI + WebSocket）。默认只监听本机；所有请求都要访问令牌。"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
from dataclasses import asdict
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect

from engine.app import Engine
from engine.core.clock import utc_day_start

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def check_listen_host(host: str) -> None:
    """禁止暴露到公网：只允许回环地址或 Tailscale 网段。"""
    if host in ("localhost",):
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as e:
        raise SystemExit(f"listen_host={host} 不是合法 IP") from e
    if ip.is_loopback or ip in TAILSCALE_NET:
        return
    raise SystemExit(f"拒绝启动：listen_host={host} 会把引擎暴露到网络上。"
                     "只允许 127.0.0.1 或 Tailscale 地址（100.64.0.0/10）。远程请用 SSH 隧道或 Tailscale。")


def _dec(x: object) -> object:
    if isinstance(x, Decimal):
        return format(x, "f")
    if isinstance(x, dict):
        return {k: _dec(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_dec(v) for v in x]
    return x


def create_app(engine: Engine, access_token: str) -> FastAPI:
    app = FastAPI(title="AI Quant Engine", version="0.1.0")
    pending_confirm: dict[str, str] = {}

    def auth(authorization: str = Header(default="")) -> None:
        tok = authorization.removeprefix("Bearer ").strip()
        if not tok or not hmac.compare_digest(tok, access_token):
            raise HTTPException(401, "invalid token")

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def status() -> dict:
        return {
            "mode": engine.cfg.engine.mode, "environment": engine.cfg.exchange.environment,
            "safety": engine.safety.level.value, "reasons": sorted(r.value for r in engine.safety.reasons),
            "uptime_days": round(engine.uptime_days(), 3),
            "equity": _dec(engine.last_equity), "positions": len(engine.portfolio.positions),
            "server_time_ms": engine.clock.now_ms(),
        }

    @app.get("/api/positions", dependencies=[Depends(auth)])
    async def positions() -> list[dict]:
        out = []
        for p in engine.portfolio.positions.values():
            px = engine.store.marks.get(p.symbol)
            out.append(_dec({
                "symbol": p.symbol, "side": p.side.value, "qty": p.qty, "entry": p.entry_price,
                "stop": p.stop, "take_profit": p.take_profit, "mark": px,
                "progress_r": round(float(p.progress_r(px)), 3) if px else None,
                "opened_ms": p.opened_ms, "breakeven": p.breakeven_done, "partial": p.partial_done,
            }))
        return out

    @app.get("/api/pnl", dependencies=[Depends(auth)])
    async def pnl(days: int = Query(1, ge=1, le=365)) -> dict:
        since = utc_day_start(engine.clock.now_ms()) - (days - 1) * 86_400_000
        rows = engine.db.closed_positions(since)
        eq = engine.db.equity_series(since)
        return {
            "trades": [r.model_dump() for r in rows],
            "equity": [{"ts_ms": e.ts_ms, "equity": e.equity} for e in eq],
        }

    @app.get("/api/scanner/last", dependencies=[Depends(auth)])
    async def scanner_last() -> dict:
        run = engine.db.last_scan()
        if run is None:
            return {}
        return {"run": run.model_dump(), "candidates": [c.model_dump() for c in engine.db.candidates_of(run.id)]}

    @app.get("/api/trades", dependencies=[Depends(auth)])
    async def trades(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return [t.model_dump() for t in engine.db.tickets(limit)]

    @app.get("/api/config/risk", dependencies=[Depends(auth)])
    async def risk_config() -> dict:
        return _dec(asdict(engine.limits))

    @app.post("/api/control/{action}", dependencies=[Depends(auth)])
    async def control(action: str, confirm: str | None = None) -> dict:
        if action == "pause":
            engine.pause_by_user()
        elif action == "start":
            engine.resume_by_user()
        elif action in ("stop", "flatten"):
            # 二次确认：第一次调用返回 confirm 令牌，第二次带上令牌才执行
            if confirm is None or pending_confirm.get(action) != confirm:
                tok = secrets.token_urlsafe(8)
                pending_confirm[action] = tok
                return {"confirm_required": True, "confirm": tok}
            pending_confirm.pop(action, None)
            if action == "stop":
                engine.halt_by_user()
            else:
                await engine.flatten_all()
        else:
            raise HTTPException(404, "unknown action")
        return {"ok": True, "safety": engine.safety.level.value}

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, token: str = Query("")) -> None:
        if not token or not hmac.compare_digest(token, access_token):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = engine.bus.queue()
        try:
            while True:
                ev = await q.get()
                await ws.send_json({"kind": ev.kind, "ts_ms": ev.ts_ms, "channel": ev.channel,
                                    "important": ev.important, "payload": _dec(ev.payload)})
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            engine.bus.unqueue(q)

    return app
