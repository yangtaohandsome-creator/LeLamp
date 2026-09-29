import unittest
import numpy as np
from lelamp.motion.visual_tracking import JointPositionHistory

class EgoCompensationTests(unittest.TestCase):
    def history(self):
        h=JointPositionHistory()
        for t,q in [(1,[0,0]),(1.04,[4,-2]),(1.08,[8,-4])]: h.add(t,q)
        return h

    def test_interpolates_actual_movement_and_response_sign(self):
        delta,stamp=self.history().displacement(1.02,1.09,.1)
        np.testing.assert_allclose(delta,[6,-3])
        response=np.array([[-.02,0],[0,-.02]])
        np.testing.assert_allclose(response@delta,[-.12,.06])
        self.assertEqual(stamp,1.08)

    def test_no_extrapolation_or_stale_feedback(self):
        h=self.history()
        for capture,now in [(.99,1.09),(1.09,1.10),(1.02,1.3)]:
            self.assertIsNone(h.displacement(capture,now,.1))

    def test_rejects_gap_and_invalid_sample(self):
        h=self.history()
        h.add(1.09,[float('nan'),0])
        self.assertEqual(len(h.samples),3)
        h.add(1.4,[9,0])
        self.assertIsNone(h.displacement(1.02,1.41,.1))

    def test_stationary_feedback_cannot_invent_command_motion(self):
        h=JointPositionHistory()
        h.add(1,[2,3]);h.add(1.04,[2,3])
        np.testing.assert_array_equal(h.displacement(1.02,1.05,.1)[0],[0,0])
