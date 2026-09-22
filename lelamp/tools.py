"""High-level LeLamp tools shared by local commands and external agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .app import LampApp


@dataclass(frozen=True)
class ToolOutcome:
    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.status not in {"failed", "cancelled"},
                "status": self.status, "message": self.message, "data": self.data}


class ToolSource(StrEnum):
    AGENT = "agent"
    LOCAL_VOICE = "local_voice"
    VISION = "vision"
    SCHEDULED = "scheduled"
    CONTROL_API = "control_api"


class ToolExecutor:
    """The only public path from intent sources into coordinated app actions."""

    MOTIONS = {
        "happy_wiggle", "excited", "sad", "shy", "shock",
        "nod", "headshake", "curious",
    }
    EXPRESSIONS = MOTIONS
    LIGHT_MODES = {"on", "off", "office", "warm"}
    SCHEDULED_TOOLS = {
        "play_motion", "set_light", "enter_work_light",
        "update_work_light", "exit_work_light", "sleep", "stop_tracking",
        "start_face_tracking",
        "turn_base", "reset_base_heading",
        "set_base_heading",
    }

    def __init__(self, app: "LampApp") -> None:
        self.app = app

    def _callback_info(
        self, args: dict[str, Any], source: ToolSource
    ) -> dict[str, Any]:
        callback_info: dict[str, Any] = {"source": source.value}
        on_complete = args.get("on_complete")
        agent_task = str(args.get("agent_task", "")).strip()
        if on_complete is not None and agent_task:
            raise ValueError("on_complete 和 agent_task 不能同时设置")
        if on_complete is not None:
            if not isinstance(on_complete, dict):
                raise ValueError("on_complete 必须是对象")
            tool_name = str(on_complete.get("tool", ""))
            tool_args = on_complete.get("arguments", {})
            if tool_name not in self.SCHEDULED_TOOLS:
                raise ValueError(f"不支持定时执行的工具: {tool_name}")
            if not isinstance(tool_args, dict):
                raise ValueError("on_complete.arguments 必须是对象")
            callback_info["action"] = {"tool": tool_name, "arguments": dict(tool_args)}
        elif agent_task:
            callback_info["agent_task"] = agent_task
        return callback_info

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        source: ToolSource | str = ToolSource.CONTROL_API,
    ) -> ToolOutcome:
        try:
            source = ToolSource(source)
        except ValueError as exc:
            raise ValueError(f"未知工具调用来源: {source}") from exc
        args = arguments or {}
        if name == "create_timer":
            duration = args.get("duration_seconds", 0)
            message = str(args.get("message", ""))
            timer = await self.app.timers.create_timer(
                duration, message, self._callback_info(args, source)
            )
            return ToolOutcome("completed", "计时器已创建", timer.as_dict())

        if name == "create_alarm":
            alarm = await self.app.alarms.create_alarm(
                str(args.get("trigger_at", "")),
                str(args.get("message", "")),
                str(args.get("recurrence", "once")),
                self._callback_info(args, source),
                args.get("day_of_week"),
            )
            return ToolOutcome("completed", "闹钟已创建", alarm.as_dict())

        if name == "cancel_alarm":
            alarm = await self.app.alarms.cancel_alarm(str(args.get("alarm_id", "")))
            return ToolOutcome("completed", "闹钟已取消", alarm.as_dict())

        if name == "get_alarm":
            alarm = await self.app.alarms.get_alarm(str(args.get("alarm_id", "")))
            return ToolOutcome("completed", "闹钟状态读取成功", alarm.as_dict())

        if name == "list_alarms":
            alarms = await self.app.alarms.list_alarms(bool(args.get("include_finished", False)))
            return ToolOutcome(
                "completed", "闹钟列表读取成功",
                {"alarms": [alarm.as_dict() for alarm in alarms]},
            )

        if name == "pause_timer":
            timer = await self.app.timers.pause_timer(str(args.get("timer_id", "")))
            return ToolOutcome("completed", "计时器已暂停", timer.as_dict())

        if name == "resume_timer":
            timer = await self.app.timers.resume_timer(str(args.get("timer_id", "")))
            return ToolOutcome("completed", "计时器已继续", timer.as_dict())

        if name == "cancel_timer":
            timer = await self.app.timers.cancel_timer(str(args.get("timer_id", "")))
            return ToolOutcome("completed", "计时器已取消", timer.as_dict())

        if name == "add_timer_time":
            timer = await self.app.timers.add_time(
                str(args.get("timer_id", "")), args.get("seconds", 0)
            )
            return ToolOutcome("completed", "计时器已加时", timer.as_dict())

        if name == "get_timer_remaining":
            timer = await self.app.timers.get_remaining(str(args.get("timer_id", "")))
            return ToolOutcome("completed", "计时器状态读取成功", timer.as_dict())

        if name == "list_timers":
            timers = await self.app.timers.list_timers(
                bool(args.get("include_finished", False))
            )
            return ToolOutcome(
                "completed", "计时器列表读取成功",
                {"timers": [timer.as_dict() for timer in timers]},
            )

        if name == "play_motion":
            motion = str(args.get("name", ""))
            if motion not in self.MOTIONS:
                raise ValueError(f"不支持的动作: {motion}")
            if source is ToolSource.AGENT and self.app.should_defer_agent_motion(motion):
                queued, reason = self.app.queue_expression(motion)
                return ToolOutcome(
                    "completed",
                    "Agent 情绪动作已改为随本轮播报执行" if queued else reason,
                    {"motion": motion, "queued": queued, "timing": "with_reply"},
                )
            await self.app.play_motion(motion)
            return ToolOutcome("completed", "动作已完成", {"motion": motion})

        if name == "turn_base":
            direction = str(args.get("direction", ""))
            steps = args.get("steps", 1)
            if isinstance(steps, bool) or not isinstance(steps, int):
                raise ValueError("steps 必须是正整数")
            heading = await self.app.turn_base(direction, steps)
            return ToolOutcome(
                "completed", "底座转向已完成",
                {"direction": direction, "steps": steps,
                 "base_heading_degrees": heading},
            )

        if name == "reset_base_heading":
            heading = await self.app.reset_base_heading()
            return ToolOutcome(
                "completed", "底座已回到初始正面",
                {"base_heading_degrees": heading},
            )

        if name == "set_base_heading":
            position = str(args.get("position", ""))
            heading = await self.app.set_base_heading(position)
            return ToolOutcome(
                "completed", "底座绝对朝向已设置",
                {"position": position, "base_heading_degrees": heading},
            )

        if name == "queue_expression":
            if source is not ToolSource.AGENT:
                raise ValueError("queue_expression 只允许 Agent 自主表达调用")
            expression = str(args.get("name", ""))
            if expression not in self.EXPRESSIONS:
                raise ValueError(f"不支持的情绪动作: {expression}")
            queued, reason = self.app.queue_expression(expression)
            return ToolOutcome(
                "completed",
                "情绪动作已加入本轮播报" if queued else reason,
                {"expression": expression, "queued": queued},
            )

        if name == "set_light":
            mode = str(args.get("mode", ""))
            if mode not in self.LIGHT_MODES:
                raise ValueError(f"不支持的灯光模式: {mode}")
            brightness = args.get("brightness")
            if brightness is not None:
                brightness = float(brightness)
                if not 0 <= brightness <= 100:
                    raise ValueError("brightness 必须在 0～100")
            if mode == "off":
                self.app.set_light((0, 0, 0), 0)
            elif mode in {"on", "office"}:
                self.app.lighting.office_mode(brightness)
            else:
                self.app.lighting.warm_mode(brightness)
            return ToolOutcome("completed", "灯光已设置", {"mode": mode})

        if name == "enter_work_light":
            pose = str(args.get("pose", "high"))
            tone = str(args.get("tone", "white"))
            await self.app.enter_work_light(pose, tone)
            return ToolOutcome(
                "completed", "已进入办公照明模式",
                {"pose": pose, "tone": tone, "brightness": self.app.work_brightness},
            )

        if name == "update_work_light":
            pose = args.get("pose")
            tone = args.get("tone")
            step = args.get("brightness_step")
            if step is not None:
                step = int(step)
            await self.app.update_work_light(pose, tone, step)
            return ToolOutcome(
                "completed", "办公照明已调整",
                {
                    "pose": self.app.work_pose,
                    "tone": self.app.work_tone,
                    "brightness": self.app.work_brightness,
                },
            )

        if name == "exit_work_light":
            exited = await self.app.exit_work_light()
            return ToolOutcome(
                "completed",
                "已退出办公照明模式" if exited else "当前未处于办公照明模式",
                {"exited": exited},
            )

        if name == "sleep":
            reason = str(args.get("reason", "user_request"))
            self.app.request_shutdown(reason)
            return ToolOutcome(
                "shutdown_requested",
                "已登记休眠请求；请先完成简短告别，设备随后休眠",
                {"reason": reason},
            )

        if name == "get_robot_state":
            return ToolOutcome("completed", "状态读取成功", self.app.get_robot_state())

        if name == "get_vision_state":
            return ToolOutcome(
                "completed", "视觉状态读取成功", self.app.get_vision_state()
            )

        if name == "get_system_health":
            from .diagnostics import run_live
            return ToolOutcome("completed", "只读自检完成", await run_live(self.app))

        if name == "stop_tracking":
            was_tracking = self.app.current_mode == "tracking"
            await self.app.stop_tracking()
            return ToolOutcome(
                "completed",
                "跟踪已停止" if was_tracking else "当前没有运行跟踪",
                {"was_tracking": was_tracking},
            )

        if name == "start_face_tracking":
            await self.app.start_face_tracking()
            return ToolOutcome(
                "completed", "已开始人脸跟踪", {"mode": "tracking"}
            )

        raise ValueError(f"未知工具: {name}")
