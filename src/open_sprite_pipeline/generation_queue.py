from __future__ import annotations

from collections import deque
from threading import Condition
from typing import Callable, TypeVar


ResultT = TypeVar("ResultT")


class GenerationQueueFullError(RuntimeError):
    pass


class GpuGenerationQueue:
    """Bounded, in-process FIFO ownership queue for one GPU generation lane."""

    def __init__(self, max_waiting: int = 8) -> None:
        if max_waiting < 1:
            raise ValueError("max_waiting must be at least 1")
        self.max_waiting = max_waiting
        self._condition = Condition()
        self._waiting: deque[int] = deque()
        self._active_ticket: int | None = None
        self._next_ticket = 1

    def snapshot(self) -> dict[str, int | bool | None]:
        with self._condition:
            return {
                "busy": self._active_ticket is not None,
                "active_ticket": self._active_ticket,
                "waiting": len(self._waiting),
                "max_waiting": self.max_waiting,
            }

    def run(self, callback: Callable[[], ResultT]) -> ResultT:
        with self._condition:
            if len(self._waiting) >= self.max_waiting:
                raise GenerationQueueFullError(
                    f"The GPU generation queue already has {self.max_waiting} waiting jobs."
                )
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiting.append(ticket)
            while self._active_ticket is not None or self._waiting[0] != ticket:
                self._condition.wait()
            self._waiting.popleft()
            self._active_ticket = ticket

        try:
            return callback()
        finally:
            with self._condition:
                self._active_ticket = None
                self._condition.notify_all()
