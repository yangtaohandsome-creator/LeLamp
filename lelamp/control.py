"""Authenticated HTTP bridge used by the OpenClaw LeLamp tool plugin."""
from __future__ import annotations

import hmac
import os
from typing import Any

from aiohttp import web

from .tools import ToolExecutor


class ControlServer:
    def __init__(self, tools: ToolExecutor) -> None:
        self.tools = tools
        self.host = os.getenv("LELAMP_CONTROL_BIND", "127.0.0.1")
        self.port = int(os.getenv("LELAMP_CONTROL_PORT", "18790"))
        self.token = os.getenv("LELAMP_CONTROL_TOKEN", "")
        self._runner: web.AppRunner | None = None

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        return bool(self.token) and hmac.compare_digest(supplied, expected)

    async def _health(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"status": "unauthorized"}, status=401)
        return web.json_response({"status": "ok"})

    async def _execute(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"status": "unauthorized"}, status=401)
        try:
            body: Any = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            outcome = await self.tools.execute(request.match_info["name"], body)
            return web.json_response(outcome.as_dict())
        except ValueError as exc:
            return web.json_response({"status": "invalid", "message": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"status": "failed", "message": str(exc)}, status=500)

    async def start(self) -> None:
        if not self.token:
            raise RuntimeError("LELAMP_CONTROL_TOKEN 未配置")
        app = web.Application(client_max_size=16 * 1024)
        app.router.add_get("/health", self._health)
        app.router.add_post("/v1/tools/{name}", self._execute)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        print(f"LeLamp control ready: http://{self.host}:{self.port}", flush=True)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
