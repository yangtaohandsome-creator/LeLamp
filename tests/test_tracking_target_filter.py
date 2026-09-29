import unittest
import numpy as np
from lelamp.motion.target_filter import AdaptiveTargetFilter


class AdaptiveFilterTests(unittest.TestCase):
    def test_repeated_frame_does_not_advance(self):
        f = AdaptiveTargetFilter()
        f.update([0, 0], 1, 'hand1', [0, 0])
        out = f.update([.02, 0], 1.1, 'hand1', [0, 0])
        for _ in range(5):
            np.testing.assert_array_equal(f.update([.02, 0], 1.1, 'hand1', [0, 0]), out)

    def test_resets_on_switch_gap_or_backward_time(self):
        for key, stamp in [('hand2', 1.2), ('hand1', 2), ('hand1', .9)]:
            f = AdaptiveTargetFilter()
            f.update([0, 0], 1, 'hand1', [0, 0])
            np.testing.assert_array_equal(f.update([1, 1], stamp, key, [0, 0]), [1, 1])

    def test_small_noise_attenuated_and_fast_motion_less_filtered(self):
        slow, fast = AdaptiveTargetFilter(), AdaptiveTargetFilter()
        for f in (slow, fast):
            f.update([0, 0], 1, 'a', [0, 0])
        a = slow.update([.01, 0], 1.1, 'a', [.01, 0])[0] / .01
        b = fast.update([.3, 0], 1.1, 'a', [.3, 0])[0] / .3
        self.assertLess(a, .6)
        self.assertGreater(b, .9)

    def test_camera_motion_removal_preserves_stationary_target(self):
        f = AdaptiveTargetFilter()
        response = np.array([[-.018, -.0005], [-.0003, -.022]])
        stable = np.array([.5, .5])
        for i in range(20):
            capture_q = np.array([i * .2, i * .1])
            current_q = capture_q + [.3, .2]
            raw = stable + response @ capture_q
            corrected = raw + response @ (current_q - capture_q)
            filtered = f.update(corrected - response @ current_q,
                                1 + i * .1, 'a', [.01, .01])
            np.testing.assert_allclose(filtered + response @ current_q, corrected)
            np.testing.assert_allclose(f.speed, [0, 0], atol=1e-12)
