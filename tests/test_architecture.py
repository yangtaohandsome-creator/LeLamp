"""Hardware-free regression checks for the architecture migration."""
import asyncio
import os
import unittest
from unittest.mock import patch

import numpy as np
from lelamp.app import ActionResult, LampApp, RemoteActionResult, match_local_command
from lelamp.voice.audio import read_capture_block, scale_pcm_s16le
from lelamp.voice.vad import capture_utterance
from lelamp.voice.config import has_meaningful_text
from lelamp.motion.controller import MotionController


class FakeMotion:
    robot = None

    def __init__(self):
        self.events = []
        self.started = asyncio.Event()
        self.block_play = False

    async def play(self, name, transition_seconds=None):
        self.events.append("play:" + name)
        self.play_transition = transition_seconds
        self.started.set()
        try:
            if self.block_play:
                await asyncio.Event().wait()
        finally:
            self.events.append("play:stopped")

    async def sleep(self):
        self.events.append("sleep")

    async def standby(self):
        self.events.append("standby")

    async def work_pose(self, pose="high"):
        self.events.append("work:" + pose)

    def close(self):
        pass


class FakeLighting:
    def __init__(self):
        self.events = []

    def set_light(self, color, brightness):
        self.events.append(("solid", color, brightness))

    def office_mode(self, brightness=None):
        self.events.append(("office", brightness))

    def warm_mode(self, brightness=None):
        self.events.append(("warm", brightness))

    def work_light(self, tone, brightness, seconds=0.8):
        self.events.append(("work", tone, brightness))

    def fade_off(self, seconds=0.8):
        self.events.append(("fade_off", seconds))

    def close(self):
        pass

    def thinking(self):
        self.events.append(("state", "thinking"))

    def speaking(self):
        self.events.append(("state", "speaking"))

    def turn_done(self):
        self.events.append(("state", "turn_done"))

    def wake_required(self):
        self.events.append(("state", "wake_required"))

    def thinking(self):
        self.events.append(("state", "thinking"))

    def speaking(self):
        self.events.append(("state", "speaking"))

    def turn_done(self):
        self.events.append(("state", "turn_done"))

    def wake_required(self):
        self.events.append(("state", "wake_required"))


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_motion_returns_to_standby(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        await app.play_motion("nod")
        self.assertEqual(motion.events, ["play:nod", "play:stopped", "standby"])
        self.assertAlmostEqual(motion.play_transition, 0.5)

    async def test_sleep_state_uses_startup_transition(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        app.mechanically_asleep = True
        await app.play_motion("nod")
        self.assertAlmostEqual(motion.play_transition, 1.5)

    async def test_replay_forces_startup_transition(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        await app.play_motion("nod", force_startup=True)
        self.assertAlmostEqual(motion.play_transition, 1.5)

    async def test_tracking_stops_before_motion_and_resumes(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        started = asyncio.Event()

        async def tracking():
            motion.events.append("tracking:start")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                motion.events.append("tracking:stop")

        await app.set_mode("tracking", tracking)
        await started.wait()
        await app.play_motion("nod")
        await asyncio.sleep(0)
        self.assertEqual(motion.events[:5], [
            "tracking:start", "tracking:stop", "play:nod",
            "play:stopped", "tracking:start",
        ])
        self.assertTrue(app.tracking)
        await app.sleep()
        self.assertFalse(app.tracking)

    async def test_new_sleep_prevents_old_mode_restore(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        async def reading():
            motion.events.append("reading")
        await app.set_mode("reading", reading)
        await asyncio.sleep(0)
        motion.block_play = True
        playing = asyncio.create_task(app.play_motion("nod"))
        await motion.started.wait()
        await app.sleep()
        with self.assertRaises(asyncio.CancelledError):
            await playing
        self.assertEqual(motion.events, ["reading", "play:nod", "play:stopped", "sleep"])
        self.assertEqual(app.current_mode, "sleep")

    async def test_reading_restored_without_sleep(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        async def reading():
            motion.events.append("reading")
        await app.set_mode("reading", reading)
        await asyncio.sleep(0)
        await app.play_motion("headshake")
        await asyncio.sleep(0)
        self.assertEqual(motion.events, ["reading", "play:headshake", "play:stopped", "reading"])

    async def test_failed_motion_can_be_replaced(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        async def broken(name, transition_seconds=None):
            raise RuntimeError("serial failed")
        motion.play = broken
        with self.assertRaises(RuntimeError):
            await app.play_motion("nod")
        await app.sleep()
        self.assertEqual(motion.events, ["sleep"])

    async def test_sleep_is_the_idempotent_mechanical_shutdown(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        await app.sleep()
        await app.sleep()
        self.assertEqual(motion.events, ["sleep"])
        self.assertTrue(app.mechanically_asleep)
        await app.enter_standby()
        self.assertEqual(motion.events, ["sleep", "standby"])
        self.assertFalse(app.mechanically_asleep)

    async def test_sleep_command_requests_central_shutdown(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        result = await app.handle_text("睡眠", [])
        self.assertEqual(result, ActionResult("歇一会儿，有事叫我。", "shutdown_requested"))
        self.assertEqual(motion.events, [])

    async def test_local_motion_uses_shared_tool_executor(self):
        motion = FakeMotion()
        lighting = FakeLighting()
        app = LampApp(motion=motion, lighting=lighting)
        await app.handle_text("点头", "session")
        self.assertEqual(motion.events, ["play:nod", "play:stopped", "standby"])

    async def test_work_light_defaults_adjust_and_reset(self):
        motion = FakeMotion()
        lighting = FakeLighting()
        app = LampApp(motion=motion, lighting=lighting)
        await app.tools.execute("enter_work_light")
        self.assertEqual(app.get_robot_state()["work_brightness"], 75)
        self.assertEqual(motion.events[-1], "work:high")
        self.assertEqual(lighting.events[-1], ("work", "white", 75))
        await app.tools.execute("update_work_light", {
            "pose": "low", "tone": "warm", "brightness_step": -25,
        })
        state = app.get_robot_state()
        self.assertEqual((state["work_pose"], state["work_tone"], state["work_brightness"]),
                         ("low", "warm", 50))
        await app.tools.execute("exit_work_light")
        self.assertEqual(app.current_mode, "standby")
        self.assertIsNone(app.get_robot_state()["work_pose"])
        await app.tools.execute("enter_work_light")
        state = app.get_robot_state()
        self.assertEqual((state["work_pose"], state["work_tone"], state["work_brightness"]),
                         ("high", "white", 75))

    async def test_work_light_motion_uses_active_transition_and_restores_pose(self):
        motion = FakeMotion()
        app = LampApp(motion=motion, lighting=FakeLighting())
        await app.enter_work_light("low", "white")
        await app.play_motion("headshake")
        self.assertAlmostEqual(motion.play_transition, 0.5)
        self.assertEqual(motion.events[-1], "work:low")

    async def test_agent_sleep_request_becomes_shutdown_result(self):
        motion = FakeMotion()
        app = LampApp(motion=motion, lighting=FakeLighting())

        async def agent_reply(text, session_id):
            self.assertEqual(session_id, "wake-session")
            app.request_shutdown("farewell")
            return "回头见。"

        with patch("lelamp.app.ask_agent", side_effect=agent_reply):
            result = await app.handle_text("我先走了，再见", "wake-session")
        self.assertEqual(result, ActionResult("回头见。", "shutdown_requested"))
        self.assertIsNone(app.consume_shutdown_request())

    async def test_agent_expression_starts_with_tts_and_restores_standby(self):
        motion = FakeMotion()
        app = LampApp(motion=motion, lighting=FakeLighting())

        async def agent_reply(_text, _session_id):
            outcome = await app.tools.execute("queue_expression", {"name": "curious"})
            self.assertTrue(outcome.data["queued"])
            return "这事有点奇怪。"

        def speaking(_text, on_playback_start):
            self.assertEqual(motion.events, [])
            on_playback_start()
            return (0.1, 0.2, 0.3)

        with patch("lelamp.app.ask_agent", side_effect=agent_reply), patch(
            "lelamp.app.speak", side_effect=speaking
        ):
            result = await app.handle_text("这是怎么回事", "session")
            metrics = await app.speak_response(result.text)
        self.assertEqual(metrics, (0.1, 0.2, 0.3))
        self.assertEqual(motion.events, ["play:curious", "play:stopped", "standby"])

    async def test_agent_play_motion_misuse_is_deferred_to_reply(self):
        motion = FakeMotion()
        app = LampApp(motion=motion, lighting=FakeLighting())

        async def agent_reply(_text, _session_id):
            outcome = await app.tools.execute("play_motion", {"name": "happy_wiggle"})
            self.assertTrue(outcome.data["queued"])
            self.assertEqual(motion.events, [])
            return "这确实是个好消息。"

        def speaking(_text, on_playback_start):
            on_playback_start()
            return (0.1, 0.2, 0.3)

        with patch("lelamp.app.ask_agent", side_effect=agent_reply), patch(
            "lelamp.app.speak", side_effect=speaking
        ):
            result = await app.handle_text("我有一个特别好的消息", "session")
            await app.speak_response(result.text)
        self.assertEqual(motion.events, ["play:happy_wiggle", "play:stopped", "standby"])

    async def test_work_light_rejects_automatic_expression(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        await app.enter_work_light()
        outcome = await app.tools.execute("queue_expression", {"name": "happy_wiggle"})
        self.assertFalse(outcome.data["queued"])
        self.assertIsNone(app._pending_expression)

    async def test_remote_text_uses_agent_tts_and_deferred_sleep(self):
        motion = FakeMotion()
        app = LampApp(motion=motion, lighting=FakeLighting())
        app.mechanically_asleep = True

        async def agent_reply(text, session_id):
            self.assertEqual((text, session_id), ("我先走了", "remote-user"))
            app.request_shutdown("farewell")
            return "回头见。"

        with patch("lelamp.app.ask_agent", side_effect=agent_reply), patch(
            "lelamp.app.speak", return_value=(0.1, 0.2, 0.3)
        ) as mocked_speak:
            result = await app.process_remote_text("我先走了", "remote-user")
        self.assertEqual(result, RemoteActionResult("回头见。", "shutdown_requested", True))
        mocked_speak.assert_called_once_with("回头见。")
        self.assertEqual(motion.events, ["standby", "sleep"])
        self.assertTrue(app.mechanically_asleep)

    async def test_tool_state_and_deferred_sleep(self):
        app = LampApp(motion=FakeMotion(), lighting=FakeLighting())
        outcome = await app.tools.execute("sleep", {"reason": "farewell"})
        self.assertEqual(outcome.status, "shutdown_requested")
        self.assertFalse(app.mechanically_asleep)
        self.assertEqual(app.consume_shutdown_request(), "farewell")
        state = await app.tools.execute("get_robot_state")
        self.assertEqual(state.data["current_mode"], "normal")

    async def test_stop_tracking_returns_to_standby(self):
        motion = FakeMotion()
        app = LampApp(motion=motion)
        async def tracking():
            await asyncio.Event().wait()
        await app.set_mode("tracking", tracking)
        await asyncio.sleep(0)
        await app.stop_tracking()
        self.assertEqual(motion.events, ["standby"])
        self.assertEqual(app.current_mode, "standby")


class VoiceTests(unittest.TestCase):
    def test_exact_commands_only(self):
        self.assertEqual(match_local_command(" 点头。 "), "nod")
        for text in ("不要点头", "为什么你会点头", "如果我让你点头呢", "点头然后摇头"):
            self.assertIsNone(match_local_command(text))
        for text in ("是", "好", "好的"):
            self.assertTrue(has_meaningful_text(text))
        self.assertFalse(has_meaningful_text("……！？"))

    def test_pcm_gain(self):
        pcm = np.array([1000, -2000, 30000], dtype="<i2").tobytes()
        actual = np.frombuffer(scale_pcm_s16le(pcm, 200), dtype="<i2")
        np.testing.assert_array_equal(actual, [2000, -4000, 32767])

    def test_audio_conversion_keeps_original_dc(self):
        import io
        from types import SimpleNamespace
        capture = SimpleNamespace(stdout=io.BytesIO(
            np.full((4800, 2), 1638, dtype="<i2").tobytes()))
        audio = read_capture_block(capture)
        self.assertEqual(len(audio), 1600)
        self.assertAlmostEqual(float(audio.mean()), 1638 / 32768, places=5)

    def test_short_speech_and_pause_preserved(self):
        quiet = np.full(1600, 0.052, dtype=np.float32)
        speech = quiet + np.tile(np.array([-0.04, 0.04], dtype=np.float32), 800)
        blocks = iter([speech] + [quiet] * 8 + [speech] + [quiet] * 12)
        with patch.dict(os.environ, {"VAD_MODE": "rms", "VAD_SILENCE_SECONDS": "1.2",
                                     "VAD_MAX_SECONDS": "15"}):
            result = capture_utterance(lambda: next(blocks), 15, "test")
        self.assertEqual(len(result), 22 * 1600)

    def test_dc_noise_not_speech(self):
        quiet = np.full(1600, 0.052, dtype=np.float32)
        with patch.dict(os.environ, {"VAD_MODE": "rms"}):
            result = capture_utterance(lambda: quiet, -1, "test")
        self.assertIsNone(result)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_holds_before_release(self):
        from types import SimpleNamespace
        events = []
        robot = SimpleNamespace(bus=SimpleNamespace(disable_torque=lambda: events.append("release")))
        controller = MotionController(robot=robot)
        async def move(target, duration):
            events.append("move")
        controller.move_to = move
        async def hold(seconds):
            self.assertEqual(seconds, 1)
            events.append("hold")
        with patch("lelamp.motion.controller.sleep_action", return_value={"x.pos": 1}), \
             patch("lelamp.motion.controller.sleep_transition_seconds", return_value=1.5), \
             patch("lelamp.motion.controller.sleep_hold_seconds", return_value=1), \
             patch("lelamp.motion.controller.asyncio.sleep", side_effect=hold):
            await controller.sleep()
        self.assertEqual(events, ["move", "hold", "release"])

    async def test_failed_park_does_not_release(self):
        from types import SimpleNamespace
        released = []
        robot = SimpleNamespace(bus=SimpleNamespace(disable_torque=lambda: released.append(True)))
        controller = MotionController(robot=robot)
        async def fail(*args):
            raise RuntimeError("cannot move")
        controller.move_to = fail
        with patch("lelamp.motion.controller.sleep_action", return_value={"x.pos": 1}):
            with self.assertRaises(RuntimeError):
                await controller.sleep()
        self.assertEqual(released, [])


if __name__ == "__main__":
    unittest.main()
