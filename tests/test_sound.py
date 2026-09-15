import os
import unittest
from unittest.mock import patch

from lelamp.audio import SoundPlayer


class SoundPlayerTests(unittest.TestCase):
    def test_disabled_sound_has_no_cue_and_does_not_play(self):
        player = SoundPlayer()
        with patch.dict(os.environ, {"SOUND_ENABLED": "0", "SOUND_WAKE": "wake.wav"}):
            self.assertFalse(player.has_cue("wake"))
            self.assertEqual(player.play("wake"), 0.0)

    def test_rejects_path_outside_sound_directory(self):
        player = SoundPlayer()
        with patch.dict(os.environ, {"SOUND_ENABLED": "1", "SOUND_WAKE": "../wake.wav"}):
            with self.assertRaisesRegex(ValueError, "路径不合法"):
                player.cue_path("wake")


if __name__ == "__main__":
    unittest.main()
