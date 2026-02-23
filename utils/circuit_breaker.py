"""
Reusable circuit-breaker state machine.

States:
  CLOSED   — normal operation
  OPEN     — halt triggered; no operations allowed
  HALF     — (future) allow one probe request through

Usage:
    breaker = CircuitBreaker(name="kalshi_orders")
    if breaker.is_closed():
        ... place order ...
        breaker.record_success()
    else:
        log.warning("Circuit open: %s", breaker.reason)
"""

from __future__ import annotations
import time
from enum import Enum, auto


class _State(Enum):
    CLOSED = auto()
    OPEN = auto()


class CircuitBreaker:
    __slots__ = ("name", "_state", "_reason", "_opened_at_s", "_failure_count", "_failure_threshold")

    def __init__(self, name: str, failure_threshold: int = 3) -> None:
        self.name = name
        self._state = _State.CLOSED
        self._reason: str | None = None
        self._opened_at_s: float = 0.0
        self._failure_count: int = 0
        self._failure_threshold = failure_threshold

    @property
    def reason(self) -> str | None:
        return self._reason

    def is_closed(self) -> bool:
        return self._state == _State.CLOSED

    def is_open(self) -> bool:
        return self._state == _State.OPEN

    def trip(self, reason: str) -> None:
        self._state = _State.OPEN
        self._reason = reason
        self._opened_at_s = time.monotonic()

    def reset(self) -> None:
        self._state = _State.CLOSED
        self._reason = None
        self._failure_count = 0

    def record_failure(self, reason: str) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self.trip(reason)

    def record_success(self) -> None:
        self._failure_count = 0
