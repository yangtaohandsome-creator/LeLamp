from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class AlarmStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MISSED = "missed"


class AlarmRecurrence(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class AlarmSnapshot:
    alarm_id: str
    trigger_at: str
    next_trigger_at: str | None
    recurrence: AlarmRecurrence
    status: AlarmStatus
    message: str
    callback_info: Mapping[str, Any] = field(default_factory=dict)
    day_of_week: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "alarm_id": self.alarm_id,
            "trigger_at": self.trigger_at,
            "next_trigger_at": self.next_trigger_at,
            "recurrence": self.recurrence.value,
            "status": self.status.value,
            "message": self.message,
            "callback_info": dict(self.callback_info),
            "day_of_week": self.day_of_week,
        }


@dataclass
class _Alarm:
    alarm_id: str
    trigger_at: str
    next_trigger_at: str | None
    recurrence: AlarmRecurrence
    status: AlarmStatus
    message: str
    callback_info: dict[str, Any]
    day_of_week: int | None = None
    task: asyncio.Task[None] | None = None
    generation: int = 0


CompletionCallback = Callable[[AlarmSnapshot], Awaitable[None] | None]


class AlarmManager:
    """Persistent absolute-time alarms with one hardware-neutral callback."""

    def __init__(
        self,
        completion_callback: CompletionCallback | None = None,
        root: Path | None = None,
    ) -> None:
        self._completion_callback = completion_callback
        self._root = root or Path(__file__).resolve().parents[2]
        self._alarms: dict[str, _Alarm] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._load_locked()
            self._started = True
            scheduled = [
                alarm for alarm in self._alarms.values()
                if alarm.status is AlarmStatus.SCHEDULED
            ]
            # Let the rest of LeLamp run even when a fresh device has neither
            # network location nor a saved location config. Alarm creation will
            # still report the missing timezone clearly.
            if not scheduled:
                return
            now = self._now()
            changed = False
            for alarm in scheduled:
                next_at = self._parse(alarm.next_trigger_at or alarm.trigger_at)
                if next_at <= now:
                    if alarm.recurrence is AlarmRecurrence.ONCE:
                        alarm.status = AlarmStatus.MISSED
                        alarm.next_trigger_at = None
                        changed = True
                        continue
                    alarm.next_trigger_at = self._next_occurrence(
                        next_at, alarm.recurrence, now, alarm.day_of_week
                    ).isoformat()
                    changed = True
                self._schedule_locked(alarm)
            if changed:
                self._persist_locked()

    async def create_alarm(
        self,
        trigger_at: str,
        message: str = "",
        recurrence: str = "once",
        callback_info: Mapping[str, Any] | None = None,
        day_of_week: int | None = None,
    ) -> AlarmSnapshot:
        await self.start()
        recurrence_value = AlarmRecurrence(str(recurrence))
        if recurrence_value is AlarmRecurrence.WEEKLY:
            try:
                day_of_week = int(day_of_week) if day_of_week is not None else None
            except (TypeError, ValueError) as exc:
                raise ValueError("weekly 闹钟的 day_of_week 必须是 1～7") from exc
            if day_of_week is None or not 1 <= day_of_week <= 7:
                raise ValueError("weekly 闹钟必须提供 1～7 的 day_of_week")
        elif day_of_week is not None:
            raise ValueError("只有 weekly 闹钟可以设置 day_of_week")
        parsed = self._parse(trigger_at)
        now = self._now()
        if recurrence_value is not AlarmRecurrence.ONCE:
            parsed = self._first_recurring_occurrence(
                parsed, recurrence_value, now, day_of_week
            )
        if parsed <= now:
            raise ValueError("trigger_at 必须是未来时间")
        async with self._lock:
            self._ensure_open()
            alarm_id = f"alarm-{self._next_id}"
            self._next_id += 1
            alarm = _Alarm(
                alarm_id=alarm_id,
                trigger_at=parsed.isoformat(),
                next_trigger_at=parsed.isoformat(),
                recurrence=recurrence_value,
                status=AlarmStatus.SCHEDULED,
                message=str(message),
                callback_info=dict(callback_info or {}),
                day_of_week=day_of_week,
            )
            self._alarms[alarm_id] = alarm
            self._schedule_locked(alarm)
            self._persist_locked()
            return self._snapshot(alarm)

    async def cancel_alarm(self, alarm_id: str) -> AlarmSnapshot:
        await self.start()
        async with self._lock:
            alarm = self._get_locked(alarm_id)
            if alarm.status is not AlarmStatus.SCHEDULED:
                raise ValueError(f"闹钟 {alarm_id} 已经是 {alarm.status.value}")
            alarm.status = AlarmStatus.CANCELLED
            alarm.next_trigger_at = None
            self._cancel_task_locked(alarm)
            self._persist_locked()
            return self._snapshot(alarm)

    async def get_alarm(self, alarm_id: str) -> AlarmSnapshot:
        await self.start()
        async with self._lock:
            return self._snapshot(self._get_locked(alarm_id))

    async def list_alarms(self, include_finished: bool = False) -> list[AlarmSnapshot]:
        await self.start()
        async with self._lock:
            values = self._alarms.values()
            if not include_finished:
                values = (alarm for alarm in values if alarm.status is AlarmStatus.SCHEDULED)
            return [self._snapshot(alarm) for alarm in values]

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for alarm in self._alarms.values():
                self._cancel_task_locked(alarm)

    def _schedule_locked(self, alarm: _Alarm) -> None:
        alarm.generation += 1
        generation = alarm.generation
        alarm.task = asyncio.create_task(self._wait(alarm.alarm_id, generation))

    async def _wait(self, alarm_id: str, generation: int) -> None:
        try:
            async with self._lock:
                alarm = self._get_locked(alarm_id)
                target = self._parse(alarm.next_trigger_at or alarm.trigger_at)
            await asyncio.sleep(max(0.0, (target - self._now()).total_seconds()))
            callback = None
            snapshot = None
            async with self._lock:
                alarm = self._get_locked(alarm_id)
                if (
                    self._closed
                    or alarm.status is not AlarmStatus.SCHEDULED
                    or alarm.generation != generation
                ):
                    return
                fired_at = self._parse(alarm.next_trigger_at or alarm.trigger_at)
                if alarm.recurrence is AlarmRecurrence.ONCE:
                    alarm.status = AlarmStatus.COMPLETED
                    alarm.next_trigger_at = None
                else:
                    alarm.next_trigger_at = self._next_occurrence(
                        fired_at, alarm.recurrence, self._now(), alarm.day_of_week
                    ).isoformat()
                    self._schedule_locked(alarm)
                self._persist_locked()
                snapshot = self._snapshot(alarm)
                callback = self._completion_callback
            if callback is not None and snapshot is not None:
                result = callback(snapshot)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            return

    def _timezone(self) -> ZoneInfo:
        timezone_id = os.getenv("LELAMP_TIMEZONE", "").strip()
        if not timezone_id:
            raise ValueError("当前定位没有可用时区，无法创建 Alarm")
        try:
            return ZoneInfo(timezone_id)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"无效时区: {timezone_id}") from exc

    def _now(self) -> datetime:
        return datetime.now(self._timezone())

    def _parse(self, value: str) -> datetime:
        if not value:
            raise ValueError("trigger_at 不能为空")
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("trigger_at 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValueError("trigger_at 必须包含时区偏移")
        return parsed.astimezone(self._timezone())

    def _next_occurrence(
        self,
        previous: datetime,
        recurrence: AlarmRecurrence,
        now: datetime,
        day_of_week: int | None = None,
    ) -> datetime:
        if recurrence is AlarmRecurrence.WEEKLY:
            candidate = previous + timedelta(days=7)
            while candidate <= now:
                candidate += timedelta(days=7)
            return candidate
        candidate = previous
        while candidate <= now:
            candidate += timedelta(days=1)
            if recurrence is AlarmRecurrence.WEEKDAYS:
                while candidate.weekday() >= 5:
                    candidate += timedelta(days=1)
        return candidate

    def _first_recurring_occurrence(
        self,
        requested: datetime,
        recurrence: AlarmRecurrence,
        now: datetime,
        day_of_week: int | None = None,
    ) -> datetime:
        """Use the requested local clock time at its nearest valid occurrence."""
        candidate = now.replace(
            hour=requested.hour,
            minute=requested.minute,
            second=requested.second,
            microsecond=requested.microsecond,
        )
        if recurrence is AlarmRecurrence.WEEKLY:
            assert day_of_week is not None
            candidate += timedelta(days=(day_of_week - candidate.isoweekday()) % 7)
            if candidate <= now:
                candidate += timedelta(days=7)
            return candidate
        while candidate <= now or (
            recurrence is AlarmRecurrence.WEEKDAYS and candidate.weekday() >= 5
        ):
            candidate += timedelta(days=1)
        return candidate

    def _state_path(self) -> Path:
        return self._root / os.getenv("ALARM_STATE_FILE", "runtime_state/alarms.json")

    def _persist_locked(self) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "next_id": self._next_id,
            "alarms": [
                {
                    "alarm_id": alarm.alarm_id,
                    "trigger_at": alarm.trigger_at,
                    "next_trigger_at": alarm.next_trigger_at,
                    "recurrence": alarm.recurrence.value,
                    "status": alarm.status.value,
                    "message": alarm.message,
                    "callback_info": alarm.callback_info,
                    "day_of_week": alarm.day_of_week,
                }
                for alarm in self._alarms.values()
            ],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _load_locked(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._next_id = max(1, int(data.get("next_id", 1)))
            for item in data.get("alarms", []):
                alarm = _Alarm(
                    alarm_id=str(item["alarm_id"]),
                    trigger_at=str(item["trigger_at"]),
                    next_trigger_at=item.get("next_trigger_at"),
                    recurrence=AlarmRecurrence(item["recurrence"]),
                    status=AlarmStatus(item["status"]),
                    message=str(item.get("message", "")),
                    callback_info=dict(item.get("callback_info", {})),
                    day_of_week=item.get("day_of_week"),
                )
                self._alarms[alarm.alarm_id] = alarm
        except Exception as exc:
            raise RuntimeError(f"Alarm 配置读取失败: {exc}") from exc

    def _snapshot(self, alarm: _Alarm) -> AlarmSnapshot:
        return AlarmSnapshot(
            alarm_id=alarm.alarm_id,
            trigger_at=alarm.trigger_at,
            next_trigger_at=alarm.next_trigger_at,
            recurrence=alarm.recurrence,
            status=alarm.status,
            message=alarm.message,
            callback_info=dict(alarm.callback_info),
            day_of_week=alarm.day_of_week,
        )

    def _cancel_task_locked(self, alarm: _Alarm) -> None:
        alarm.generation += 1
        if alarm.task is not None and alarm.task is not asyncio.current_task():
            alarm.task.cancel()
        alarm.task = None

    def _get_locked(self, alarm_id: str) -> _Alarm:
        try:
            return self._alarms[alarm_id]
        except KeyError as exc:
            raise ValueError(f"找不到闹钟: {alarm_id}") from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("AlarmManager 已关闭")
