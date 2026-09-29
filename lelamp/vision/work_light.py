"""Isolated work-light hand interaction; no face fallback or hardware access."""
from collections import defaultdict, deque
from statistics import median
import time

from .gestures import StableGestureDetector
from .exit_sequence import ExitGestureSequence
from .types import TrackingDirective


class WorkLightHandSession:
    def __init__(self, vision, clock=time.monotonic):
        self.vision = vision
        self.config = vision.config
        self.clock = clock
        self.detector = StableGestureDetector(self.config)
        self.exit_history = ExitGestureSequence()
        self.exit_requested = False
        self.hand_override_started = False
        self.phase = 'light_hold'
        self.selected = None
        self.last_seen = None
        self.deadline = None
        self.sequence = -1
        self.scales = defaultdict(deque)
        self.choice = None
        self.choice_since = None
        self.prime = None
        self.ambiguous = False
        self.state = {}
        self._publish()

    @property
    def dimmed(self):
        return self.phase in ('light_follow', 'light_lost')

    def discard_pending_events(self):
        self.detector.reset()
        self.exit_history.clear()
        self.exit_requested = False
        self.prime = None
        self.scales.clear()
        self.choice = self.choice_since = None
        snap = self.vision.latest_snapshot()
        if snap is not None:
            self.sequence = snap.source_frame_sequence

    def lock(self):
        self.hand_override_started = False
        self.phase = 'light_hold'
        self.selected = self.last_seen = self.deadline = None
        self.ambiguous = False
        self.vision.set_active_hand_target(None)
        self.discard_pending_events()
        self._publish()

    def request_hand(self):
        if not self.config.hand_control_calibrated:
            raise ValueError('手部近距离和手势置信度阈值尚未标定')
        if self.phase == 'light_follow':
            return
        self.lock()
        self.phase = 'light_acquire'
        self.deadline = self.clock() + 5.0
        self._publish()

    def _pick(self, hands, stamp, recovery=False):
        # A visible owner is never displaced by another closer hand.
        if recovery:
            owner = next((h for h in hands if h.track_id == self.selected), None)
            if owner is not None:
                self.ambiguous = False
                return owner
        self.ambiguous = False
        if len(hands) <= 1:
            self.choice = self.choice_since = None
            return hands[0] if hands else None
        ranked = []
        for hand in hands:
            samples = self.scales[hand.track_id]
            while samples and samples[0][0] < stamp - .3 - 1e-6:
                samples.popleft()
            if len(samples) < 3:
                self.ambiguous = True
                self.choice = self.choice_since = None
                return None
            ranked.append((median(v for _, v in samples), hand))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if ranked[0][0] < ranked[1][0] * self.config.work_light_nearest_ratio:
            self.ambiguous = True
            self.choice = self.choice_since = None
            return None
        hand = ranked[0][1]
        if self.choice != hand.track_id:
            self.choice, self.choice_since = hand.track_id, stamp
        self.ambiguous = stamp - self.choice_since < self.config.gesture_confirm_seconds
        return None if self.ambiguous else hand

    def _follow(self, hand, stamp):
        self.hand_override_started = True
        self.phase, self.selected, self.last_seen = 'light_follow', hand.track_id, stamp
        self.deadline = self.prime = None
        self.vision.set_active_hand_target(hand.track_id)

    def directive(self):
        if self.exit_requested:
            return TrackingDirective('hold', source='hand')
        now = self.clock()
        snap, _, _, candidates = self.vision.latest_tracking_inputs()
        fresh = snap is not None and 0 <= now - snap.captured_at <= self.config.result_max_age_ms / 1000
        hands = tuple(h for h in snap.hands if h.track_id) if fresh else ()
        visible = {h.track_id: h for h in hands}
        targets = {c.track_id: c.target for c in candidates if c.visible and c.target is not None
                   and not c.target.position_is_prediction}
        if self.phase == 'light_follow' and (self.selected not in visible or self.selected not in targets):
            self.phase = 'light_lost'
            self.deadline = self.last_seen + self.config.work_light_lost_seconds
            self.exit_history.clear()
            self.detector.reset()  # Sustained fist must be confirmed anew on return.
            self.prime = None
        if self.phase in ('light_lost', 'light_acquire') and now >= self.deadline:
            self.lock()  # Deadline wins over a late result, even a newly confirmed fist.
            return TrackingDirective('hold', source='hand')
        if fresh and snap.source_frame_sequence != self.sequence:
            self.sequence = snap.source_frame_sequence
            stamp = snap.captured_at
            for hand in hands:
                history = self.scales[hand.track_id]
                history.append((stamp, hand.palm_scale))
                while history and history[0][0] < stamp - .3 - 1e-6:
                    history.popleft()
            for key in tuple(self.scales):
                if not self.scales[key] or stamp - self.scales[key][-1][0] > .3:
                    del self.scales[key]
            chosen = self._pick(hands, stamp, recovery=self.phase == 'light_lost') if self.phase != 'light_follow' else visible.get(self.selected)
            # Only the selected candidate is confirmed. Nearer invalid gestures must
            # not cause a farther fist to be chosen, or queued events to replay.
            if chosen is None:
                self.detector.reset()
                self.exit_history.clear()
                self.prime = None
                events = ()
            else:
                if self.exit_history and self.exit_history[-1].hand_track_id != chosen.track_id:
                    self.exit_history.clear()
                    self.prime = None
                events = self.detector.update((chosen,), stamp)
            if self.phase == 'light_follow':
                self.last_seen = stamp
            for event in events:
                if chosen.track_id not in targets:
                    continue
                near = self.config.hand_control_calibrated and chosen.palm_scale >= self.config.hand_near_enter_scale
                if self.phase == 'light_hold':
                    if event.gesture == 'Open_Palm' and near:
                        self.prime = (event.hand_track_id, event.confirmed_at)
                        if self.hand_override_started:
                            self._exit_event(event, hold_baseline=True)
                    elif event.gesture == 'Closed_Fist':
                        if near and self.prime and self.prime[0] == event.hand_track_id and 0 <= event.confirmed_at - self.prime[1] <= self.config.gesture_transition_timeout_seconds:
                            if self.hand_override_started:
                                self._exit_event(event)
                            else:
                                self.exit_history.clear()
                            self._follow(chosen, stamp)
                        self.prime = None
                elif self.phase == 'light_acquire':
                    if event.gesture == 'Closed_Fist' and near:
                        self._follow(chosen, stamp)
                elif self.phase == 'light_lost':
                    if event.gesture == 'Closed_Fist':
                        self._follow(chosen, stamp)
                elif self.phase == 'light_follow':
                    self._exit_event(event)
                    if event.gesture == 'Open_Palm':
                        if self.exit_history.complete():
                            self.exit_requested = True
                            self._publish(now)
                            return TrackingDirective('hold', source='hand')
                        # Gesture lock alone preserves the exit window and this
                        # Open_Palm as the same short-lived resume baseline.
                        self.phase = 'light_hold'
                        self.last_seen = self.deadline = None
                        self.prime = (event.hand_track_id, event.confirmed_at)
                        self.exit_history[-1].hold_baseline = True
                        self.vision.set_active_hand_target(None)
                        break
        target = targets.get(self.selected) if self.phase == 'light_follow' else None
        self._publish(now)
        return TrackingDirective('follow' if target else 'hold', target, 'hand')

    def _exit_event(self, event, *, hold_baseline=False):
        self.exit_history.add(event.gesture, event.hand_track_id, event.confirmed_at,
                              self.config.gesture_exit_sequence_seconds, hold_baseline=hold_baseline)

    def _publish(self, now=None):
        now = self.clock() if now is None else now
        self.state = dict(tracking_phase=self.phase, tracking_source='hand' if self.phase == 'light_follow' else 'none',
                          active_hand_target_id=self.selected, hand_control_calibrated=self.config.hand_control_calibrated,
                          dimmed=self.dimmed, target_ambiguous=self.ambiguous,
                          lost_remaining_seconds=max(0, self.deadline-now) if self.phase == 'light_lost' else None)
