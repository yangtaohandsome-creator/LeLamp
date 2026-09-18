from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

import numpy as np

from lelamp.test.test_aec_barge_in import (
    RATE,
    _read_wav,
    _resample,
    build_pipeline,
    prepare_reference,
)


class AecDiagnosticTests(unittest.TestCase):
    def test_pipeline_keeps_probe_and_dsp_in_one_process(self):
        command = build_pipeline(
            reference_wav=Path("/tmp/ref.wav"),
            raw_wav=Path("/tmp/raw.wav"),
            clean_wav=Path("/tmp/clean.wav"),
            channel="channel_1",
            reference_delay_ms=40,
            suppression_level="low",
            capture_device="hw:test,0",
            playback_device="plughw:test,0",
        )
        joined = " ".join(command)
        self.assertIn("webrtcechoprobe0", joined)
        self.assertIn("webrtcdsp", joined)
        self.assertIn("mic_channels.src_1", joined)
        self.assertIn("ts-offset=40000000", joined)
        self.assertIn("echo-suppression-level=low", joined)
        self.assertNotIn("shell=True", joined)

    def test_reference_is_48k_mono_and_exact_duration(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            samples = (np.sin(np.arange(8000) * 0.05) * 12000).astype("<i2")
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(samples.tobytes())
            result = prepare_reference(
                root / "reference.wav",
                duration_seconds=2.0,
                source_wav=source,
                text="unused",
                volume_percent=110,
            )
            audio, rate = _read_wav(result)
            self.assertEqual(rate, RATE)
            self.assertEqual(len(audio), RATE * 2)
            self.assertLessEqual(float(np.max(np.abs(audio))), 1.0)

    def test_resample_preserves_duration(self):
        source = np.ones(24_000, dtype=np.float32)
        converted = _resample(source, 24_000, 48_000)
        self.assertEqual(len(converted), 48_000)


if __name__ == "__main__":
    unittest.main()
