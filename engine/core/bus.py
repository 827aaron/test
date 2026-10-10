"""进程内事件总线：API 的 WebSocket、TG 推送、存储都从这里订阅。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    kind: str  # 如 "scan", "ticket", "order", "position", "safety", "alert", "message"
    payload: dict[str, Any]
    ts_ms: int
    important: bool = False
    channel: str = "trading"  # trading（#交易大厅）| positions（#持仓动态）| review（#复盘）


@dataclass
class EventBus:
    _subs: list[Callable[[Event], Any]] = field(default_factory=list)
    _queues: list[asyncio.Queue[Event]] = field(default_factory=list)

    def subscribe(self, fn: Callable[[Event], Any]) -> None:
        self._subs.append(fn)

    def queue(self, maxsize: int = 1000) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def unqueue(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, ev: Event) -> None:
        for fn in self._subs:
            try:
                res = fn(ev)
                if asyncio.iscoroutine(res):
                    asyncio.get_running_loop().create_task(res)
            except Exception:  # 订阅者出错不能影响交易主流程
                log.exception("event subscriber failed for %s", ev.kind)
        for q in self._queues:
            if q.full():
                q.get_nowait()  # 丢弃最旧的
            q.put_nowait(ev)
