import os
import unittest
from unittest.mock import Mock, patch

from lelamp.voice import tts


class TtsBackendTests(unittest.TestCase):
    def tearDown(self):
        tts._edge_client = None

    def test_remote_backend_stays_available(self):
        with patch.dict(os.environ, {"TTS_BACKEND": "remote"}), patch.object(
            tts, "_speak_remote", return_value=(0.1, 0.2, 0.3)
        ) as remote:
            self.assertEqual(tts.speak("测试"), (0.1, 0.2, 0.3))
            remote.assert_called_once_with("测试", None)

    def test_edge_failure_falls_back_to_remote(self):
        edge = Mock()
        edge.speak.side_effect = RuntimeError("not ready")
        with patch.dict(os.environ, {
            "TTS_BACKEND": "edge",
            "TTS_FALLBACK_BACKEND": "remote",
        }), patch.object(tts, "_get_edge_client", return_value=edge), patch.object(
            tts, "_speak_remote", return_value=(0.2, 0.3, 0.4)
        ) as remote:
            self.assertEqual(tts.speak("测试"), (0.2, 0.3, 0.4))
            remote.assert_called_once_with("测试", None)

    def test_preconnect_only_runs_for_edge_backend(self):
        edge = Mock()
        with patch.object(tts, "_get_edge_client", return_value=edge):
            with patch.dict(os.environ, {"TTS_BACKEND": "remote"}):
                tts.preconnect_tts()
            edge.preconnect.assert_not_called()
            with patch.dict(os.environ, {"TTS_BACKEND": "edge"}):
                tts.preconnect_tts()
            edge.preconnect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
