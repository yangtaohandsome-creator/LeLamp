"""Client for the local Pi Agent Core service."""
from __future__ import annotations

import os

import httpx

from .common import AgentError, sanitize_spoken_content
from .openclaw import build_messages


async def ask_agent(text: str, session_id: str) -> str:
    base_url = os.getenv("PI_AGENT_URL", "http://127.0.0.1:18792").rstrip("/")
    token = os.getenv("PI_AGENT_TOKEN", "")
    if not token:
        raise AgentError("PI_AGENT_TOKEN 未配置")
    payload = {
        "session_id": session_id,
        "text": text,
        "context": build_messages(text)[0]["content"],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base_url}/v1/chat",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        content = body["answer"]
        if not isinstance(content, str):
            raise AgentError("Pi Agent 返回了不支持的消息格式")
        metrics = body.get("metrics")
        if isinstance(metrics, dict):
            model_calls = metrics.get("model_call_details", [])
            tools = metrics.get("tool_calls", [])
            model_summary = ", ".join(
                f"#{item.get('call')} 首字 {float(item.get('first_token_ms', 0)) / 1000:.2f}s/完整 {float(item.get('duration_ms', 0)) / 1000:.2f}s"
                for item in model_calls if isinstance(item, dict)
            )
            tool_summary = ", ".join(
                f"{item.get('name')} {float(item.get('duration_ms', 0)) / 1000:.2f}s"
                for item in tools if isinstance(item, dict)
            )
            print(
                "PI AGENT 分段 | "
                f"模型: {model_summary or '无'} | "
                f"工具: {tool_summary or '无'} | "
                f"总计: {float(metrics.get('total_ms', 0)) / 1000:.2f}s",
                flush=True,
            )
        return sanitize_spoken_content(content)
    except AgentError:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise AgentError(f"Pi Agent 调用失败: {exc}") from exc


async def clear_session(session_id: str) -> None:
    if not session_id:
        return
    base_url = os.getenv("PI_AGENT_URL", "http://127.0.0.1:18792").rstrip("/")
    token = os.getenv("PI_AGENT_TOKEN", "")
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.delete(
                f"{base_url}/v1/sessions/{session_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError:
        return
