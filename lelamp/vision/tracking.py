"""App-owned target arbitration for nested face and hand tracking."""
from __future__ import annotations

import time

from .config import VisionConfig
from .exit_sequence import ExitGestureSequence
from .types import HandTrackingCandidate, TrackingDirective


FACE_FOLLOW = "face_follow"
HAND_ACQUIRE = "hand_acquire"
HAND_FOLLOW = "hand_follow"
HAND_HOLD = "hand_hold"


class NestedTrackingSession:
    """Choose the single tracking source; it never sends hardware commands."""

    def __init__(self, vision, config: VisionConfig | None = None, clock=time.monotonic):
        self.vision = vision
        self.config = config or vision.config
        self.clock = clock
        self.phase = FACE_FOLLOW
        self.selected_hand_id: str | None = None
        self._selected_last_seen: float | None = None
        self._primed_hand_id: str | None = None
        self._prime_deadline = 0.0
        self._acquire_deadline = 0.0
        self._release_armed = False
        self._exit_gestures = ExitGestureSequence()
        self._last_snapshot_sequence = -1
        self._recenter_until_face = False
        self._last_gesture: str | None = None
        self._last_gesture_at: float | None = None
        self.state = {}
        self._publish_state()

    @property
    def calibrated(self) -> bool:
        return self.config.hand_control_calibrated

    def force_face(self) -> None:
        self._return_to_face(recenter=True)
        self.discard_pending_events()

    def request_hand(self) -> None:
        if not self.calibrated:
            raise ValueError("手部近距离和手势置信度阈值尚未标定")
        self.phase = HAND_ACQUIRE
        self.selected_hand_id = None
        self._selected_last_seen = None
        self._release_armed = False
        self._primed_hand_id = None
        self._clear_exit_sequence()
        self._acquire_deadline = self.clock() + self.config.hand_voice_acquire_seconds
        self.vision.set_active_hand_target(None)
        self.discard_pending_events()
        self._publish_state()

    def discard_pending_events(self) -> None:
        self._clear_exit_sequence()
        snapshot = self.vision.latest_snapshot()
        if snapshot is not None:
            self._last_snapshot_sequence = snapshot.source_frame_sequence

    def directive(self) -> TrackingDirective:
        now = self.clock()
        snapshot, face_target, face_visible, candidates = (
            self.vision.latest_tracking_inputs()
        )
        by_id = {item.track_id: item for item in candidates}

        if self.phase == HAND_ACQUIRE:
            candidate = self._unique_near(candidates)
            if candidate is not None:
                self._select_hand(candidate, HAND_FOLLOW, release_armed=False)
                self._clear_exit_sequence()
            elif now >= self._acquire_deadline:
                self._return_to_face(recenter=False)

        if snapshot is not None and snapshot.source_frame_sequence != self._last_snapshot_sequence:
            self._last_snapshot_sequence = snapshot.source_frame_sequence
            unique_near = self._unique_near(candidates)
            for event in snapshot.gesture_events:
                self._last_gesture = event.gesture
                self._last_gesture_at = event.confirmed_at
                candidate = by_id.get(event.hand_track_id)
                self._handle_event(
                    event.gesture, candidate, unique_near, event.confirmed_at
                )

        candidate = by_id.get(self.selected_hand_id) if self.selected_hand_id else None
        if candidate is not None and candidate.visible:
            self._selected_last_seen = candidate.last_seen_at
        if self.selected_hand_id is not None:
            last_seen = (
                candidate.last_seen_at if candidate is not None
                else self._selected_last_seen
            )
            target_expired = (
                last_seen is None
                or now - last_seen > self.config.hand_target_release_seconds
            )
            if target_expired and self.phase == HAND_FOLLOW:
                self._return_to_face(recenter=True)
                candidate = None
            elif target_expired and self.phase == HAND_HOLD:
                # Holding is a mechanical state, not a live hand-target lease.
                # Forget the stale identity while keeping the last safe pose.
                if self._primed_hand_id == self.selected_hand_id:
                    self._primed_hand_id = None
                self.selected_hand_id = None
                self._selected_last_seen = None
                self._clear_exit_sequence()
                self.vision.set_active_hand_target(None)
                candidate = None

        directive = self._make_directive(face_target, face_visible, candidate)
        self._publish_state(now, directive, candidate)
        return directive

    def _handle_event(
        self,
        gesture: str,
        candidate: HandTrackingCandidate | None,
        unique_near: HandTrackingCandidate | None,
        confirmed_at: float,
    ) -> None:
        if self.phase == FACE_FOLLOW:
            if gesture == "Open_Palm" and self._is_near(candidate, entering=True):
                self._primed_hand_id = candidate.track_id
                self._prime_deadline = (
                    confirmed_at + self.config.gesture_transition_timeout_seconds
                )
            elif gesture == "Closed_Fist":
                if confirmed_at > self._prime_deadline:
                    self._primed_hand_id = None
                if (
                    candidate is not None
                    and candidate.track_id == self._primed_hand_id
                    and self._is_near(candidate, entering=False)
                ):
                    self._select_hand(candidate, HAND_FOLLOW, release_armed=True)
                    self._primed_hand_id = None
                    # Face-mode Open_Palm -> Closed_Fist only enters hand
                    # tracking and must never seed the exit gesture sequence.
                    self._clear_exit_sequence()
            return

        if self.phase == HAND_ACQUIRE:
            if (
                gesture == "Closed_Fist" and candidate is not None
                and candidate.track_id == self.selected_hand_id
            ):
                self._release_armed = True
                self._clear_exit_sequence()
            return

        if self.phase == HAND_HOLD:
            if (
                candidate is None
                or unique_near is None
                or candidate.track_id != unique_near.track_id
            ):
                return
            if gesture == "Open_Palm":
                self._primed_hand_id = candidate.track_id
                self._prime_deadline = (
                    confirmed_at + self.config.gesture_transition_timeout_seconds
                )
                self._append_exit_gesture(
                    gesture, candidate.track_id, confirmed_at,
                    hold_baseline=True,
                )
            elif gesture == "Closed_Fist":
                if confirmed_at > self._prime_deadline:
                    self._primed_hand_id = None
                if (
                    candidate.track_id == self._primed_hand_id
                    and self._is_near(candidate, entering=True)
                ):
                    self._append_exit_gesture(
                        gesture, candidate.track_id, confirmed_at
                    )
                    self._select_hand(
                        candidate, HAND_FOLLOW, release_armed=True
                    )
                    self._primed_hand_id = None
            return

        if candidate is None or candidate.track_id != self.selected_hand_id:
            return
        if self.phase == HAND_FOLLOW:
            if gesture == "Closed_Fist":
                self._release_armed = True
                self._append_exit_gesture(
                    gesture, candidate.track_id, confirmed_at
                )
            elif gesture == "Open_Palm" and self._release_armed:
                self._append_exit_gesture(
                    gesture, candidate.track_id, confirmed_at
                )
                if self._exit_sequence_complete():
                    self._return_to_face(recenter=True)
                    return
                self.phase = HAND_HOLD
                self._release_armed = False
                # Reuse the same Open_Palm -> Closed_Fist transition used by
                # face mode; this locking Open_Palm is valid only briefly.
                self._primed_hand_id = candidate.track_id
                self._prime_deadline = (
                    confirmed_at + self.config.gesture_transition_timeout_seconds
                )
                self._exit_gestures[-1].hold_baseline = True

    def _make_directive(self, face_target, face_visible, hand_candidate):
        if self.phase == HAND_FOLLOW:
            if hand_candidate is not None and hand_candidate.target is not None:
                return TrackingDirective("follow", hand_candidate.target, "hand")
            return TrackingDirective("hold", source="hand")
        if self.phase == HAND_HOLD:
            return TrackingDirective("hold", source="hand")
        if self._recenter_until_face:
            if face_visible and face_target is not None:
                self._recenter_until_face = False
                return TrackingDirective("follow", face_target, "face")
            return TrackingDirective("home", source="face")
        if face_target is not None:
            return TrackingDirective("follow", face_target, "face")
        return TrackingDirective("home", source="face")

    def _select_hand(self, candidate, phase, release_armed):
        self.phase = phase
        self.selected_hand_id = candidate.track_id
        self._selected_last_seen = candidate.last_seen_at
        self._release_armed = release_armed
        self._recenter_until_face = False
        self.vision.set_active_hand_target(candidate.track_id)

    def _return_to_face(self, recenter: bool):
        self.phase = FACE_FOLLOW
        self.selected_hand_id = None
        self._selected_last_seen = None
        self._release_armed = False
        self._primed_hand_id = None
        self._clear_exit_sequence()
        self._acquire_deadline = 0.0
        self._recenter_until_face = recenter
        self.vision.set_active_hand_target(None)

    def _clear_exit_sequence(self) -> None:
        self._exit_gestures.clear()

    def _append_exit_gesture(
        self,
        gesture: str,
        hand_track_id: str,
        confirmed_at: float,
        *,
        hold_baseline: bool = False,
    ) -> None:
        self._exit_gestures.add(
            gesture, hand_track_id, confirmed_at,
            self.config.gesture_exit_sequence_seconds, hold_baseline=hold_baseline,
        )

    def _exit_sequence_complete(self) -> bool:
        return self._exit_gestures.complete()

    def _unique_near(self, candidates):
        near = [
            item for item in candidates
            if item.visible and self._is_near(item, entering=True)
        ]
        return near[0] if len(near) == 1 else None

    def _is_near(self, candidate, *, entering: bool) -> bool:
        if candidate is None or not candidate.visible or not self.calibrated:
            return False
        threshold = (
            self.config.hand_near_enter_scale if entering
            else self.config.hand_near_exit_scale
        )
        return candidate.palm_scale >= threshold

    def _publish_state(self, now=None, directive=None, candidate=None):
        now = self.clock() if now is None else now
        lost_ms = None
        if self._selected_last_seen is not None:
            lost_ms = max(0.0, now - self._selected_last_seen) * 1000
        self.state = {
            "tracking_source": "hand" if self.phase.startswith("hand_") and self.phase != HAND_ACQUIRE else "face",
            "tracking_phase": self.phase,
            "active_hand_target_id": self.selected_hand_id,
            "hand_control_calibrated": self.calibrated,
            "hand_near": self._is_near(candidate, entering=True),
            "hand_lost_ms": round(lost_ms, 1) if lost_ms is not None else None,
            "last_gesture": self._last_gesture,
            "last_gesture_at": self._last_gesture_at,
            "directive": directive.mode if directive is not None else None,
        }
