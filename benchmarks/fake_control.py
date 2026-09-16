"""Loopback-only recording Control API for Agent A/B tests; never touches hardware."""
from __future__ import annotations

import argparse
import os
import time
from aiohttp import web


CALLS: list[dict] = []


def outcome(name: str, body: dict) -> dict:
    data: dict = dict(body)
    messages = {
        "queue_expression": "情绪动作已加入本轮播报",
        "play_motion": "动作已完成",
        "sleep": "已登记休眠请求",
    }
    if name == "get_robot_state":
        data = {
            "current_mode": "normal", "motion_active": False,
            "tracking": False, "speaking": False,
            "mechanically_asleep": False, "base_heading_degrees": 0,
            "base_heading_position": "front", "work_light": False,
        }
    elif name == "create_timer":
        data = {"timer_id": "timer-1", "duration": body.get("duration_seconds"), "remaining": body.get("duration_seconds"), "status": "running", "message": body.get("message", ""), "callback_info": {}}
    elif name in {"get_timer_remaining", "pause_timer", "resume_timer", "cancel_timer", "add_timer_time"}:
        data = {"timer_id": body.get("timer_id", "timer-1"), "duration": 1500, "remaining": 1200, "status": "running", "message": "提醒喝水", "callback_info": {}}
    elif name == "list_timers":
        data = {"timers": [{"timer_id": "timer-1", "duration": 1500, "remaining": 1200, "status": "running", "message": "提醒喝水", "callback_info": {}}]}
    elif name == "create_alarm":
        data = {"alarm_id": "alarm-1", "trigger_at": body.get("trigger_at"), "status": "scheduled", "recurrence": body.get("recurrence"), "message": body.get("message", "")}
    elif name in {"get_alarm", "cancel_alarm"}:
        data = {"alarm_id": body.get("alarm_id", "alarm-1"), "trigger_at": "2026-09-16T10:30:00+08:00", "status": "scheduled", "recurrence": "once", "message": "提醒开会"}
    elif name == "list_alarms":
        data = {"alarms": [{"alarm_id": "alarm-1", "trigger_at": "2026-09-16T10:30:00+08:00", "status": "scheduled", "recurrence": "once", "message": "提醒开会"}]}
    return {"ok": True, "status": "shutdown_requested" if name == "sleep" else "completed", "message": messages.get(name, "执行成功"), "data": data}


async def execute(request: web.Request) -> web.Response:
    token = os.getenv("LELAMP_CONTROL_TOKEN", "")
    if not token or request.headers.get("Authorization") != f"Bearer {token}":
        return web.json_response({"ok": False, "message": "unauthorized"}, status=401)
    body = await request.json()
    name = request.match_info["name"]
    CALLS.append({"name": name, "arguments": body, "finished_at": time.time()})
    return web.json_response(outcome(name, body))


async def reset(_request: web.Request) -> web.Response:
    CALLS.clear()
    return web.json_response({"ok": True})


async def calls(_request: web.Request) -> web.Response:
    return web.json_response({"calls": CALLS})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18793)
    args = parser.parse_args()
    app = web.Application(client_max_size=32 * 1024)
    app.router.add_post("/v1/tools/{name}", execute)
    app.router.add_post("/reset", reset)
    app.router.add_get("/calls", calls)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
