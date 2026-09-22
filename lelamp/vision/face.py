"""OpenCV YuNet adapter."""
from __future__ import annotations

from .config import VisionConfig
from .types import FaceObservation


class YuNetFaceDetector:
    def __init__(self, config: VisionConfig) -> None:
        if not config.yunet_model.is_file():
            raise FileNotFoundError(f"找不到 YuNet 模型: {config.yunet_model}")
        import cv2

        self._cv2 = cv2
        self._size = config.model_size
        self._detector = cv2.FaceDetectorYN.create(
            str(config.yunet_model),
            "",
            self._size,
            config.face_score_threshold,
            config.face_nms_threshold,
            config.face_top_k,
        )

    def detect(self, image) -> tuple[FaceObservation, ...]:
        width, height = self._size
        self._detector.setInputSize(self._size)
        _status, faces = self._detector.detect(image)
        if faces is None:
            return ()
        observations = []
        for row in faces:
            x, y, box_width, box_height = (float(value) for value in row[:4])
            landmarks = tuple(
                (float(row[index]) / width, float(row[index + 1]) / height)
                for index in range(4, 14, 2)
            )
            if len(landmarks) >= 3:
                # YuNet order begins with both eyes and nose.  Excluding mouth
                # corners keeps speech and expressions from moving the target.
                stable_landmarks = landmarks[:3]
                anchor = (
                    sum(point[0] for point in stable_landmarks) / len(stable_landmarks),
                    sum(point[1] for point in stable_landmarks) / len(stable_landmarks),
                )
            else:
                anchor = ((x + box_width / 2) / width, (y + box_height / 2) / height)
            observations.append(
                FaceObservation(
                    track_id=None,
                    bbox_normalized=(
                        x / width,
                        y / height,
                        box_width / width,
                        box_height / height,
                    ),
                    anchor_normalized=anchor,
                    five_landmarks_normalized=landmarks,
                    confidence=float(row[-1]),
                )
            )
        return tuple(observations)

    def close(self) -> None:
        self._detector = None
