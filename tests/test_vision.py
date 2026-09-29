"""Hardware-free checks for the formal vision foundation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from lelamp.app import LampApp
from lelamp.vision.config import VisionConfig, load_vision_config, save_hand_calibration
from lelamp.vision.controller import VisionController
from lelamp.vision.target import FaceTargetManager
from lelamp.vision.gestures import StableGestureDetector
from lelamp.vision.hand_target import HandTargetManager
from lelamp.vision.hands import palm_scale
from lelamp.vision.hand_pose import finger_pip_angles, normalize_world_landmarks
from lelamp.vision.tracking import (
    FACE_FOLLOW, HAND_FOLLOW, HAND_HOLD, NestedTrackingSession,
)
from lelamp.vision.types import (
    FaceObservation, FramePacket, GestureEvent, HandObservation,
    HandTrackingCandidate, TrackingDirective, TrackingTarget, VisionSnapshot,
)
from lelamp.motion.visual_tracking import (
    VisualTrackingConfig, VisualTrackingRunner, load_response_matrix,
)


def make_config(**overrides) -> VisionConfig:
    values = {
        "enabled": True,
        "camera": 0,
        "capture_width": 640,
        "capture_height": 480,
        "capture_fps": 30.0,
        "capture_fourcc": "MJPG",
        "rotation": 0,
        "model_width": 320,
        "model_height": 240,
        "inference_hz": 20.0,
        "result_max_age_ms": 250.0,
        "camera_start_timeout_seconds": 0.5,
        "yunet_model": Path("face.onnx"),
        "gesture_model": Path("gesture.task"),
        "face_score_threshold": 0.7,
        "face_nms_threshold": 0.3,
        "face_top_k": 5000,
        "face_target_min_area": 0.025,
        "face_target_center_weight": 0.15,
        "face_association_max_distance": 0.22,
        "face_association_min_iou": 0.05,
        "face_association_min_size_ratio": 0.35,
        "face_target_predict_seconds": 0.3,
        "face_target_release_seconds": 1.2,
        "face_velocity_smoothing": 0.45,
        "max_hands": 2,
        "hand_detection_confidence": 0.5,
        "hand_presence_confidence": 0.5,
        "hand_tracking_confidence": 0.5,
    }
    values.update(overrides)
    return VisionConfig(**values)


class FakeFrameSource:
    def __init__(self, config):
        self.config = config
        self.packet = None
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        def produce():
            sequence = 0
            while not self.stop_event.wait(0.005):
                sequence += 1
                self.packet = FramePacket(
                    sequence,
                    time.monotonic(),
                    self.config.capture_size,
                    self.config.model_size,
                    0,
                    False,
                    np.zeros((480, 640, 3), dtype=np.uint8),
                )
        self.thread = threading.Thread(target=produce, daemon=True)
        self.thread.start()

    def wait_for_first_frame(self, timeout):
        deadline = time.monotonic() + timeout
        while self.packet is None and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.packet

    def latest(self):
        return self.packet

    def stop(self, timeout=1):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout)

    def health(self):
        return {
            "status": "running" if not self.stop_event.is_set() else "stopped",
            "error": None,
            "latest_sequence": self.packet.sequence if self.packet else None,
        }


class FakeFace:
    def __init__(self, _config): pass
    def detect(self, _image):
        return (FaceObservation(None, (0.1, 0.1, 0.4, 0.4), (0.3, 0.3), (), 0.9),)
    def close(self): pass


class FakeHands:
    def __init__(self, _config): pass
    def recognize(self, _image, _timestamp_ms):
        points = tuple((0.5, 0.5) for _ in range(21))
        return (HandObservation(None, "Right", points, (0.5, 0.5), "Open_Palm", 0.8),)
    def close(self): pass


class VisionFoundationTests(unittest.TestCase):
    def test_device_hand_calibration_overrides_blank_project_values(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config_path = directory / "vision.conf"
            calibration_path = directory / "hand.json"
            config_path.write_text(
                "VISION_ENABLED=1\nVISION_CAPTURE_FOURCC=MJPG\n"
                "VISION_HAND_NEAR_ENTER_SCALE=\n"
                "VISION_HAND_NEAR_EXIT_SCALE=\n"
                "VISION_GESTURE_MIN_CONFIDENCE=\n"
            )
            with patch.dict(
                "os.environ",
                {"VISION_HAND_CALIBRATION_FILE": str(calibration_path)},
                clear=False,
            ):
                save_hand_calibration(0.22, 0.19, 0.5)
                config = load_vision_config(config_path)
        self.assertTrue(config.hand_control_calibrated)
        self.assertEqual(
            (config.hand_near_enter_scale, config.hand_near_exit_scale,
             config.gesture_min_confidence),
            (0.22, 0.19, 0.5),
        )

    def test_config_reads_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"VISION_INFERENCE_HZ": "8"}, clear=False
        ):
            path = Path(temp) / "vision.conf"
            path.write_text(
                "VISION_ENABLED=1\nVISION_CAPTURE_FOURCC=MJPG\n"
                "VISION_MODEL_WIDTH=320\nVISION_MODEL_HEIGHT=240\n"
            )
            config = load_vision_config(path)
        self.assertTrue(config.enabled)
        self.assertEqual(config.model_size, (320, 240))
        self.assertEqual(config.inference_hz, 8)
        self.assertEqual(config.gesture_exit_sequence_seconds, 3.0)

    def test_joint_controller_publishes_latest_snapshot_without_queueing(self):
        controller = VisionController(
            make_config(),
            camera_factory=FakeFrameSource,
            face_factory=FakeFace,
            hands_factory=FakeHands,
        )
        controller.start()
        deadline = time.monotonic() + 1
        while controller.latest_snapshot() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.12)
        snapshot = controller.latest_snapshot()
        state = controller.state()
        controller.stop()
        self.assertIsNotNone(snapshot)
        self.assertEqual((len(snapshot.faces), len(snapshot.hands)), (1, 1))
        self.assertEqual(snapshot.hands[0].gesture, "Open_Palm")
        self.assertEqual(snapshot.active_face_target_id, "face-1")
        self.assertEqual(snapshot.faces[0].track_id, "face-1")
        self.assertEqual(state["tracking_target"]["track_id"], "face-1")
        self.assertTrue(state["tracking_target"]["visible"])
        self.assertEqual(state["status"], "running")
        self.assertGreaterEqual(state["processed_frames"], 1)
        self.assertGreaterEqual(state["skipped_frames"], 1)

    def test_expired_tracking_target_is_not_returned_to_motion(self):
        controller = VisionController(make_config())
        controller._status = "running"
        controller._tracking_target = TrackingTarget(
            "face", "face-1", (0.5, 0.5), (0.0, 0.0),
            time.monotonic() - 1.0, 0.9,
        )
        self.assertIsNone(controller.latest_tracking_target())


def face(x, y, width, height, confidence=0.9):
    return FaceObservation(
        None,
        (x, y, width, height),
        (x + width / 2, y + height * 0.42),
        (),
        confidence,
    )


class FaceTargetManagerTests(unittest.TestCase):
    def test_selects_foreground_face_and_filters_small_background_candidate(self):
        manager = FaceTargetManager(make_config())
        small = face(0.48, 0.25, 0.08, 0.10)
        foreground = face(0.05, 0.12, 0.32, 0.42)

        update = manager.update((small, foreground), 10.0)

        self.assertTrue(update.visible)
        self.assertEqual(update.eligible_face_count, 1)
        self.assertIsNone(update.faces[0].track_id)
        self.assertEqual(update.faces[1].track_id, "face-1")
        self.assertEqual(update.target.track_id, "face-1")

    def test_keeps_existing_face_when_larger_person_appears_elsewhere(self):
        manager = FaceTargetManager(make_config())
        first = manager.update((face(0.08, 0.12, 0.28, 0.38),), 20.0)
        original_id = first.target.track_id

        update = manager.update((
            face(0.10, 0.12, 0.28, 0.38),
            face(0.52, 0.05, 0.40, 0.55),
        ), 20.1)

        self.assertEqual(update.faces[0].track_id, original_id)
        self.assertIsNone(update.faces[1].track_id)
        self.assertEqual(update.target.track_id, original_id)
        self.assertGreater(update.target.velocity_normalized_per_second[0], 0)

    def test_short_loss_holds_id_then_releases_before_selecting_new_face(self):
        manager = FaceTargetManager(make_config())
        initial = manager.update((face(0.1, 0.1, 0.3, 0.4),), 30.0)

        predicted = manager.update((), 30.2)
        held = manager.update((face(0.65, 0.2, 0.2, 0.3),), 30.7)
        held_id = manager.active_track_id
        replacement = manager.update((face(0.65, 0.2, 0.2, 0.3),), 31.3)

        self.assertFalse(predicted.visible)
        self.assertEqual(predicted.target.track_id, initial.target.track_id)
        self.assertIsNone(held.target)
        self.assertEqual(held_id, initial.target.track_id)
        self.assertEqual(manager.active_track_id, replacement.target.track_id)
        self.assertNotEqual(replacement.target.track_id, initial.target.track_id)


def hand(center=(0.5, 0.5), scale=0.12, gesture="Open_Palm", confidence=0.9):
    points = [(center[0], center[1])] * 21
    points[0] = (center[0], center[1] + scale)
    points[9] = center
    points[5] = (center[0] - scale / 2, center[1])
    points[17] = (center[0] + scale / 2, center[1])
    return HandObservation(
        None, "Right", tuple(points), center, gesture, confidence, scale
    )


class HandPerceptionTests(unittest.TestCase):
    def test_world_hand_normalization_ignores_position_size_and_rotation(self):
        points = [(0.0, 0.0, 0.0)] * 21
        points[0] = (0.0, 0.0, 0.0)
        for base, x in ((5, -0.4), (9, 0.0), (13, 0.2), (17, 0.4)):
            for offset in range(4):
                points[base + offset] = (
                    x, 0.8 + offset * 0.25,
                    (offset / 10 if x < 0 else -offset / 10),
                )
        for offset in range(1, 5):
            points[offset] = (-0.4 - offset * 0.1, offset * 0.15, 0.0)
        points = tuple(points)

        def transformed(point):
            x, y, z = point
            # A rigid rotation around two camera axes, then scale and shift.
            return (3 * (-y + 2), 3 * (z + 1), 3 * (-x - 1))

        original = normalize_world_landmarks(points, "Right")
        rotated = normalize_world_landmarks(
            tuple(transformed(point) for point in points), "Right"
        )
        mirrored = normalize_world_landmarks(
            tuple((-x, y, z) for x, y, z in points), "Left"
        )
        np.testing.assert_allclose(rotated, original, atol=1e-6)
        np.testing.assert_allclose(mirrored, original, atol=1e-6)
        np.testing.assert_allclose(
            finger_pip_angles(rotated), finger_pip_angles(original), atol=1e-6
        )

    def test_world_hand_normalization_rejects_invalid_geometry(self):
        with self.assertRaises(ValueError):
            normalize_world_landmarks(((0.0, 0.0, 0.0),) * 21)

    def calibrated_config(self, **overrides):
        return replace(
            make_config(), hand_near_enter_scale=0.1,
            hand_near_exit_scale=0.08, gesture_min_confidence=0.7,
            gesture_confirm_seconds=0.2, gesture_cooldown_seconds=0.1,
            **overrides,
        )

    def test_palm_scale_uses_palm_bones_not_fingertips(self):
        observation = hand(scale=0.12)
        self.assertAlmostEqual(palm_scale(
            observation.landmarks_normalized, 320, 240
        ), 0.12, places=4)

    def test_hand_target_keeps_id_and_velocity(self):
        manager = HandTargetManager(self.calibrated_config())
        first = manager.update((hand((0.4, 0.5)),), 10.0)
        second = manager.update((hand((0.45, 0.5)),), 10.1)
        self.assertEqual(first.hands[0].track_id, "hand-1")
        self.assertEqual(second.hands[0].track_id, "hand-1")
        self.assertGreater(second.hands[0].velocity_normalized_per_second[0], 0)

    def test_gesture_detector_emits_once_after_time_confirmation(self):
        detector = StableGestureDetector(self.calibrated_config())
        tracked = replace(hand(), track_id="hand-1")
        self.assertEqual(detector.update((tracked,), 20.0), ())
        events = detector.update((tracked,), 20.21)
        self.assertEqual([(item.hand_track_id, item.gesture) for item in events], [
            ("hand-1", "Open_Palm")
        ])
        self.assertEqual(detector.update((tracked,), 20.5), ())

    def test_union_control_does_not_convert_simultaneous_labels_to_sequence(self):
        detector = StableGestureDetector(self.calibrated_config())
        tracked = replace(hand(), track_id='hand-1', gesture='Open_Palm | Victory',
                          gesture_candidates=(('Open_Palm', .9), ('Victory', .9)))
        detector.update((tracked,), 1.0)
        self.assertEqual(detector.update((tracked,), 1.3)[0].gesture, 'Open_Palm')
        conflict = replace(tracked, gesture_candidates=(('Open_Palm', .9), ('Closed_Fist', .9)))
        self.assertEqual(detector.update((conflict,), 1.4), ())
        self.assertEqual(detector.update((tracked,), 1.5), ())
        self.assertEqual(detector.update((tracked,), 1.8)[0].gesture, 'Open_Palm')

    def test_uncalibrated_gesture_detector_never_emits(self):
        detector = StableGestureDetector(make_config())
        tracked = replace(hand(), track_id="hand-1")
        detector.update((tracked,), 1.0)
        self.assertEqual(detector.update((tracked,), 2.0), ())


class FakeTrackingVision:
    def __init__(self, config):
        self.config = config
        self.snapshot = None
        self.face_target = TrackingTarget(
            "face", "face-1", (0.5, 0.5), (0.0, 0.0), 0.0, 0.9
        )
        self.face_visible = True
        self.candidates = ()
        self.active_hand = None

    def latest_snapshot(self): return self.snapshot
    def set_active_hand_target(self, track_id): self.active_hand = track_id
    def latest_tracking_inputs(self):
        return self.snapshot, self.face_target, self.face_visible, self.candidates


class NestedTrackingSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.config = replace(
            make_config(), hand_near_enter_scale=0.1,
            hand_near_exit_scale=0.08, gesture_min_confidence=0.7,
        )
        self.vision = FakeTrackingVision(self.config)
        target = TrackingTarget(
            "hand", "hand-1", (0.7, 0.5), (0.0, 0.0), self.now, 0.9
        )
        self.candidate = HandTrackingCandidate(
            "hand-1", target, 0.12, "Right", "Open_Palm", 0.9,
            True, self.now,
        )
        self.vision.candidates = (self.candidate,)
        self.session = NestedTrackingSession(
            self.vision, self.config, clock=lambda: self.now
        )

    def event(self, sequence, gesture, candidate=None):
        candidate = candidate or self.candidate
        candidate = replace(
            candidate,
            target=(
                replace(candidate.target, captured_at=self.now)
                if candidate.target is not None else None
            ),
            last_seen_at=self.now,
        )
        self.vision.candidates = (candidate,)
        self.vision.snapshot = VisionSnapshot(
            sequence, self.now, self.now, (), (), gesture_events=(
                GestureEvent(gesture, candidate.track_id, 0.9, self.now),
            ),
        )
        return self.session.directive()

    def test_full_nested_hand_cycle_and_distance_only_gates_start(self):
        self.event(1, "Open_Palm")
        directive = self.event(2, "Closed_Fist")
        self.assertEqual((self.session.phase, directive.source), (HAND_FOLLOW, "hand"))
        self.assertEqual(self.session._exit_gestures, [])

        far = replace(self.candidate, palm_scale=0.01)
        self.vision.candidates = (far,)
        directive = self.session.directive()
        self.assertEqual((directive.mode, directive.source), ("follow", "hand"))

        self.event(3, "Open_Palm", far)
        self.assertEqual(self.session.phase, HAND_HOLD)
        self.assertEqual(self.session.directive().mode, "hold")

        self.event(4, "Closed_Fist", far)
        self.assertEqual(self.session.phase, HAND_HOLD)
        self.event(5, "Open_Palm", self.candidate)
        self.event(6, "Closed_Fist", self.candidate)
        self.assertEqual(self.session.phase, HAND_FOLLOW)
        self.event(7, "Open_Palm", self.candidate)
        self.assertEqual(self.session.phase, FACE_FOLLOW)

        self.vision.face_target = None
        self.vision.face_visible = False
        self.assertEqual(self.session.directive().mode, "home")

    def test_hold_survives_hand_release_and_does_not_follow_visible_face(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        self.assertEqual(self.session.phase, HAND_HOLD)

        self.vision.candidates = ()
        self.vision.snapshot = VisionSnapshot(
            4, self.now, self.now, (), (), gesture_events=()
        )
        self.now += self.config.hand_target_release_seconds + 0.1
        directive = self.session.directive()

        self.assertEqual(self.session.phase, HAND_HOLD)
        self.assertEqual((directive.mode, directive.source), ("hold", "hand"))
        self.assertIsNone(self.session.selected_hand_id)
        self.assertIsNone(self.vision.active_hand)
        self.assertEqual(self.session._exit_gestures, [])

    def test_hold_reacquires_near_open_then_fist_after_old_id_expires(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        self.vision.candidates = ()
        self.now += self.config.hand_target_release_seconds + 0.1
        self.session.directive()

        replacement = replace(
            self.candidate,
            track_id="hand-2",
            target=replace(self.candidate.target, track_id="hand-2"),
            last_seen_at=self.now,
        )
        self.event(4, "Open_Palm", replacement)
        directive = self.event(5, "Closed_Fist", replacement)

        self.assertEqual(self.session.phase, HAND_FOLLOW)
        self.assertEqual(self.session.selected_hand_id, "hand-2")
        self.assertEqual((directive.mode, directive.source), ("follow", "hand"))
        self.event(6, "Open_Palm", replacement)
        self.assertEqual(self.session.phase, FACE_FOLLOW)

    def test_hold_requires_same_hand_open_then_fist_inside_transition_window(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        self.assertEqual(self.session.phase, HAND_HOLD)

        self.now += self.config.gesture_transition_timeout_seconds + 0.1
        self.event(4, "Closed_Fist")
        self.assertEqual(self.session.phase, HAND_HOLD)

        self.event(5, "Open_Palm")
        self.now += 0.4
        self.event(6, "Closed_Fist")
        self.assertEqual(self.session.phase, HAND_FOLLOW)

    def test_hold_open_from_other_hand_does_not_authorize_fist(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        replacement = replace(
            self.candidate,
            track_id="hand-2",
            target=replace(self.candidate.target, track_id="hand-2"),
        )

        self.event(4, "Open_Palm", replacement)
        self.event(5, "Closed_Fist", self.candidate)

        self.assertEqual(self.session.phase, HAND_HOLD)

    def test_locked_open_fist_open_exit_behavior_is_unchanged(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        self.now += self.config.gesture_exit_sequence_seconds + 0.1

        self.event(4, "Open_Palm")
        self.now += 0.4
        self.event(5, "Closed_Fist")
        self.assertEqual(self.session.phase, HAND_FOLLOW)
        self.now += 0.4
        self.event(6, "Open_Palm")

        self.assertEqual(self.session.phase, FACE_FOLLOW)

    def test_switching_hand_clears_incomplete_exit_sequence(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.event(3, "Open_Palm")
        replacement = replace(
            self.candidate,
            track_id="hand-2",
            target=replace(self.candidate.target, track_id="hand-2"),
        )

        self.now += 0.5
        self.event(4, "Closed_Fist", replacement)
        self.now += 0.5
        self.event(5, "Open_Palm", replacement)

        self.assertEqual(self.session.phase, HAND_HOLD)

    def test_follow_still_returns_to_face_after_hand_release_timeout(self):
        self.event(1, "Open_Palm")
        self.event(2, "Closed_Fist")
        self.vision.candidates = ()
        self.now += self.config.hand_target_release_seconds + 0.1

        directive = self.session.directive()

        self.assertEqual(self.session.phase, FACE_FOLLOW)
        self.assertEqual((directive.mode, directive.source), ("follow", "face"))

    def test_voice_hand_requires_calibration(self):
        vision = FakeTrackingVision(make_config())
        session = NestedTrackingSession(vision, vision.config)
        with self.assertRaisesRegex(ValueError, "尚未标定"):
            session.request_hand()


class FakeVision:
    def __init__(self):
        self.config = make_config()
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self):
        self.running = True
        self.starts += 1
        return True

    def stop(self):
        self.running = False
        self.stops += 1

    def state(self):
        return {
            "enabled": True,
            "requested": self.running,
            "status": "running" if self.running else "stopped",
            "running": self.running,
            "snapshot": None,
        }

    def latest_tracking_target(self):
        return None


class FakeMotion:
    robot = None
    async def standby(self, transition_seconds=None): pass
    async def sleep(self): pass
    async def work_pose(self, pose="high", transition_seconds=None): pass
    async def tracking_home(self, transition_seconds=None): pass
    def tracking_bounds(self, minimum, maximum):
        return tuple(minimum), tuple(maximum)
    def read_action(self):
        return {
            "base_yaw.pos": 0.0, "base_pitch.pos": 0.0,
            "elbow_pitch.pos": 0.0, "wrist_roll.pos": 0.0,
            "wrist_pitch.pos": 0.0,
        }
    def send_tracking_action(self, action): pass
    def close(self): pass


class VisualTrackingControlTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_response_cannot_drive_remapped_wrist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visual_response.json"
            payload = {
                "controlled_joints": ["base_yaw", "wrist_pitch"],
                "response_matrix": [[0.01, 0], [0, 0.01]],
            }
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "重新标定"):
                load_response_matrix(path)
            payload["controlled_motor_ids"] = [1, 4]
            path.write_text(json.dumps(payload))
            np.testing.assert_array_equal(
                load_response_matrix(path), np.array([[0.01, 0], [0, 0.01]])
            )

    def config(self):
        return VisualTrackingConfig(
            control_hz=50, feedback_hz=5, setpoint=(0.5, 0.42),
            deadzone=np.array([0.02, 0.02]), position_gain=2,
            max_prediction_seconds=0.1,
            max_velocity=np.array([10.0, 10.0]),
            max_acceleration=np.array([100.0, 100.0]),
            minimum=np.array([-50.0, -50.0]),
            maximum=np.array([50.0, 50.0]),
        )

    async def test_stale_or_missing_target_sends_no_tracking_commands(self):
        class Motion(FakeMotion):
            def __init__(self): self.sent = []
            def send_tracking_action(self, action): self.sent.append(action)
        motion = Motion()
        runner = VisualTrackingRunner(
            motion, lambda: None, config=self.config(),
            response_matrix=np.array([[0.01, 0.0], [0.0, 0.01]]),
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.07)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(motion.sent, [])

    async def test_target_error_drives_rate_limited_commands(self):
        class Motion(FakeMotion):
            def __init__(self): self.sent = []
            def send_tracking_action(self, action): self.sent.append(dict(action))
        motion = Motion()
        target = TrackingTarget(
            "face", "face-1", (0.7, 0.42), (0.0, 0.0),
            time.monotonic(), 0.9,
        )
        runner = VisualTrackingRunner(
            motion, lambda: target, config=self.config(),
            response_matrix=np.array([[0.01, 0.0], [0.0, 0.01]]),
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.07)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreater(len(motion.sent), 1)
        self.assertLess(motion.sent[-1]["base_yaw.pos"], 0)
        self.assertAlmostEqual(motion.sent[-1]["wrist_pitch.pos"], 0, places=3)

    async def test_tracking_saturates_at_effective_hardware_boundary(self):
        class Motion(FakeMotion):
            def __init__(self): self.sent = []
            def tracking_bounds(self, minimum, maximum):
                return (-5.0, -10.0), (5.0, 10.0)
            def send_tracking_action(self, action): self.sent.append(dict(action))

        motion = Motion()
        target = TrackingTarget(
            "face", "face-1", (0.0, 0.42), (0.0, 0.0),
            time.monotonic(), 0.9,
        )
        config = replace(
            self.config(), max_velocity=np.array([100.0, 100.0]),
            max_acceleration=np.array([1000.0, 1000.0]),
        )
        runner = VisualTrackingRunner(
            motion, lambda: target, config=config,
            response_matrix=np.array([[0.01, 0], [0, 0.01]]),
        )
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.18)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(motion.sent)
        self.assertAlmostEqual(motion.sent[-1]["base_yaw.pos"], 5.0)
        self.assertTrue(all(action["base_yaw.pos"] <= 5.0 for action in motion.sent))
        self.assertEqual(runner.state["limits"]["maximum"], [5.0, 10.0])


class FakeLighting:
    def work_light(self, tone, brightness, seconds): pass
    def fade_off(self, seconds): pass
    def close(self): pass


class AppVisionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_hand_tool_refuses_to_guess_uncalibrated_thresholds(self):
        app = LampApp(
            motion=FakeMotion(), lighting=FakeLighting(), vision=FakeVision()
        )
        outcome = await app.tools.execute("start_hand_tracking")
        self.assertEqual(outcome.status, "failed")
        self.assertIn("尚未标定", outcome.message)
        self.assertEqual(app.current_mode, "normal")
        await app.timers.close()
        await app.alarms.close()
        await app.announcements.close()

    async def test_voice_and_work_light_own_vision_lifecycle(self):
        vision = FakeVision()
        vision.config = replace(vision.config, work_light_hand_enabled=False)
        app = LampApp(
            motion=FakeMotion(), lighting=FakeLighting(), vision=vision
        )
        app._started = True

        await app.set_voice_session_active(True)
        self.assertTrue(vision.running)
        await app.set_voice_session_active(False)
        self.assertFalse(vision.running)

        await app.enter_work_light()
        self.assertTrue(vision.running)
        await app.set_voice_session_active(False)
        self.assertTrue(vision.running)
        self.assertTrue(app.keeps_mode_after_voice_timeout())
        await app.prepare_for_voice_session()
        self.assertEqual(app.current_mode, "work_light")
        await app.exit_work_light()
        self.assertFalse(vision.running)

        await app.timers.close()
        await app.alarms.close()
        await app.announcements.close()


if __name__ == "__main__":
    unittest.main()
