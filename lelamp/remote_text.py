"""Authenticated LAN endpoint for sending text through the LeLamp agent."""
from __future__ import annotations

import hmac
import os
import re
from collections import OrderedDict
from typing import Any

from aiohttp import web


_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RemoteTextServer:
    def __init__(self, lamp_app) -> None:
        self.lamp_app = lamp_app
        self.host = os.getenv("LELAMP_REMOTE_BIND", "0.0.0.0")
        self.port = int(os.getenv("LELAMP_REMOTE_PORT", "18791"))
        self.token = os.getenv("LELAMP_REMOTE_TOKEN", "")
        self._runner: web.AppRunner | None = None
        self._results: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()

    def _authorized(self, request: web.Request) -> bool:
        supplied = request.headers.get("Authorization", "")
        return bool(self.token) and hmac.compare_digest(
            supplied, f"Bearer {self.token}"
        )

    async def _health(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response({"ok": True, "service": "lelamp-remote-text"})

    async def _submit(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body: Any = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
            request_id = str(body.get("request_id", "")).strip()
            session_id = str(body.get("session_id", "")).strip()
            text = str(body.get("text", "")).strip()
            locale = str(body.get("locale", "zh-CN")).strip() or "zh-CN"
            if not _ID_PATTERN.fullmatch(request_id):
                raise ValueError("request_id 必须为 1～128 位字母、数字或 ._:-")
            if not _ID_PATTERN.fullmatch(session_id):
                raise ValueError("session_id 必须为 1～128 位字母、数字或 ._:-")
            if not text or len(text) > 2000:
                raise ValueError("text 长度必须为 1～2000 个字符")

            fingerprint = f"{session_id}\0{locale}\0{text}"
            cached = self._results.get(request_id)
            if cached is not None:
                if cached[0] != fingerprint:
                    return web.json_response(
                        {"ok": False, "error": "request_id 已用于其他请求"}, status=409
                    )
                self._results.move_to_end(request_id)
                return web.json_response(cached[1])

            result = await self.lamp_app.process_remote_text(text, session_id)
            response = {
                "ok": result.status != "failed",
                "request_id": request_id,
                "session_id": session_id,
                "answer": result.text,
                "status": result.status,
                "spoken": result.spoken,
            }
            self._results[request_id] = (fingerprint, response)
            while len(self._results) > 100:
                self._results.popitem(last=False)
            return web.json_response(response)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def start(self) -> None:
        if not self.token:
            raise RuntimeError("LELAMP_REMOTE_TOKEN 未配置")
        app = web.Application(client_max_size=32 * 1024)
        app.router.add_get("/healthz", self._health)
        app.router.add_post("/api/v1/agent/text", self._submit)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        print(f"LeLamp remote text ready: http://{self.host}:{self.port}", flush=True)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
