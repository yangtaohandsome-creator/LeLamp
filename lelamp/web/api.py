"""Same-origin LAN console hosted by the existing remote HTTP listener."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from ..tools import ToolExecutor
from .maintenance import MaintenanceController

STATIC = Path(__file__).with_name("static")
BUTTON_TOOLS = {
    "start_face_tracking", "start_hand_tracking", "stop_tracking",
    "play_motion", "turn_base", "reset_base_heading", "set_base_heading",
    "enter_work_light", "update_work_light", "exit_work_light",
    "set_light", "create_timer", "pause_timer", "resume_timer",
    "cancel_timer", "add_timer_time", "create_alarm", "cancel_alarm",
    "create_todo", "update_todo", "complete_todo", "delete_todo",
}
ACTION_FIELDS = {
    "start_face_tracking": set(), "start_hand_tracking": set(), "stop_tracking": set(),
    "play_motion": {"name"}, "turn_base": {"direction", "steps"},
    "reset_base_heading": set(), "set_base_heading": {"position"},
    "enter_work_light": {"pose", "tone"}, "update_work_light": {"pose", "tone", "brightness_step"},
    "exit_work_light": set(), "set_light": {"mode", "brightness"},
    "create_timer": {"duration_seconds", "message"}, "pause_timer": {"timer_id"},
    "resume_timer": {"timer_id"}, "cancel_timer": {"timer_id"},
    "add_timer_time": {"timer_id", "seconds"},
    "create_alarm": {"trigger_at", "message", "recurrence", "day_of_week"},
    "cancel_alarm": {"alarm_id"}, "create_todo": {"text"},
    "update_todo": {"todo_id", "text"}, "complete_todo": {"todo_id"},
    "delete_todo": {"todo_id"}, "sleep_now": set(), "enter_standby": set(),
}


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return request.headers.get("Sec-Fetch-Site", "same-origin") in {"same-origin", "none"}
    parts = urlsplit(origin)
    return parts.scheme == request.scheme and parts.netloc == request.host


async def _body(request: web.Request) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="需要 application/json")
    if not _same_origin(request):
        raise web.HTTPForbidden(text="只接受同源请求")
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="JSON 格式错误") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="请求体必须是 JSON 对象")
    return body


def _fields(body: dict, *, required=(), optional=()):
    unknown = set(body) - set(required) - set(optional)
    if unknown or any(key not in body for key in required):
        raise ValueError(f"请求字段不匹配: {sorted(unknown)}")


class WebConsole:
    def __init__(self, app, *, maintenance=None):
        self.app = app
        self.maintenance = maintenance or MaintenanceController()
        app.maintenance = self.maintenance
        from .preview import PreviewOutput
        self.preview = PreviewOutput(app, self.maintenance)
        self._watchdog: asyncio.Task | None = None
        self._chat_task = None
        self._maintenance_owner = None

    def routes(self, router):
        router.add_get("/", self.index)
        router.add_get("/web/{filename}", self.asset)
        router.add_get("/api/v1/web/state", self.state)
        router.add_get("/api/v1/web/vision", self.vision_state)
        router.add_get("/api/v1/web/vision/preview", self.preview_image)
        router.add_get("/api/v1/web/health", self.health)
        router.add_get("/api/v1/web/items", self.items)
        router.add_get("/api/v1/web/maintenance", self.maintenance_state)
        router.add_post("/api/v1/web/chat", self.chat)
        router.add_post("/api/v1/web/action", self.action)
        router.add_post("/api/v1/web/maintenance/{step}", self.maintenance_step)

    async def start(self):
        self._watchdog = asyncio.create_task(self._watch())

    async def preview_image(self, request):
        if not _same_origin(request):
            raise web.HTTPForbidden()
        return await self.preview.get(request)

    async def close(self):
        await self.preview.close()
        if self._chat_task is not None:
            self._chat_task.cancel()
            await asyncio.gather(self._chat_task, return_exceptions=True)
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
        if self.maintenance.active:
            await self.maintenance.disconnect_browser()
            await self.maintenance.end()

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            try:
                await self.maintenance.check_heartbeat()
            except Exception as exc:
                self.maintenance.error = f"维护设备释放失败，保持维护模式: {exc}"

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def asset(self, request):
        name = request.match_info["filename"]
        if name not in {"app.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def state(self, request):
        state = self.app.get_robot_state()
        state.update({"voice_running": self.app.voice_task is not None and not self.app.voice_task.done(),
                      "maintenance": self.maintenance.state(),
                      "session_epoch": self.app._sleep_generation,
                      "simulation": getattr(self.app, "simulation", False)})
        return web.json_response({"ok": True, "state": state})

    async def vision_state(self, request):
        from .vision import vision_view
        return web.json_response({"ok": True, "vision": vision_view(self.app, self.maintenance.active)},
                                 headers={"Cache-Control": "no-store"})

    async def health(self, request):
        if getattr(self.app, "simulation", False):
            return web.json_response({"ok": True, "health": {"checks": [
                {"name": "simulation", "status": "unknown", "reason": "本机模拟未连接麦克风、Agent、灯光或舵机；只能验证网页流程"}
            ]}})
        from ..diagnostics import run_live
        return web.json_response({"ok": True, "health": await run_live(self.app)})

    async def items(self, request):
        timers, alarms, todos = await asyncio.gather(
            self.app.timers.list_timers(), self.app.alarms.list_alarms(),
            self.app.todos.list_todos(True))
        return web.json_response({"ok": True, "timers": [x.as_dict() for x in timers],
                                  "alarms": [x.as_dict() for x in alarms],
                                  "todos": [x.as_dict() for x in todos]})

    async def maintenance_state(self, request):
        return web.json_response({"ok": True, "maintenance": self.maintenance.state()})

    async def chat(self, request):
        body = await _body(request)
        try:
            _fields(body, required=("text", "session_id"), optional=("speak",))
            text = body["text"]
            session_id = body["session_id"]
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise ValueError("text 长度必须为 1～2000")
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or not all(c.isalnum() or c in "._:-" for c in session_id):
                raise ValueError("session_id 格式无效")
            speak = body.get("speak", True)
            if not isinstance(speak, bool):
                raise ValueError("speak 必须是布尔值")
            async with self.app._web_lock:
                if self.maintenance.active:
                    raise ValueError("维护模式中不能对话")
                self._chat_task = asyncio.current_task()
                try:
                    return web.json_response(await self.app.process_web_text(text.strip(), session_id, speak))
                finally:
                    self._chat_task = None
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except asyncio.CancelledError:
            return web.json_response({"ok": False, "status": "interrupted", "error": "网页请求已中断"}, status=409)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def action(self, request):
        body = await _body(request)
        try:
            _fields(body, required=("name",), optional=("arguments",))
            name, args = body["name"], body.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict):
                raise ValueError("操作名称或参数格式错误")
            if name not in BUTTON_TOOLS | {"sleep_now", "enter_standby"}:
                raise ValueError("网页不支持该操作")
            self._validate_action(name, args)
            if self._chat_task is not None:
                self._chat_task.cancel()
            async with self.app._web_lock:
                if self.maintenance.active:
                    raise ValueError("维护模式中只允许维护操作")
                return web.json_response(await self.app.execute_web_action(name, args))
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @staticmethod
    def _validate_action(name, args):
        if set(args) - ACTION_FIELDS[name]:
            raise ValueError("操作含有不支持的参数")
        if any(isinstance(x, (dict, list)) for x in args.values()):
            raise ValueError("网页按钮仅接受简单参数")
        if name == "play_motion" and args.get("name") not in ToolExecutor.MOTIONS:
            raise ValueError("动作名不在已确认清单")
        if name == "turn_base" and (args.get("direction") not in {"left", "right"} or args.get("steps") not in {1, 2}):
            raise ValueError("只允许左/右转 30° 或 60°")
        if name == "update_work_light" and args.get("brightness_step") not in {None, -25, 25}:
            raise ValueError("亮度每次只能变化 25%")
        if name in {"create_timer", "add_timer_time"}:
            key = "duration_seconds" if name == "create_timer" else "seconds"
            value = args.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or value > 31536000:
                raise ValueError(f"{key} 必须是有效正秒数")
        if name == "create_alarm" and args.get("recurrence") == "weekly" and args.get("day_of_week") not in range(1, 8):
            raise ValueError("每周闹钟需要星期 1～7")
        for key, value in args.items():
            if key in {"steps", "brightness_step", "brightness", "duration_seconds", "seconds", "day_of_week"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{key} 必须是数值")
                if key in {"steps", "brightness_step", "day_of_week"} and not isinstance(value, int):
                    raise ValueError(f"{key} 必须是整数")
            elif not isinstance(value, str) or len(value) > 2000:
                raise ValueError(f"{key} 必须是长度不超过 2000 的文本")

    async def maintenance_step(self, request):
        body = await _body(request)
        step = request.match_info["step"]
        allowed = {"begin", "end", "heartbeat", "disconnect", "open", "record_start", "record_stop",
                   "record_save", "record_preview", "center", "range_start", "range_stop",
                   "calibration_save", "pose_capture", "pose_save"}
        allowed.update({"calibration_restore", "reconnect"})
        if step not in allowed:
            raise web.HTTPNotFound()
        try:
            client = request.headers.get("X-LeLamp-Client", "")
            if not 8 <= len(client) <= 128 or not all(c.isalnum() or c in "-_." for c in client):
                raise ValueError("缺少有效的维护客户端标识")
            if step == "reconnect":
                _fields(body)
                if not self.maintenance.active or not self.maintenance.state()["disconnected"]:
                    raise ValueError("当前没有断开的维护会话")
                self._maintenance_owner = client
                self.maintenance.touch()
                self.maintenance._disconnected = False
                return web.json_response({"ok": True, "data": self.maintenance.state()})
            if step != "begin" and client != self._maintenance_owner:
                raise ValueError("当前维护由另一网页持有，请回到发起维护的浏览器")
            if step == "heartbeat":
                _fields(body)
                self.maintenance.touch()
                return web.json_response({"ok": True, "data": self.maintenance.state()})
            if step == "disconnect":
                _fields(body)
                await self.maintenance.disconnect_browser()
                return web.json_response({"ok": True, "data": self.maintenance.state()})
            if step == "begin" and self._chat_task is not None:
                self._chat_task.cancel()
            async with self.app._web_lock:
                m = self.maintenance
                m.touch()
                if step == "begin":
                    _fields(body)
                    await self.app.begin_web_maintenance()
                    self._maintenance_owner = client
                    data = m.state()
                elif step == "end":
                    _fields(body)
                    if not m.active:
                        raise ValueError("当前不在维护模式")
                    await self.app.end_web_maintenance()
                    self._maintenance_owner = None
                    data = m.state()
                else:
                    if not m.active:
                        raise ValueError("请先进入维护模式")
                    data = await self._maintenance_operation(step, body)
                return web.json_response({"ok": True, "data": data})
        except asyncio.CancelledError:
            return web.json_response({"ok": False, "status": "interrupted", "error": "维护操作已停止，设备保持维护模式"}, status=409)
        except (ValueError, TypeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _maintenance_operation(self, step, body):
        m = self.maintenance
        if step == "heartbeat":
            _fields(body)
            return m.state()
        if step == "disconnect":
            _fields(body)
            await m.disconnect_browser()
            return m.state()
        if step == "open":
            _fields(body, required=("device",))
            return await m.open_device(body["device"])
        if step == "record_start":
            _fields(body)
            return await m.start_recording()
        if step == "record_stop":
            _fields(body)
            await m.stop_sampling()
            return m.state()
        if step == "record_save":
            _fields(body, required=("name",), optional=("overwrite",))
            return await m.save_recording(body["name"], body.get("overwrite", False) is True)
        if step == "record_preview":
            _fields(body, required=("name",))
            return await m.preview(body["name"], self.app.motion)
        if step == "center":
            _fields(body)
            return await m.center()
        if step == "range_start":
            _fields(body)
            return await m.start_ranges()
        if step == "range_stop":
            _fields(body)
            return await m.calibration_result()
        if step == "calibration_save":
            _fields(body)
            return await m.save_calibration()
        if step == "calibration_restore":
            _fields(body, required=("device",))
            return await m.restore_calibration(body["device"])
        if step == "pose_capture":
            _fields(body, required=("pose",))
            return await m.capture_pose(body["pose"])
        if step == "pose_save":
            _fields(body, required=("pose", "values"))
            if not isinstance(body["values"], dict):
                raise ValueError("姿态数据必须为对象")
            return m.save_pose(body["pose"], body["values"])
        raise ValueError("未知维护步骤")
