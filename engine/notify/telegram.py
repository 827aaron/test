"""Telegram 推送与命令（/status /pos /pnl /pause /stop）。只响应用户本人的账号。

格式化和命令逻辑是纯函数，方便测试；网络部分用 python-telegram-bot。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from engine.core.bus import Event

log = logging.getLogger(__name__)

IMPORTANT_KINDS = {"position_opened", "position_closed", "alert", "safety", "stop_restored",
                   "position_reduced", "daily_loss"}
BRIEF_KINDS = IMPORTANT_KINDS | {"scan", "ticket_void", "stop_moved", "reconcile"}


def should_send(ev: Event, mode: str) -> bool:
    if mode == "full":
        return True
    if mode == "brief":
        return ev.kind in BRIEF_KINDS and (ev.kind != "reconcile" or ev.important)
    return ev.important or ev.kind in IMPORTANT_KINDS


def format_event(ev: Event) -> str:
    p = ev.payload
    k = ev.kind
    if k == "scan":
        sel = p.get("selected", [])
        picks = "\n".join(f"  • {c['symbol']} {c['side']} 分数 {c['score']}：{c['reason']}" for c in sel) or "  （无）"
        halt = "\n⏸ BTC 波动过大，暂停 15 分钟" if p.get("btc_halt") else ""
        return (f"🔍 扫描完成：{p.get('total')} → 范围 {p.get('after_liquidity')} → 有信号 "
                f"{p.get('with_signals')} → 达标 {p.get('scored')} → 入选 {len(sel)}{halt}\n{picks}")
    if k == "ticket":
        checks = p.get("failed", [])
        tail = ("\n❌ " + "；".join(checks)) if checks else ""
        return (f"📝 交易单 {p.get('symbol')} {p.get('side')} 入场 {p.get('entry_low')}–{p.get('entry_high')} "
                f"止损 {p.get('stop')} 止盈 {p.get('tp')}\n风控：{p.get('verdict')} 数量 {p.get('qty')} "
                f"EV ${p.get('ev')}（比率 {p.get('ev_ratio')}）{tail}")
    if k == "position_opened":
        return (f"✅ 开仓 {p['symbol']} {p['side']} 数量 {p['qty']} 均价 {p['entry']}\n"
                f"止损 {p['stop']}  止盈 {p.get('take_profit')}  耗时 {p.get('latency_ms')}ms")
    if k == "position_closed":
        pnl = p.get("pnl")
        pnl_s = f" 盈亏 ${pnl}（{p.get('r')}R）" if pnl is not None else ""
        return f"🏁 平仓 {p['symbol']}：{p.get('reason')}{pnl_s}"
    if k == "position_reduced":
        return f"✂️ 减仓 {p['symbol']} {p['qty']}，剩余 {p['remaining']}：{p.get('reason')}"
    if k == "stop_moved":
        return f"🛡 {p['symbol']} 止损 {p['old']} → {p['new']}（{p.get('reason')}）"
    if k == "stop_restored":
        return f"🛡 {p['symbol']} 止损单丢失，已补挂在 {p['stop']}"
    if k == "ticket_void":
        return f"🚫 交易单作废：{p.get('reason')}"
    if k == "alert":
        return f"🚨 {p.get('symbol', '')} {p.get('message')}"
    if k == "safety":
        return f"⚠️ 系统状态 {p.get('before')} → {p.get('after')}（{p.get('why')}）"
    if k == "reconcile":
        parts = [f"{name} {', '.join(p[key])}" for key, name in
                 (("adopted", "认领"), ("vanished", "已平"), ("qty_fixed", "数量修正"),
                  ("stops_restored", "补挂止损"), ("force_closed", "强制平仓")) if p.get(key)]
        if p.get("error"):
            parts.append(f"错误 {p['error']}")
        return "🔄 对账：" + ("；".join(parts) if parts else "一致")
    return f"[{k}] {p}"


@dataclass
class StatusProvider:
    """命令需要的数据来源，由引擎注入。"""

    status: Callable[[], Awaitable[str]]
    positions: Callable[[], Awaitable[str]]
    pnl: Callable[[], Awaitable[str]]
    pause: Callable[[], Awaitable[str]]
    stop: Callable[[], Awaitable[str]]


COMMANDS = ("status", "pos", "pnl", "pause", "stop")


async def handle_command(cmd: str, chat_id: int | str, owner_chat_id: str | None,
                         provider: StatusProvider) -> str | None:
    """返回要回复的文字；陌生账号返回 None（完全不回应）。"""
    if owner_chat_id is None or str(chat_id) != str(owner_chat_id):
        log.warning("忽略陌生账号的 TG 命令 chat_id=%s", chat_id)
        return None
    cmd = cmd.lstrip("/").split("@")[0].lower()
    if cmd == "status":
        return await provider.status()
    if cmd == "pos":
        return await provider.positions()
    if cmd == "pnl":
        return await provider.pnl()
    if cmd == "pause":
        return await provider.pause()
    if cmd == "stop":
        return await provider.stop()
    return "可用命令：/status /pos /pnl /pause /stop"


class TelegramNotifier:
    def __init__(self, token: str, owner_chat_id: str, mode: str, provider: StatusProvider) -> None:
        self.token = token
        self.owner = owner_chat_id
        self.mode = mode
        self.provider = provider
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._app: Any = None

    def on_event(self, ev: Event) -> None:
        if should_send(ev, self.mode):
            if self._q.full():
                self._q.get_nowait()
            self._q.put_nowait(format_event(ev))

    async def run(self) -> None:
        from telegram import Update
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

        app = Application.builder().token(self.token).build()
        self._app = app
        pending_stop: set[int] = set()

        async def on_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None or update.message is None or update.message.text is None:
                return
            chat_id = update.effective_chat.id
            cmd = update.message.text.split()[0]
            if cmd.lstrip("/").startswith("stop") and str(chat_id) == str(self.owner):
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                pending_stop.add(chat_id)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("确认停机", callback_data="stop:yes"),
                                            InlineKeyboardButton("取消", callback_data="stop:no")]])
                await update.message.reply_text("确认停机？停机后不再开新单（已有持仓仍受止损保护）。",
                                                reply_markup=kb)
                return
            reply = await handle_command(cmd, chat_id, self.owner, self.provider)
            if reply:
                await update.message.reply_text(reply)

        async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
            q = update.callback_query
            if q is None or update.effective_chat is None:
                return
            chat_id = update.effective_chat.id
            if str(chat_id) != str(self.owner):
                return
            await q.answer()
            if q.data == "stop:yes" and chat_id in pending_stop:
                pending_stop.discard(chat_id)
                await q.edit_message_text(await self.provider.stop())
            else:
                pending_stop.discard(chat_id)
                await q.edit_message_text("已取消")

        for c in COMMANDS:
            app.add_handler(CommandHandler(c, on_cmd))
        app.add_handler(CallbackQueryHandler(on_button))
        await app.initialize()
        await app.start()
        assert app.updater is not None
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            while True:
                text = await self._q.get()
                try:
                    await app.bot.send_message(chat_id=self.owner, text=text[:4000])
                except Exception:
                    log.warning("TG 推送失败", exc_info=True)
                    await asyncio.sleep(2)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
