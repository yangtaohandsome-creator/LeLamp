import os
import subprocess
import time
import httpx
from .config import env_float
from .audio import scale_pcm_s16le

def speak(text: str, on_playback_start=None) -> tuple[float, float, float]:
    tts_url = os.getenv("TTS_URL", "http://192.168.40.209:8200/v1/tts/stream")
    request_started = time.perf_counter()
    player = None
    first_chunk_at = None
    last_chunk_at = None
    total_bytes = 0
    sample_rate = 44100
    channels = 1
    try:
        with httpx.stream(
            "POST",
            tts_url,
            json={"text": text, "format": "pcm"},
            timeout=90,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            sample_format = response.headers.get("x-sample-format", "s16le").lower()
            if "audio" not in content_type or sample_format != "s16le":
                raise ValueError(
                    f"TTS returned unsupported audio: {content_type}, {sample_format}"
                )
            sample_rate = int(response.headers.get("x-sample-rate", "44100"))
            channels = int(response.headers.get("x-channels", "1"))
            player = subprocess.Popen(
                [
                    "aplay", "-q", "-D",
                    os.getenv("APLAY_DEVICE", "plughw:seeed2micvoicec,0"),
                    "-t", "raw", "-f", "S16_LE", "-c", str(channels),
                    "-r", str(sample_rate),
                ],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert player.stdin is not None
            volume_percent = env_float("APLAY_VOLUME_PERCENT", 100.0)
            if not 0 < volume_percent <= 200:
                raise ValueError("APLAY_VOLUME_PERCENT 必须在 1～200 之间")
            for chunk in response.iter_bytes(chunk_size=4096):
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                chunk = scale_pcm_s16le(chunk, volume_percent)
                player.stdin.write(chunk)
                player.stdin.flush()
                if on_playback_start is not None:
                    callback, on_playback_start = on_playback_start, None
                    callback()
                total_bytes += len(chunk)
                last_chunk_at = time.perf_counter()
            player.stdin.close()
    finally:
        if player is not None:
            if player.stdin is not None and not player.stdin.closed:
                player.stdin.close()
            if player.wait(timeout=120) != 0:
                raise RuntimeError("aplay failed while streaming TTS audio")
    first = first_chunk_at or request_started
    last = last_chunk_at or first
    return (
        first - request_started,
        last - first,
        total_bytes / (sample_rate * channels * 2),
    )
