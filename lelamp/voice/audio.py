import os
import subprocess
import wave
from pathlib import Path
import numpy as np
from .config import SAMPLE_RATE, BLOCK_SAMPLES

def start_capture() -> subprocess.Popen:
    """Use ALSA directly: PortAudio does not expose this ReSpeaker's input."""
    return subprocess.Popen(
        [
            "arecord",
            "-D", os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"),
            "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", "48000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

def read_capture_block(capture: subprocess.Popen) -> np.ndarray:
    """Read 100 ms of 48 kHz stereo PCM and convert it to 16 kHz mono."""
    return read_capture_stereo_block(capture).mean(axis=1)


def read_capture_stereo_block(capture: subprocess.Popen) -> np.ndarray:
    """Read 100 ms of 48 kHz stereo PCM as normalized 16 kHz samples."""
    assert capture.stdout is not None
    raw = capture.stdout.read(48000 // 10 * 2 * 2)
    if len(raw) != 48000 // 10 * 2 * 2:
        raise RuntimeError("arecord stopped unexpectedly")
    stereo = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32)
    return (stereo[::3] / 32768.0).copy()


def select_kws_audio(stereo: np.ndarray) -> np.ndarray:
    """Select the configured KWS input without changing VAD/ASR audio."""
    source = os.getenv("KWS_AUDIO_CHANNEL", "mean").strip().lower()
    if source == "mean":
        return stereo.mean(axis=1).copy()
    if source in {"0", "channel_0"}:
        return stereo[:, 0].copy()
    if source in {"1", "channel_1"}:
        return stereo[:, 1].copy()
    raise ValueError("KWS_AUDIO_CHANNEL 必须是 mean、0 或 1")

def measure_noise(read_block, seconds: float) -> list[float]:
    """Consume capture startup audio and return centered RMS noise samples."""
    levels: list[float] = []
    block_count = max(0, round(seconds * SAMPLE_RATE / BLOCK_SAMPLES))
    for _ in range(block_count):
        block = read_block()
        centered = block - block.mean()
        levels.append(float(np.sqrt(np.mean(np.square(centered)))))
    return levels

def stop_capture(capture: subprocess.Popen) -> None:
    capture.terminate()
    try:
        capture.wait(timeout=3)
    except subprocess.TimeoutExpired:
        capture.kill()
        capture.wait()
    if capture.stdout is not None:
        capture.stdout.close()

def write_mono_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)

def scale_pcm_s16le(chunk: bytes, percent: float) -> bytes:
    """Apply configurable software speaker gain to signed 16-bit PCM."""
    if percent == 100 or not chunk:
        return chunk
    usable = len(chunk) - (len(chunk) % 2)
    samples = np.frombuffer(chunk[:usable], dtype="<i2").astype(np.float32)
    samples *= percent / 100.0
    scaled = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
    return scaled + chunk[usable:]
