#!/usr/bin/env python3
"""Record bounded full-pipeline telemetry with the normal follow preview."""
import argparse
import asyncio
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lelamp.motion import tracking_diagnostics
from lelamp.vision import latency
from vision_preview import run

async def main(seconds):
    directory = Path('runtime_state/tracking_diagnostics')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'latency-{time.time_ns()}.jsonl'
    recorder = tracking_diagnostics.Recorder(path)
    tracking_diagnostics.active_recorder = latency.recorder = recorder
    print(f'Latency recording: {path}; duration={seconds}s', flush=True)
    args = argparse.Namespace(follow=True, snapshot=None, windowed=False,
        hand_calibration=False, screen_width=1920, screen_height=1080)
    task = asyncio.create_task(run(args))
    try:
        await asyncio.wait_for(task, seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        tracking_diagnostics.active_recorder = latency.recorder = None
        recorder.close()
        print(f'Finished: {path}; dropped={recorder.dropped}; error={recorder.error}', flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=90)
    args = parser.parse_args()
    asyncio.run(main(args.seconds))
