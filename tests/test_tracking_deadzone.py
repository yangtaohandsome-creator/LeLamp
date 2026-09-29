import unittest
import numpy as np
from lelamp.motion.visual_tracking import smooth_deadzone

class SmoothDeadzoneTests(unittest.TestCase):
    def test_inside_and_far_unchanged(self):
        np.testing.assert_array_equal(smooth_deadzone(np.array([.045,-.035]),np.array([.045,.035]),.04),[0,0])
        np.testing.assert_array_equal(smooth_deadzone(np.array([.18,-.3]),np.array([.045,.035]),.04),[.18,-.3])

    def test_boundary_continuity_and_symmetry(self):
        d=np.array([.045,.045])
        small=smooth_deadzone(np.array([.0450001,-.0450001]),d,.04)
        self.assertLess(abs(small[0]),1.01e-7)
        self.assertEqual(small[0],-small[1])
        self.assertLess(smooth_deadzone(np.array([.065]),d[:1],.04)[0],.025)

    def test_monotonic_bounded_and_memoryless(self):
        e=np.linspace(0,.3,1000)
        shaped=smooth_deadzone(e,.045,.04)
        self.assertTrue(np.all(np.diff(shaped)>=0))
        self.assertTrue(np.all(shaped<=e))
        self.assertEqual(smooth_deadzone(np.array([0.]),.045,.04)[0],0)

    def test_rollback(self):
        np.testing.assert_array_equal(smooth_deadzone(np.array([.044,.046]),.045,0),[0,.046])

    def test_local_slope_is_bounded_including_old_steep_band(self):
        for d in (0., .035, .045):
            e=np.linspace(-.5,.5,200001)
            out=smooth_deadzone(e,d,.04)
            slope=np.diff(out)/np.diff(e)
            self.assertTrue(np.all(slope >= -1e-10))
            self.assertLessEqual(float(slope.max()),1.500001)
