"""订单状态跟踪：消费用户数据流，执行器可以等待某个订单成交。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from engine.core.types import ExchangeOrder, UserEvent


@dataclass
class OrderTracker:
    orders: dict[str, ExchangeOrder] = field(default_factory=dict)
    _waiters: dict[str, asyncio.Event] = field(default_factory=dict)
    listeners: list[Callable[[UserEvent], None]] = field(default_factory=list)

    def update(self, o: ExchangeOrder) -> None:
        prev = self.orders.get(o.client_order_id)
        if prev is not None and prev.update_ms > o.update_ms:
            return  # 乱序到达的旧消息
        self.orders[o.client_order_id] = o
        ev = self._waiters.get(o.client_order_id)
        if ev is not None:
            ev.set()

    def on_event(self, ev: UserEvent) -> None:
        if ev.order is not None:
            self.update(ev.order)
        for fn in self.listeners:
            fn(ev)

    async def wait_change(self, client_order_id: str, timeout: float) -> ExchangeOrder | None:
        ev = self._waiters.setdefault(client_order_id, asyncio.Event())
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass
        return self.orders.get(client_order_id)
