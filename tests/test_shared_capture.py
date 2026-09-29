import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from lelamp.voice.shared_capture import SharedCapture


class SharedCaptureTests(unittest.TestCase):
    def test_one_stream_supplies_listening_and_realtime_tap(self):
        stereo = np.column_stack((
            np.full(4800, 65536000, dtype="<i4"),
            np.full(4800, -65536000, dtype="<i4"),
        ))
        process = SimpleNamespace(
            stdout=io.BytesIO(stereo.tobytes()), stderr=io.BytesIO(),
            terminate=lambda: None, wait=lambda timeout=None: 0, kill=lambda: None,
        )
        capture = SharedCapture()
        capture.resume()
        with patch("lelamp.voice.shared_capture.subprocess.Popen", return_value=process):
            capture.start()
            block = capture.read_stereo()
            capture.close()
        self.assertEqual(block.shape, (1600, 2))
        self.assertAlmostEqual(float(block[:, 0].mean()), 1000 / 32768, places=5)

    def test_pause_discards_old_audio_before_resume(self):
        capture = SharedCapture()
        capture._blocks.append(np.ones((1600, 2), dtype=np.float32))
        capture.pause()
        self.assertFalse(capture._blocks)
        capture.resume()
        self.assertTrue(capture._listening)


if __name__ == "__main__":
    unittest.main()
