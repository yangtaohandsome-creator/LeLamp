import asyncio
import json
import tempfile
import unittest
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from lelamp.app import ActionResult, LampApp
from lelamp.voice.playback import SpeechInterrupted
from lelamp.control import ControlServer
from lelamp.tools import ToolSource
from lelamp.voice.announcement import AnnouncementQueue
from lelamp.web.api import WebConsole
from lelamp.web.demo import DemoApp


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = DemoApp(self.root)
        self.console = WebConsole(self.app, maintenance=self.app.maintenance)
        server = web.Application()
        self.console.routes(server.router)
        self.client = TestClient(TestServer(server))
        await self.client.start_server()
        self.headers = {"X-LeLamp-Client": "test-browser-one"}

    async def asyncTearDown(self):
        await self.client.close()
        await self.console.close()
        await self.app.close()
        self.temp.cleanup()

    async def post(self, route, body=None):
        response = await self.client.post("/api/v1/web/" + route,
                                          json=body or {}, headers=self.headers)
        return response, await response.json()

    async def test_demo_maintenance_lock_is_private(self):
        from lelamp.web.maintenance import MaintenanceController
        self.assertIsNone(MaintenanceController().lock_path)
        self.assertEqual(self.app.maintenance.lock_path.parent, self.root)
        await self.post('maintenance/begin')
        response, result = await self.post('maintenance/open', {'device':'follower'})
        self.assertEqual(response.status, 200, result)
        self.assertTrue((self.root / 'demo-device.lock').exists())

    async def test_site_and_strict_same_origin_actions(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("录制与校准", await response.text())
        response = await self.client.post("/api/v1/web/action", json={"name":"sleep_now"},
                                          headers={"Origin":"https://evil.example"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/v1/web/action", data='{"name":"sleep_now"}')
        self.assertEqual(response.status, 415)
        for body in ({"name":"raw_servo"}, {"name":"sleep_now","arguments":{"pin":1}},
                     {"name":"turn_base","arguments":{"direction":"left","steps":True}},
                     {"name":"create_timer","arguments":{"duration_seconds":True}}):
            response, _ = await self.post("action", body)
            self.assertEqual(response.status, 400)
        self.assertEqual(self.app.motion.events, [])

    async def test_actions_and_persistent_management_use_shared_tools(self):
        _, result = await self.post("action", {"name":"play_motion","arguments":{"name":"nod"}})
        self.assertTrue(result["ok"])
        self.assertEqual(self.app.motion.events.count("play:nod"), 1)
        await self.post("action", {"name":"enter_work_light"})
        await self.post("action", {"name":"update_work_light","arguments":{"pose":"low","tone":"warm","brightness_step":-25}})
        state = (await (await self.client.get("/api/v1/web/state")).json())["state"]
        self.assertEqual((state["work_pose"],state["work_tone"],state["work_brightness"]), ("low","warm",50))
        _, result = await self.post("action", {"name":"create_todo","arguments":{"text":"记得喝水"}})
        todo_id = result["data"]["todo_id"]
        await self.post("action", {"name":"complete_todo","arguments":{"todo_id":todo_id}})
        self.assertEqual((await self.app.todos.list_todos(True))[0].status, "completed")

    async def test_chat_speaks_and_button_preempts_pending_agent(self):
        _, result = await self.post("chat", {"text":"你好","session_id":"web-test"})
        self.assertTrue(result["spoken"])
        self.assertEqual(len(self.app.spoken), 1)
        started = asyncio.Event()
        async def delayed(*_):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.app, "handle_text", side_effect=delayed):
            old = asyncio.create_task(self.post("chat", {"text":"慢请求","session_id":"web-test"}))
            await asyncio.wait_for(started.wait(), 1)
            _, result = await asyncio.wait_for(self.post("action", {"name":"sleep_now"}), 1)
            self.assertTrue(result["ok"])
            response, interrupted = await old
            self.assertEqual(response.status, 409)
            self.assertEqual(interrupted["status"], "interrupted")

    async def test_maintenance_defers_timer_and_rejects_other_sources(self):
        voice = asyncio.create_task(asyncio.Event().wait())
        self.app.voice_task = voice
        await self.post("maintenance/begin")
        self.assertTrue(voice.cancelled())
        with self.assertRaises(RuntimeError):
            await self.app.tools.execute("play_motion", {"name":"nod"}, source=ToolSource.AGENT)
        response, _ = await self.post("chat", {"text":"你好","session_id":"test"})
        self.assertEqual(response.status, 400)
        timer = await self.app.timers.create_timer(.03, "计时已完成", {"action":{"tool":"play_motion","arguments":{"name":"nod"}}})
        alarm = await self.app.alarms.create_alarm(
            (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=.03)).isoformat(), "开会")
        await asyncio.sleep(.06)
        self.assertEqual((await self.app.timers.get_remaining(timer.timer_id)).status, "completed")
        self.assertEqual((await self.app.alarms.get_alarm(alarm.alarm_id)).status, "completed")
        self.assertNotIn("play:nod", self.app.motion.events)
        self.assertEqual(self.app.spoken, [])
        await self.post("maintenance/end")
        await self.app.drain_announcements()
        self.assertEqual(self.app.motion.events.count("play:nod"), 1)
        self.assertEqual(len(self.app.spoken), 2)

    async def test_tts_takeover_waits_for_thread_to_stop(self):
        started = threading.Event()
        stopped = threading.Event()
        def speak(_text, _callback, control):
            started.set()
            while not control.cancelled:
                stopped.wait(.005)
            stopped.set()
            raise SpeechInterrupted()
        self.app._speak_now = LampApp._speak_now.__get__(self.app)
        with patch("lelamp.app.speak", side_effect=speak), patch.object(self.app.barge_in, "create_session", return_value=None):
            handle = await self.app.submit_announcement(source="local_reply", text="较长回答", priority=0)
            drain = asyncio.create_task(self.app.drain_announcements())
            await asyncio.to_thread(started.wait, 1)
            await asyncio.wait_for(self.app.web_takeover(), 1)
            self.assertTrue(stopped.is_set())
            self.assertFalse(self.app.speaking)
            self.assertTrue((await handle.wait()).interrupted)
            await drain
            await self.app.web_release()

    async def test_scheduled_side_effect_is_not_replayed_after_preemption(self):
        started = asyncio.Event()
        async def slow_speech(*_):
            started.set()
            await asyncio.Event().wait()
        with patch.object(self.app, "_speak_now", side_effect=slow_speech):
            await self.app.submit_announcement(source="timer", text="动作完成", priority=20,
                callback_info={"action":{"tool":"play_motion","arguments":{"name":"nod"}}})
            drain = asyncio.create_task(self.app.drain_announcements())
            await started.wait()
            await self.app.web_takeover()
            await drain
            await self.app.web_release()
        await self.app.drain_announcements()
        self.assertEqual(self.app.motion.events.count("play:nod"), 1)

    async def test_calibration_write_failure_restores_old_hardware(self):
        await self.post("maintenance/begin")
        await self.post("maintenance/open", {"device":"follower"})
        device = self.app.maintenance.device
        baseline = device.bus.read_calibration()
        await self.post("maintenance/center")
        await self.post("maintenance/range_start")
        await asyncio.sleep(.12)
        await self.post("maintenance/range_stop")
        original = device.bus.write_calibration
        attempts = []
        def fail_once(values):
            attempts.append(values)
            if len(attempts) == 1:
                raise RuntimeError("simulated partial write")
            original(values)
        with patch.object(device.bus, "write_calibration", side_effect=fail_once):
            response, _ = await self.post("maintenance/calibration_save")
        self.assertEqual(response.status, 500)
        self.assertEqual(device.bus.read_calibration(), baseline)
        self.assertTrue(self.app.maintenance.active)

    async def test_recording_backup_disconnect_and_reconnect(self):
        await self.post("maintenance/begin")
        await self.post("maintenance/record_start")
        await asyncio.sleep(.12)
        await self.post("maintenance/record_stop")
        response, result = await self.post("maintenance/record_save", {"name":"custom"})
        self.assertEqual(response.status, 200, result)
        original = (self.root / "recordings/custom.csv").read_bytes()
        response, _ = await self.post("maintenance/record_save", {"name":"custom"})
        self.assertEqual(response.status, 400)
        _, result = await self.post("maintenance/record_save", {"name":"custom","overwrite":True})
        self.assertEqual(Path(result["data"]["backup"]).read_bytes(), original)
        await self.post("maintenance/record_start")
        self.app.maintenance.last_contact -= 30
        await self.app.maintenance.check_heartbeat()
        m = self.app.maintenance
        self.assertTrue(m.active)
        self.assertFalse(m._sampling)
        self.assertIsNone(m.device)
        self.assertTrue(self.app._voice_pause_requested)
        await self.post("maintenance/end")
        self.assertFalse(m.active)

    async def test_calibration_ranges_backup_restore_and_pose_save(self):
        await self.post("maintenance/begin")
        await self.post("maintenance/open", {"device":"follower"})
        path = self.root / "follower.json"
        path.write_text('{"original":true}')
        await self.post("maintenance/center")
        await self.post("maintenance/range_start")
        await asyncio.sleep(.12)
        _, result = await self.post("maintenance/range_stop")
        self.assertEqual(len(result["data"]["range_min"]), 5)
        response, result = await self.post("maintenance/calibration_save")
        self.assertEqual(response.status, 200, result)
        self.assertIn("wrist_pitch", json.loads(path.read_text()))
        self.assertEqual(Path(result["data"]["backup"]).read_text(), '{"original":true}')
        await self.post("maintenance/calibration_restore", {"device":"follower"})
        self.assertEqual(path.read_text(), '{"original":true}')
        _, captured = await self.post("maintenance/pose_capture", {"pose":"standby"})
        old_config = (self.root / "motion.conf").read_text()
        _, saved = await self.post("maintenance/pose_save", captured["data"])
        self.assertEqual(Path(saved["data"]["backup"]).read_text(), old_config)
        captured["data"]["values"]["MOTION_STANDBY_BASE_YAW"] = 99
        response, _ = await self.post("maintenance/pose_save", captured["data"])
        self.assertEqual(response.status, 400)

    async def test_device_release_failure_keeps_maintenance_exclusive(self):
        await self.post("maintenance/begin")
        await self.post("maintenance/open", {"device":"leader"})
        bus = self.app.maintenance.device.bus
        with patch.object(bus, "disconnect", side_effect=RuntimeError("serial failure")):
            response, _ = await self.post("maintenance/end")
            self.assertEqual(response.status, 500)
            self.assertTrue(self.app.maintenance.active)
            self.assertTrue(self.app._voice_pause_requested)
        await self.post("maintenance/end")

    async def test_stale_agent_tool_cannot_run_after_web_takeover(self):
        control = ControlServer(self.app.tools)
        control.token = "test-only"
        server = web.Application()
        server.router.add_post('/v1/tools/{name}', control._execute)
        client = TestClient(TestServer(server))
        await client.start_server()
        try:
            self.app._active_agent_turn_id = "old-turn"
            await self.app.web_takeover()
            await self.app.web_release()
            self.app._active_agent_turn_id = "new-turn"
            response = await client.post('/v1/tools/play_motion', json={"name":"nod"},
                headers={"Authorization":"Bearer test-only", "X-LeLamp-Turn":"old-turn"})
            self.assertEqual(response.status, 409)
            self.assertNotIn("play:nod", self.app.motion.events)
        finally:
            await client.close()


    async def test_vision_buttons_use_web_tools_and_stop_does_not_restore(self):
        with patch.object(self.app.tools, 'execute', wraps=self.app.tools.execute) as execute:
            _, result = await self.post('action', {'name': 'start_face_tracking'})
            self.assertTrue(result['ok'])
            self.assertEqual(execute.call_args.kwargs['source'], ToolSource.WEB)
            await asyncio.sleep(.12)
            v = (await (await self.client.get('/api/v1/web/vision')).json())['vision']
            self.assertEqual(v['tracking_phase'], 'face_follow')
            self.assertTrue(v['tracking_active'])
            _, result = await self.post('action', {'name': 'start_hand_tracking'})
            self.assertTrue(result['ok'])
            self.assertEqual(execute.call_args.args[0], 'start_hand_tracking')
            self.assertEqual(self.app._tracking_session.state['tracking_phase'], 'hand_acquire')
            await self.post('action', {'name': 'stop_tracking'})
            self.assertEqual(self.app.current_mode, 'standby')
            self.assertIsNone(self.app.current_motion_task)
            await self.app.web_release()
            self.assertIsNone(self.app.current_motion_task)

    async def test_vision_get_is_read_only_and_excludes_stale_private_data(self):
        state = {'running': True, 'status': 'running', 'enabled': True,
                 'result_max_age_ms': 250, 'snapshot': {'result_age_ms': 900,
                 'face_count': 3, 'hand_count': 2, 'hands': [{'gesture': 'Open_Palm'}]},
                 'motion_tracking': {'command': [20, 30], 'limits': [40, 50]}}
        with patch.object(self.app, 'get_vision_state', return_value=state), \
             patch.object(self.app, 'web_takeover', side_effect=AssertionError('read took control')), \
             patch.object(self.app.vision, 'start', side_effect=AssertionError('read started camera')), \
             patch.object(self.app.motion, 'read_action', side_effect=AssertionError('read touched servo')):
            for _ in range(2):
                response = await self.client.get('/api/v1/web/vision')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                result = await response.json()
                self.assertFalse(result['vision']['fresh'])
                self.assertEqual(result['vision']['gestures'], [])
                self.assertIsNone(result['vision']['face_count'])
                self.assertNotIn('command', json.dumps(result))
                self.assertNotIn('limits', json.dumps(result))

    async def test_vision_empty_parameters_and_origin(self):
        for name in ('start_face_tracking', 'start_hand_tracking', 'stop_tracking'):
            response, _ = await self.post('action', {'name': name, 'arguments': {'angle': 10}})
            self.assertEqual(response.status, 400)
            response = await self.client.post('/api/v1/web/action', json={'name': name},
                headers={'Origin': 'https://elsewhere.invalid'})
            self.assertEqual(response.status, 403)
        self.assertEqual(self.app.motion.events, [])

    async def test_vision_work_light_and_uncalibrated_rejected_before_takeover(self):
        await self.post('action', {'name': 'enter_work_light'})
        with patch.object(self.app, 'web_takeover', side_effect=AssertionError('invalid request took control')):
            for name in ('start_face_tracking',):
                response, result = await self.post('action', {'name': name})
                self.assertEqual(response.status, 400)
                self.assertIn('退出办公照明', result['error'])
        response, result = await self.post('action', {'name': 'start_hand_tracking'})
        self.assertEqual(response.status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(self.app.current_mode, 'work_light')
        await self.post('action', {'name': 'exit_work_light'})
        self.app.vision.calibrated = False
        response, result = await self.post('action', {'name': 'start_hand_tracking'})
        self.assertEqual(response.status, 400)
        self.assertIn('尚未标定', result['error'])
        self.assertNotEqual(self.app.current_mode, 'tracking')
        _, result = await self.post('action', {'name': 'start_face_tracking'})
        self.assertTrue(result['ok'])

    async def test_vision_sleep_start_sleep_and_maintenance(self):
        await self.post('action', {'name': 'sleep_now'})
        self.assertTrue(self.app.mechanically_asleep)
        await self.post('action', {'name': 'start_face_tracking'})
        await asyncio.sleep(.12)
        self.assertFalse(self.app.mechanically_asleep)
        await self.post('action', {'name': 'sleep_now'})
        self.assertTrue(self.app.mechanically_asleep)
        self.assertEqual(self.app.current_mode, 'sleep')
        self.assertIsNone(self.app.current_motion_task)
        self.assertFalse(self.app.vision.running)
        await self.post('action', {'name': 'start_face_tracking'})
        await self.post('maintenance/begin')
        for name in ('start_face_tracking', 'start_hand_tracking', 'stop_tracking'):
            response, _ = await self.post('action', {'name': name})
            self.assertEqual(response.status, 400)
        response = await self.client.get('/api/v1/web/vision')
        self.assertTrue((await response.json())['vision']['maintenance_active'])
        await self.post('maintenance/end')
        self.assertIsNone(self.app.current_motion_task)
        self.assertFalse(self.app.vision.running)

    async def test_vision_temporary_motion_resumes_one_task(self):
        await self.post('action', {'name': 'start_face_tracking'})
        old_task = self.app.current_motion_task
        # The web request must finish after the action, while exactly one
        # restored persistent runner continues in the background.
        response, result = await asyncio.wait_for(self.post('action',
            {'name': 'play_motion', 'arguments': {'name': 'nod'}}), .8)
        self.assertEqual(response.status, 200)
        self.assertTrue(result['ok'])
        self.assertTrue(old_task.done())
        self.assertEqual(self.app.current_mode, 'tracking')
        self.assertIsNotNone(self.app.current_motion_task)
        self.assertFalse(self.app.current_motion_task.done())
        self.assertEqual(self.app.motion.events.count('play:nod'), 1)

    async def test_real_tracking_entrypoints_from_sleep_and_source_switch(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from lelamp.vision.tracking import NestedTrackingSession
        # Real LampApp entrypoints + real session; replace only mechanical runner.
        self.app.start_face_tracking = LampApp.start_face_tracking.__get__(self.app)
        self.app.start_hand_tracking = LampApp.start_hand_tracking.__get__(self.app)
        self.app.vision.config = SimpleNamespace(hand_control_calibrated=True, hand_voice_acquire_seconds=5)
        self.app.vision.latest_snapshot = lambda: None
        self.app.vision.latest_tracking_inputs = lambda: (None, None, False, ())
        self.app.vision.set_active_hand_target = Mock()
        live = set()
        class Runner:
            def __init__(self, motion, provider):
                self.state = {'status': 'created'}
                self.provider = provider
            async def run(self):
                task = asyncio.current_task()
                live.add(task)
                self.state['status'] = 'running'
                try:
                    while True:
                        self.provider()
                        await asyncio.sleep(.001)
                finally:
                    live.remove(task)
                    self.state['status'] = 'stopped'
        with patch('lelamp.motion.visual_tracking.VisualTrackingRunner', Runner):
            await self.post('action', {'name': 'sleep_now'})
            await self.post('action', {'name': 'start_face_tracking'})
            await asyncio.sleep(.01)
            self.assertIsInstance(self.app._tracking_session, NestedTrackingSession)
            session = self.app._tracking_session
            self.assertFalse(self.app.mechanically_asleep)
            self.assertEqual(len(live), 1)
            await self.post('action', {'name': 'start_hand_tracking'})
            await asyncio.sleep(.01)
            self.assertIs(self.app._tracking_session, session)
            self.assertEqual(session.state['tracking_phase'], 'hand_acquire')
            self.assertEqual(len(live), 1)
            await self.post('action', {'name': 'start_face_tracking'})
            await asyncio.sleep(.01)
            self.assertEqual(session.state['tracking_phase'], 'face_follow')
            self.assertEqual(len(live), 1)
            await self.post('action', {'name': 'sleep_now'})
            self.assertEqual(len(live), 0)
            self.assertEqual(self.app.current_mode, 'sleep')
            self.assertTrue(self.app.mechanically_asleep)

    async def test_vision_wait_timeout_lock_and_error_projection(self):
        self.app.vision.hand_visible = False
        await self.post('action', {'name': 'start_hand_tracking'})
        self.app._tracking_session.requested_at -= 6
        await asyncio.sleep(.2)
        response = await self.client.get('/api/v1/web/vision')
        self.assertEqual((await response.json())['vision']['tracking_phase'], 'face_follow')
        self.app._tracking_session.state.update(tracking_phase='hand_hold', tracking_source='hand')
        await asyncio.sleep(.12)
        response = await self.client.get('/api/v1/web/vision')
        self.assertEqual((await response.json())['vision']['tracking_phase'], 'hand_hold')
        self.app.vision.error = 'camera unavailable'
        response = await self.client.get('/api/v1/web/vision')
        v = (await response.json())['vision']
        self.assertEqual(v['error'], 'camera unavailable')
        self.assertFalse(v['fresh'])
        self.assertEqual(v['gestures'], [])


class QueueControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_preserves_notifications_and_interrupts_chat(self):
        started = asyncio.Event()
        async def processor(batch):
            if batch[0].source == "local_reply":
                started.set()
                await asyncio.Event().wait()
            return batch[0].text, (0,0,0)
        queue = AnnouncementQueue(processor)
        chat = await queue.submit(announcement_id="chat",source="local_reply",text="你好",priority=0)
        reminder = await queue.submit(announcement_id="timer",source="timer",text="喝水",priority=20)
        drain = asyncio.create_task(queue.drain_and_pause())
        await started.wait()
        await queue.pause_for_control()
        await drain
        self.assertTrue((await chat.wait()).interrupted)
        self.assertFalse(reminder.future.done())
        await queue.drain_and_pause(max_priority=-10)
        self.assertFalse(reminder.future.done())
        await queue.drain_and_pause()
        self.assertTrue((await reminder.wait()).success)
        await queue.close()

    async def test_supervisor_survives_pause_but_not_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            app = DemoApp(Path(directory))
            started = asyncio.Event()
            async def voice(_):
                started.set()
                await asyncio.Event().wait()
            with patch("lelamp.app.run_voice", side_effect=voice):
                worker = asyncio.create_task(app.run_voice_supervisor())
                await started.wait()
                await app.pause_voice_for_web()
                self.assertFalse(worker.done())
                worker.cancel()
                await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
            await app.close()
