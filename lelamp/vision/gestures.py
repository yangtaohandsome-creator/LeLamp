"""Turn stable MediaPipe labels into one-shot, hardware-neutral events."""
from __future__ import annotations

from dataclasses import dataclass

from .config import VisionConfig
from .types import GestureEvent, HandObservation


SUPPORTED = {"Open_Palm", "Closed_Fist"}


@dataclass
class _GestureState:
    candidate: str | None = None
    candidate_since: float = 0.0
    last_valid_at: float = 0.0
    emitted: str | None = None
    last_emitted_at: float = -1e9


class StableGestureDetector:
    def __init__(self, config: VisionConfig) -> None:
        self.config = config
        self._states: dict[str, _GestureState] = {}

    def reset(self) -> None:
        self._states.clear()

    def progress(self, captured_at: float) -> tuple[tuple[str, str, float], ...]:
        if self.config.gesture_min_confidence is None:
            return ()
        return tuple(
            (
                track_id,
                state.candidate,
                min(1.0, max(0.0, (
                    captured_at - state.candidate_since
                ) / self.config.gesture_confirm_seconds)),
            )
            for track_id, state in sorted(self._states.items())
            if state.candidate is not None
        )

    def update(
        self, hands: tuple[HandObservation, ...], captured_at: float
    ) -> tuple[GestureEvent, ...]:
        threshold = self.config.gesture_min_confidence
        if threshold is None:
            return ()
        events = []
        visible_ids = {hand.track_id for hand in hands if hand.track_id}
        for hand in hands:
            if hand.track_id is None:
                continue
            state = self._states.setdefault(hand.track_id, _GestureState())
            label, confidence = hand.gesture, hand.gesture_confidence
            if hand.gesture_candidates is not None:
                control = [(name, score) for name, score in hand.gesture_candidates
                           if name in SUPPORTED]
                # Simultaneous open and fist is not a temporal transition.
                if len(control) > 1:
                    self._states.pop(hand.track_id, None)
                    continue
                label, confidence = control[0] if control else (None, 0.0)
            valid = label in SUPPORTED and confidence >= threshold
            if not valid:
                if captured_at - state.last_valid_at > self.config.gesture_gap_reset_seconds:
                    state.candidate = None
                    state.emitted = None
                continue
            state.last_valid_at = captured_at
            if label != state.candidate:
                state.candidate = label
                state.candidate_since = captured_at
                state.emitted = None
                continue
            if (
                state.emitted != label
                and captured_at - state.candidate_since >= self.config.gesture_confirm_seconds
                and captured_at - state.last_emitted_at >= self.config.gesture_cooldown_seconds
            ):
                events.append(GestureEvent(
                    gesture=label,
                    hand_track_id=hand.track_id,
                    confidence=confidence,
                    confirmed_at=captured_at,
                ))
                state.emitted = label
                state.last_emitted_at = captured_at
        for track_id in tuple(self._states):
            state = self._states[track_id]
            if (
                track_id not in visible_ids
                and captured_at - state.last_valid_at > self.config.hand_target_release_seconds
            ):
                del self._states[track_id]
        return tuple(events)
