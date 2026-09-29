"""Hash-checked bundled gesture inference and landmark transforms."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import time
from typing import Callable
from zipfile import ZipFile

import numpy as np

from .hand_pose import normalize_world_landmarks
from .types import Point3


BUNDLE_SHA256 = "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
LABELS = (
    "None", "Closed_Fist", "Open_Palm", "Pointing_Up",
    "Thumb_Down", "Thumb_Up", "Victory", "ILoveYou",
)


@dataclass(frozen=True)
class Prediction:
    label: str
    confidence: float
    scores: tuple[float, ...]
    elapsed_ms: float


def right_hand_score(handedness: str, confidence: float) -> float:
    """Match MediaPipe's HandednessToMatrixCalculator input."""
    if handedness not in ("Left", "Right") or not 0.0 <= confidence <= 1.0:
        raise ValueError("需要有效的左右手标签及其置信度")
    return confidence if handedness == "Right" else 1.0 - confidence


def preprocess_landmarks(
    points: tuple[Point3, ...] | np.ndarray,
    image_size: tuple[int, int] | None = None,
    rotation_radians: float = 0.0,
) -> np.ndarray:
    """Reproduce the stock wrist-origin and XY-bounds preprocessing.

    `image_size` is only for image-normalized landmarks. World landmarks skip
    the aspect-ratio correction. The caller supplies the actual ROI rotation.
    """
    matrix = np.asarray(points, dtype=np.float32).copy()
    if matrix.shape != (21, 3) or not np.isfinite(matrix).all():
        raise ValueError("需要21个有限的XYZ关键点")
    if image_size is not None:
        width, height = image_size
        if width <= 0 or height <= 0:
            raise ValueError("图像尺寸必须为正数")
        maximum = max(width, height)
        matrix[:, 0] = (matrix[:, 0] - 0.5) * (width / maximum) + 0.5
        matrix[:, 1] = (matrix[:, 1] - 0.5) * (height / maximum) + 0.5
    if rotation_radians:
        x, y = matrix[:, 0] - 0.5, matrix[:, 1] - 0.5
        cosine, sine = np.cos(rotation_radians), np.sin(-rotation_radians)
        matrix[:, 0] = x * cosine - y * sine + 0.5
        matrix[:, 1] = y * cosine + x * sine + 0.5
    matrix -= matrix[0].copy()
    span = max(float(np.ptp(matrix[:, 0])), float(np.ptp(matrix[:, 1]))) + 1e-5
    return matrix / span


def palm_aligned_inputs(
    image_points: tuple[Point3, ...] | np.ndarray,
    world_points: tuple[Point3, ...] | np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Experimental orthographic frontal projection of a 3D hand skeleton.

    This intentionally uses a simple projection. It must not be interpreted as
    a calibrated camera projection or as a validated gesture prediction.
    """
    image = np.asarray(image_points, dtype=np.float32)
    if image.shape != (21, 3) or not np.isfinite(image).all():
        raise ValueError("图像关节点无效")
    local = np.asarray(
        normalize_world_landmarks(tuple(map(tuple, world_points))),
        dtype=np.float32,
    )
    # Preserve the observed image left/right orientation while making fingers
    # point upward. Keep the original handedness probability separately.
    if image[17, 0] < image[5, 0]:
        local[:, 0] *= -1.0
    local[:, 1] *= -1.0
    return preprocess_landmarks(local), preprocess_landmarks(local)


def image_roll_inputs(image_points, image_size, *, flip_z=False) -> np.ndarray:
    """Diagnostic branch only: preserve image XYZ, remove roll, optionally reflect Z.

    Z reflection is an ablation, not an inferred palm/back orientation decision.
    Keep the same two normalization passes as the recorded-data experiment.
    """
    points = preprocess_landmarks(image_points, image_size)
    direction = points[9, :2] - points[0, :2]
    if np.linalg.norm(direction) < 1e-6:
        raise ValueError("手腕与中指根投影重合，无法确定平面角度")
    angle = float(np.arctan2(direction[1], direction[0]) + np.pi / 2)
    result = preprocess_landmarks(points, rotation_radians=angle)
    if flip_z:
        result[:, 2] *= -1
    return result


def _load_bundle(bundle: Path) -> tuple[bytes, bytes]:
    raw = bundle.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BUNDLE_SHA256:
        raise ValueError("模型包与已验证版本不同；需要重新核对张量、类别顺序和前处理")
    with ZipFile(BytesIO(raw)) as outer:
        inner = outer.read("hand_gesture_recognizer.task")
    with ZipFile(BytesIO(inner)) as gesture:
        return (
            gesture.read("gesture_embedder.tflite"),
            gesture.read("canned_gesture_classifier.tflite"),
        )


class BundledGestureClassifier:
    def __init__(
        self,
        bundle: Path,
        interpreter_factory: Callable[..., object] | None = None,
    ) -> None:
        if interpreter_factory is None:
            try:
                from ai_edge_litert.interpreter import Interpreter
            except ImportError as exc:
                raise RuntimeError(
                    "视觉融合需要 ai-edge-litert==2.2.0；请安装 vision 可选依赖"
                ) from exc
            interpreter_factory = Interpreter
        embedder_bytes, classifier_bytes = _load_bundle(bundle)
        self._embedder = interpreter_factory(model_content=embedder_bytes, num_threads=1)
        self._classifier = interpreter_factory(model_content=classifier_bytes, num_threads=1)
        for interpreter in (self._embedder, self._classifier):
            interpreter.allocate_tensors()
        expected = (
            (("hand", (1, 21, 3)), ("handedness", (1, 1)),
             ("world_hand", (1, 21, 3))),
            (("hand_embedding", (1, 128)),),
        )
        for interpreter, definitions in zip(
            (self._embedder, self._classifier), expected
        ):
            details = interpreter.get_input_details()
            if tuple(
                (item["name"], tuple(item["shape"])) for item in details
            ) != definitions or any(item["dtype"] != np.float32 for item in details):
                raise ValueError("TFLite输入张量与已验证模型不符")
        if tuple(self._classifier.get_output_details()[0]["shape"]) != (1, 8):
            raise ValueError("TFLite分类输出与已验证模型不符")

    def classify(
        self, hand: np.ndarray, world_hand: np.ndarray, right_score: float
    ) -> Prediction:
        if not 0.0 <= right_score <= 1.0:
            raise ValueError("右手概率必须在0到1之间")
        inputs = (
            np.asarray(hand, dtype=np.float32).reshape(1, 21, 3),
            np.array([[right_score]], dtype=np.float32),
            np.asarray(world_hand, dtype=np.float32).reshape(1, 21, 3),
        )
        if not all(np.isfinite(value).all() for value in inputs):
            raise ValueError("模型输入含非有限数值")
        start = time.perf_counter()
        for detail, value in zip(self._embedder.get_input_details(), inputs):
            self._embedder.set_tensor(detail["index"], value)
        self._embedder.invoke()
        embedding = self._embedder.get_tensor(
            self._embedder.get_output_details()[0]["index"]
        )
        self._classifier.set_tensor(
            self._classifier.get_input_details()[0]["index"], embedding
        )
        self._classifier.invoke()
        scores = self._classifier.get_tensor(
            self._classifier.get_output_details()[0]["index"]
        ).reshape(-1)
        if scores.shape != (8,) or not np.isfinite(scores).all():
            raise ValueError("分类器输出无效")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        winner = int(np.argmax(scores))
        return Prediction(
            LABELS[winner], float(scores[winner]),
            tuple(float(value) for value in scores), elapsed_ms,
        )
