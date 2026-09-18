import asyncio
import unittest
from unittest.mock import patch

import numpy as np

from lelamp.app import LampApp
from lelamp.voice.announcement import (
    AnnouncementInterrupted,
    AnnouncementPriority,
    AnnouncementQueue,
)
from lelamp.voice.barge_in import strip_stop_prefix
from lelamp.voice.barge_in import BargeInUtterance
from lelamp.voice.playback import SpeechInterrupted


class _Lighting:
    def __init__(self):
        self.interrupt_modes = []

    def speaking(self):
        pass

    def interrupted(self, *, work_light=False):
        self.interrupt_modes.append(work_light)

    def close(self):
        pass


class _Motion:
    robot = None

    def close(self):
        pass


class _Session:
    def wait_result(self, _timeout):
        return BargeInUtterance(np.ones(1600, dtype=np.float32), "speech")

    def close(self):
        pass


class _Controller:
    def create_session(self, **_kwargs):
        return _Session()


class BargeInTextTests(unittest.TestCase):
    def test_stop_word_only(self):
        self.assertEqual(strip_stop_prefix("别说了。"), ("", True))

    def test_stop_prefix_preserves_question(self):
        self.assertEqual(
            strip_stop_prefix("等等，我问的是昨天那个"),
            ("我问的是昨天那个", True),
        )

    def test_normal_text_is_untouched(self):
        self.assertEqual(strip_stop_prefix("今天怎么样"), ("今天怎么样", False))


class BargeInQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_interrupt_returns_audio_and_expected_queue_result(self):
        lighting = _Lighting()
        app = LampApp(motion=_Motion(), lighting=lighting)
        app.barge_in = _Controller()
        with patch("lelamp.app.speak", side_effect=SpeechInterrupted):
            handle = await app.submit_announcement(
                announcement_id="reply", source="local_reply", text="较长回答",
                priority=AnnouncementPriority.LOCAL_REPLY,
            )
            await app.drain_announcements()
        result = await handle.wait()
        self.assertTrue(result.success)
        self.assertTrue(result.interrupted)
        captured = app.take_barge_in_utterance()
        self.assertIsNotNone(captured)
        self.assertEqual(len(captured.audio), 1600)
        self.assertEqual(lighting.interrupt_modes, [False])
        await app.announcements.close()

    async def test_work_light_interrupt_uses_brightness_pulse(self):
        lighting = _Lighting()
        app = LampApp(motion=_Motion(), lighting=lighting)
        app.current_mode = "work_light"
        app.barge_in = _Controller()
        with patch("lelamp.app.speak", side_effect=SpeechInterrupted):
            handle = await app.submit_announcement(
                announcement_id="reply", source="local_reply", text="较长回答",
                priority=AnnouncementPriority.LOCAL_REPLY,
            )
            await app.drain_announcements()
        self.assertTrue((await handle.wait()).interrupted)
        self.assertEqual(lighting.interrupt_modes, [True])
        await app.announcements.close()

    async def test_interrupt_pauses_queue_and_preserves_later_item(self):
        calls = 0

        async def process(items):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AnnouncementInterrupted
            return items[0].text, (0, 0, 1)

        queue = AnnouncementQueue(process)
        first = await queue.submit(
            announcement_id="reply", source="local_reply", text="回答",
            priority=AnnouncementPriority.LOCAL_REPLY,
        )
        later = await queue.submit(
            announcement_id="timer", source="timer", text="提醒",
            priority=AnnouncementPriority.NOTIFICATION,
        )
        await queue.drain_and_pause()
        interrupted = await first.wait()
        self.assertTrue(interrupted.success)
        self.assertTrue(interrupted.interrupted)
        self.assertFalse(later.future.done())
        self.assertTrue(queue.has_pending())
        await queue.drain_and_pause()
        self.assertTrue((await later.wait()).success)
        await queue.close()


if __name__ == "__main__":
    unittest.main()
