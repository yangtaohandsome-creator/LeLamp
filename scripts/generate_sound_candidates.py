"""Generate the small, original WAV cue candidates shipped with LeLamp."""
from __future__ import annotations

from pathlib import Path
import wave

import numpy as np


RATE = 44100
OUT = Path(__file__).resolve().parents[1] / "lelamp/audio/sounds"


def tone(frequency: float, seconds: float, volume: float = 0.34,
         attack: float = 0.012, release: float = 0.10) -> np.ndarray:
    count = max(1, round(RATE * seconds))
    t = np.arange(count, dtype=np.float64) / RATE
    # A quiet fundamental plus two harmonics sounds closer to a small bell.
    data = (
        np.sin(2 * np.pi * frequency * t)
        + 0.24 * np.sin(2 * np.pi * frequency * 2.01 * t)
        + 0.08 * np.sin(2 * np.pi * frequency * 3.98 * t)
    )
    envelope = np.ones(count)
    attack_samples = min(count, max(1, round(RATE * attack)))
    release_samples = min(count, max(1, round(RATE * release)))
    envelope[:attack_samples] *= np.linspace(0, 1, attack_samples)
    envelope[-release_samples:] *= np.linspace(1, 0, release_samples) ** 1.5
    return data * envelope * volume


def silence(seconds: float) -> np.ndarray:
    return np.zeros(round(RATE * seconds), dtype=np.float64)


def chord(frequencies: tuple[float, ...], seconds: float, volume: float = 0.22) -> np.ndarray:
    return sum((tone(freq, seconds, volume) for freq in frequencies), np.zeros(round(RATE * seconds)))


def save(name: str, parts: list[np.ndarray]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    audio = np.concatenate(parts)
    peak = max(1.0, float(np.max(np.abs(audio))))
    pcm = np.round(audio / peak * 30000).astype("<i2")
    with wave.open(str(OUT / name), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm.tobytes())


def main() -> None:
    # A: restrained and warm; B: bright digital; C: playful/expressive.
    save("wake_a.wav", [tone(523.25, .16), silence(.035), tone(659.25, .22)])
    save("wake_b.wav", [tone(659.25, .10), silence(.025), tone(880, .10), silence(.025), tone(1108.73, .18)])
    save("wake_c.wav", [tone(440, .12), silence(.025), chord((659.25, 783.99), .24)])

    save("timer_a.wav", [tone(783.99, .22), silence(.12), tone(783.99, .30)])
    save("timer_b.wav", [tone(659.25, .13), silence(.07), tone(783.99, .13), silence(.07), tone(987.77, .25)])
    save("timer_c.wav", [chord((523.25, 659.25), .20), silence(.11), chord((587.33, 783.99), .30)])

    save("alarm_a.wav", [tone(698.46, .20), silence(.10), tone(698.46, .20), silence(.10), tone(880, .34)])
    save("alarm_b.wav", [tone(523.25, .16), silence(.06), tone(659.25, .16), silence(.06), tone(783.99, .16), silence(.06), tone(1046.50, .34)])
    save("alarm_c.wav", [chord((587.33, 739.99), .24), silence(.10), chord((659.25, 880), .24), silence(.10), chord((587.33, 739.99), .34)])


if __name__ == "__main__":
    main()
