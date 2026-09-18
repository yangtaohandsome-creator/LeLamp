"""Submit one WAV file through LeLamp's existing ASR client."""
from __future__ import annotations

import argparse
import asyncio
import wave

import numpy as np

from ..voice.asr import transcribe


def read_mono_16k(path):
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        if wav.getsampwidth() != 2:
            raise ValueError("只支持 S16_LE WAV")
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    audio = audio.reshape(-1, channels).mean(axis=1).astype(np.float32) / 32768.0
    if rate != 16_000:
        count = round(len(audio) * 16_000 / rate)
        audio = np.interp(
            np.arange(count) * rate / 16_000, np.arange(len(audio)), audio
        ).astype(np.float32)
    return audio


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    args = parser.parse_args()
    result = asyncio.run(transcribe(read_mono_16k(args.wav)))
    print(f"ASR RESULT: {result}", flush=True)


if __name__ == "__main__":
    main()
