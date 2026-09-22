"""Configuration for the formal LeLamp vision runtime."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import dotenv_values


CONFIG_PATH = Path(__file__).resolve().parents[2] / "vision.conf"


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _source(value: str) -> int | str:
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    return stripped


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool
    camera: int | str
    capture_width: int
    capture_height: int
    capture_fps: float
    capture_fourcc: str
    rotation: int
    model_width: int
    model_height: int
    inference_hz: float
    result_max_age_ms: float
    camera_start_timeout_seconds: float
    yunet_model: Path
    gesture_model: Path
    face_score_threshold: float
    face_nms_threshold: float
    face_top_k: int
    face_target_min_area: float
    face_target_center_weight: float
    face_association_max_distance: float
    face_association_min_iou: float
    face_association_min_size_ratio: float
    face_target_predict_seconds: float
    face_target_release_seconds: float
    face_velocity_smoothing: float
    max_hands: int
    hand_detection_confidence: float
    hand_presence_confidence: float
    hand_tracking_confidence: float

    @property
    def capture_size(self) -> tuple[int, int]:
        return self.capture_width, self.capture_height

    @property
    def model_size(self) -> tuple[int, int]:
        return self.model_width, self.model_height


def load_vision_config(path: Path = CONFIG_PATH) -> VisionConfig:
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    values.update(os.environ)

    def get(name: str, default: str) -> str:
        return str(values.get(name, default))

    config = VisionConfig(
        enabled=_bool(values.get("VISION_ENABLED"), False),
        camera=_source(get("VISION_CAMERA", "/dev/video0")),
        capture_width=int(get("VISION_CAPTURE_WIDTH", "640")),
        capture_height=int(get("VISION_CAPTURE_HEIGHT", "480")),
        capture_fps=float(get("VISION_CAPTURE_FPS", "30")),
        capture_fourcc=get("VISION_CAPTURE_FOURCC", "MJPG").upper(),
        rotation=int(get("VISION_ROTATION", "0")),
        model_width=int(get("VISION_MODEL_WIDTH", "320")),
        model_height=int(get("VISION_MODEL_HEIGHT", "240")),
        inference_hz=float(get("VISION_INFERENCE_HZ", "10")),
        result_max_age_ms=float(get("VISION_RESULT_MAX_AGE_MS", "250")),
        camera_start_timeout_seconds=float(
            get("VISION_CAMERA_START_TIMEOUT_SECONDS", "5")
        ),
        yunet_model=Path(get("VISION_YUNET_MODEL", "")).expanduser(),
        gesture_model=Path(get("VISION_GESTURE_MODEL", "")).expanduser(),
        face_score_threshold=float(get("VISION_FACE_SCORE_THRESHOLD", "0.7")),
        face_nms_threshold=float(get("VISION_FACE_NMS_THRESHOLD", "0.3")),
        face_top_k=int(get("VISION_FACE_TOP_K", "5000")),
        face_target_min_area=float(get("VISION_FACE_TARGET_MIN_AREA", "0.025")),
        face_target_center_weight=float(
            get("VISION_FACE_TARGET_CENTER_WEIGHT", "0.15")
        ),
        face_association_max_distance=float(
            get("VISION_FACE_ASSOCIATION_MAX_DISTANCE", "0.22")
        ),
        face_association_min_iou=float(
            get("VISION_FACE_ASSOCIATION_MIN_IOU", "0.05")
        ),
        face_association_min_size_ratio=float(
            get("VISION_FACE_ASSOCIATION_MIN_SIZE_RATIO", "0.35")
        ),
        face_target_predict_seconds=float(
            get("VISION_FACE_TARGET_PREDICT_SECONDS", "0.30")
        ),
        face_target_release_seconds=float(
            get("VISION_FACE_TARGET_RELEASE_SECONDS", "1.20")
        ),
        face_velocity_smoothing=float(
            get("VISION_FACE_VELOCITY_SMOOTHING", "0.45")
        ),
        max_hands=int(get("VISION_MAX_HANDS", "2")),
        hand_detection_confidence=float(
            get("VISION_HAND_DETECTION_CONFIDENCE", "0.5")
        ),
        hand_presence_confidence=float(
            get("VISION_HAND_PRESENCE_CONFIDENCE", "0.5")
        ),
        hand_tracking_confidence=float(
            get("VISION_HAND_TRACKING_CONFIDENCE", "0.5")
        ),
    )
    if min(*config.capture_size, *config.model_size) <= 0:
        raise ValueError("视觉采集和模型尺寸必须为正整数")
    if config.capture_fps <= 0 or config.inference_hz <= 0:
        raise ValueError("视觉采集和推理频率必须大于 0")
    if len(config.capture_fourcc) != 4:
        raise ValueError("VISION_CAPTURE_FOURCC 必须是四字符编码")
    if config.rotation not in (0, 90, 180, 270):
        raise ValueError("VISION_ROTATION 只能是 0、90、180 或 270")
    if config.max_hands < 1:
        raise ValueError("VISION_MAX_HANDS 必须至少为 1")
    if not 0 < config.face_target_min_area < 1:
        raise ValueError("VISION_FACE_TARGET_MIN_AREA 必须在 0～1 之间")
    if not 0 < config.face_association_max_distance <= 2:
        raise ValueError("VISION_FACE_ASSOCIATION_MAX_DISTANCE 必须在 0～2 之间")
    if not 0 <= config.face_association_min_iou <= 1:
        raise ValueError("VISION_FACE_ASSOCIATION_MIN_IOU 必须在 0～1 之间")
    if not 0 < config.face_association_min_size_ratio <= 1:
        raise ValueError("VISION_FACE_ASSOCIATION_MIN_SIZE_RATIO 必须在 0～1 之间")
    if not 0 <= config.face_target_predict_seconds <= config.face_target_release_seconds:
        raise ValueError("人脸预测时间必须小于等于目标释放时间")
    if not 0 <= config.face_velocity_smoothing <= 1:
        raise ValueError("VISION_FACE_VELOCITY_SMOOTHING 必须在 0～1 之间")
    return config
