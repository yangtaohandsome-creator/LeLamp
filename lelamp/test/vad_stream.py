"""Read 48 kHz S16 mono PCM on stdin and emit Silero speech transitions."""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

from ..voice.config import load_voice_config
from ..voice.vad import make_vad


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--min-speech", type=float, default=0.05)
    args = parser.parse_args()
    load_voice_config()
    # Barge-in runs under loud playback and motor noise, so it deliberately
    # uses stricter settings than the normal post-wake capture VAD. These
    # overrides live only in this subprocess.
    os.environ["VAD_MODEL_THRESHOLD"] = str(args.threshold)
    os.environ["VAD_MODEL_MIN_SPEECH_SECONDS"] = str(args.min_speech)
    detector = make_vad()
    if detector is None:
        raise RuntimeError("Silero VAD 不可用")
    print("READY", flush=True)
    active = False
    while True:
        data = sys.stdin.buffer.read(9600)  # 100 ms, 48 kHz, S16 mono
        if not data:
            break
        pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        count = round(len(pcm) / 3)
        pcm16 = np.interp(np.arange(count) * 3, np.arange(len(pcm)), pcm).astype(np.float32)
        detector.accept_waveform(pcm16)
        speech = bool(detector.is_speech_detected())
        if speech and not active:
            print("SPEECH", flush=True)
        active = speech


if __name__ == "__main__":
    main()
