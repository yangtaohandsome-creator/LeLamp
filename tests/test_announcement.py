import asyncio
import unittest

from lelamp.voice.announcement import AnnouncementPriority, AnnouncementQueue


class AnnouncementQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_priority_then_fifo(self):
        batches = []

        async def process(items):
            batches.append([item.announcement_id for item in items])
            return items[0].text, (0.1, 0.2, 0.3)

        queue = AnnouncementQueue(process)
        notification = await queue.submit(
            announcement_id="timer-1", source="timer", text="提醒",
            priority=AnnouncementPriority.NOTIFICATION,
            sound_before="timer",
        )
        remote = await queue.submit(
            announcement_id="remote-1", source="remote_reply", text="远程回答",
            priority=AnnouncementPriority.REMOTE_REPLY,
        )
        local = await queue.submit(
            announcement_id="local-1", source="local_reply", text="本地回答",
            priority=AnnouncementPriority.LOCAL_REPLY,
        )
        await queue.drain_and_pause()
        self.assertEqual(batches, [["local-1"], ["remote-1"], ["timer-1"]])
        self.assertTrue((await local.wait()).success)
        self.assertTrue((await remote.wait()).success)
        self.assertTrue((await notification.wait()).success)
        self.assertEqual(notification.announcement.sound_before, "timer")
        self.assertFalse(queue.has_pending())
        await queue.close()

    async def test_merge_only_consecutive_plain_notifications(self):
        batches = []

        async def process(items):
            batches.append([item.announcement_id for item in items])
            return "merged", (0, 0, 0)

        queue = AnnouncementQueue(process)
        first = await queue.submit(
            announcement_id="timer-1", source="timer", text="喝水",
            priority=AnnouncementPriority.NOTIFICATION, mergeable=True,
        )
        second = await queue.submit(
            announcement_id="alarm-1", source="alarm", text="开会",
            priority=AnnouncementPriority.NOTIFICATION, mergeable=True,
        )
        action = await queue.submit(
            announcement_id="timer-2", source="timer", text="点头",
            priority=AnnouncementPriority.NOTIFICATION,
            callback_info={"action": {"tool": "play_motion"}},
        )
        last = await queue.submit(
            announcement_id="timer-3", source="timer", text="休息",
            priority=AnnouncementPriority.NOTIFICATION, mergeable=True,
        )
        await queue.drain_and_pause()
        self.assertEqual(batches, [["timer-1", "alarm-1"], ["timer-2"], ["timer-3"]])
        self.assertEqual((await first.wait()).text, "merged")
        self.assertEqual((await second.wait()).text, "merged")
        self.assertTrue((await action.wait()).success)
        self.assertTrue((await last.wait()).success)
        await queue.close()

    async def test_one_failure_does_not_stop_later_jobs(self):
        seen = []

        async def process(items):
            item = items[0]
            seen.append(item.announcement_id)
            if item.announcement_id == "bad":
                raise RuntimeError("tts failed")
            return item.text, (0, 0, 1)

        queue = AnnouncementQueue(process)
        bad = await queue.submit(
            announcement_id="bad", source="system", text="bad", priority=10,
        )
        good = await queue.submit(
            announcement_id="good", source="system", text="good", priority=10,
        )
        await queue.drain_and_pause()
        self.assertEqual(seen, ["bad", "good"])
        self.assertFalse((await bad.wait()).success)
        self.assertEqual((await bad.wait()).error, "tts failed")
        self.assertTrue((await good.wait()).success)
        await queue.close()

    async def test_close_fails_pending_without_speaking(self):
        called = False

        async def process(_items):
            nonlocal called
            called = True
            return "", (0, 0, 0)

        queue = AnnouncementQueue(process)
        handle = await queue.submit(
            announcement_id="pending", source="timer", text="later", priority=20,
        )
        await queue.close()
        result = await handle.wait()
        self.assertFalse(result.success)
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
