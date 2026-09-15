"""Playback for short, local WAV cues without business decisions."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
import wave

from dotenv import load_dotenv

from ..voice.audio import scale_pcm_s16le


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "sound.conf"
SOUND_DIR = Path(__file__).resolve().parent / "sounds"


class SoundPlayer:
    """Resolve configured cue names and play PCM WAV files through ALSA."""

    def __init__(self) -> None:
        load_dotenv(CONFIG_PATH, override=True)

    def cue_path(self, cue: str) -> Path | None:
        enabled = os.getenv("SOUND_ENABLED", "0").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return None
        filename = os.getenv(f"SOUND_{cue.upper()}", "").strip()
        if not filename:
            return None
        path = (SOUND_DIR / filename).resolve()
        if path.parent != SOUND_DIR.resolve():
            raise ValueError(f"提示音路径不合法: {filename}")
        if not path.is_file():
            raise FileNotFoundError(f"找不到提示音: {path}")
        return path

    def has_cue(self, cue: str) -> bool:
        return self.cue_path(cue) is not None

    def play(self, cue: str) -> float:
        """Play one configured cue and return elapsed playback seconds."""
        path = self.cue_path(cue)
        if path is None:
            return 0.0
        with wave.open(str(path), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                raise ValueError("提示音必须是未压缩的 16-bit PCM WAV")
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            pcm = wav.readframes(wav.getnframes())
        volume = float(os.getenv("SOUND_VOLUME_PERCENT", "70"))
        if not 0 < volume <= 200:
            raise ValueError("SOUND_VOLUME_PERCENT 必须在 1～200 之间")
        pcm = scale_pcm_s16le(pcm, volume)
        started = time.perf_counter()
        result = subprocess.run(
            [
                "aplay", "-q", "-D",
                os.getenv("APLAY_DEVICE", "plughw:seeed2micvoicec,0"),
                "-t", "raw", "-f", "S16_LE", "-c", str(channels),
                "-r", str(sample_rate),
            ],
            input=pcm,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"提示音播放失败: {cue}")
        return time.perf_counter() - started
