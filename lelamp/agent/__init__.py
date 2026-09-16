"""Thin, selectable Agent backend used by LampApp."""
from __future__ import annotations

import os

from .common import AgentError


async def ask_agent(text: str, session_id: str) -> str:
    if os.getenv("AGENT_BACKEND", "openclaw").strip().lower() == "pi":
        from .pi import ask_agent as ask
    else:
        from .openclaw import ask_agent as ask
    return await ask(text, session_id)


async def clear_session(session_id: str) -> None:
    if os.getenv("AGENT_BACKEND", "openclaw").strip().lower() != "pi":
        return
    from .pi import clear_session as clear
    await clear(session_id)


__all__ = ["AgentError", "ask_agent", "clear_session"]
