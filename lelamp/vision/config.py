"""Configuration for the formal LeLamp vision runtime."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dotenv import dotenv_values


CONFIG_PATH = Path(__file__).resolve().parents[2] / "vision.conf"
DEFAULT_HAND_CALIBRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime_state" / "vision_hand_calibration.json"
)


def hand_calibration_path() -> Path:
    return Path(os.getenv(
        "VISION_HAND_CALIBRATION_FILE", str(DEFAULT_HAND_CALIBRATION_PATH)
    )).expanduser()


def save_hand_calibration(
    enter_scale: float,
    exit_scale: float,
    gesture_min_confidence: float,
    summary: dict | None = None,
    path: Path | None = None,
) -> Path:
    if not 0 < exit_scale < enter_scale < 1:
        raise ValueError("手部近距离标定必须满足 0 < exit < enter < 1")
    if not 0 < gesture_min_confidence <= 1:
        raise ValueError("手势置信度标定必须在 0～1 之间")
    destination = path or hand_calibration_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "hand_near_enter_scale": round(float(enter_scale), 6),
        "hand_near_exit_scale": round(float(exit_scale), 6),
        "gesture_min_confidence": round(float(gesture_min_confidence), 5),
        "summary": summary or {},
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


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
    hand_near_enter_scale: float | None = None
    hand_near_exit_scale: float | None = None
    gesture_min_confidence: float | None = None
    gesture_confirm_seconds: float = 0.35
    gesture_transition_timeout_seconds: float = 1.5
    gesture_exit_sequence_seconds: float = 3.0
    gesture_cooldown_seconds: float = 0.4
    gesture_gap_reset_seconds: float = 0.25
    hand_association_max_distance: float = 0.25
    hand_association_min_size_ratio: float = 0.35
    hand_target_predict_seconds: float = 0.3
    hand_target_release_seconds: float = 2.0
    hand_velocity_smoothing: float = 0.45
    hand_voice_acquire_seconds: float = 5.0
    work_light_hand_enabled: bool = True
    work_light_lost_seconds: float = 5.0
    work_light_nearest_ratio: float = 1.5

    @property
    def capture_size(self) -> tuple[int, int]:
        return self.capture_width, self.capture_height

    @property
    def model_size(self) -> tuple[int, int]:
        return self.model_width, self.model_height

    @property
    def hand_control_calibrated(self) -> bool:
        return (
            self.hand_near_enter_scale is not None
            and self.hand_near_exit_scale is not None
            and self.gesture_min_confidence is not None
        )


def load_vision_config(path: Path = CONFIG_PATH) -> VisionConfig:
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    calibration_file = hand_calibration_path()
    if calibration_file.is_file():
        try:
            calibration = json.loads(calibration_file.read_text(encoding="utf-8"))
            values.update({
                "VISION_HAND_NEAR_ENTER_SCALE": calibration["hand_near_enter_scale"],
                "VISION_HAND_NEAR_EXIT_SCALE": calibration["hand_near_exit_scale"],
                "VISION_GESTURE_MIN_CONFIDENCE": calibration["gesture_min_confidence"],
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"手部视觉标定文件无效: {calibration_file}") from exc
    values.update(os.environ)

    def get(name: str, default: str) -> str:
        return str(values.get(name, default))

    def optional_float(name: str) -> float | None:
        value = str(values.get(name, "")).strip()
        return float(value) if value else None

    config = VisionConfig(
        work_light_hand_enabled=_bool(values.get("VISION_WORK_LIGHT_HAND_ENABLED"), True),
        work_light_lost_seconds=float(get("VISION_WORK_LIGHT_LOST_SECONDS", "5")),
        work_light_nearest_ratio=float(get("VISION_WORK_LIGHT_NEAREST_RATIO", "1.5")),
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
        hand_near_enter_scale=optional_float("VISION_HAND_NEAR_ENTER_SCALE"),
        hand_near_exit_scale=optional_float("VISION_HAND_NEAR_EXIT_SCALE"),
        gesture_min_confidence=optional_float("VISION_GESTURE_MIN_CONFIDENCE"),
        gesture_confirm_seconds=float(get("VISION_GESTURE_CONFIRM_SECONDS", "0.35")),
        gesture_transition_timeout_seconds=float(
            get("VISION_GESTURE_TRANSITION_TIMEOUT_SECONDS", "1.5")
        ),
        gesture_exit_sequence_seconds=float(
            get("VISION_GESTURE_EXIT_SEQUENCE_SECONDS", "3.0")
        ),
        gesture_cooldown_seconds=float(get("VISION_GESTURE_COOLDOWN_SECONDS", "0.4")),
        gesture_gap_reset_seconds=float(get("VISION_GESTURE_GAP_RESET_SECONDS", "0.25")),
        hand_association_max_distance=float(
            get("VISION_HAND_ASSOCIATION_MAX_DISTANCE", "0.25")
        ),
        hand_association_min_size_ratio=float(
            get("VISION_HAND_ASSOCIATION_MIN_SIZE_RATIO", "0.35")
        ),
        hand_target_predict_seconds=float(
            get("VISION_HAND_TARGET_PREDICT_SECONDS", "0.30")
        ),
        hand_target_release_seconds=float(
            get("VISION_HAND_TARGET_RELEASE_SECONDS", "2.0")
        ),
        hand_velocity_smoothing=float(
            get("VISION_HAND_VELOCITY_SMOOTHING", "0.45")
        ),
        hand_voice_acquire_seconds=float(
            get("VISION_HAND_VOICE_ACQUIRE_SECONDS", "5.0")
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
    near_values = (config.hand_near_enter_scale, config.hand_near_exit_scale)
    if (near_values[0] is None) != (near_values[1] is None):
        raise ValueError("手部近距离进入和退出阈值必须同时标定")
    if near_values[0] is not None and not 0 < near_values[1] < near_values[0] < 1:
        raise ValueError("手部近距离阈值必须满足 0 < exit < enter < 1")
    if config.gesture_min_confidence is not None and not 0 < config.gesture_min_confidence <= 1:
        raise ValueError("手势置信度阈值必须在 0～1 之间")
    if min(
        config.gesture_confirm_seconds,
        config.gesture_transition_timeout_seconds,
        config.gesture_exit_sequence_seconds,
        config.gesture_cooldown_seconds,
        config.gesture_gap_reset_seconds,
        config.hand_target_release_seconds,
        config.hand_voice_acquire_seconds,
    ) <= 0:
        raise ValueError("手部状态机时间参数必须大于 0")
    if not 0 <= config.hand_target_predict_seconds <= config.hand_target_release_seconds:
        raise ValueError("手目标预测时间必须小于等于释放时间")
    if not 0 < config.hand_association_max_distance <= 2:
        raise ValueError("手目标关联距离必须在 0～2 之间")
    if not 0 < config.hand_association_min_size_ratio <= 1:
        raise ValueError("手目标关联尺寸比例必须在 0～1 之间")
    if not 0 <= config.hand_velocity_smoothing <= 1:
        raise ValueError("手目标速度平滑系数必须在 0～1 之间")
    if not 0 < config.work_light_lost_seconds < 60 or not 1 < config.work_light_nearest_ratio <= 10:
        raise ValueError("照明丢失时间/多手分离比例无效")
    return config
