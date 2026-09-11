import os
from pathlib import Path
import httpx

def load_agent_prompt() -> str:
    """Load OpenClaw-style identity files from the runtime root."""
    root = Path(__file__).resolve().parents[2]
    sections = []
    for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md"):
        path = root / name
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            sections.append(f"# {name}\n{content}")
    if sections:
        return "\n\n".join(sections)
    return os.getenv(
        "LLM_SYSTEM_PROMPT",
        "你是老灯。用中文简短回答，最多两句话。",
    )

def ask_llm(text: str, history: list[dict[str, str]]) -> str:
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    api_key = os.environ["OPENAI_API_KEY"]
    model = os.getenv("LLM_MODEL", "qwen3.7-flash")
    history_turns = int(os.getenv("CONVERSATION_HISTORY_TURNS", "6"))
    messages = [
        {
            "role": "system",
            "content": load_agent_prompt(),
        },
        *history[-2 * history_turns:],
        {"role": "user", "content": text},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "enable_thinking": os.getenv("LLM_ENABLE_THINKING", "0") == "1",
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", "64")),
    }
    response = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()
