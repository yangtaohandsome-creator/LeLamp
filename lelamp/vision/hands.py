"""MediaPipe Gesture Recognizer adapter."""
from __future__ import annotations

from .config import VisionConfig
from .types import HandObservation


class MediaPipeHands:
    def __init__(self, config: VisionConfig) -> None:
        if not config.gesture_model.is_file():
            raise FileNotFoundError(
                f"找不到 Gesture Recognizer 模型: {config.gesture_model}"
            )
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        self._mp = mp
        self._recognizer = vision.GestureRecognizer.create_from_options(
            vision.GestureRecognizerOptions(
                base_options=python.BaseOptions(
                    model_asset_path=str(config.gesture_model)
                ),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=config.max_hands,
                min_hand_detection_confidence=config.hand_detection_confidence,
                min_hand_presence_confidence=config.hand_presence_confidence,
                min_tracking_confidence=config.hand_tracking_confidence,
            )
        )
        self._last_timestamp_ms = -1

    def recognize(self, rgb_image, timestamp_ms: int) -> tuple[HandObservation, ...]:
        timestamp_ms = max(self._last_timestamp_ms + 1, int(timestamp_ms))
        self._last_timestamp_ms = timestamp_ms
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb_image)
        result = self._recognizer.recognize_for_video(image, timestamp_ms)
        observations = []
        for index, landmarks in enumerate(result.hand_landmarks):
            points = tuple((float(item.x), float(item.y)) for item in landmarks)
            palm_indices = (0, 5, 9, 13, 17)
            palm = (
                sum(points[item][0] for item in palm_indices) / len(palm_indices),
                sum(points[item][1] for item in palm_indices) / len(palm_indices),
            )
            handedness = None
            if index < len(result.handedness) and result.handedness[index]:
                handedness = result.handedness[index][0].category_name
            gesture = None
            confidence = 0.0
            if index < len(result.gestures) and result.gestures[index]:
                category = result.gestures[index][0]
                gesture = category.category_name
                confidence = float(category.score)
            observations.append(
                HandObservation(
                    track_id=None,
                    handedness=handedness,
                    landmarks_normalized=points,
                    palm_center_normalized=palm,
                    gesture=gesture,
                    gesture_confidence=confidence,
                )
            )
        return tuple(observations)

    def close(self) -> None:
        self._recognizer.close()
