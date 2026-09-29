from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping


class TimerStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TimerSnapshot:
    timer_id: str
    duration: float
    remaining: float
    status: TimerStatus
    message: str
    callback_info: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timer_id": self.timer_id,
            "duration": round(self.duration, 3),
            "remaining": round(self.remaining, 3),
            "status": self.status.value,
            "message": self.message,
            "callback_info": dict(self.callback_info),
        }


@dataclass
class _Timer:
    timer_id: str
    duration: float
    remaining: float
    status: TimerStatus
    message: str
    callback_info: dict[str, Any]
    deadline: float | None = None
    task: asyncio.Task[None] | None = None
    generation: int = 0


CompletionCallback = Callable[[TimerSnapshot], Awaitable[None] | None]


class TimerManager:
    """Multiple independent timers with one hardware-agnostic completion callback."""

    def __init__(self, completion_callback: CompletionCallback | None = None) -> None:
        self._completion_callback = completion_callback
        self._timers: dict[str, _Timer] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._closed = False

    async def create_timer(
        self,
        duration: float,
        message: str = "",
        callback_info: Mapping[str, Any] | None = None,
    ) -> TimerSnapshot:
        duration = self._positive(duration, "duration")
        async with self._lock:
            self._ensure_open()
            timer_id = f"timer-{self._next_id}"
            self._next_id += 1
            timer = _Timer(
                timer_id=timer_id,
                duration=duration,
                remaining=duration,
                status=TimerStatus.RUNNING,
                message=str(message),
                callback_info=dict(callback_info or {}),
            )
            self._timers[timer_id] = timer
            self._start_locked(timer)
            return self._snapshot_locked(timer)

    async def pause_timer(self, timer_id: str) -> TimerSnapshot:
        async with self._lock:
            timer = self._get_locked(timer_id)
            self._require_status(timer, TimerStatus.RUNNING)
            timer.remaining = self._remaining_locked(timer)
            timer.status = TimerStatus.PAUSED
            self._cancel_task_locked(timer)
            return self._snapshot_locked(timer)

    async def resume_timer(self, timer_id: str) -> TimerSnapshot:
        async with self._lock:
            timer = self._get_locked(timer_id)
            self._require_status(timer, TimerStatus.PAUSED)
            timer.status = TimerStatus.RUNNING
            self._start_locked(timer)
            return self._snapshot_locked(timer)

    async def cancel_timer(self, timer_id: str) -> TimerSnapshot:
        async with self._lock:
            timer = self._get_locked(timer_id)
            if timer.status not in (TimerStatus.RUNNING, TimerStatus.PAUSED):
                raise ValueError(f"计时器 {timer_id} 已经是 {timer.status.value}")
            timer.remaining = self._remaining_locked(timer)
            timer.status = TimerStatus.CANCELLED
            self._cancel_task_locked(timer)
            return self._snapshot_locked(timer)

    async def add_time(self, timer_id: str, seconds: float) -> TimerSnapshot:
        seconds = self._positive(seconds, "seconds")
        async with self._lock:
            timer = self._get_locked(timer_id)
            if timer.status not in (TimerStatus.RUNNING, TimerStatus.PAUSED):
                raise ValueError(f"计时器 {timer_id} 已经是 {timer.status.value}")
            timer.duration += seconds
            timer.remaining = self._remaining_locked(timer) + seconds
            if timer.status is TimerStatus.RUNNING:
                self._cancel_task_locked(timer)
                self._start_locked(timer)
            return self._snapshot_locked(timer)

    async def get_remaining(self, timer_id: str) -> TimerSnapshot:
        async with self._lock:
            return self._snapshot_locked(self._get_locked(timer_id))

    async def list_timers(self, include_finished: bool = False) -> list[TimerSnapshot]:
        async with self._lock:
            timers = self._timers.values()
            if not include_finished:
                timers = (
                    timer for timer in timers
                    if timer.status in (TimerStatus.RUNNING, TimerStatus.PAUSED)
                )
            return [self._snapshot_locked(timer) for timer in timers]

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for timer in self._timers.values():
                self._cancel_task_locked(timer)

    def _start_locked(self, timer: _Timer) -> None:
        timer.generation += 1
        generation = timer.generation
        timer.deadline = asyncio.get_running_loop().time() + timer.remaining
        timer.task = asyncio.create_task(self._wait(timer.timer_id, generation))

    async def _wait(self, timer_id: str, generation: int) -> None:
        try:
            async with self._lock:
                timer = self._get_locked(timer_id)
                delay = self._remaining_locked(timer)
            await asyncio.sleep(delay)
            callback = None
            snapshot = None
            async with self._lock:
                timer = self._get_locked(timer_id)
                if (
                    self._closed
                    or timer.status is not TimerStatus.RUNNING
                    or timer.generation != generation
                ):
                    return
                timer.remaining = 0.0
                timer.deadline = None
                timer.task = None
                timer.status = TimerStatus.COMPLETED
                snapshot = self._snapshot_locked(timer)
                callback = self._completion_callback
            if callback is not None and snapshot is not None:
                result = callback(snapshot)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            return

    def _remaining_locked(self, timer: _Timer) -> float:
        if timer.status is TimerStatus.RUNNING and timer.deadline is not None:
            return max(0.0, timer.deadline - asyncio.get_running_loop().time())
        return max(0.0, timer.remaining)

    def _snapshot_locked(self, timer: _Timer) -> TimerSnapshot:
        return TimerSnapshot(
            timer_id=timer.timer_id,
            duration=timer.duration,
            remaining=self._remaining_locked(timer),
            status=timer.status,
            message=timer.message,
            callback_info=dict(timer.callback_info),
        )

    def _cancel_task_locked(self, timer: _Timer) -> None:
        timer.generation += 1
        if timer.task is not None and timer.task is not asyncio.current_task():
            timer.task.cancel()
        timer.task = None
        timer.deadline = None

    def _get_locked(self, timer_id: str) -> _Timer:
        try:
            return self._timers[timer_id]
        except KeyError as exc:
            raise ValueError(f"找不到计时器: {timer_id}") from exc

    @staticmethod
    def _positive(value: float, name: str) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是正数") from exc
        if value <= 0:
            raise ValueError(f"{name} 必须是正数")
        return value

    def _require_status(self, timer: _Timer, expected: TimerStatus) -> None:
        if timer.status is not expected:
            raise ValueError(
                f"计时器 {timer.timer_id} 当前是 {timer.status.value}，"
                f"不能执行需要 {expected.value} 状态的操作"
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TimerManager 已关闭")
