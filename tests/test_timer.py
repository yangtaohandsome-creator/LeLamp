import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from lelamp.app import LampApp
from lelamp.timer import TimerManager, TimerStatus


class TimerManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.completed = []
        self.manager = TimerManager(self.completed.append)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_multiple_timers_complete_independently_once(self):
        first = await self.manager.create_timer(0.03, "first")
        second = await self.manager.create_timer(0.01, "second")
        self.assertEqual((first.timer_id, second.timer_id), ("timer-1", "timer-2"))
        await asyncio.sleep(0.06)
        self.assertEqual([timer.timer_id for timer in self.completed], ["timer-2", "timer-1"])
        self.assertEqual((await self.manager.get_remaining(first.timer_id)).status,
                         TimerStatus.COMPLETED)

    async def test_pause_resume_and_add_time(self):
        timer = await self.manager.create_timer(0.08)
        await asyncio.sleep(0.02)
        paused = await self.manager.pause_timer(timer.timer_id)
        frozen = paused.remaining
        await asyncio.sleep(0.04)
        self.assertAlmostEqual(
            (await self.manager.get_remaining(timer.timer_id)).remaining,
            frozen,
            delta=0.01,
        )
        added = await self.manager.add_time(timer.timer_id, 0.04)
        self.assertAlmostEqual(added.duration, 0.12)
        self.assertAlmostEqual(added.remaining, frozen + 0.04, delta=0.01)
        await self.manager.resume_timer(timer.timer_id)
        await asyncio.sleep(0.12)
        self.assertEqual(len(self.completed), 1)

    async def test_cancel_does_not_complete_and_default_list_hides_finished(self):
        cancelled = await self.manager.create_timer(0.02)
        completed = await self.manager.create_timer(0.01)
        await self.manager.cancel_timer(cancelled.timer_id)
        await asyncio.sleep(0.04)
        self.assertEqual([timer.timer_id for timer in self.completed], [completed.timer_id])
        self.assertEqual(await self.manager.list_timers(), [])
        all_timers = await self.manager.list_timers(include_finished=True)
        self.assertEqual(
            [timer.status for timer in all_timers],
            [TimerStatus.CANCELLED, TimerStatus.COMPLETED],
        )

    async def test_invalid_transitions_and_values(self):
        with self.assertRaises(ValueError):
            await self.manager.create_timer(0)
        timer = await self.manager.create_timer(10)
        with self.assertRaises(ValueError):
            await self.manager.resume_timer(timer.timer_id)
        await self.manager.cancel_timer(timer.timer_id)
        with self.assertRaises(ValueError):
            await self.manager.add_time(timer.timer_id, 1)
        with self.assertRaises(ValueError):
            await self.manager.get_remaining("missing")

    async def test_close_never_fires_completion(self):
        await self.manager.create_timer(0.01)
        await self.manager.close()
        await asyncio.sleep(0.03)
        self.assertEqual(self.completed, [])


class FakeMotion:
    robot = None

    def close(self):
        pass


class FakeLighting:
    def close(self):
        pass

    def speaking(self):
        pass


class TimerAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_operations_and_queued_default_announcement(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        created = await app.tools.execute("create_timer", {
            "duration_seconds": 0.01,
            "message": "",
        })
        self.assertEqual(created.data["timer_id"], "timer-1")
        await asyncio.sleep(0.03)
        self.assertTrue(app.announcement_pending())
        with patch.object(app, "_speak_now", new=AsyncMock(return_value=(0, 0, 0))) as speak:
            await app.drain_announcements()
        speak.assert_awaited_once_with("计时结束了。", None)
        self.assertFalse(app.announcement_pending())
        await app.timers.close()
        await app.announcements.close()

    async def test_list_tool_returns_creation_order(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        await app.tools.execute("create_timer", {"duration_seconds": 10, "message": "a"})
        await app.tools.execute("create_timer", {"duration_seconds": 20, "message": "b"})
        listed = await app.tools.execute("list_timers")
        self.assertEqual(
            [timer["timer_id"] for timer in listed.data["timers"]],
            ["timer-1", "timer-2"],
        )
        await app.timers.close()
        await app.announcements.close()

    async def test_timer_executes_high_level_action_before_announcement(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        action = AsyncMock(return_value=type("Outcome", (), {"status": "completed"})())
        created = await app.tools.execute("create_timer", {
            "duration_seconds": 0.01,
            "message": "办公照明已退出。",
            "on_complete": {"tool": "exit_work_light", "arguments": {}},
        })
        self.assertEqual(created.data["callback_info"]["action"]["tool"], "exit_work_light")
        await asyncio.sleep(0.03)
        with patch.object(app.tools, "execute", new=action), patch.object(
            app, "_speak_now", new=AsyncMock(return_value=(0, 0, 0))
        ) as speak:
            await app.drain_announcements()
        action.assert_awaited_once_with("exit_work_light", {})
        speak.assert_awaited_once_with("办公照明已退出。", None)
        await app.timers.close()
        await app.announcements.close()

    async def test_timer_rejects_unsafe_scheduled_tool(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        with self.assertRaises(ValueError):
            await app.tools.execute("create_timer", {
                "duration_seconds": 10,
                "message": "x",
                "on_complete": {"tool": "create_timer", "arguments": {}},
            })
        await app.timers.close()
        await app.announcements.close()

    async def test_timer_runs_deferred_agent_task(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        created = await app.tools.execute("create_timer", {
            "duration_seconds": 0.01,
            "message": "任务到期。",
            "agent_task": "现在查询天气，有雨就提醒带伞。",
        })
        self.assertEqual(
            created.data["callback_info"]["agent_task"],
            "现在查询天气，有雨就提醒带伞。",
        )
        await asyncio.sleep(0.03)
        with patch.object(
            app, "handle_text", new=AsyncMock(return_value=__import__(
                "lelamp.app", fromlist=["ActionResult"]
            ).ActionResult("记得带伞。"))
        ) as agent, patch.object(
            app, "_speak_now", new=AsyncMock(return_value=(0, 0, 0))
        ) as speak:
            await app.drain_announcements()
        self.assertEqual(agent.await_count, 1)
        self.assertEqual(agent.await_args.args[0], "现在查询天气，有雨就提醒带伞。")
        self.assertTrue(agent.await_args.args[1].startswith("scheduled-timer-1-"))
        speak.assert_awaited_once_with("记得带伞。", None)
        await app.timers.close()
        await app.announcements.close()

    async def test_timer_rejects_fixed_action_and_agent_task_together(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        with self.assertRaises(ValueError):
            await app.tools.execute("create_timer", {
                "duration_seconds": 10,
                "message": "x",
                "on_complete": {"tool": "play_motion", "arguments": {"name": "nod"}},
                "agent_task": "重新判断一次",
            })
        await app.timers.close()
        await app.announcements.close()


if __name__ == "__main__":
    unittest.main()
