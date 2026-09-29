#!/usr/bin/env python3
"""Compare Silero barge-in parameters on the fixed V0 dataset.

This is a tuning aid, not a claim that the small positive set is sufficient.
For an already-triggered record, only its 0.5 s AEC-clean pre-roll is replayed;
audio after cancellation is raw microphone and must not be used for detection.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_barge_in_dataset import load_manifest, read_mono, replay_vad  # noqa: E402
from lelamp.voice.config import load_voice_config  # noqa: E402


def values(text: str) -> list[float]:
    return [float(item) for item in text.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/barge_in/dataset.jsonl")
    parser.add_argument("--data-root", type=Path, default=ROOT / "voice_debug")
    parser.add_argument("--thresholds", default="0.20,0.25,0.30,0.35,0.40,0.45,0.50,0.60")
    parser.add_argument("--min-speech", default="0.05,0.10,0.15,0.20,0.25,0.30,0.40")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    load_voice_config()

    samples = []
    for row in load_manifest(args.manifest):
        if row["evaluation"] == "vad_replay":
            path = args.data_root / row["audio"]["clean"]
            samples.append((row, path, None, False))
        elif row["evaluation"] == "retention_record" and row.get("scored"):
            path = args.data_root / row["audio"]["capture"]
            expected = row["label"] in {"correct_interrupt", "missed_interrupt"}
            samples.append((row, path, 0.5, expected))

    missing = [str(path) for _, path, _, _ in samples if not path.is_file()]
    if missing:
        print("缺少音频：", *missing, sep="\n", file=sys.stderr)
        return 2

    # Avoid decoding the same WAV for every parameter pair.
    prepared = []
    for row, path, limit, expected in samples:
        audio, rate = read_mono(path)
        if limit is not None:
            audio = audio[: round(rate * limit)]
        prepared.append((row, audio, rate, expected))

    matrix = []
    for threshold in values(args.thresholds):
        for minimum in values(args.min_speech):
            predictions = []
            for row, audio, rate, expected in prepared:
                # replay_vad accepts a path, so use its core logic here with a
                # temporary in-memory resample avoided by data already being
                # standard 16/48 kHz.  A tiny WAV-free helper keeps this scan fast.
                from lelamp.voice.vad import make_vad
                from lelamp.voice.config import SAMPLE_RATE
                if rate != SAMPLE_RATE:
                    size = round(len(audio) * SAMPLE_RATE / rate)
                    current = np.interp(
                        np.linspace(0, len(audio) - 1, size),
                        np.arange(len(audio)), audio,
                    ).astype(np.float32)
                else:
                    current = audio
                vad = make_vad(threshold=threshold, min_speech_seconds=minimum)
                triggered = False
                latency = None
                block = SAMPLE_RATE // 10
                for offset in range(0, len(current), block):
                    chunk = current[offset:offset + block]
                    if len(chunk) < block:
                        chunk = np.pad(chunk, (0, block - len(chunk)))
                    vad.accept_waveform(chunk)
                    if vad.is_speech_detected():
                        triggered = True
                        latency = (offset + block) / SAMPLE_RATE
                        break
                predictions.append((row, expected, triggered, latency))
            positives = [item for item in predictions if item[1]]
            negatives = [item for item in predictions if not item[1]]
            current_delay_negatives = [
                item for item in negatives
                if item[0].get("recorded_delay_ms", 80) == 80
            ]
            row = {
                "threshold": threshold,
                "min_speech_seconds": minimum,
                "true_positives": sum(item[2] for item in positives),
                "positive_samples": len(positives),
                "false_positives": sum(item[2] for item in negatives),
                "negative_samples": len(negatives),
                "false_positives_80ms": sum(item[2] for item in current_delay_negatives),
                "negative_samples_80ms": len(current_delay_negatives),
                "mean_positive_trigger_seconds": float(np.mean([
                    item[3] for item in positives if item[2]
                ])) if any(item[2] for item in positives) else None,
            }
            matrix.append(row)

    baseline = next(
        row for row in matrix
        if row["threshold"] == 0.40 and row["min_speech_seconds"] == 0.20
    )
    # A candidate dominates baseline only when it keeps at least the same
    # positive recall and does not add false positives, improving one metric.
    dominating = [
        row for row in matrix
        if row["true_positives"] >= baseline["true_positives"]
        and row["false_positives"] <= baseline["false_positives"]
        and (
            row["true_positives"] > baseline["true_positives"]
            or row["false_positives"] < baseline["false_positives"]
        )
    ]
    report = {"baseline": baseline, "dominates_baseline": dominating, "matrix": matrix}
    print(json.dumps({"baseline": baseline, "dominates_baseline": dominating}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整网格结果: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
