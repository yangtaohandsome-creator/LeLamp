"""Safely capture stereo servo noise for every LeLamp motion class."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from ..motion.controller import MotionController
from ..motion.config import active_transition_seconds

MOTIONS = ("happy_wiggle", "excited", "sad", "shy", "shock", "nod", "headshake", "curious")


async def main_async(args):
    root = args.output_dir / time.strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    wav = root / "servo_noise_stereo.wav"
    markers = []
    started = time.monotonic()
    capture = subprocess.Popen([
        "arecord", "-q", "-D", args.capture_device, "-t", "wav", "-f", "S16_LE",
        "-c", "2", "-r", "48000", str(wav),
    ])
    motion = MotionController(args.port, args.lamp_id)

    async def marked(name, category, operation):
        begin = time.monotonic() - started
        status, error = "completed", ""
        try:
            await operation()
        except Exception as exc:
            status, error = "failed", repr(exc)
            raise
        finally:
            markers.append({"name": name, "category": category, "start": begin,
                            "end": time.monotonic() - started, "status": status, "error": error})

    async def baseline(label):
        begin = time.monotonic() - started
        await asyncio.sleep(args.baseline_seconds)
        markers.append({"name": label, "category": "baseline", "start": begin,
                        "end": time.monotonic() - started, "status": "completed", "error": ""})

    try:
        await asyncio.sleep(.5)
        await marked("initial_standby", "posture", lambda: motion.standby())
        for name in MOTIONS:
            for repeat in range(1, args.repeats + 1):
                await baseline(f"{name}_baseline_{repeat}")
                await marked(f"{name}_{repeat}", f"motion:{name}",
                             lambda n=name: motion.play(n, active_transition_seconds()))
                await marked(f"{name}_restore_{repeat}", "restore", lambda: motion.standby(active_transition_seconds()))
        for pose in ("high", "low"):
            for repeat in range(1, args.repeats + 1):
                await baseline(f"work_{pose}_baseline_{repeat}")
                await marked(f"work_{pose}_{repeat}", f"posture:work_{pose}",
                             lambda p=pose: motion.work_pose(p))
                await marked(f"work_{pose}_restore_{repeat}", "restore", lambda: motion.standby())
        for repeat in range(1, args.repeats + 1):
            for degrees in (30, 60, 0, -30, -60, 0):
                await baseline(f"yaw_{degrees}_{repeat}_baseline")
                async def turn(target=degrees):
                    motion.set_base_yaw_offset_degrees(target)
                    await motion.standby(active_transition_seconds())
                await marked(f"yaw_{degrees}_{repeat}", f"turn:{degrees}", turn)
        for repeat in range(1, args.repeats + 1):
            await baseline(f"sleep_baseline_{repeat}")
            await marked(f"sleep_{repeat}", "posture:sleep", motion.sleep)
            if repeat < args.repeats:
                await marked(f"wake_after_sleep_{repeat}", "posture:wake", motion.standby)
    finally:
        if motion.robot is not None:
            try:
                if motion.base_yaw_offset_degrees:
                    motion.set_base_yaw_offset_degrees(0)
                await motion.sleep()
            except Exception as exc:
                markers.append({"name": "final_sleep", "category": "safety", "start": time.monotonic()-started,
                                "end": time.monotonic()-started, "status": "failed", "error": repr(exc)})
            motion.close()
        capture.send_signal(signal.SIGINT)
        try: capture.wait(timeout=5)
        except subprocess.TimeoutExpired:
            capture.kill(); capture.wait()
        (root / "markers.json").write_text(json.dumps({"sample_rate": 48000, "channels": 2,
            "wav": wav.name, "markers": markers}, ensure_ascii=False, indent=2) + "\n")
        print(f"SERVO NOISE CAPTURE SAVED: {root.resolve()}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--lamp-id", default="lamppi")
    parser.add_argument("--capture-device", default=os.getenv("ARECORD_DEVICE", "hw:seeed2micvoicec,0"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--baseline-seconds", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path, default=Path("voice_debug/servo_noise"))
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__": main()
