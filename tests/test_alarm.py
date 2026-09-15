import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from lelamp.alarm import AlarmManager, AlarmRecurrence, AlarmSnapshot, AlarmStatus
from lelamp.app import LampApp
from lelamp.timer import TimerSnapshot, TimerStatus
from lelamp.voice.announcement import AnnouncementPriority


class FakeMotion:
    robot = None
    async def play(self, name, transition_seconds=None): pass
    async def sleep(self): pass
    async def standby(self): pass
    async def work_pose(self, pose="high"): pass
    def close(self): pass


class FakeLighting:
    def close(self): pass


class AlarmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "LELAMP_TIMEZONE": "Asia/Shanghai",
            "ALARM_STATE_FILE": "state/alarms.json",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def future(self, seconds=1):
        return (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=seconds)).isoformat()

    async def test_alarm_fires_once_and_persists(self):
        fired = []
        manager = AlarmManager(fired.append, self.root)
        alarm = await manager.create_alarm(
            self.future(0.05), "提醒喝水", callback_info={"source": "test"}
        )
        self.assertEqual(alarm.alarm_id, "alarm-1")
        await asyncio.sleep(0.1)
        snapshot = await manager.get_alarm("alarm-1")
        self.assertEqual(snapshot.status, AlarmStatus.COMPLETED)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].message, "提醒喝水")
        saved = json.loads((self.root / "state/alarms.json").read_text())
        self.assertEqual(saved["alarms"][0]["status"], "completed")
        await manager.close()

    async def test_cancel_does_not_fire(self):
        fired = []
        manager = AlarmManager(fired.append, self.root)
        alarm = await manager.create_alarm(self.future(0.05), "不应播报")
        cancelled = await manager.cancel_alarm(alarm.alarm_id)
        self.assertEqual(cancelled.status, AlarmStatus.CANCELLED)
        await asyncio.sleep(0.1)
        self.assertEqual(fired, [])
        self.assertEqual(await manager.list_alarms(), [])
        await manager.close()

    async def test_restart_restores_future_alarm(self):
        first = AlarmManager(root=self.root)
        created = await first.create_alarm(self.future(10), "重启恢复")
        await first.close()
        second = AlarmManager(root=self.root)
        await second.start()
        restored = await second.get_alarm(created.alarm_id)
        self.assertEqual(restored.status, AlarmStatus.SCHEDULED)
        self.assertEqual(restored.message, "重启恢复")
        await second.close()

    async def test_weekday_alarm_normalizes_to_nearest_valid_day(self):
        manager = AlarmManager(root=self.root)
        timezone = ZoneInfo("Asia/Shanghai")
        # Tuesday morning: even if the Agent supplied next Monday, the first
        # weekday recurrence at 10:30 must be this Tuesday at 10:30.
        now = datetime(2026, 9, 15, 10, 8, tzinfo=timezone)
        requested = datetime(2026, 9, 21, 10, 30, tzinfo=timezone)
        actual = manager._first_recurring_occurrence(
            requested, AlarmRecurrence.WEEKDAYS, now
        )
        self.assertEqual(actual, datetime(2026, 9, 15, 10, 30, tzinfo=timezone))

    async def test_weekday_alarm_skips_weekend(self):
        manager = AlarmManager(root=self.root)
        timezone = ZoneInfo("Asia/Shanghai")
        friday_late = datetime(2026, 9, 18, 11, 0, tzinfo=timezone)
        requested = datetime(2026, 9, 21, 10, 30, tzinfo=timezone)
        actual = manager._first_recurring_occurrence(
            requested, AlarmRecurrence.WEEKDAYS, friday_late
        )
        self.assertEqual(actual, datetime(2026, 9, 21, 10, 30, tzinfo=timezone))

    async def test_weekly_alarm_uses_explicit_iso_weekday(self):
        manager = AlarmManager(root=self.root)
        timezone = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 9, 15, 10, 11, tzinfo=timezone)  # Tuesday
        model_date = datetime(2026, 9, 15, 16, 30, tzinfo=timezone)
        with patch.object(manager, "_now", return_value=now):
            alarm = await manager.create_alarm(
                model_date.isoformat(), "周五提醒", "weekly", {}, day_of_week=5
            )
        self.assertEqual(alarm.recurrence, AlarmRecurrence.WEEKLY)
        self.assertEqual(alarm.day_of_week, 5)
        self.assertEqual(
            datetime.fromisoformat(alarm.next_trigger_at),
            datetime(2026, 9, 18, 16, 30, tzinfo=timezone),
        )
        following = manager._next_occurrence(
            datetime.fromisoformat(alarm.next_trigger_at),
            AlarmRecurrence.WEEKLY,
            datetime(2026, 9, 18, 16, 31, tzinfo=timezone),
            5,
        )
        self.assertEqual(following, datetime(2026, 9, 25, 16, 30, tzinfo=timezone))
        await manager.close()

    async def test_weekly_alarm_requires_day_of_week(self):
        manager = AlarmManager(root=self.root)
        with self.assertRaisesRegex(ValueError, "day_of_week"):
            await manager.create_alarm(self.future(10), recurrence="weekly")
        await manager.close()

    async def test_past_once_is_missed_and_recurring_moves_forward(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        state = {
            "next_id": 3,
            "alarms": [
                {"alarm_id": "alarm-1", "trigger_at": (now-timedelta(days=1)).isoformat(),
                 "next_trigger_at": (now-timedelta(days=1)).isoformat(), "recurrence": "once",
                 "status": "scheduled", "message": "old", "callback_info": {}},
                {"alarm_id": "alarm-2", "trigger_at": (now-timedelta(days=2)).isoformat(),
                 "next_trigger_at": (now-timedelta(days=2)).isoformat(), "recurrence": "daily",
                 "status": "scheduled", "message": "daily", "callback_info": {}},
            ],
        }
        path = self.root / "state/alarms.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        manager = AlarmManager(root=self.root)
        await manager.start()
        self.assertEqual((await manager.get_alarm("alarm-1")).status, AlarmStatus.MISSED)
        recurring = await manager.get_alarm("alarm-2")
        self.assertEqual(recurring.status, AlarmStatus.SCHEDULED)
        self.assertGreater(datetime.fromisoformat(recurring.next_trigger_at), now)
        await manager.close()

    async def test_tool_executor_preserves_scheduled_action(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        await app.alarms.close()
        app.alarms = AlarmManager(app._on_alarm_complete, self.root)
        outcome = await app.tools.execute("create_alarm", {
            "trigger_at": self.future(10), "recurrence": "once", "message": "点头",
            "on_complete": {"tool": "play_motion", "arguments": {"name": "nod"}},
        })
        self.assertEqual(outcome.data["callback_info"]["action"]["tool"], "play_motion")
        listed = await app.tools.execute("list_alarms")
        self.assertEqual(len(listed.data["alarms"]), 1)
        await app.alarms.close()

    async def test_manager_can_start_without_timezone_when_empty(self):
        os.environ.pop("LELAMP_TIMEZONE", None)
        manager = AlarmManager(root=self.root)
        await manager.start()
        with self.assertRaisesRegex(ValueError, "没有可用时区"):
            await manager.create_alarm("2026-09-20T08:00:00+08:00")
        await manager.close()

    async def test_plain_timer_and_alarm_share_and_merge_in_announcement_queue(self):
        events = []

        class FakeSoundPlayer:
            def has_cue(self, cue):
                return True

            def play(self, cue):
                events.append(("sound", cue))
                return 0.1

        app = LampApp(
            motion=FakeMotion(), lighting=FakeLighting(),
            sound_player=FakeSoundPlayer(),
        )
        timer = TimerSnapshot(
            "timer-1", 1, 0, TimerStatus.COMPLETED, "喝水", {"source": "agent"}
        )
        alarm = AlarmSnapshot(
            "alarm-1", self.future(), None, AlarmRecurrence.ONCE,
            AlarmStatus.COMPLETED, "开会", {"source": "agent"},
        )
        async def speak_now(text, expression):
            events.append(("speech", text))
            return (0, 0, 0)

        with patch.object(app, "_speak_now", side_effect=speak_now) as speak:
            await app._on_timer_complete(timer)
            await app._on_alarm_complete(alarm)
            await app.drain_announcements()
        speak.assert_awaited_once_with("喝水；开会。", None)
        self.assertEqual(events, [("sound", "alarm"), ("speech", "喝水；开会。")])
        await app.timers.close()
        await app.alarms.close()
        await app.announcements.close()

    async def test_started_local_turn_finishes_before_timer_announcement(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        timer = TimerSnapshot(
            "timer-1", 1, 0, TimerStatus.COMPLETED, "时间到了", {"source": "agent"}
        )
        app.local_speech_started()
        await app._on_timer_complete(timer)
        self.assertTrue(app.announcement_pending())
        self.assertFalse(app.announcement_interrupted())
        local = await app.submit_announcement(
            source="local_reply", text="当前问题的回答",
            priority=AnnouncementPriority.LOCAL_REPLY,
        )
        app.local_speech_finished()
        spoken = []

        async def speak(text, expression):
            spoken.append((text, expression))
            return (0, 0, 0)

        with patch.object(app, "_speak_now", side_effect=speak):
            await app.drain_announcements()
        self.assertEqual(
            spoken,
            [("当前问题的回答", None), ("时间到了", None)],
        )
        self.assertTrue((await local.wait()).success)
        await app.timers.close()
        await app.alarms.close()
        await app.announcements.close()

    async def test_notification_interrupts_only_before_speech_starts(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        timer = TimerSnapshot(
            "timer-1", 1, 0, TimerStatus.COMPLETED, "时间到了", {"source": "agent"}
        )
        await app._on_timer_complete(timer)
        self.assertTrue(app.announcement_interrupted())
        app.local_speech_started()
        self.assertFalse(app.announcement_interrupted())
        await app.timers.close()
        await app.alarms.close()
        await app.announcements.close()
