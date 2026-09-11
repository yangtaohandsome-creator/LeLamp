"""Client for the OpenClaw agent Gateway."""
from __future__ import annotations

import os

import httpx


class OpenClawError(RuntimeError):
    pass


async def ask_agent(text: str, session_id: str) -> str:
    base_url = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        raise OpenClawError("OPENCLAW_GATEWAY_TOKEN 未配置")
    payload = {
        "model": os.getenv("OPENCLAW_MODEL", "openclaw/lelamp"),
        "messages": [{"role": "user", "content": text}],
        "user": f"lelamp-{session_id}",
        "stream": False,
        # This cap covers OpenClaw's complete agent/tool turn, not only visible text.
        "max_tokens": int(os.getenv("OPENCLAW_MAX_TOKENS", "512")),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-openclaw-agent-id": os.getenv("OPENCLAW_AGENT_ID", "lelamp"),
                },
                json=payload,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"].get("content", "")
        if not isinstance(content, str):
            raise OpenClawError("OpenClaw 返回了不支持的消息格式")
        return content.strip()
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenClawError(f"OpenClaw 调用失败: {exc}") from exc
