"""Hardware-free checks for the formal vision foundation."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from lelamp.app import LampApp
from lelamp.vision.config import VisionConfig, load_vision_config
from lelamp.vision.controller import VisionController
from lelamp.vision.target import FaceTargetManager
from lelamp.vision.types import (
    FaceObservation, FramePacket, HandObservation, TrackingTarget,
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


class FakeVision:
    def __init__(self):
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
    def read_action(self):
        return {
            "base_yaw.pos": 0.0, "base_pitch.pos": 0.0,
            "elbow_pitch.pos": 0.0, "wrist_roll.pos": 0.0,
            "wrist_pitch.pos": 0.0,
        }
    def send_tracking_action(self, action): pass
    def close(self): pass


class VisualTrackingControlTests(unittest.IsolatedAsyncioTestCase):
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


class FakeLighting:
    def work_light(self, tone, brightness, seconds): pass
    def fade_off(self, seconds): pass
    def close(self): pass


class AppVisionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_and_work_light_own_vision_lifecycle(self):
        vision = FakeVision()
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
