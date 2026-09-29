import asyncio
from dataclasses import replace
import time
import unittest
import numpy as np
from lelamp.motion.velocity_profile import velocity_step
from lelamp.motion.visual_tracking import VisualTrackingConfig, VisualTrackingRunner
from lelamp.vision.types import TrackingDirective, TrackingTarget


class VelocityProfileTests(unittest.TestCase):
    def test_exact_first_ramp_integration(self):
        dx, v, a = velocity_step([0], [0], [80], [210], [1000], .04)
        np.testing.assert_allclose(dx, [1000 * .04**3 / 6])
        np.testing.assert_allclose(v, [.8])
        np.testing.assert_allclose(a, [40])

    def test_full_transition_ends_at_target_without_overshoot(self):
        for target in (0., .1, -2., 80., -80.):
            v, a = np.zeros(1), np.zeros(1)
            values = []
            for _ in range(200):
                _, v, a = velocity_step(v, a, [target], [210], [1000], .04)
                values.append(v[0])
            self.assertAlmostEqual(v[0], target)
            self.assertAlmostEqual(a[0], 0)
            self.assertLessEqual(max(values), max(0, target) + 1e-8)
            self.assertGreaterEqual(min(values), min(0, target) - 1e-8)

    def test_replans_reversal_and_stop_with_bounds_and_variable_dt(self):
        rng = np.random.default_rng(1234)
        v, a = np.zeros(2), np.zeros(2)
        maximum, limit, jerk = np.array([80, 40]), np.array([210, 140]), np.array([1000, 700])
        for index in range(2500):
            wanted = rng.uniform(-maximum, maximum) if index < 2000 else np.zeros(2)
            dt = rng.uniform(.001, .1)
            dx, nv, na = velocity_step(v, a, wanted, limit, jerk, dt)
            self.assertTrue(np.all(np.abs(nv) <= maximum + 1e-8))
            self.assertTrue(np.all(np.abs(na) <= limit + 1e-8))
            self.assertTrue(np.all(np.abs(na-a) <= jerk*dt + 1e-8))
            self.assertTrue(np.all(np.abs(nv-v) <= limit*dt + 1e-8))
            self.assertTrue(np.all(np.abs(dx) <= maximum*dt + 1e-8))
            v, a = nv, na
        np.testing.assert_allclose(v, 0, atol=1e-9)
        np.testing.assert_allclose(a, 0, atol=1e-9)

    def test_partitioning_same_profile_gives_same_displacement(self):
        dx, v, a = velocity_step([12], [80], [-30], [210], [1000], .2)
        total, sv, sa = np.zeros(1), np.array([12.]), np.array([80.])
        for _ in range(20):
            step, sv, sa = velocity_step(sv, sa, [-30], [210], [1000], .01)
            total += step
        np.testing.assert_allclose(total, dx, atol=1e-9)
        np.testing.assert_allclose(sv, v, atol=1e-9)
        np.testing.assert_allclose(sa, a, atol=1e-9)


class RunnerProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_hold_loss_and_limits_still_override_profile(self):
        class Motion:
            def __init__(self): self.sent = []
            async def tracking_home(self): pass
            def tracking_bounds(self, lo, hi): return ([-.01, -.01], [.01, .01])
            def read_action(self): return {'base_yaw.pos': 0., 'wrist_pitch.pos': 0.}
            def send_tracking_action(self, action): self.sent.append(action.copy())
        motion = Motion()
        config = VisualTrackingConfig(100, 5, (.5, .5), np.zeros(2), 2.8, 0,
            np.array([80., 40.]), np.array([210., 140.]), np.array([-1., -1.]),
            np.array([1., 1.]), jerk_limit_enabled=True)
        target = TrackingTarget('hand', 'hand-1', (.1, .1), (0., 0.), time.monotonic(), .9)
        directive = TrackingDirective('follow', target, 'hand')
        runner = VisualTrackingRunner(motion, lambda: directive, config=config,
                                      response_matrix=np.eye(2)*.02)
        task = asyncio.create_task(runner.run())
        try:
            await asyncio.sleep(.1)
            self.assertTrue(motion.sent)
            self.assertTrue(all(abs(a[k]) <= .01 for a in motion.sent for k in a))
            for mode in ('hold', 'follow'):
                directive = TrackingDirective(mode, None, 'hand')
                await asyncio.sleep(.025)
                n = len(motion.sent)
                await asyncio.sleep(.04)
                self.assertEqual(len(motion.sent), n)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
