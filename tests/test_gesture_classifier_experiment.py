"""Offline gesture-chain checks; no camera, TFLite runtime, or robot required."""
from __future__ import annotations

import unittest

import numpy as np

from lelamp.vision.gesture_classifier_experiment import (
    palm_aligned_inputs, preprocess_landmarks, right_hand_score, image_roll_inputs,
)


class GestureClassifierExperimentTests(unittest.TestCase):
    def test_stock_preprocessing_corrects_image_aspect_and_uses_wrist_origin(self):
        points = np.zeros((21, 3), dtype=np.float32)
        points[:] = (0.5, 0.5, 0.0)
        points[5] = (0.7, 0.5, 0.1)
        points[17] = (0.5, 0.7, -0.1)
        actual = preprocess_landmarks(points, image_size=(640, 480))
        span = 0.2 + 1e-5
        np.testing.assert_allclose(actual[5], (0.2 / span, 0, 0.1 / span), rtol=1e-4)
        np.testing.assert_allclose(actual[17], (0, 0.15 / span, -0.1 / span), rtol=1e-4)
        np.testing.assert_allclose(actual[0], (0, 0, 0))

    def test_palm_alignment_is_stable_under_3d_rigid_rotation(self):
        points = np.zeros((21, 3), dtype=np.float32)
        points[5] = (-0.4, 0.8, 0)
        points[9] = (0, 0.9, 0)
        points[17] = (0.4, 0.8, 0)
        points[8] = (-0.5, 1.5, 0.2)
        points[12] = (0, 1.6, -0.1)
        points[20] = (0.5, 1.5, 0.1)
        rotated = np.stack((-points[:, 1], points[:, 2], -points[:, 0]), axis=1)
        image = points.copy()
        image[:, 0] = 0.5 + points[:, 0] / 4
        frontal = palm_aligned_inputs(image, points)
        turned = palm_aligned_inputs(image, rotated)
        for first, second in zip(frontal, turned):
            np.testing.assert_allclose(first, second, atol=1e-5)

    def test_image_roll_keeps_geometry_and_removes_inversion(self):
        points = np.zeros((21, 3), dtype=np.float32)
        points[9] = (0, -0.3, 0.02)
        points[5] = (-0.2, -0.2, -0.03)
        points[17] = (0.2, -0.2, 0.01)
        expected = image_roll_inputs(points + (0.5, 0.5, 0), (640, 480))
        for angle in (0, 0.5, 1.57, 3.14, 4.7):
            # Rotate in aspect-corrected space, then restore normalized pixels.
            actual_points = points.copy()
            actual_points[:, 1] *= 480 / 640
            c, sn = np.cos(angle), np.sin(angle)
            actual_points[:, :2] = actual_points[:, :2] @ np.array([[c, sn], [-sn, c]])
            actual_points[:, 1] *= 640 / 480
            actual_points += (0.5, 0.5, 0)
            actual = image_roll_inputs(actual_points, (640, 480))
            np.testing.assert_allclose(actual, expected, atol=3e-5)
            reflected = image_roll_inputs(actual_points, (640, 480), flip_z=True)
            np.testing.assert_allclose(reflected[:, :2], actual[:, :2])
            np.testing.assert_allclose(reflected[:, 2], -actual[:, 2])
        with self.assertRaises(ValueError):
            image_roll_inputs(np.zeros((21, 3)), (640, 480))

    def test_handedness_score_is_right_probability(self):
        self.assertAlmostEqual(right_hand_score("Right", 0.8), 0.8)
        self.assertAlmostEqual(right_hand_score("Left", 0.8), 0.2)
        with self.assertRaises(ValueError):
            right_hand_score("unknown", 0.8)

    def test_bad_landmarks_rejected_before_inference(self):
        with self.assertRaises(ValueError):
            preprocess_landmarks(np.full((21, 3), np.nan))
        with self.assertRaises(ValueError):
            palm_aligned_inputs(np.zeros((21, 3)), np.zeros((21, 3)))


if __name__ == "__main__":
    unittest.main()
