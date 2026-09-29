#!/usr/bin/env python3
"""Evaluate the fixed LeLamp barge-in audio manifest.

Audio stays under ``voice_debug`` and is intentionally not committed.  The
tracked manifest gives every sample a stable label and relative path.
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import Counter
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lelamp.voice.config import SAMPLE_RATE, load_voice_config  # noqa: E402
from lelamp.voice.vad import make_vad  # noqa: E402


def load_manifest(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"只支持 16-bit PCM: {path}")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples / 32768.0, rate


def resample(audio: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == SAMPLE_RATE:
        return audio
    duration = len(audio) / source_rate
    size = round(duration * SAMPLE_RATE)
    if not size or not len(audio):
        return np.empty(0, dtype=np.float32)
    positions = np.linspace(0, len(audio) - 1, size)
    return np.interp(positions, np.arange(len(audio)), audio).astype(np.float32)


def replay_vad(path: Path, threshold: float, min_speech: float) -> tuple[bool, float | None]:
    audio, rate = read_mono(path)
    audio = resample(audio, rate)
    vad = make_vad(threshold=threshold, min_speech_seconds=min_speech)
    if vad is None:
        raise RuntimeError("Silero VAD 不可用")
    block_samples = SAMPLE_RATE // 10
    for offset in range(0, len(audio), block_samples):
        block = audio[offset:offset + block_samples]
        if len(block) < block_samples:
            block = np.pad(block, (0, block_samples - len(block)))
        vad.accept_waveform(block)
        if vad.is_speech_detected():
            return True, (offset + len(block)) / SAMPLE_RATE
    return False, None


def main() -> int:
    parser = argparse.ArgumentParser(description="小灯 Barge-in 固定数据集评测")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "benchmarks/barge_in/dataset.jsonl",
    )
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "voice_debug")
    parser.add_argument("--threshold", type=float, default=0.40)
    parser.add_argument("--min-speech", type=float, default=0.20)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    load_voice_config()
    rows = load_manifest(args.manifest)
    labels = Counter(row["label"] for row in rows)
    missing: list[str] = []
    for row in rows:
        for relative in row.get("audio", {}).values():
            if not (args.data_root / relative).is_file():
                missing.append(f"{row['id']}: {relative}")
        trace = row.get("asr_trace")
        if trace and not (args.data_root / trace).is_file():
            missing.append(f"{row['id']}: {trace}")

    print(f"样本总数: {len(rows)} | {dict(labels)}")
    if missing:
        print(f"缺失文件: {len(missing)}")
        for item in missing:
            print(f"  {item}")
        return 2
    print("清单文件检查通过。")
    if args.validate_only:
        return 0

    results = []
    for row in rows:
        if not row.get("scored") or row["evaluation"] != "vad_replay":
            continue
        clean = args.data_root / row["audio"]["clean"]
        triggered, latency = replay_vad(clean, args.threshold, args.min_speech)
        results.append({
            "id": row["id"],
            "label": row["label"],
            "recorded_delay_ms": row.get("recorded_delay_ms"),
            "triggered": triggered,
            "trigger_latency_seconds": latency,
        })

    negatives = [item for item in results if item["label"] == "no_interrupt"]
    false_positives = [item for item in negatives if item["triggered"]]
    retention = [
        row for row in rows
        if row.get("scored") and row["evaluation"] == "retention_record"
    ]
    by_delay = {}
    for delay in sorted({item["recorded_delay_ms"] for item in negatives}):
        group = [item for item in negatives if item["recorded_delay_ms"] == delay]
        false_count = sum(item["triggered"] for item in group)
        by_delay[str(int(delay))] = {
            "samples": len(group),
            "false_interrupts": false_count,
            "false_interrupt_rate": false_count / len(group),
        }
    report = {
        "configuration": {
            "threshold": args.threshold,
            "min_speech_seconds": args.min_speech,
        },
        "vad_replay": {
            "no_interrupt_samples": len(negatives),
            "false_interrupts": len(false_positives),
            "false_interrupt_rate": (
                len(false_positives) / len(negatives) if negatives else None
            ),
            "false_interrupt_ids": [item["id"] for item in false_positives],
            "by_recorded_delay_ms": by_delay,
        },
        "retention_inventory": {
            "samples": len(retention),
            "correct_with_text": sum(
                row["label"] == "correct_interrupt" and bool(row.get("observed_asr"))
                for row in retention
            ),
            "detected_but_empty_asr": sum(
                row["label"] == "missed_interrupt" and not row.get("observed_asr")
                for row in retention
            ),
            "archived_false_interrupts": sum(
                row["label"] == "false_interrupt" for row in retention
            ),
        },
        "results": results,
    }
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整结果: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
