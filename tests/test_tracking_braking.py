import unittest
import numpy as np
from lelamp.motion.visual_tracking import braking_velocity
class BrakingTests(unittest.TestCase):
    def test_far_target_retains_requested_speed(self):
        np.testing.assert_allclose(braking_velocity(np.array([60.,-30]),np.array([60.,-40]),np.array([40.,-20]),np.array([210.,140]),.15,.04),[60,-30])
    def test_near_target_brakes_symmetrically(self):
        np.testing.assert_allclose(braking_velocity(np.array([9.,-9]),np.array([5.,-5]),np.array([35.,-35]),np.array([210.,210]),.15,.04),[0,0])
    def test_moving_away_does_not_subtract_wrong_direction_travel(self):
        np.testing.assert_allclose(braking_velocity(np.array([-9.]),np.array([-5.]),np.array([35.]),np.array([210.]),.15,.04),[-9])
