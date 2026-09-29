import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/barge_in/dataset.jsonl"


class BargeInDatasetTests(unittest.TestCase):
    def test_manifest_has_unique_ids_and_no_motion_samples(self):
        rows = [json.loads(line) for line in MANIFEST.read_text().splitlines() if line]
        ids = [row["id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(rows)
        self.assertTrue(all(row["motion_active"] is False for row in rows))

    def test_scored_rows_have_supported_evaluation(self):
        rows = [json.loads(line) for line in MANIFEST.read_text().splitlines() if line]
        supported = {"vad_replay", "retention_record"}
        self.assertTrue(
            all(not row.get("scored") or row["evaluation"] in supported for row in rows)
        )


if __name__ == "__main__":
    unittest.main()
