"""Image-space visual servo for the single motion task owned by LampApp."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Callable, Protocol

import numpy as np

from .config import load_motion_config


CALIBRATION_PATH = (
    Path(__file__).resolve().parent / "calibration" / "visual_response.json"
)
CONTROLLED_JOINTS = ("base_yaw", "wrist_pitch")


class TargetLike(Protocol):
    position_normalized: tuple[float, float]
    velocity_normalized_per_second: tuple[float, float]
    captured_at: float


@dataclass(frozen=True)
class VisualTrackingConfig:
    control_hz: float
    feedback_hz: float
    setpoint: tuple[float, float]
    deadzone: tuple[float, float]
    position_gain: float
    max_prediction_seconds: float
    max_velocity: np.ndarray
    max_acceleration: np.ndarray
    minimum: np.ndarray
    maximum: np.ndarray


def _number(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def load_visual_tracking_config() -> VisualTrackingConfig:
    load_motion_config()
    config = VisualTrackingConfig(
        control_hz=max(1.0, _number("MOTION_TRACKING_CONTROL_HZ", 25)),
        feedback_hz=max(0.5, _number("MOTION_TRACKING_FEEDBACK_HZ", 5)),
        setpoint=(
            _number("MOTION_TRACKING_SETPOINT_X", 0.5),
            _number("MOTION_TRACKING_SETPOINT_Y", 0.42),
        ),
        deadzone=np.array([
            max(0.0, _number("MOTION_TRACKING_DEADZONE_X", 0.035)),
            max(0.0, _number("MOTION_TRACKING_DEADZONE_Y", 0.045)),
        ]),
        position_gain=max(0.0, _number("MOTION_TRACKING_POSITION_GAIN", 2.0)),
        max_prediction_seconds=max(
            0.0, _number("MOTION_TRACKING_MAX_PREDICTION_SECONDS", 0.12)
        ),
        max_velocity=np.array([
            max(0.1, _number("MOTION_TRACKING_MAX_VELOCITY_BASE_YAW", 18)),
            max(0.1, _number("MOTION_TRACKING_MAX_VELOCITY_WRIST_PITCH", 15)),
        ]),
        max_acceleration=np.array([
            max(0.1, _number("MOTION_TRACKING_MAX_ACCELERATION_BASE_YAW", 55)),
            max(0.1, _number("MOTION_TRACKING_MAX_ACCELERATION_WRIST_PITCH", 45)),
        ]),
        minimum=np.array([
            _number("MOTION_TRACKING_MIN_BASE_YAW", -55),
            _number("MOTION_TRACKING_MIN_WRIST_PITCH", -45),
        ]),
        maximum=np.array([
            _number("MOTION_TRACKING_MAX_BASE_YAW", 55),
            _number("MOTION_TRACKING_MAX_WRIST_PITCH", 45),
        ]),
    )
    if not all(0.0 <= item <= 1.0 for item in (*config.setpoint, *config.deadzone)):
        raise ValueError("视觉跟踪画面目标点和死区必须在 0～1")
    if np.any(config.minimum >= config.maximum):
        raise ValueError("视觉跟踪关节软限位无效")
    return config


def load_response_matrix(path: Path = CALIBRATION_PATH) -> np.ndarray:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"尚未完成视觉关节响应标定: {path}") from exc
    joints = tuple(payload.get("controlled_joints", ()))
    if joints != CONTROLLED_JOINTS:
        raise ValueError(f"视觉标定关节不匹配: {joints}")
    response = np.asarray(payload.get("response_matrix"), dtype=np.float64)
    if response.shape != (2, 2) or not np.all(np.isfinite(response)):
        raise ValueError("视觉标定响应矩阵必须是有限的 2×2 数组")
    singular = np.linalg.svd(response, compute_uv=False)
    if singular[-1] < 1e-5 or singular[0] / singular[-1] > 50:
        raise ValueError("视觉标定响应矩阵退化，需重新标定")
    return response


class VisualTrackingRunner:
    """Drive the latest face target without queueing frames or blocking moves."""

    def __init__(
        self,
        motion,
        target_provider: Callable[[], TargetLike | None],
        *,
        config: VisualTrackingConfig | None = None,
        response_matrix: np.ndarray | None = None,
    ) -> None:
        self.motion = motion
        self.target_provider = target_provider
        self.config = config or load_visual_tracking_config()
        self.response = (
            np.asarray(response_matrix, dtype=np.float64)
            if response_matrix is not None else load_response_matrix()
        )
        if self.response.shape != (2, 2):
            raise ValueError("response_matrix 必须是 2×2")
        self.inverse = np.linalg.pinv(self.response, rcond=1e-3)
        self.state = {
            "status": "created", "control_hz": self.config.control_hz,
            "commands_sent": 0, "target_visible": False,
        }

    async def run(self) -> None:
        self.state["status"] = "entering_home"
        await self.motion.tracking_home()
        current = self.motion.read_action()
        command = np.array([current[f"{name}.pos"] for name in CONTROLLED_JOINTS])
        velocity = np.zeros(2, dtype=np.float64)
        period = 1.0 / self.config.control_hz
        feedback_period = 1.0 / self.config.feedback_hz
        loop = asyncio.get_running_loop()
        previous = loop.time()
        next_due = previous
        next_feedback = previous + feedback_period
        self.state["status"] = "running"
        print(
            "人脸跟踪已启动 | 初始姿态=tracking_home | "
            f"control={self.config.control_hz:g}Hz",
            flush=True,
        )
        try:
            while True:
                now = loop.time()
                if now < next_due:
                    await asyncio.sleep(next_due - now)
                    now = loop.time()
                dt = min(0.1, max(1e-3, now - previous))
                previous = now
                next_due = max(next_due + period, now)

                target = self.target_provider()
                if target is None:
                    velocity.fill(0.0)
                    self.state["target_visible"] = False
                    continue

                age = max(0.0, time.monotonic() - target.captured_at)
                prediction = min(age, self.config.max_prediction_seconds)
                position = np.asarray(target.position_normalized, dtype=np.float64)
                position += np.asarray(
                    target.velocity_normalized_per_second, dtype=np.float64
                ) * prediction
                error = np.asarray(self.config.setpoint) - position
                error[np.abs(error) <= self.config.deadzone] = 0.0
                wanted = self.inverse @ (error * self.config.position_gain)
                wanted = np.clip(
                    wanted, -self.config.max_velocity, self.config.max_velocity
                )
                dv = np.clip(
                    wanted - velocity,
                    -self.config.max_acceleration * dt,
                    self.config.max_acceleration * dt,
                )
                velocity += dv
                command = np.clip(
                    command + velocity * dt,
                    self.config.minimum,
                    self.config.maximum,
                )
                action = dict(current)
                for index, name in enumerate(CONTROLLED_JOINTS):
                    action[f"{name}.pos"] = float(command[index])
                self.motion.send_tracking_action(action)
                self.state.update({
                    "target_visible": True,
                    "target_error": [round(float(item), 4) for item in error],
                    "command": [round(float(item), 3) for item in command],
                    "commands_sent": int(self.state["commands_sent"]) + 1,
                })
                if now >= next_feedback:
                    actual = self.motion.read_action()
                    self.state["actual"] = [
                        round(float(actual[f"{name}.pos"]), 3)
                        for name in CONTROLLED_JOINTS
                    ]
                    next_feedback = now + feedback_period
        finally:
            self.state["status"] = "stopped"
            self.state["target_visible"] = False
            print("人脸跟踪控制循环已停止。", flush=True)
