#!/usr/bin/env python3
"""Measure camera image displacement caused by small tracking-joint moves."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lelamp.motion.config import motion_port, tracking_home_action
from lelamp.motion.controller import MotionController
from lelamp.motion.visual_tracking import CALIBRATION_PATH, CONTROLLED_JOINTS
from lelamp.vision.camera import LatestFrameSource
from lelamp.vision.config import load_vision_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="标定关节小幅运动与摄像头画面位移的关系"
    )
    parser.add_argument("--delta", type=float, default=3.0, help="单侧扰动，默认 3")
    parser.add_argument("--settle", type=float, default=1.0, help="到位后等待秒数")
    parser.add_argument("--frames", type=int, default=7, help="每个位置取中值的帧数")
    parser.add_argument("--output", type=Path, default=CALIBRATION_PATH)
    return parser.parse_args()


def wait_new_frame(camera: LatestFrameSource, after: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        packet = camera.latest()
        if packet is not None and packet.sequence > after:
            return packet
        time.sleep(0.01)
    raise RuntimeError("等待摄像头新帧超时")


def capture_median(camera: LatestFrameSource, count: int) -> np.ndarray:
    import cv2

    frames = []
    sequence = camera.latest().sequence if camera.latest() is not None else 0
    for _ in range(count):
        packet = wait_new_frame(camera, sequence)
        sequence = packet.sequence
        gray = cv2.cvtColor(packet.image, cv2.COLOR_BGR2GRAY)
        frames.append(gray.astype(np.float32))
    return np.median(np.stack(frames), axis=0).astype(np.float32)


def displacement(first: np.ndarray, second: np.ndarray) -> tuple[float, float, float]:
    import cv2

    height, width = first.shape
    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    shift, quality = cv2.phaseCorrelate(first, second, window)
    return float(shift[0]), float(shift[1]), float(quality)


async def run(args: argparse.Namespace) -> None:
    if not 0.5 <= args.delta <= 8.0:
        raise ValueError("--delta 必须在 0.5～8 之间")
    if args.frames < 3:
        raise ValueError("--frames 至少为 3")

    vision_config = load_vision_config()
    camera = LatestFrameSource(vision_config)
    motion = MotionController(port=motion_port())
    initial = None
    camera.start()
    try:
        first = camera.wait_for_first_frame(vision_config.camera_start_timeout_seconds)
        if first is None:
            raise RuntimeError(camera.health().get("error") or "摄像头启动失败")
        print(f"摄像头已就绪: {camera.health()['negotiated']}", flush=True)
        motion.connect()
        initial = motion.read_action()
        home = tracking_home_action()
        await motion.move_tracking_raw(home, 1.0)

        columns: list[tuple[float, float]] = []
        quality_by_joint: dict[str, float] = {}
        measured_positions: dict[str, dict[str, float]] = {}
        for joint in CONTROLLED_JOINTS:
            key = f"{joint}.pos"
            samples = {}
            images = {}
            for direction, sign in (("minus", -1.0), ("plus", 1.0)):
                target = dict(home)
                target[key] += sign * args.delta
                print(f"标定 {joint} {direction}: {target[key]:.3f}", flush=True)
                await motion.move_tracking_raw(target, 0.45)
                await asyncio.sleep(args.settle)
                samples[direction] = motion.read_action()[key]
                images[direction] = await asyncio.to_thread(
                    capture_median, camera, args.frames
                )
            await motion.move_tracking_raw(home, 0.45)
            actual_delta = samples["plus"] - samples["minus"]
            if abs(actual_delta) < args.delta:
                raise RuntimeError(f"{joint} 实际运动量不足: {actual_delta:.3f}")
            dx, dy, quality = displacement(images["minus"], images["plus"])
            height, width = images["minus"].shape
            column = (dx / width / actual_delta, dy / height / actual_delta)
            if quality < 0.08 or np.hypot(*column) < 1e-4:
                raise RuntimeError(
                    f"{joint} 图像响应不可靠: quality={quality:.3f}, response={column}"
                )
            columns.append(column)
            quality_by_joint[joint] = quality
            measured_positions[joint] = samples
            print(
                f"{joint}: shift=({dx:.2f}, {dy:.2f})px "
                f"response=({column[0]:.6f}, {column[1]:.6f})/unit "
                f"quality={quality:.3f}",
                flush=True,
            )

        matrix = np.asarray(columns, dtype=float).T
        singular = np.linalg.svd(matrix, compute_uv=False)
        condition = float(singular[0] / singular[-1]) if singular[-1] else float("inf")
        if not np.isfinite(condition) or condition > 50:
            raise RuntimeError(f"响应矩阵不可用，条件数={condition:.2f}")
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "camera": camera.health()["negotiated"],
            "controlled_joints": list(CONTROLLED_JOINTS),
            "home_action": home,
            "requested_delta": args.delta,
            "measured_positions": measured_positions,
            "response_matrix": matrix.tolist(),
            "phase_correlation_quality": quality_by_joint,
            "condition_number": condition,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(args.output)
        print(f"标定完成: {args.output} | condition={condition:.2f}", flush=True)
    finally:
        try:
            if initial is not None:
                await motion.move_tracking_raw(initial, 0.8)
                if motion.robot is not None and motion.robot.bus.is_connected:
                    motion.robot.bus.disable_torque()
        finally:
            motion.close()
            camera.stop()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
