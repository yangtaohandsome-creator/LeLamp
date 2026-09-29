"""Stable, hardware-neutral data contracts for vision results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


Point = tuple[float, float]
Point3 = tuple[float, float, float]
Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class FramePacket:
    sequence: int
    captured_at: float
    capture_size: tuple[int, int]
    model_input_size: tuple[int, int]
    rotation: int
    mirrored: bool
    image: np.ndarray


@dataclass(frozen=True)
class FaceObservation:
    track_id: str | None
    bbox_normalized: Box
    anchor_normalized: Point
    five_landmarks_normalized: tuple[Point, ...]
    confidence: float
    velocity_normalized_per_second: Point = (0.0, 0.0)


@dataclass(frozen=True)
class HandObservation:
    track_id: str | None
    handedness: str | None
    landmarks_normalized: tuple[Point, ...]
    palm_center_normalized: Point
    gesture: str | None
    gesture_confidence: float
    palm_scale: float = 0.0
    velocity_normalized_per_second: Point = (0.0, 0.0)
    landmarks_normalized_3d: tuple[Point3, ...] = ()
    world_landmarks: tuple[Point3, ...] = ()
    normalized_world_landmarks: tuple[Point3, ...] = ()
    finger_angles: tuple[float, ...] = ()
    gesture_candidates: tuple[tuple[str, float], ...] | None = None


@dataclass(frozen=True)
class TrackingTarget:
    kind: str
    track_id: str
    position_normalized: Point
    velocity_normalized_per_second: Point
    captured_at: float
    confidence: float
    position_is_prediction: bool = False


@dataclass(frozen=True)
class TrackingDirective:
    mode: str
    target: TrackingTarget | None = None
    source: str = "none"


@dataclass(frozen=True)
class GestureEvent:
    gesture: str
    hand_track_id: str
    confidence: float
    confirmed_at: float


@dataclass(frozen=True)
class HandTrackingCandidate:
    track_id: str
    target: TrackingTarget | None
    palm_scale: float
    handedness: str | None
    gesture: str | None
    gesture_confidence: float
    visible: bool
    last_seen_at: float


@dataclass(frozen=True)
class VisionSnapshot:
    source_frame_sequence: int
    captured_at: float
    completed_at: float
    faces: tuple[FaceObservation, ...]
    hands: tuple[HandObservation, ...]
    active_face_target_id: str | None = None
    active_hand_target_id: str | None = None
    gesture_events: tuple[GestureEvent, ...] = ()
    gesture_progress: tuple[tuple[str, str, float], ...] = ()
    camera_status: str = "running"

    @property
    def result_age_ms(self) -> float:
        import time
        return max(0.0, (time.monotonic() - self.captured_at) * 1000.0)


def json_safe_snapshot(snapshot: VisionSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "source_frame_sequence": snapshot.source_frame_sequence,
        "captured_at": snapshot.captured_at,
        "completed_at": snapshot.completed_at,
        "result_age_ms": round(snapshot.result_age_ms, 1),
        "face_count": len(snapshot.faces),
        "hand_count": len(snapshot.hands),
        "gestures": [hand.gesture for hand in snapshot.hands if hand.gesture],
        "hands": [
            {
                "track_id": hand.track_id,
                "handedness": hand.handedness,
                "gesture": hand.gesture,
                "gesture_confidence": round(hand.gesture_confidence, 4),
                "gesture_candidates": hand.gesture_candidates,
                "palm_scale": round(hand.palm_scale, 5),
            }
            for hand in snapshot.hands
        ],
        "gesture_events": [
            {
                "gesture": event.gesture,
                "hand_track_id": event.hand_track_id,
                "confidence": round(event.confidence, 4),
                "confirmed_at": event.confirmed_at,
            }
            for event in snapshot.gesture_events
        ],
        "gesture_progress": [
            {"hand_track_id": track_id, "gesture": gesture, "progress": round(progress, 3)}
            for track_id, gesture, progress in snapshot.gesture_progress
        ],
        "active_face_target_id": snapshot.active_face_target_id,
        "active_hand_target_id": snapshot.active_hand_target_id,
        "camera_status": snapshot.camera_status,
    }
