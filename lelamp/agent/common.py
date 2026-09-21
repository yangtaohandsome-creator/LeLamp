"""Shared Agent errors and spoken-output safety for every backend."""
from __future__ import annotations

import re


class AgentError(RuntimeError):
    pass


class AgentConnectionError(AgentError):
    """Connection never reached the Agent; one retry cannot repeat tool actions."""


SPOKEN_OUTPUT_RULE = (
    "你的最终 content 会直接由台灯朗读。只输出给用户听的最终台词，"
    "严禁输出分析、推理、候选解释、规则引用或工具选择过程。"
    "需要执行工具时必须真实调用工具，不能在 content 中描述准备调用；"
    "工具没有实际成功时，只能简短说明没有执行成功。"
)
_RETRY_PROMPT = "刚才没听清，你再说一遍？"
_EXECUTION_FAILED_PROMPT = "这次动作没执行成功，你再说一遍。"
_ANALYSIS_MARKERS = (
    "用户输入", "结合上下文", "考虑到", "按照规则", "不应该擅自",
    "不需要调用", "需要澄清", "最有可能", "直接回答要求", "NO_REPLY",
    "The user says", "voice recognition error", "According to the rules",
    "I should", "Since the user", "Let me call", "This is a request",
)
_TOOL_NAME_RE = re.compile(r"\blelamp_[a-z0-9_]+\b", re.IGNORECASE)


def sanitize_spoken_content(content: str) -> str:
    """Remove reasoning accidentally returned in user-visible content."""
    cleaned = re.sub(
        r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE
    ).strip()
    marker_count = sum(marker in cleaned for marker in _ANALYSIS_MARKERS)
    if _RETRY_PROMPT in cleaned and marker_count:
        return _RETRY_PROMPT
    if _TOOL_NAME_RE.search(cleaned) or "NO_REPLY" in cleaned:
        return _EXECUTION_FAILED_PROMPT
    if marker_count >= 2:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
        if paragraphs:
            final = paragraphs[-1]
            if (
                len(final) <= 200
                and not any(marker in final for marker in _ANALYSIS_MARKERS)
                and not _TOOL_NAME_RE.search(final)
            ):
                return final
        return _EXECUTION_FAILED_PROMPT
    return cleaned
