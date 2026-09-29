"""Compare ReSpeaker input channels for the local 小灯 wake-word model.

This program intentionally does not start VAD, ASR, Agent, TTS, lighting, or
motion.  It feeds the same microphone audio to three independent KWS streams:
channel 0, channel 1 and the average currently used by the main application.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

from ..voice.audio import read_capture_stereo_block, start_capture, stop_capture, write_mono_wav
from ..voice.config import SAMPLE_RATE, load_voice_config
from ..voice.kws import make_spotter


def decode(spotter, stream, samples: np.ndarray) -> str:
    stream.accept_waveform(SAMPLE_RATE, samples)
    while spotter.is_ready(stream):
        spotter.decode_stream(stream)
    return str(spotter.get_result(stream) or "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare KWS channel 0, channel 1 and mean input")
    parser.add_argument("--duration", type=float, default=90.0, help="recording duration in seconds")
    parser.add_argument("--output-dir", default="voice_debug/kws_diagnostic")
    args = parser.parse_args()
    if args.duration <= 0:
        raise ValueError("--duration 必须大于 0")

    load_voice_config()
    model_dir = Path(os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"))
    keywords_file = Path(os.getenv("KWS_KEYWORDS_FILE", str(model_dir / "keywords_xiao_deng.txt")))
    names = ("channel_0", "channel_1", "mean")
    spotters = {name: make_spotter(model_dir, keywords_file) for name in names}
    streams = {name: spotter.create_stream() for name, spotter in spotters.items()}
    hits = {name: [] for name in names}
    audio = {name: [] for name in names}
    output = Path(args.output_dir) / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)

    print("KWS DIAGNOSTIC READY", flush=True)
    print("请在 %d 秒内自然说 20 次“小灯”，每次间隔约 1 秒。Ctrl-C 可提前结束。" % args.duration, flush=True)
    print("会同时比较 channel_0、channel_1 和 mean；不启动 ASR、TTS 或舵机。", flush=True)
    capture = start_capture()
    started = time.monotonic()
    next_level_log = started
    try:
        while time.monotonic() - started < args.duration:
            stereo = read_capture_stereo_block(capture)
            samples = {
                "channel_0": stereo[:, 0].copy(),
                "channel_1": stereo[:, 1].copy(),
                "mean": stereo.mean(axis=1).copy(),
            }
            elapsed = time.monotonic() - started
            for name, block in samples.items():
                audio[name].append(block)
                result = decode(spotters[name], streams[name], block)
                if result:
                    hits[name].append({"seconds": round(elapsed, 2), "result": result})
                    print(f"HIT {name}: {result} | {elapsed:.2f} 秒", flush=True)
                    spotters[name].reset_stream(streams[name])
            if time.monotonic() >= next_level_log:
                rms = {
                    name: float(np.sqrt(np.mean(np.square(block - block.mean()))))
                    for name, block in samples.items()
                }
                print("RMS " + " | ".join(f"{name}={value:.5f}" for name, value in rms.items()), flush=True)
                next_level_log += 1.0
    except KeyboardInterrupt:
        print("诊断提前停止。", flush=True)
    finally:
        stop_capture(capture)

    summary = {"duration_seconds": round(time.monotonic() - started, 2), "hits": hits}
    for name, chunks in audio.items():
        write_mono_wav(output / f"{name}.wav", np.concatenate(chunks) if chunks else np.array([], dtype=np.float32))
        summary[f"{name}_hit_count"] = len(hits[name])
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print("诊断结果：" + " | ".join(f"{name}={len(hits[name])}" for name in names), flush=True)
    print(f"录音和统计已保存到: {output}", flush=True)


if __name__ == "__main__":
    main()
