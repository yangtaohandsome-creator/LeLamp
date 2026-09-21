"""Measure formal Edge/mpg123/aplay PCM-to-microphone acoustic latency."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from ..voice.config import load_voice_config
from ..voice.playback import PlaybackControl
from ..voice.tts import close_tts, preconnect_tts, speak


CAPTURE_RATE = 48_000
PLAYBACK_RATE = 24_000
CAPTURE_CHANNELS = 2
CAPTURE_PERIOD_MS = 20
DEFAULT_TEXT = "小灯正在测量扬声器和麦克风之间的真实延迟，请暂时保持安静。"


@dataclass(frozen=True)
class LatencyRun:
    run: int
    channel: int
    latency_ms: float
    correlation: float
    capture_origin_monotonic: float
    first_pcm_monotonic: float
    expected_start_ms: float
    detected_start_ms: float
    playback_seconds: float
    capture_seconds: float


def _write_wav(path: Path, pcm: np.ndarray, rate: int, channels: int = 1) -> None:
    data = np.asarray(pcm, dtype="<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data.tobytes())


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio.astype(np.float32, copy=True)
    count = max(1, round(len(audio) * target_rate / source_rate))
    return np.interp(
        np.arange(count, dtype=np.float64) * source_rate / target_rate,
        np.arange(len(audio), dtype=np.float64),
        audio,
    ).astype(np.float32)


def _preemphasis(audio: np.ndarray) -> np.ndarray:
    x = audio.astype(np.float64, copy=False)
    if len(x) < 2:
        return x.copy()
    return np.concatenate(([0.0], x[1:] - 0.97 * x[:-1]))


def estimate_latency(
    microphone: np.ndarray,
    reference: np.ndarray,
    *,
    rate: int,
    expected_start_samples: int,
    search_before_ms: float = 80,
    search_after_ms: float = 350,
) -> tuple[float, float, int]:
    """Return latency ms, normalized correlation and detected start sample."""
    # The first 1.5 seconds contain enough speech structure while avoiding a
    # long template that magnifies clock drift and room reverberation.
    template = reference[: min(len(reference), round(1.5 * rate))]
    if len(template) < round(0.4 * rate):
        raise ValueError("reference 太短，无法测量")
    signal = _preemphasis(microphone)
    template = _preemphasis(template)
    template -= template.mean()
    template_energy = float(np.dot(template, template))
    if template_energy <= 1e-9:
        raise ValueError("reference 没有有效能量")

    start = max(0, expected_start_samples - round(search_before_ms * rate / 1000))
    end = min(
        len(signal) - len(template),
        expected_start_samples + round(search_after_ms * rate / 1000),
    )
    if end <= start:
        raise ValueError("录音长度不足，无法搜索回声")
    segment = signal[start : end + len(template)]
    fft_size = 1 << (len(segment) + len(template) - 2).bit_length()
    convolution = np.fft.irfft(
        np.fft.rfft(segment, fft_size) * np.fft.rfft(template[::-1], fft_size),
        fft_size,
    )
    correlations = convolution[len(template) - 1 : len(template) - 1 + end - start + 1]
    squared = np.square(segment)
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    window_energy = cumulative[len(template):] - cumulative[:-len(template)]
    window_energy = window_energy[: len(correlations)]
    scores = correlations / np.sqrt(np.maximum(window_energy * template_energy, 1e-12))
    local_index = int(np.argmax(np.abs(scores)))
    detected = start + local_index
    latency_ms = (detected - expected_start_samples) * 1000 / rate
    return latency_ms, float(abs(scores[local_index])), detected


class Capture:
    def __init__(self, device: str):
        self.device = device
        self.blocks: list[bytes] = []
        self.origin: float | None = None
        self._stop = threading.Event()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                "arecord", "-q", "-D", self.device, "-t", "raw",
                "-f", "S16_LE", "-c", str(CAPTURE_CHANNELS),
                "-r", str(CAPTURE_RATE),
                "--buffer-time", "200000", "--period-time", "20000",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        size = round(CAPTURE_RATE * CAPTURE_PERIOD_MS / 1000) * CAPTURE_CHANNELS * 2
        while not self._stop.is_set():
            block = self.process.stdout.read(size)
            if len(block) != size:
                return
            received = time.perf_counter()
            if self.origin is None:
                # A block timestamp describes its end. Subtract its duration
                # to estimate the time represented by sample zero.
                self.origin = received - CAPTURE_PERIOD_MS / 1000
            self.blocks.append(block)

    def stop(self) -> np.ndarray:
        self._stop.set()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.thread is not None:
            self.thread.join(timeout=2)
        raw = b"".join(self.blocks)
        if not raw:
            error = ""
            if self.process is not None and self.process.stderr is not None:
                error = self.process.stderr.read().decode(errors="replace")
            raise RuntimeError(f"没有采集到麦克风音频: {error}")
        return np.frombuffer(raw, dtype="<i2").reshape(-1, CAPTURE_CHANNELS).copy()


def run_once(index: int, output: Path, text: str) -> list[LatencyRun]:
    capture = Capture(os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"))
    reference_parts: list[bytes] = []
    first_pcm_at: float | None = None
    control = PlaybackControl()

    def collect(pcm: bytes, rate: int, channels: int) -> None:
        nonlocal first_pcm_at
        if rate != PLAYBACK_RATE or channels != 1:
            raise RuntimeError(f"非预期播放格式: {rate} Hz, {channels} channels")
        if first_pcm_at is None:
            first_pcm_at = time.perf_counter()
        reference_parts.append(pcm)

    control.on_pcm = collect
    capture.start()
    time.sleep(0.6)
    metrics = speak(text, control=control)
    time.sleep(0.6)
    microphone = capture.stop()
    if capture.origin is None or first_pcm_at is None or not reference_parts:
        raise RuntimeError("没有获得完整的播放/采集时间点")
    reference_i16 = np.frombuffer(b"".join(reference_parts), dtype="<i2").copy()
    reference_48 = _resample(reference_i16.astype(np.float32), PLAYBACK_RATE, CAPTURE_RATE)
    expected_start = round((first_pcm_at - capture.origin) * CAPTURE_RATE)

    run_dir = output / f"run_{index:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_wav(run_dir / "reference_pcm.wav", reference_i16, PLAYBACK_RATE)
    _write_wav(run_dir / "raw_mic_stereo.wav", microphone, CAPTURE_RATE, CAPTURE_CHANNELS)

    results = []
    for channel in range(CAPTURE_CHANNELS):
        latency, score, detected = estimate_latency(
            microphone[:, channel].astype(np.float32), reference_48,
            rate=CAPTURE_RATE, expected_start_samples=expected_start,
        )
        results.append(LatencyRun(
            run=index,
            channel=channel,
            latency_ms=latency,
            correlation=score,
            capture_origin_monotonic=capture.origin,
            first_pcm_monotonic=first_pcm_at,
            expected_start_ms=expected_start * 1000 / CAPTURE_RATE,
            detected_start_ms=detected * 1000 / CAPTURE_RATE,
            playback_seconds=metrics[2],
            capture_seconds=len(microphone) / CAPTURE_RATE,
        ))
    (run_dir / "metrics.json").write_text(
        json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.runs <= 30:
        raise ValueError("runs 必须在 1～30")
    load_voice_config()
    if os.getenv("TTS_BACKEND", "").strip().lower() != "edge":
        raise RuntimeError("该诊断必须使用正式 Edge TTS backend")
    root = args.output or Path("voice_debug/tts_latency") / datetime.now().strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    print(f"TTS ACOUSTIC LATENCY TEST | runs={args.runs} | output={root}", flush=True)
    preconnect_tts()
    time.sleep(2.0)
    all_results: list[LatencyRun] = []
    try:
        for index in range(1, args.runs + 1):
            results = run_once(index, root, args.text)
            all_results.extend(results)
            print(
                f"RUN {index}: " + " | ".join(
                    f"ch{r.channel}={r.latency_ms:.2f}ms score={r.correlation:.3f}"
                    for r in results
                ),
                flush=True,
            )
            time.sleep(0.8)
    finally:
        close_tts()

    summary = {"runs": args.runs, "channels": {}}
    for channel in range(CAPTURE_CHANNELS):
        rows = [item for item in all_results if item.channel == channel]
        values = np.array([item.latency_ms for item in rows])
        scores = np.array([item.correlation for item in rows])
        summary["channels"][str(channel)] = {
            "median_ms": float(np.median(values)),
            "p05_ms": float(np.percentile(values, 5)),
            "p95_ms": float(np.percentile(values, 95)),
            "min_ms": float(values.min()),
            "max_ms": float(values.max()),
            "median_correlation": float(np.median(scores)),
        }
    (root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
