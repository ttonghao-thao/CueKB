"""One absolute request deadline, propagated through authoritative and remote I/O.

Adapters must use ``remaining_timeout_ms`` before *each* blocking operation; a
future timeout alone cannot cancel a database statement or network request.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from time import perf_counter
from typing import Iterator
from uuid import UUID, uuid4


class SearchDeadlineExceeded(TimeoutError):
    """The whole request budget is exhausted; never return partial evidence."""

    error_code = "SEARCH_DEADLINE_EXCEEDED"


@dataclass
class SearchExecution:
    deadline: float
    started: float = field(default_factory=perf_counter)
    trace_id: UUID = field(default_factory=uuid4)
    timings_ms: dict[str, float] = field(default_factory=dict)
    backend_calls: dict[str, int] = field(default_factory=dict)
    refill_used: bool = False
    _lock: Lock = field(default_factory=Lock, repr=False)

    @classmethod
    def create(cls, timeout_ms: int, trace_id: UUID | None = None) -> "SearchExecution":
        started = perf_counter()
        return cls(
            started=started,
            deadline=started + timeout_ms / 1000,
            trace_id=trace_id or uuid4(),
        )

    def checkpoint(self) -> None:
        if perf_counter() >= self.deadline:
            raise SearchDeadlineExceeded("search_deadline_exceeded")

    def remaining_ms(self, maximum_ms: int | None = None) -> int:
        self.checkpoint()
        value = max(1, int((self.deadline - perf_counter()) * 1000))
        return min(value, maximum_ms) if maximum_ms is not None else value

    def record_call(self, stage: str) -> None:
        with self._lock:
            self.backend_calls[stage] = self.backend_calls.get(stage, 0) + 1

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        self.checkpoint()
        mark = perf_counter()
        try:
            yield
        finally:
            with self._lock:
                self.timings_ms[name] = (
                    self.timings_ms.get(name, 0.0) + (perf_counter() - mark) * 1000
                )


_CURRENT_EXECUTION: ContextVar[SearchExecution | None] = ContextVar(
    "cuekb_search_execution", default=None
)


def current_execution() -> SearchExecution | None:
    return _CURRENT_EXECUTION.get()


def remaining_timeout_ms(maximum_ms: int) -> int:
    execution = current_execution()
    return execution.remaining_ms(maximum_ms) if execution else maximum_ms


@contextmanager
def activate_execution(execution: SearchExecution) -> Iterator[SearchExecution]:
    token = _CURRENT_EXECUTION.set(execution)
    try:
        execution.checkpoint()
        yield execution
    finally:
        _CURRENT_EXECUTION.reset(token)


class SearchConfigurationChanged(RuntimeError):
    """An immutable online model/index binding changed during this request."""

    error_code = "SEARCH_CONFIGURATION_CHANGED"
