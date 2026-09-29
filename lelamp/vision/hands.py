"""MediaPipe Gesture Recognizer adapter."""
from __future__ import annotations

import time
from . import latency
from .config import VisionConfig
from .hand_pose import finger_pip_angles, normalize_world_landmarks
from .types import HandObservation


def palm_scale(points, model_width: int, model_height: int) -> float:
    """Palm bone span as model pixels divided by model width."""
    import math

    aspect = model_height / model_width
    def distance(first: int, second: int) -> float:
        dx = points[first][0] - points[second][0]
        dy = (points[first][1] - points[second][1]) * aspect
        return math.hypot(dx, dy)
    return max(distance(0, 9), distance(5, 17))


class MediaPipeHands:
    def __init__(self, config: VisionConfig, *, fused: bool = True) -> None:
        if not config.gesture_model.is_file():
            raise FileNotFoundError(
                f"找不到 Gesture Recognizer 模型: {config.gesture_model}"
            )
        import mediapipe as mp
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        self._classifier = None
        if fused:
            from .gesture_classifier import BundledGestureClassifier
            self._classifier = BundledGestureClassifier(config.gesture_model)
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
        self._model_width = config.model_width
        self._model_height = config.model_height

    def recognize(self, rgb_image, timestamp_ms: int) -> tuple[HandObservation, ...]:
        timestamp_ms = max(self._last_timestamp_ms + 1, int(timestamp_ms))
        self._last_timestamp_ms = timestamp_ms
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb_image)
        official_started = time.monotonic()
        result = self._recognizer.recognize_for_video(image, timestamp_ms)
        official_done = time.monotonic()
        aligned_seconds = roll_seconds = 0.0
        observations = []
        for index, landmarks in enumerate(result.hand_landmarks):
            points = tuple((float(item.x), float(item.y)) for item in landmarks)
            points_3d = tuple(
                (float(item.x), float(item.y), float(item.z))
                for item in landmarks
            )
            palm_indices = (0, 5, 9, 13, 17)
            palm = (
                sum(points[item][0] for item in palm_indices) / len(palm_indices),
                sum(points[item][1] for item in palm_indices) / len(palm_indices),
            )
            handedness = None
            if index < len(result.handedness) and result.handedness[index]:
                handedness = result.handedness[index][0].category_name
            world_points = ()
            normalized_world_points = ()
            angles = ()
            if index < len(result.hand_world_landmarks):
                world_points = tuple(
                    (float(item.x), float(item.y), float(item.z))
                    for item in result.hand_world_landmarks[index]
                )
                try:
                    normalized_world_points = normalize_world_landmarks(
                        world_points, handedness
                    )
                    angles = finger_pip_angles(normalized_world_points)
                except ValueError:
                    # Bad 3D geometry must not interrupt 2D hand tracking.
                    pass
            gesture = None
            confidence = 0.0
            if index < len(result.gestures) and result.gestures[index]:
                category = result.gestures[index][0]
                gesture = category.category_name
                confidence = float(category.score)
            candidates = None
            if self._classifier is not None:
                from .gesture_classifier import palm_aligned_inputs, image_roll_inputs, preprocess_landmarks
                from .gesture_fusion import fuse_gestures
                aligned_pair = ('None', 0.0)
                roll_pair = ('None', 0.0)
                branch_started = time.monotonic()
                try:
                    aligned = self._classifier.classify(*palm_aligned_inputs(points_3d, world_points), .5)
                    aligned_pair = (aligned.label, aligned.confidence)
                except ValueError:
                    pass
                aligned_seconds += time.monotonic() - branch_started
                branch_started = time.monotonic()
                try:
                    roll = image_roll_inputs(points_3d, (self._model_width, self._model_height), flip_z=True)
                    prediction = self._classifier.classify(roll, preprocess_landmarks(world_points), .5)
                    roll_pair = (prediction.label, prediction.confidence)
                except ValueError:
                    pass
                roll_seconds += time.monotonic() - branch_started
                merged = fuse_gestures((gesture, confidence), aligned_pair, roll_pair)
                candidates = tuple((label, max(score for source, (name, score) in
                    enumerate(((gesture, confidence), aligned_pair, roll_pair))
                    if name == label and (label != 'Thumb_Up' or source == 0)))
                    for label in merged.labels)
                gesture = merged.label
                confidence = max((score for _, score in candidates), default=0.0)
            observations.append(
                HandObservation(
                    track_id=None,
                    handedness=handedness,
                    landmarks_normalized=points,
                    palm_center_normalized=palm,
                    gesture=gesture,
                    gesture_confidence=confidence,
                    palm_scale=palm_scale(
                        points, self._model_width, self._model_height
                    ),
                    landmarks_normalized_3d=points_3d,
                    world_landmarks=world_points,
                    normalized_world_landmarks=normalized_world_points,
                    finger_angles=angles,
                    gesture_candidates=candidates,
                )
            )
        latency.emit('hands_detail', timestamp_ms=timestamp_ms,
                     official_started=official_started, official_done=official_done,
                     aligned_seconds=aligned_seconds, roll_seconds=roll_seconds,
                     hands=len(observations))
        return tuple(observations)

    def close(self) -> None:
        self._recognizer.close()
