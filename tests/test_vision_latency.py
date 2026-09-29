import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('latency_analysis', Path(__file__).resolve().parents[1] / 'scripts/analyze_vision_latency.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class LatencyAnalysisTest(unittest.TestCase):
    def test_first_consumption_is_not_reuse_and_old_targets_are_not_misattributed(self):
        rows = [dict(type='start', t=0), dict(type='perception', t=11.1,
            seq=1, captured_at=11, started=11.01, resized=11.011,
            face_done=11.02, hands_started=11.021, hands_done=11.09,
            completed=11.091, published=11.1, faces=1, hands=1)]
        for target_at, write_done in [(11,11.12),(11,11.16),(10,11.20)]:
            rows.append(dict(type='cycle', t=write_done, interval=.04,
                cycle_start=write_done-.001, target_at=target_at,
                write_done=write_done, write_ms=1))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in rows))
            result=module.analyze(path)
        self.assertEqual(result['control_unmatched'],1)
        self.assertEqual(result['metrics']['capture_to_first_write']['n'],1)
        self.assertEqual(result['metrics']['capture_to_first_write']['p50'],120)
        self.assertEqual(result['metrics']['reused_control_target_age']['p50'],160)
