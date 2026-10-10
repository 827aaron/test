"""全局安全状态。出现异常时默认不做：先暂停开新单，持仓保护继续。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SafetyLevel(StrEnum):
    RUNNING = "RUNNING"  # 正常
    PAUSED_NEW = "PAUSED_NEW"  # 暂停开新单，持仓管理照常
    HALTED = "HALTED"  # 停机：不开新单；需要人工恢复


class PauseReason(StrEnum):
    USER = "USER"
    DISCONNECTED = "DISCONNECTED"
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
    DAILY_LOSS = "DAILY_LOSS"
    REPEATED_ERRORS = "REPEATED_ERRORS"
    KEY_UNSAFE = "KEY_UNSAFE"
    STARTUP = "STARTUP"


@dataclass
class SafetyState:
    """多个原因可以同时存在；所有自动原因都解除后才恢复。HALTED 只能人工解除。"""

    reasons: set[PauseReason] = field(default_factory=lambda: {PauseReason.STARTUP})
    halted: bool = False
    _listeners: list = field(default_factory=list, repr=False)

    @property
    def level(self) -> SafetyLevel:
        if self.halted:
            return SafetyLevel.HALTED
        return SafetyLevel.PAUSED_NEW if self.reasons else SafetyLevel.RUNNING

    @property
    def can_open_new(self) -> bool:
        return self.level is SafetyLevel.RUNNING

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _emit(self, before: SafetyLevel, why: str) -> None:
        if self.level != before:
            for fn in self._listeners:
                fn(before, self.level, why)

    def pause(self, reason: PauseReason) -> None:
        before = self.level
        self.reasons.add(reason)
        self._emit(before, f"pause:{reason}")

    def clear(self, reason: PauseReason) -> None:
        before = self.level
        self.reasons.discard(reason)
        self._emit(before, f"clear:{reason}")

    def halt(self, why: str) -> None:
        before = self.level
        self.halted = True
        self._emit(before, f"halt:{why}")

    def resume_by_user(self) -> None:
        """人工恢复：解除 HALTED 和 USER 暂停；当日亏损停机在新的一天之前不能解除。"""
        before = self.level
        if PauseReason.DAILY_LOSS not in self.reasons:
            self.halted = False
        self.reasons.discard(PauseReason.USER)
        self._emit(before, "resume:user")
