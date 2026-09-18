import os
import subprocess
import time
import httpx
from .config import env_float
from .audio import scale_pcm_s16le
from .playback import PlaybackControl, SpeechInterrupted

_edge_client = None
_edge_lock = __import__("threading").Lock()


def _get_edge_client():
    global _edge_client
    with _edge_lock:
        if _edge_client is None:
            from .edge_tts import EdgeSpeechClient
            _edge_client = EdgeSpeechClient()
        return _edge_client


def preconnect_tts() -> None:
    """Speculatively prepare the selected online backend without blocking."""
    if os.getenv("TTS_BACKEND", "remote").strip().lower() != "edge":
        return
    try:
        _get_edge_client().preconnect()
    except Exception as exc:
        print(f"Edge TTS 预连接启动失败，将使用远程 TTS: {exc}", flush=True)


def close_tts() -> None:
    global _edge_client
    with _edge_lock:
        client, _edge_client = _edge_client, None
    if client is not None:
        client.close()

def _speak_remote(
    text: str,
    on_playback_start=None,
    control: PlaybackControl | None = None,
) -> tuple[float, float, float]:
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
            if control is not None:
                control.format(sample_rate, channels)
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
                if control is not None and control.cancelled:
                    raise SpeechInterrupted
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                chunk = scale_pcm_s16le(chunk, volume_percent)
                if control is not None:
                    control.pcm(chunk, sample_rate, channels)
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
            cancelled = control is not None and control.cancelled
            if cancelled and player.poll() is None:
                player.terminate()
            if player.stdin is not None and not player.stdin.closed:
                try:
                    player.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            if player.wait(timeout=120) != 0 and not (
                cancelled
            ):
                raise RuntimeError("aplay failed while streaming TTS audio")
    if control is not None and control.cancelled:
        raise SpeechInterrupted
    first = first_chunk_at or request_started
    last = last_chunk_at or first
    return (
        first - request_started,
        last - first,
        total_bytes / (sample_rate * channels * 2),
    )


def speak(
    text: str,
    on_playback_start=None,
    control: PlaybackControl | None = None,
) -> tuple[float, float, float]:
    backend = os.getenv("TTS_BACKEND", "remote").strip().lower()
    if backend == "remote":
        if control is None:
            return _speak_remote(text, on_playback_start)
        return _speak_remote(text, on_playback_start, control)
    if backend != "edge":
        raise ValueError(f"不支持的 TTS_BACKEND: {backend}")
    try:
        if control is None:
            return _get_edge_client().speak(text, on_playback_start)
        return _get_edge_client().speak(text, on_playback_start, control)
    except SpeechInterrupted:
        raise
    except Exception as exc:
        fallback = os.getenv("TTS_FALLBACK_BACKEND", "remote").strip().lower()
        if fallback != "remote":
            raise
        print(f"Edge TTS 不可用，回退远程 TTS: {exc}", flush=True)
        if control is None:
            return _speak_remote(text, on_playback_start)
        return _speak_remote(text, on_playback_start, control)
