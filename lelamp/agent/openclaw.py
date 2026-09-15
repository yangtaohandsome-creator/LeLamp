"""Client for the OpenClaw agent Gateway."""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


class OpenClawError(RuntimeError):
    pass


def build_messages(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    city = os.getenv("LELAMP_LOCATION_CITY", "").strip()
    timezone_id = os.getenv("LELAMP_TIMEZONE", "").strip()
    if city:
        local_time = "未知"
        if timezone_id:
            try:
                local_time = datetime.now(ZoneInfo(timezone_id)).isoformat(timespec="seconds")
            except ZoneInfoNotFoundError:
                pass
        messages.append({
            "role": "system",
            "content": (
                f"LeLamp 启动时通过公网 IP 定位到的当前城市是 {city}，"
                f"当地 IANA 时区是 {timezone_id or '未知'}，当前当地时间是 {local_time}。"
                "用户询问本地、这里或未指定地点的天气等位置相关信息时，使用这个城市；"
                "用户明确指定其他地点时，以用户指定地点为准。"
            ),
        })
    messages.append({"role": "user", "content": text})
    return messages


async def ask_agent(text: str, session_id: str) -> str:
    base_url = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789").rstrip("/")
    token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        raise OpenClawError("OPENCLAW_GATEWAY_TOKEN 未配置")
    payload = {
        "model": os.getenv("OPENCLAW_MODEL", "openclaw/lelamp"),
        "messages": build_messages(text),
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
