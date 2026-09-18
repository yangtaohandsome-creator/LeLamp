"""Lightweight, in-memory serialization for all app-level speech output."""
from __future__ import annotations

import asyncio
import heapq
import sys
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Mapping


class AnnouncementPriority(IntEnum):
    LOCAL_REPLY = 0
    REMOTE_REPLY = 10
    NOTIFICATION = 20


class AnnouncementInterrupted(Exception):
    """The active item was intentionally stopped for a user utterance."""


@dataclass(frozen=True)
class Announcement:
    announcement_id: str
    source: str
    text: str
    priority: int
    mergeable: bool = False
    callback_info: Mapping[str, Any] = field(default_factory=dict)
    expression: str | None = None
    sound_before: str | None = None


@dataclass(frozen=True)
class AnnouncementResult:
    announcement_id: str
    text: str
    success: bool
    metrics: tuple[float, float, float] = (0.0, 0.0, 0.0)
    error: str | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class AnnouncementHandle:
    announcement: Announcement
    future: asyncio.Future[AnnouncementResult]

    async def wait(self) -> AnnouncementResult:
        return await self.future


@dataclass(order=True)
class _Entry:
    priority: int
    sequence: int
    announcement: Announcement = field(compare=False)
    future: asyncio.Future[AnnouncementResult] = field(compare=False)


Processor = Callable[
    [list[Announcement]],
    Awaitable[tuple[str, tuple[float, float, float]]],
]


class AnnouncementQueue:
    """One gated consumer for speech jobs; business decisions stay in LampApp."""

    def __init__(self, processor: Processor) -> None:
        self._processor = processor
        self._condition = asyncio.Condition()
        self._heap: list[_Entry] = []
        self._sequence = 0
        self._worker: asyncio.Task[None] | None = None
        self._active_entries: list[_Entry] = []
        self._playback_allowed = False
        self._pause_when_empty = False
        self._closed = False
        self._paused = asyncio.Event()
        self._paused.set()
        # VAD executes in a worker thread, so its pending check must be thread-safe.
        self._interrupt = threading.Event()

    async def start(self) -> None:
        async with self._condition:
            if self._closed:
                raise RuntimeError("AnnouncementQueue 已关闭")
            if self._worker is None:
                self._worker = asyncio.create_task(self._run())

    async def submit(
        self,
        *,
        announcement_id: str,
        source: str,
        text: str,
        priority: int,
        mergeable: bool = False,
        callback_info: Mapping[str, Any] | None = None,
        expression: str | None = None,
        sound_before: str | None = None,
    ) -> AnnouncementHandle:
        await self.start()
        loop = asyncio.get_running_loop()
        announcement = Announcement(
            announcement_id=str(announcement_id),
            source=str(source),
            text=str(text),
            priority=int(priority),
            mergeable=bool(mergeable),
            callback_info=dict(callback_info or {}),
            expression=expression,
            sound_before=sound_before,
        )
        future: asyncio.Future[AnnouncementResult] = loop.create_future()
        async with self._condition:
            if self._closed:
                raise RuntimeError("AnnouncementQueue 已关闭")
            self._sequence += 1
            heapq.heappush(
                self._heap,
                _Entry(announcement.priority, self._sequence, announcement, future),
            )
            self._interrupt.set()
            self._paused.clear()
            self._condition.notify_all()
        return AnnouncementHandle(announcement, future)

    async def drain_and_pause(self) -> None:
        """Allow playback until the current backlog is empty, then close the gate."""
        await self.start()
        async with self._condition:
            if not self._heap and not self._active_entries:
                self._playback_allowed = False
                self._pause_when_empty = False
                self._interrupt.clear()
                self._paused.set()
                return
            self._playback_allowed = True
            self._pause_when_empty = True
            self._paused.clear()
            self._condition.notify_all()
        await self._paused.wait()

    def has_pending(self) -> bool:
        return self._interrupt.is_set()

    def interruption_requested(self) -> bool:
        return self._interrupt.is_set()

    async def close(self) -> None:
        async with self._condition:
            if self._closed:
                return
            self._closed = True
            pending = list(self._heap)
            self._heap.clear()
            self._interrupt.clear()
            self._paused.set()
            self._condition.notify_all()
            worker = self._worker
        for entry in pending:
            self._finish_failed(entry, "播报队列已关闭")
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._worker = None

    async def _run(self) -> None:
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._closed
                    or (self._playback_allowed and bool(self._heap))
                )
                if self._closed:
                    return
                entries = self._pop_batch_locked()
                self._active_entries = entries
            try:
                spoken_text, metrics = await self._processor(
                    [entry.announcement for entry in entries]
                )
            except AnnouncementInterrupted:
                for entry in entries:
                    if not entry.future.done():
                        entry.future.set_result(AnnouncementResult(
                            announcement_id=entry.announcement.announcement_id,
                            text=entry.announcement.text,
                            success=True,
                            interrupted=True,
                        ))
                async with self._condition:
                    # A live user utterance owns the microphone now. Preserve
                    # queued notifications for the next safe playback window.
                    self._playback_allowed = False
                    self._pause_when_empty = False
                    if not self._heap:
                        self._interrupt.clear()
                    self._paused.set()
                    self._condition.notify_all()
            except asyncio.CancelledError:
                for entry in entries:
                    self._finish_failed(entry, "播报已取消")
                raise
            except Exception as exc:
                ids = ",".join(
                    entry.announcement.announcement_id for entry in entries
                )
                print(
                    f"ANNOUNCEMENT FAILED: {ids} | {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                for entry in entries:
                    self._finish_failed(entry, str(exc))
            else:
                for entry in entries:
                    if not entry.future.done():
                        entry.future.set_result(AnnouncementResult(
                            announcement_id=entry.announcement.announcement_id,
                            text=spoken_text,
                            success=True,
                            metrics=metrics,
                        ))
            finally:
                async with self._condition:
                    self._active_entries = []
                    if self._pause_when_empty and not self._heap:
                        self._playback_allowed = False
                        self._pause_when_empty = False
                        self._interrupt.clear()
                        self._paused.set()
                    self._condition.notify_all()

    def _pop_batch_locked(self) -> list[_Entry]:
        first = heapq.heappop(self._heap)
        entries = [first]
        if not first.announcement.mergeable:
            return entries
        # Merge only consecutive, same-priority text notifications. Stopping at
        # a non-mergeable entry preserves arrival order around actions/Agent jobs.
        while self._heap:
            following = self._heap[0]
            if (
                following.priority != first.priority
                or not following.announcement.mergeable
            ):
                break
            entries.append(heapq.heappop(self._heap))
        return entries

    @staticmethod
    def _finish_failed(entry: _Entry, error: str) -> None:
        if not entry.future.done():
            entry.future.set_result(AnnouncementResult(
                announcement_id=entry.announcement.announcement_id,
                text=entry.announcement.text,
                success=False,
                error=error,
            ))
