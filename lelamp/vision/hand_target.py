"""Stable short-lived association for MediaPipe hand observations."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from .config import VisionConfig
from .types import HandObservation, HandTrackingCandidate, TrackingTarget


@dataclass
class _Track:
    track_id: str
    handedness: str | None
    center: tuple[float, float]
    palm_scale: float
    velocity: tuple[float, float]
    confidence: float
    gesture: str | None
    gesture_confidence: float
    last_seen_at: float


@dataclass(frozen=True)
class HandTargetUpdate:
    hands: tuple[HandObservation, ...]
    candidates: tuple[HandTrackingCandidate, ...]


def _distance(first, second) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


class HandTargetManager:
    """Assign stable IDs without deciding which hand may control motion."""

    def __init__(self, config: VisionConfig) -> None:
        self.config = config
        self._tracks: dict[str, _Track] = {}
        self._next_track_number = 1

    def reset(self) -> None:
        self._tracks.clear()
        self._next_track_number = 1

    def update(
        self, hands: tuple[HandObservation, ...], captured_at: float
    ) -> HandTargetUpdate:
        self._tracks = {
            track_id: track for track_id, track in self._tracks.items()
            if captured_at - track.last_seen_at <= self.config.hand_target_release_seconds
        }
        matches: list[tuple[float, str, int]] = []
        for track_id, track in self._tracks.items():
            elapsed = max(0.0, captured_at - track.last_seen_at)
            predicted = (
                track.center[0] + track.velocity[0] * min(
                    elapsed, self.config.hand_target_predict_seconds
                ),
                track.center[1] + track.velocity[1] * min(
                    elapsed, self.config.hand_target_predict_seconds
                ),
            )
            for index, hand in enumerate(hands):
                if (
                    track.handedness and hand.handedness
                    and track.handedness != hand.handedness
                ):
                    continue
                distance = _distance(predicted, hand.palm_center_normalized)
                if distance > self.config.hand_association_max_distance:
                    continue
                if track.palm_scale > 0 and hand.palm_scale > 0:
                    ratio = min(track.palm_scale, hand.palm_scale) / max(
                        track.palm_scale, hand.palm_scale
                    )
                    if ratio < self.config.hand_association_min_size_ratio:
                        continue
                    size_penalty = abs(math.log(hand.palm_scale / track.palm_scale))
                else:
                    size_penalty = 0.0
                matches.append((distance + size_penalty * 0.05, track_id, index))

        assigned_tracks: set[str] = set()
        assigned_hands: set[int] = set()
        assignments: dict[int, str] = {}
        for _, track_id, index in sorted(matches):
            if track_id in assigned_tracks or index in assigned_hands:
                continue
            assigned_tracks.add(track_id)
            assigned_hands.add(index)
            assignments[index] = track_id

        for index in range(len(hands)):
            if index in assignments:
                continue
            track_id = f"hand-{self._next_track_number}"
            self._next_track_number += 1
            assignments[index] = track_id

        updated_hands: list[HandObservation] = []
        for index, hand in enumerate(hands):
            track_id = assignments[index]
            previous = self._tracks.get(track_id)
            velocity = (0.0, 0.0)
            if previous is not None:
                elapsed = max(1e-6, captured_at - previous.last_seen_at)
                measured = (
                    (hand.palm_center_normalized[0] - previous.center[0]) / elapsed,
                    (hand.palm_center_normalized[1] - previous.center[1]) / elapsed,
                )
                alpha = self.config.hand_velocity_smoothing
                velocity = (
                    previous.velocity[0] * (1 - alpha) + measured[0] * alpha,
                    previous.velocity[1] * (1 - alpha) + measured[1] * alpha,
                )
            self._tracks[track_id] = _Track(
                track_id=track_id,
                handedness=hand.handedness,
                center=hand.palm_center_normalized,
                palm_scale=hand.palm_scale,
                velocity=velocity,
                confidence=max(hand.gesture_confidence, 0.01),
                gesture=hand.gesture,
                gesture_confidence=hand.gesture_confidence,
                last_seen_at=captured_at,
            )
            updated_hands.append(replace(
                hand, track_id=track_id,
                velocity_normalized_per_second=velocity,
            ))

        visible_ids = set(assignments.values())
        candidates = []
        for track_id, track in sorted(self._tracks.items()):
            elapsed = max(0.0, captured_at - track.last_seen_at)
            target = None
            if elapsed <= self.config.hand_target_predict_seconds:
                target = TrackingTarget(
                    kind="hand", track_id=track_id,
                    position_normalized=track.center,
                    velocity_normalized_per_second=track.velocity,
                    captured_at=track.last_seen_at,
                    confidence=track.confidence,
                )
            candidates.append(HandTrackingCandidate(
                track_id=track_id,
                target=target,
                palm_scale=track.palm_scale,
                handedness=track.handedness,
                gesture=track.gesture,
                gesture_confidence=track.gesture_confidence,
                visible=track_id in visible_ids,
                last_seen_at=track.last_seen_at,
            ))
        return HandTargetUpdate(tuple(updated_hands), tuple(candidates))
