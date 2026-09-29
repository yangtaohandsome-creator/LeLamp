"""Scan WebRTC reference delay through the exact production TTS/barge path."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from ..app import LampApp
from ..voice.announcement import AnnouncementInterrupted
from ..voice.config import load_voice_config
from ..voice.tts import close_tts, preconnect_tts
from ..voice.shared_capture import SharedCapture


PHRASES = (
    "小灯正在进行回声消除测试，请暂时保持安静。",
    "上海今天阴天，明天可能有雨，出门记得带伞。",
    "得，被嫌弃了。灯灭了，晚安。",
)


class _Motion:
    robot = None

    def close(self):
        pass


class _Lighting:
    def speaking(self):
        pass

    def interrupted(self, **_kwargs):
        pass

    def close(self):
        pass


async def scan(delays: list[int], output: Path, *, shared: bool = False) -> dict:
    app = LampApp(motion=_Motion(), lighting=_Lighting())
    microphone = None
    if shared:
        microphone = SharedCapture(app.audio_health.observe)
        microphone.pause()
        microphone.start()
        app.shared_capture = microphone
    await asyncio.to_thread(app.barge_in.prepare)
    preconnect_tts()
    await asyncio.sleep(2.0)
    rows = []
    try:
        for delay in delays:
            false_interrupts = 0
            for phrase_index, text in enumerate(PHRASES, 1):
                os.environ["BARGE_IN_AEC_DELAY_MS"] = str(delay)
                os.environ["BARGE_IN_DIAGNOSTIC_LABEL"] = (
                    f"delay-{delay:03d}-phrase-{phrase_index}"
                )
                started = time.perf_counter()
                interrupted = False
                capture_seconds = 0.0
                try:
                    await app._speak_now(text, None)
                except AnnouncementInterrupted:
                    interrupted = True
                    false_interrupts += 1
                    item = app.take_barge_in_utterance()
                    if item is not None:
                        capture_seconds = len(item.audio) / 16_000
                row = {
                    "delay_ms": delay,
                    "phrase": phrase_index,
                    "interrupted": interrupted,
                    "capture_seconds": capture_seconds,
                    "wall_seconds": time.perf_counter() - started,
                }
                rows.append(row)
                print(
                    f"delay={delay:3d}ms phrase={phrase_index} "
                    f"false_interrupt={interrupted}",
                    flush=True,
                )
                await asyncio.sleep(0.6)
            print(
                f"DELAY {delay:3d}ms: false interrupts "
                f"{false_interrupts}/{len(PHRASES)}",
                flush=True,
            )
    finally:
        if microphone is not None:
            microphone.close()
            app.shared_capture = None
        close_tts()
        await app.announcements.close()
        app.motion.close()
        app.lighting.close()
    summary = {
        "delays_ms": delays,
        "phrases_per_delay": len(PHRASES),
        "false_interrupts": {
            str(delay): sum(
                int(row["interrupted"]) for row in rows if row["delay_ms"] == delay
            )
            for delay in delays
        },
        "rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delays", default="0,4,8,12,16,20,80")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--shared", action="store_true")
    args = parser.parse_args()
    delays = [int(value.strip()) for value in args.delays.split(",") if value.strip()]
    if not delays or any(not 0 <= value <= 500 for value in delays):
        raise ValueError("delay 必须在 0～500 ms")
    load_voice_config()
    os.environ["BARGE_IN_DEBUG"] = "0"
    # Consecutive diagnostic turns must wait for the next speculative Edge
    # connection instead of exercising the production fast-fallback window.
    os.environ["EDGE_TTS_READY_WAIT_SECONDS"] = "3.0"
    output = args.output or Path("voice_debug/aec/formal_delay") / datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )
    os.environ["BARGE_IN_DIAGNOSTIC_DIR"] = str(output / "audio")
    print(f"FORMAL AEC DELAY SCAN | output={output}", flush=True)
    summary = asyncio.run(scan(delays, output, shared=args.shared))
    print(json.dumps(summary["false_interrupts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
