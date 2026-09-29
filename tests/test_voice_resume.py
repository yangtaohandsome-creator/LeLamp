"""Real voice coroutine with fake devices/services; no microphone or Agent."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from lelamp import app as module
from lelamp.app import VoiceResumeRequest
from lelamp.voice.capture_task import capture_call
from lelamp.voice.announcement import AnnouncementQueue
from lelamp.web.demo import DemoApp


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_joins_blocked_reader_even_on_second_cancel(self):
        started, stopped, ended = threading.Event(), threading.Event(), threading.Event()
        def read():
            started.set()
            stopped.wait(2)
            time.sleep(.03)
            ended.set()
            raise RuntimeError('closed')
        task = asyncio.create_task(capture_call(stopped.set, read))
        await until(started.is_set)
        task.cancel()
        await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ended.is_set())


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = DemoApp(Path(self.temp.name))
        self.app.handle_text = module.LampApp.handle_text.__get__(self.app)
        self.app.light_state = Mock()
        self.app.sound_configured = Mock(return_value=False)
        self.app.current_mode = 'work_light'
        self.session = Mock(exit_requested=True)
        self.app._work_light_session = self.session

    async def asyncTearDown(self):
        await self.app.pause_voice()
        await self.app.announcements.close()
        self.temp.cleanup()

    async def test_gesture_exit_at_all_five_voice_stages(self):
        for shared in (False, True):
            for stage in ('wake', 'listen', 'asr', 'agent', 'playback'):
                with self.subTest(stage=stage, shared=shared):
                    await self.exercise_stage(stage, shared)

    async def exercise_stage(self, stage, shared):
        app = self.app
        app.current_mode = 'work_light'
        app._work_light_session = self.session
        app.mechanically_asleep = False
        self.session.exit_requested = True
        devices = []
        reached = asyncio.Event()
        in_reader = threading.Event()
        phase = [stage]
        timeouts = []
        session_ids = []
        def start():
            self.assertFalse(any(not d.is_set() for d in devices), 'overlapping capture devices')
            d = threading.Event(); devices.append(d); return d
        def read(device, *_):
            if phase[0] == 'wake':
                in_reader.set(); device.wait(2)
                raise RuntimeError('closed')
            return np.zeros((1600, 2), np.float32)
        def utterance(read_block, timeout, *_args, **_kw):
            timeouts.append(timeout)
            if phase[0] in ('listen', 'resumed'):
                in_reader.set()
                devices[-1].wait(2)
                raise RuntimeError('closed')
            return np.ones(1600, np.float32)
        class Shared:
            def __init__(self, *_): self.device=start()
            def start(self): pass
            def pause(self): pass
            def resume(self): pass
            def close(self): self.device.set()
            def read_stereo(self): return read(self.device)
            def read_mono(self): return self.read_stereo().mean(axis=1)
        async def asr(*_):
            if phase[0] == 'asr':
                reached.set(); await asyncio.Event().wait()
            return '一个测试问题'
        async def agent(text, session_id):
            session_ids.append(session_id)
            if phase[0] == 'agent':
                reached.set(); await asyncio.Event().wait()
            return '回答'
        async def playback(batch):
            reached.set(); await asyncio.Event().wait()
        await app.announcements.close()
        app.announcements = AnnouncementQueue(playback)
        spotter = Mock()
        spotter.is_ready.return_value = False
        spotter.get_result.return_value = True
        with ExitStack() as stack:
            for name, value in dict(load_voice_config=Mock(), preconnect_tts=Mock(),
                    make_spotter=Mock(return_value=spotter), start_capture=start, SharedCapture=Shared,
                    stop_capture=lambda d: d.set(), read_capture_stereo_block=read,
                    measure_noise=lambda *_: [], capture_utterance=utterance,
                    transcribe=asr, ask_agent=agent).items():
                stack.enter_context(patch.object(module, name, value))
            stack.enter_context(patch.dict('os.environ', {'VOICE_SHARED_CAPTURE':'1' if shared else '0',
                'CONVERSATION_IDLE_SECONDS':'15'}))
            supervisor = asyncio.create_task(app.run_voice_supervisor())
            try:
                await until(in_reader.is_set if stage in ('wake','listen') else reached.is_set)
                old_task = app.voice_task
                in_reader.clear()
                phase[0] = 'resumed'
                await app._exit_work_light_to_conversation(self.session, app._mode_version)
                await until(in_reader.is_set)
                self.assertTrue(old_task.done())
                self.assertEqual(app.current_mode, 'standby')
                self.assertTrue(app.voice_session_active)
                self.assertGreater(timeouts[-1], 14.8)
                self.assertEqual(app.light_state.call_args.args, ('listening',))
                self.assertTrue(all(d.is_set() for d in devices[:-1]))
                await app.pause_voice()
                self.assertIsNotNone(app._voice_context[0])
                if session_ids:
                    self.assertEqual(app._voice_context[0], session_ids[0])
                self.assertIsNone(app._active_agent_turn_id)
            finally:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
                await app.pause_voice()
                await app.resume_voice()

    async def test_request_invalidated_by_sleep_mode_web_and_maintenance(self):
        app=self.app
        await app._exit_work_light_to_conversation(self.session, app._mode_version)
        r=app._voice_resume_request
        self.assertTrue(app.voice_resume_valid(r))
        app._web_epoch += 1
        self.assertFalse(app.voice_resume_valid(r)); app._web_epoch -= 1
        app.maintenance.active=True
        self.assertFalse(app.voice_resume_valid(r)); app.maintenance.active=False
        await app.sleep()
        self.assertFalse(app.voice_resume_valid(r))

    async def test_stale_exit_does_not_pause_or_move(self):
        self.app.pause_voice=AsyncMock()
        await self.app._exit_work_light_to_conversation(self.session, -1)
        self.app.pause_voice.assert_not_called()
        self.assertNotIn('standby',self.app.motion.events)

    async def test_plain_exit_does_not_cancel_voice_or_request_listening(self):
        self.app.pause_voice=AsyncMock()
        self.assertTrue(await self.app.exit_work_light())
        self.app.pause_voice.assert_not_called()
        self.assertIsNone(self.app._voice_resume_request)

    async def test_exit_failure_does_not_request_listening(self):
        self.app.motion.standby=AsyncMock(side_effect=RuntimeError('motion failed'))
        with self.assertRaises(RuntimeError):
            await self.app._exit_work_light_to_conversation(self.session,self.app._mode_version)
        self.assertIsNone(self.app._voice_resume_request)
        self.app.light_state.assert_called_with('error')

    async def test_only_cancelled_reply_discarded_notifications_preserved(self):
        app=self.app
        reply=await app.announcements.submit(announcement_id='reply',source='local_reply',text='old',priority=0)
        timer=await app.announcements.submit(announcement_id='timer',source='timer',text='timer',priority=20)
        await app._exit_work_light_to_conversation(self.session,app._mode_version)
        self.assertTrue((await reply.wait()).interrupted)
        self.assertTrue(app.announcements.has_pending())
        self.assertEqual([e.announcement.source for e in app.announcements._heap],['timer'])

    async def test_sleep_during_exit_cleanup_prevents_stale_resume(self):
        app=self.app
        async def pause():
            await app.sleep()
        app.pause_voice=AsyncMock(side_effect=pause)
        await app._exit_work_light_to_conversation(self.session,app._mode_version)
        self.assertIsNone(app._voice_resume_request)
        self.assertEqual(app.current_mode,'sleep')

    async def test_resume_checked_again_after_voice_activation(self):
        app=self.app
        app.current_mode='standby';app._work_light_session=None
        app._voice_resume_request=VoiceResumeRequest(app._mode_version,app._web_epoch,
            app._sleep_generation,'history',4)
        real_set=app.set_voice_session_active
        async def activate(value):
            await real_set(value)
            if value:
                await app.sleep()
        app.set_voice_session_active=activate
        device=threading.Event();reading=threading.Event()
        def read(*_):
            reading.set();device.wait(2);raise RuntimeError('closed')
        with patch.object(module,'load_voice_config'), patch.object(module,'preconnect_tts'), \
                patch.object(module,'make_spotter'), patch.object(module,'start_capture',return_value=device), \
                patch.object(module,'stop_capture',side_effect=lambda _:device.set()), \
                patch.object(module,'measure_noise',return_value=[]), \
                patch.object(module,'read_capture_stereo_block',side_effect=read), \
                patch.dict('os.environ',{'VOICE_SHARED_CAPTURE':'0'}):
            task=asyncio.create_task(module.run_voice(app))
            await until(reading.is_set)
            self.assertNotIn(unittest.mock.call('listening'),app.light_state.call_args_list)
            self.assertFalse(app.voice_session_active)
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_capture_start_failure_clears_pending_resume(self):
        app=self.app
        app.current_mode='standby'
        app._voice_resume_request=VoiceResumeRequest(0,0,0,None,0)
        with patch.object(module,'load_voice_config'), patch.object(module,'preconnect_tts'), \
                patch.object(module,'make_spotter'), \
                patch.object(module,'start_capture',side_effect=RuntimeError('no audio')), \
                patch.dict('os.environ',{'VOICE_SHARED_CAPTURE':'0'}):
            with self.assertRaises(RuntimeError):
                await module.run_voice(app)
        self.assertFalse(app.voice_session_active)
        self.assertIsNone(app._voice_resume_request)
        app.light_state.assert_called_with('error')

    async def test_voice_exit_finishes_answer_then_fresh_listening(self):
        app=self.app
        devices=[];timeouts=[];ids=[];waiting=threading.Event()
        def start():
            d=threading.Event();devices.append(d);return d
        def utterance(_read,timeout,*args,**kwargs):
            timeouts.append(timeout)
            if len(timeouts)==1:return np.ones(1600,np.float32)
            waiting.set();devices[-1].wait(2);raise RuntimeError('closed')
        async def agent(text,session_id):
            ids.append(session_id)
            self.assertTrue(await app.exit_work_light())
            return '已退出照明'
        async def playback(batch):
            return batch[0].text,(0,0,0)
        await app.announcements.close();app.announcements=AnnouncementQueue(playback)
        spotter=Mock();spotter.is_ready.return_value=False;spotter.get_result.return_value=True
        with ExitStack() as stack:
            patches=dict(load_voice_config=Mock(),preconnect_tts=Mock(),make_spotter=Mock(return_value=spotter),
                start_capture=start,stop_capture=lambda d:d.set(),
                read_capture_stereo_block=lambda *_:np.zeros((1600,2),np.float32),measure_noise=lambda *_:[],
                capture_utterance=utterance,transcribe=AsyncMock(return_value='退出照明'),ask_agent=agent)
            for key,value in patches.items():stack.enter_context(patch.object(module,key,value))
            stack.enter_context(patch.dict('os.environ',{'VOICE_SHARED_CAPTURE':'0','CONVERSATION_IDLE_SECONDS':'15'}))
            task=asyncio.create_task(module.run_voice(app))
            try:
                await until(waiting.is_set)
                self.assertEqual(app.current_mode,'standby')
                self.assertGreater(timeouts[-1],14.8)
                self.assertIsNone(app._voice_resume_request)
                app.light_state.assert_called_with('listening')
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(app._voice_context,(ids[0],1))

    async def test_complete_idle_window_then_sleep_without_wake(self):
        app=self.app
        app.current_mode='standby';app._work_light_session=None
        app._voice_resume_request=VoiceResumeRequest(0,0,0,'preserved-history',3)
        ready=threading.Event();devices=[];timeouts=[]
        def start():
            d=threading.Event();devices.append(d);return d
        def utterance(_read,timeout,*args,**kwargs):
            timeouts.append(timeout)
            time.sleep(timeout)
            return None
        def read(*_):
            ready.set();devices[-1].wait(2);raise RuntimeError('closed')
        with ExitStack() as stack:
            patches=dict(load_voice_config=Mock(),preconnect_tts=Mock(),make_spotter=Mock(),
                start_capture=start,stop_capture=lambda d:d.set(),read_capture_stereo_block=read,
                measure_noise=lambda *_:[],capture_utterance=utterance,clear_session=AsyncMock())
            for key,value in patches.items():stack.enter_context(patch.object(module,key,value))
            stack.enter_context(patch.dict('os.environ',{'VOICE_SHARED_CAPTURE':'0','CONVERSATION_IDLE_SECONDS':'.08'}))
            task=asyncio.create_task(module.run_voice(app))
            try:
                await until(ready.is_set)
                self.assertGreater(timeouts[0],.06)
                self.assertEqual(app.current_mode,'sleep')
                self.assertTrue(app.mechanically_asleep)
                module.clear_session.assert_awaited_once_with('preserved-history')
                self.assertNotIn(unittest.mock.call('wake_ack'),app.light_state.call_args_list)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(app._voice_context,(None,0))

    async def test_voice_sleep_moves_before_waiting_for_vision_cleanup(self):
        app = self.app
        app.current_mode = 'standby'
        app._work_light_session = None
        app.mechanically_asleep = False
        app._voice_resume_request = VoiceResumeRequest(0, 0, 0, 'session', 1)
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        async def vision_cleanup():
            if not app.voice_session_active:
                cleanup_started.set()
                await release_cleanup.wait()
        app._sync_vision_lifecycle = vision_cleanup
        with ExitStack() as stack:
            patches = dict(load_voice_config=Mock(), preconnect_tts=Mock(),
                make_spotter=Mock(), start_capture=Mock(), stop_capture=Mock(),
                measure_noise=lambda *_: [], capture_utterance=lambda *_a, **_k: None,
                clear_session=AsyncMock())
            for key, value in patches.items():
                stack.enter_context(patch.object(module, key, value))
            stack.enter_context(patch.dict('os.environ', {'VOICE_SHARED_CAPTURE': '0'}))
            task = asyncio.create_task(module.run_voice(app))
            try:
                await until(cleanup_started.is_set)
                self.assertIn('sleep', app.motion.events)
                self.assertTrue(app.mechanically_asleep)
                self.assertFalse(app.voice_session_active)
            finally:
                task.cancel()
                release_cleanup.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_recovery_keeps_supervisor_alive_without_motion_retry(self):
        app=self.app
        app.current_mode='standby';app._work_light_session=None
        app._voice_resume_request=VoiceResumeRequest(0,0,0,None,0)
        with patch.object(module,'run_voice',side_effect=RuntimeError('capture unavailable')) as voice:
            supervisor=asyncio.create_task(app.run_voice_supervisor())
            try:
                await until(lambda:app._voice_pause_requested)
                self.assertFalse(supervisor.done())
                self.assertEqual(app.current_mode,'standby')
                self.assertEqual(app.motion.events,[])
                voice.assert_awaited_once()
                app.light_state.assert_called_with('error')
            finally:
                supervisor.cancel();await asyncio.gather(supervisor,return_exceptions=True)
