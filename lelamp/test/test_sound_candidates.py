"""Interactive playback of the local sound-cue candidates."""
from __future__ import annotations

import argparse
import os
import time

from lelamp.audio import SoundPlayer


CANDIDATES = {
    "wake": ("wake_a.wav", "wake_b.wav", "wake_c.wav"),
    "timer": ("timer_a.wav", "timer_b.wav", "timer_c.wav"),
    "alarm": ("alarm_a.wav", "alarm_b.wav", "alarm_c.wav"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="试听 LeLamp 提示音候选")
    parser.add_argument("cue", choices=(*CANDIDATES, "all"), nargs="?", default="all")
    parser.add_argument("--gap", type=float, default=1.2, help="候选之间的间隔秒数")
    args = parser.parse_args()
    player = SoundPlayer()
    os.environ["SOUND_ENABLED"] = "1"
    groups = CANDIDATES if args.cue == "all" else {args.cue: CANDIDATES[args.cue]}
    for cue, files in groups.items():
        print(f"\n=== {cue.upper()} 候选 ===", flush=True)
        for index, filename in enumerate(files, 1):
            os.environ[f"SOUND_{cue.upper()}"] = filename
            print(f"{cue.upper()}-{chr(64 + index)}: {filename}", flush=True)
            player.play(cue)
            time.sleep(max(0, args.gap))
    print("\n试听完成。记录你喜欢的组合，例如：WAKE-B、TIMER-A、ALARM-C。", flush=True)


if __name__ == "__main__":
    main()
