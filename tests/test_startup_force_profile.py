import os
import unittest
from unittest.mock import patch
from lelamp.motion.config import servo_startup_force_profile

class StartupForceTests(unittest.TestCase):
    @patch('lelamp.motion.config.load_motion_config')
    def test_explicit_rollback(self, _):
        with patch.dict(os.environ, {'MOTION_SERVO_STARTUP_FORCE_EXPERIMENT':'0'},clear=True):
            self.assertEqual(servo_startup_force_profile(),{'base_yaw':16,'wrist_pitch':16})

    @patch('lelamp.motion.config.load_motion_config')
    def test_two_joints_only(self, _):
        with patch.dict(os.environ, {'MOTION_SERVO_STARTUP_FORCE_EXPERIMENT':'1'},clear=True):
            self.assertEqual(servo_startup_force_profile(),{'base_yaw':48,'wrist_pitch':48})

    @patch('lelamp.motion.config.load_motion_config')
    def test_reject_untested_higher_value(self, _):
        with patch.dict(os.environ, {'MOTION_SERVO_STARTUP_FORCE_EXPERIMENT':'1','MOTION_SERVO_STARTUP_FORCE_BASE_YAW':'100'},clear=True):
            with self.assertRaises(ValueError):servo_startup_force_profile()
