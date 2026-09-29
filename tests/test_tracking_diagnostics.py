import json
from pathlib import Path
import tempfile
import unittest
from lelamp.motion.tracking_diagnostics import Recorder

class RecordingTests(unittest.TestCase):
    def test_preserves_marks_and_reports_loss(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'record.jsonl'
            r=Recorder(p)
            for i in range(20):
                r.emit(dict(type='cycle', cycle=i))
            r.emit(dict(type='jitter_mark'))
            r.close()
            data=[json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual([x['cycle'] for x in data if x['type']=='cycle'],list(range(20)))
            self.assertEqual(data[-2]['type'],'jitter_mark')
            self.assertEqual(data[-1]['dropped'],0)
            self.assertIsNone(r.error)
            with self.assertRaises(FileExistsError):
                Recorder(p)
