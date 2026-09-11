from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from collections import deque
import numpy as np
from .config import SAMPLE_RATE, BLOCK_SAMPLES, env_float

def make_vad() -> sherpa_onnx.VoiceActivityDetector | None:
    if os.getenv("VAD_MODE", "silero").lower() != "silero":
        return None
    import sherpa_onnx
    model = Path(os.getenv("VAD_MODEL_PATH", "vad_models/silero_vad.onnx"))
    if not model.is_file():
        print(f"VAD 模型不存在，改用 RMS: {model}", file=sys.stderr, flush=True)
        return None
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(model)
    config.silero_vad.threshold = env_float("VAD_MODEL_THRESHOLD", 0.25)
    config.silero_vad.min_silence_duration = env_float(
        "VAD_MODEL_MIN_SILENCE_SECONDS", 1.5
    )
    config.silero_vad.min_speech_duration = env_float(
        "VAD_MODEL_MIN_SPEECH_SECONDS", 0.05
    )
    config.silero_vad.max_speech_duration = env_float("VAD_MAX_SECONDS", 15.0)
    config.sample_rate = SAMPLE_RATE
    config.num_threads = int(os.getenv("VAD_NUM_THREADS", "1"))
    config.provider = "cpu"
    return sherpa_onnx.VoiceActivityDetector(
        config,
        buffer_size_in_seconds=int(env_float("VAD_MAX_SECONDS", 15.0)) + 5,
    )

def capture_utterance(
    read_block,
    start_timeout: float,
    prompt: str,
    trace_block=None,
    initial_noise_levels=None,
) -> np.ndarray | None:
    """Read speech after KWS, ending after sustained silence."""
    silence_limit = int(env_float("VAD_SILENCE_SECONDS", 1.2) * SAMPLE_RATE)
    max_samples = int(env_float("VAD_MAX_SECONDS", 15.0) * SAMPLE_RATE)
    start_floor = env_float(
        "VAD_START_RMS_THRESHOLD",
        env_float("VAD_RMS_THRESHOLD", 0.008),
    )
    continue_threshold = env_float("VAD_CONTINUE_RMS_THRESHOLD", 0.007)
    noise_multiplier = env_float("VAD_NOISE_MULTIPLIER", 1.8)
    pre_roll_blocks = max(
        0,
        round(env_float("VAD_PRE_ROLL_SECONDS", 1.0) * SAMPLE_RATE / BLOCK_SAMPLES),
    )
    pre_roll: deque[np.ndarray] = deque(maxlen=pre_roll_blocks)
    initial_levels = list(initial_noise_levels or ())
    if initial_levels:
        median_noise = float(np.median(initial_levels))
        initial_levels = [
            level for level in initial_levels
            if level <= max(start_floor, median_noise * 2.0)
        ]
    noise_levels: deque[float] = deque(initial_levels, maxlen=50)
    chunks: list[np.ndarray] = []
    model_vad = make_vad()
    vad_mode = "silero" if model_vad is not None else "rms"
    heard_speech = False
    silent_samples = 0
    started_at = time.monotonic()
    end_reason = "达到单句最大时长"

    print(prompt, flush=True)
    while sum(len(x) for x in chunks) < max_samples:
        block = read_block()
        centered = block - block.mean()
        rms = float(np.sqrt(np.mean(np.square(centered))))
        noise_reference = float(np.percentile(noise_levels, 90)) if noise_levels else 0.0
        start_threshold = max(start_floor, noise_reference * noise_multiplier)
        threshold = continue_threshold if heard_speech else start_threshold
        if model_vad is not None:
            model_vad.accept_waveform(block)
            model_speech = model_vad.is_speech_detected()
            speech_signal = model_speech
        else:
            model_speech = False
            speech_signal = rms >= threshold

        if speech_signal:
            if not heard_speech:
                chunks.extend(pre_roll)
                pre_roll.clear()
            heard_speech = True
            silent_samples = 0
        elif heard_speech:
            silent_samples += len(block)
        else:
            noise_levels.append(rms)
            pre_roll.append(block)
            if time.monotonic() - started_at > start_timeout:
                print("No speech after wake word.", flush=True)
                return None

        if trace_block is not None:
            trace_block(
                {
                    "elapsed_seconds": time.monotonic() - started_at,
                    "rms": rms,
                    "start_threshold": start_threshold,
                    "active_threshold": threshold,
                    "vad_mode": vad_mode,
                    "model_speech": model_speech,
                    "heard_speech": heard_speech,
                    "silent_seconds": silent_samples / SAMPLE_RATE,
                }
            )

        if heard_speech:
            chunks.append(block)
        model_finished = model_vad is not None and not model_vad.empty()
        rms_finished = model_vad is None and silent_samples >= silence_limit
        if heard_speech and (model_finished or rms_finished):
            end_reason = "持续静音"
            break

    if not chunks:
        return None
    audio = np.concatenate(chunks)
    print(
        f"录音时长: {len(audio) / SAMPLE_RATE:.2f} 秒 | 结束原因: {end_reason} | "
        f"尾部低音量: {silent_samples / SAMPLE_RATE:.2f} 秒 | "
        f"VAD: {vad_mode} | 启动阈值: {start_threshold:.5f} | "
        f"持续阈值: {continue_threshold:.5f}",
        flush=True,
    )
    return audio
