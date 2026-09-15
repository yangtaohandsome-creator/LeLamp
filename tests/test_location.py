import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lelamp.agent.openclaw import build_messages
from lelamp.location import refresh_location, resolve_location


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps({
            "success": True,
            "ip": "203.0.113.1",
            "country": "China",
            "region": "Shanghai",
            "city": "Shanghai",
            "timezone": {"id": "Asia/Shanghai"},
        }).encode()


class LocationTests(unittest.TestCase):
    def test_refresh_caches_city_and_agent_uses_it(self):
        old_city = os.environ.pop("LELAMP_LOCATION_CITY", None)
        old_timezone = os.environ.pop("LELAMP_TIMEZONE", None)
        try:
            with tempfile.TemporaryDirectory() as directory, patch(
                "lelamp.location.urlopen", return_value=FakeResponse()
            ):
                location = refresh_location(Path(directory))
                cached = json.loads(
                    (Path(directory) / "runtime_state/location.json").read_text()
                )
            self.assertEqual(location.city, "Shanghai")
            self.assertEqual(cached["city"], "Shanghai")
            self.assertEqual(cached["timezone"], "Asia/Shanghai")
            self.assertEqual(os.environ["LELAMP_TIMEZONE"], "Asia/Shanghai")
            messages = build_messages("今天天气怎么样")
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("Shanghai", messages[0]["content"])
            self.assertIn("Asia/Shanghai", messages[0]["content"])
            self.assertEqual(messages[1]["content"], "今天天气怎么样")
        finally:
            if old_city is None:
                os.environ.pop("LELAMP_LOCATION_CITY", None)
            else:
                os.environ["LELAMP_LOCATION_CITY"] = old_city
            if old_timezone is None:
                os.environ.pop("LELAMP_TIMEZONE", None)
            else:
                os.environ["LELAMP_TIMEZONE"] = old_timezone

    def test_failed_refresh_reuses_last_successful_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("lelamp.location.urlopen", return_value=FakeResponse()):
                refresh_location(root)
            with patch("lelamp.location.urlopen", side_effect=OSError("offline")):
                location, refreshed = resolve_location(root)
            self.assertFalse(refreshed)
            self.assertEqual((location.city, location.timezone), ("Shanghai", "Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()
