"""Short-lived target association for the primary desktop user."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .config import VisionConfig
from .types import Box, FaceObservation, Point, TrackingTarget


def _area(box: Box) -> float:
    return max(0.0, box[2]) * max(0.0, box[3])


def _distance(first: Point, second: Point) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _iou(first: Box, second: Box) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = _area(first) + _area(second) - intersection
    return intersection / union if union > 0 else 0.0


def _clamp_point(point: Point) -> Point:
    return max(0.0, min(1.0, point[0])), max(0.0, min(1.0, point[1]))


@dataclass(frozen=True)
class FaceTargetUpdate:
    faces: tuple[FaceObservation, ...]
    target: TrackingTarget | None
    visible: bool
    eligible_face_count: int


@dataclass
class _ActiveFace:
    track_id: str
    bbox: Box
    anchor: Point
    velocity: Point
    confidence: float
    last_seen_at: float


class FaceTargetManager:
    """Keep one foreground face stable without attempting identity recognition."""

    def __init__(self, config: VisionConfig) -> None:
        self.config = config
        self._active: _ActiveFace | None = None
        self._next_track_number = 1

    @property
    def active_track_id(self) -> str | None:
        return self._active.track_id if self._active is not None else None

    def reset(self) -> None:
        self._active = None
        self._next_track_number = 1

    def update(
        self, faces: tuple[FaceObservation, ...], captured_at: float
    ) -> FaceTargetUpdate:
        eligible_count = sum(
            _area(face.bbox_normalized) >= self.config.face_target_min_area
            for face in faces
        )
        if self._active is None:
            return self._select_new(faces, captured_at, eligible_count)

        matched_index = self._match_active(faces, captured_at)
        if matched_index is not None:
            return self._update_match(
                faces, matched_index, captured_at, eligible_count
            )

        elapsed = max(0.0, captured_at - self._active.last_seen_at)
        if elapsed > self.config.face_target_release_seconds:
            self._active = None
            return self._select_new(faces, captured_at, eligible_count)

        target = None
        if elapsed <= self.config.face_target_predict_seconds:
            predicted = _clamp_point((
                self._active.anchor[0] + self._active.velocity[0] * elapsed,
                self._active.anchor[1] + self._active.velocity[1] * elapsed,
            ))
            confidence_scale = max(
                0.0, 1.0 - elapsed / max(
                    self.config.face_target_predict_seconds, 1e-6
                )
            )
            target = TrackingTarget(
                kind="face",
                track_id=self._active.track_id,
                position_normalized=predicted,
                velocity_normalized_per_second=self._active.velocity,
                captured_at=self._active.last_seen_at,
                confidence=self._active.confidence * confidence_scale,
            )
        return FaceTargetUpdate(faces, target, False, eligible_count)

    def _select_new(
        self,
        faces: tuple[FaceObservation, ...],
        captured_at: float,
        eligible_count: int,
    ) -> FaceTargetUpdate:
        candidates = [
            (index, face) for index, face in enumerate(faces)
            if _area(face.bbox_normalized) >= self.config.face_target_min_area
        ]
        if not candidates:
            return FaceTargetUpdate(faces, None, False, eligible_count)

        def score(item: tuple[int, FaceObservation]) -> float:
            face = item[1]
            center_distance = min(
                1.0, _distance(face.anchor_normalized, (0.5, 0.5)) / math.sqrt(0.5)
            )
            center_bonus = 1.0 + self.config.face_target_center_weight * (
                1.0 - center_distance
            )
            return _area(face.bbox_normalized) * center_bonus * face.confidence

        index, selected = max(candidates, key=score)
        track_id = f"face-{self._next_track_number}"
        self._next_track_number += 1
        self._active = _ActiveFace(
            track_id=track_id,
            bbox=selected.bbox_normalized,
            anchor=selected.anchor_normalized,
            velocity=(0.0, 0.0),
            confidence=selected.confidence,
            last_seen_at=captured_at,
        )
        tracked = replace(
            selected, track_id=track_id,
            velocity_normalized_per_second=(0.0, 0.0),
        )
        updated_faces = faces[:index] + (tracked,) + faces[index + 1:]
        return FaceTargetUpdate(
            updated_faces, self._target(captured_at), True, eligible_count
        )

    def _match_active(
        self, faces: tuple[FaceObservation, ...], captured_at: float
    ) -> int | None:
        assert self._active is not None
        elapsed = max(0.0, captured_at - self._active.last_seen_at)
        predicted = _clamp_point((
            self._active.anchor[0] + self._active.velocity[0] * elapsed,
            self._active.anchor[1] + self._active.velocity[1] * elapsed,
        ))
        previous_area = _area(self._active.bbox)
        matches: list[tuple[float, int]] = []
        for index, face in enumerate(faces):
            area = _area(face.bbox_normalized)
            if area <= 0 or previous_area <= 0:
                continue
            size_ratio = min(area, previous_area) / max(area, previous_area)
            if size_ratio < self.config.face_association_min_size_ratio:
                continue
            distance = _distance(predicted, face.anchor_normalized)
            overlap = _iou(self._active.bbox, face.bbox_normalized)
            if (
                distance > self.config.face_association_max_distance
                and overlap < self.config.face_association_min_iou
            ):
                continue
            size_penalty = abs(math.log(area / previous_area))
            cost = distance - overlap * 0.25 + size_penalty * 0.05
            matches.append((cost, index))
        return min(matches)[1] if matches else None

    def _update_match(
        self,
        faces: tuple[FaceObservation, ...],
        index: int,
        captured_at: float,
        eligible_count: int,
    ) -> FaceTargetUpdate:
        assert self._active is not None
        selected = faces[index]
        elapsed = max(1e-6, captured_at - self._active.last_seen_at)
        measured_velocity = (
            (selected.anchor_normalized[0] - self._active.anchor[0]) / elapsed,
            (selected.anchor_normalized[1] - self._active.anchor[1]) / elapsed,
        )
        alpha = self.config.face_velocity_smoothing
        velocity = (
            self._active.velocity[0] * (1.0 - alpha) + measured_velocity[0] * alpha,
            self._active.velocity[1] * (1.0 - alpha) + measured_velocity[1] * alpha,
        )
        self._active.bbox = selected.bbox_normalized
        self._active.anchor = selected.anchor_normalized
        self._active.velocity = velocity
        self._active.confidence = selected.confidence
        self._active.last_seen_at = captured_at
        tracked = replace(
            selected,
            track_id=self._active.track_id,
            velocity_normalized_per_second=velocity,
        )
        updated_faces = faces[:index] + (tracked,) + faces[index + 1:]
        return FaceTargetUpdate(
            updated_faces, self._target(captured_at), True, eligible_count
        )

    def _target(self, captured_at: float) -> TrackingTarget:
        assert self._active is not None
        return TrackingTarget(
            kind="face",
            track_id=self._active.track_id,
            position_normalized=self._active.anchor,
            velocity_normalized_per_second=self._active.velocity,
            captured_at=captured_at,
            confidence=self._active.confidence,
        )
