"""High-level LeLamp tools shared by local commands and external agents."""
from __future__ import annotations

from dataclasses import dataclass, field
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


class ToolExecutor:
    """The only public path from intent sources into coordinated app actions."""

    MOTIONS = {
        "happy_wiggle", "excited", "sad", "shy", "shock",
        "nod", "headshake", "curious",
    }
    EXPRESSIONS = MOTIONS
    LIGHT_MODES = {"on", "off", "office", "warm"}

    def __init__(self, app: "LampApp") -> None:
        self.app = app

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolOutcome:
        args = arguments or {}
        if name == "play_motion":
            motion = str(args.get("name", ""))
            if motion not in self.MOTIONS:
                raise ValueError(f"不支持的动作: {motion}")
            if self.app.should_defer_agent_motion(motion):
                queued, reason = self.app.queue_expression(motion)
                return ToolOutcome(
                    "completed",
                    "Agent 情绪动作已改为随本轮播报执行" if queued else reason,
                    {"motion": motion, "queued": queued, "timing": "with_reply"},
                )
            await self.app.play_motion(motion)
            return ToolOutcome("completed", "动作已完成", {"motion": motion})

        if name == "queue_expression":
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

        if name == "stop_tracking":
            was_tracking = self.app.current_mode == "tracking"
            await self.app.stop_tracking()
            return ToolOutcome(
                "completed",
                "跟踪已停止" if was_tracking else "当前没有运行跟踪",
                {"was_tracking": was_tracking},
            )

        raise ValueError(f"未知工具: {name}")
