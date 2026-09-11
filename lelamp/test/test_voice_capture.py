"""Capture one utterance and save VAD diagnostics without KWS/LLM/TTS."""

import argparse
import csv
import os
import re
import time
from pathlib import Path

import numpy as np

from lelamp.voice_assistant import (
    SAMPLE_RATE,
    capture_utterance,
    env_float,
    load_voice_config,
    measure_noise,
    read_capture_block,
    start_capture,
    stop_capture,
    write_mono_wav,
)


def safe_label(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")
    return cleaned or "test"


def main() -> None:
    parser = argparse.ArgumentParser(description="LeLamp 麦克风与 VAD 单轮诊断")
    parser.add_argument("--label", default="test", help="本轮文件标签")
    parser.add_argument("--timeout", type=float, default=15.0, help="等待开口秒数")
    args = parser.parse_args()

    load_voice_config()
    output_dir = Path(os.getenv("VOICE_DIAGNOSTIC_DIR", "voice_debug/vad_tests"))
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_label(args.label)}"
    rows: list[dict[str, float | bool]] = []

    def trace(row: dict[str, float | bool]) -> None:
        rows.append(row)
        state = "SPEECH" if row["heard_speech"] else "WAIT"
        print(
            f"VAD {row['elapsed_seconds']:05.2f}s | RMS={row['rms']:.5f} | "
            f"START={row['start_threshold']:.5f} | ACTIVE={row['active_threshold']:.5f} | "
            f"{state} | MODE={row['vad_mode']} | MODEL={row['model_speech']} | "
            f"SILENCE={row['silent_seconds']:.2f}s",
            flush=True,
        )

    capture = start_capture()
    try:
        calibration_seconds = env_float("VAD_DIAGNOSTIC_CALIBRATION_SECONDS", 1.0)
        noise_levels = measure_noise(
            lambda: read_capture_block(capture),
            calibration_seconds,
        )
        if noise_levels:
            print(
                f"NOISE CALIBRATION {calibration_seconds:.2f}s | "
                f"median={np.median(noise_levels):.5f} | "
                f"p90={np.percentile(noise_levels, 90):.5f} | "
                f"max={max(noise_levels):.5f}",
                flush=True,
            )
        print(f"TEST READY: {args.label}（现在可以说话）", flush=True)
        audio = capture_utterance(
            lambda: read_capture_block(capture),
            args.timeout,
            "Listening for diagnostic utterance...",
            trace_block=trace,
            initial_noise_levels=noise_levels,
        )
    finally:
        stop_capture(capture)

    csv_path = prefix.with_suffix(".csv")
    with csv_path.open("w", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "elapsed_seconds",
                "rms",
                "start_threshold",
                "active_threshold",
                "vad_mode",
                "model_speech",
                "heard_speech",
                "silent_seconds",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    if audio is None:
        print(f"RESULT: NO SPEECH | VAD 日志: {csv_path.resolve()}", flush=True)
        return

    capture_path = prefix.with_name(prefix.name + "_capture.wav")
    asr_path = prefix.with_name(prefix.name + "_asr_input.wav")
    write_mono_wav(capture_path, audio)
    padding = env_float("ASR_TAIL_SILENCE_SECONDS", 0.5)
    padded = np.concatenate([audio, np.zeros(round(padding * SAMPLE_RATE), dtype=np.float32)])
    write_mono_wav(asr_path, padded)
    print(f"RESULT: CAPTURED {len(audio) / SAMPLE_RATE:.2f}s", flush=True)
    print(f"原始录音: {capture_path.resolve()}", flush=True)
    print(f"ASR 输入: {asr_path.resolve()}", flush=True)
    print(f"VAD 日志: {csv_path.resolve()}", flush=True)


if __name__ == "__main__":
    main()
