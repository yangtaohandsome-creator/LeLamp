"""The calibration command must park before releasing motor torque."""
import argparse
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import calibrate_visual_response as calibration


class CalibrationCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_after_connect_parks_in_sleep_pose(self):
        events = []

        class Camera:
            def __init__(self, config):
                pass

            def start(self):
                events.append("camera_start")

            def wait_for_first_frame(self, timeout):
                return object()

            def health(self):
                return {"negotiated": {"width": 640, "height": 480}}

            def stop(self):
                events.append("camera_stop")

        class Motion:
            def __init__(self, port):
                self.robot = None

            def connect(self):
                self.robot = SimpleNamespace(bus=SimpleNamespace(
                    is_connected=True,
                    motors={
                        "base_yaw": SimpleNamespace(id=1),
                        "wrist_pitch": SimpleNamespace(id=4),
                    },
                ))

            async def move_tracking_raw(self, action, duration):
                raise RuntimeError("motor move failed")

            async def sleep(self):
                events.append("sleep_before_disconnect")

            def close(self):
                events.append("motion_close")

        args = argparse.Namespace(delta=3.0, settle=1.0, frames=7)
        with patch.object(calibration, "LatestFrameSource", Camera), \
             patch.object(calibration, "MotionController", Motion), \
             patch.object(calibration, "load_vision_config", return_value=SimpleNamespace(
                 camera_start_timeout_seconds=1.0,
             )), \
             patch.object(calibration, "tracking_home_action", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "motor move failed"):
                await calibration.run(args)

        self.assertEqual(events[-3:], [
            "sleep_before_disconnect", "motion_close", "camera_stop"
        ])
