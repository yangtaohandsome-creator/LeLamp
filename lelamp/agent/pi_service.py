"""Start the local Pi Agent when the lamp app needs it."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

import httpx


class PiAgentService:
    def __init__(self) -> None:
        self.url = os.getenv("PI_AGENT_URL", "http://127.0.0.1:18792").rstrip("/")
        self.token = os.getenv("PI_AGENT_TOKEN", "")
        self.process: subprocess.Popen | None = None
        self._lock = asyncio.Lock()

    async def _healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=0.8) as client:
                response = await client.get(
                    self.url + "/health",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            if response.status_code in (401, 403):
                raise RuntimeError("Pi Agent 鉴权失败，请检查 .env 中的 PI_AGENT_TOKEN")
            return response.status_code == 200 and response.json().get("ok") is True
        except (httpx.RequestError, ValueError):
            return False

    async def ensure_ready(self) -> None:
        if not self.token:
            raise RuntimeError("PI_AGENT_TOKEN 未配置")
        if await self._healthy():
            return
        async with self._lock:
            if await self._healthy():
                return
            parsed = urlparse(self.url)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise RuntimeError("远程 Pi Agent 不可达；不会在本机启动替代进程")
            if self.process is not None and self.process.poll() is None:
                # Only restart a process started by this app. An external Agent
                # may serve another client and must never be terminated here.
                await self.close()
            script = Path(__file__).resolve().parents[2] / "pi-agent" / "run.sh"
            if not script.is_file():
                raise RuntimeError(f"Pi Agent 启动脚本不存在: {script}")
            with Path("/tmp/lelamp-pi-agent.log").open("ab") as log:
                self.process = subprocess.Popen(
                    ["bash", str(script)], cwd=script.parent.parent,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                )
            try:
                for _ in range(40):
                    if await self._healthy():
                        print("Pi Agent 已自动连接", flush=True)
                        return
                    if self.process.poll() is not None:
                        raise RuntimeError("Pi Agent 启动后退出，请查看 /tmp/lelamp-pi-agent.log")
                    await asyncio.sleep(0.25)
                raise RuntimeError("Pi Agent 10 秒内未就绪，请查看 /tmp/lelamp-pi-agent.log")
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)
