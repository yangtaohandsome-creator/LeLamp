import re
import os
from pathlib import Path
from dotenv import load_dotenv

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 1600

def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))

def voice_debug() -> bool:
    return os.getenv("VOICE_DEBUG", "1") == "1"

def has_meaningful_text(text: str) -> bool:
    """Treat Chinese characters, letters, or digits as valid ASR text."""
    return re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text) is not None

def load_voice_config() -> None:
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    # User-facing settings take precedence over legacy settings in .env.
    load_dotenv(root / "voice.conf", override=True)
