"""Deterministic work-light interaction tests, independent of ordinary tracking."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, AsyncMock

from test_vision import make_config
from lelamp.vision.types import HandObservation, HandTrackingCandidate, TrackingTarget, VisionSnapshot
from lelamp.vision.work_light import WorkLightHandSession
from lelamp.web.demo import DemoApp


def hand(name='a', label='Closed_Fist', scale=.3):
    return HandObservation(name, 'Left', (), (.5, .5), label, .9, scale)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.seq = 0
        self.snapshot = None
        self.hands = ()
        self.config = make_config(hand_near_enter_scale=.2, hand_near_exit_scale=.17,
            gesture_min_confidence=.5, gesture_confirm_seconds=.2, gesture_cooldown_seconds=0)
        self.vision = Mock(config=self.config)
        self.vision.latest_snapshot.side_effect = lambda: self.snapshot
        self.vision.latest_tracking_inputs.side_effect = self.inputs
        self.session = WorkLightHandSession(self.vision, clock=lambda: self.now)

    def inputs(self):
        candidates = tuple(HandTrackingCandidate(h.track_id,
            TrackingTarget('hand', h.track_id, (.5,.5),(0,0),self.snapshot.captured_at,.9),
            h.palm_scale,h.handedness,h.gesture,h.gesture_confidence,True,self.snapshot.captured_at) for h in self.hands)
        return self.snapshot,None,False,candidates

    def frame(self, *hands, dt=.1):
        self.now += dt
        self.seq += 1
        self.hands = hands
        self.snapshot = VisionSnapshot(self.seq,self.now,self.now,(),hands)
        return self.session.directive()

    def stable(self, *hands):
        for _ in range(5): self.frame(*hands)

    def follow(self):
        self.stable(hand(label='Open_Palm'))
        self.stable(hand())
        self.assertEqual(self.session.phase,'light_follow')

    def test_start_lock_requires_new_open_then_fist(self):
        self.stable(hand());self.assertEqual(self.session.phase,'light_hold')
        self.follow()
        self.stable(hand(label='Open_Palm'));self.assertEqual(self.session.phase,'light_hold')
        # The locking open now primes resume briefly, as in ordinary tracking.
        self.now += self.config.gesture_transition_timeout_seconds + .1
        self.stable(hand());self.assertEqual(self.session.phase,'light_hold')
        self.follow()

    def test_lost_open_ignored_then_fist_recovers_far_then_open_locks(self):
        self.follow();self.frame()
        deadline=self.session.deadline
        self.assertTrue(self.session.dimmed)
        self.stable(hand(label='Open_Palm',scale=.1))
        self.assertEqual(self.session.phase,'light_lost')
        self.assertEqual(self.session.deadline,deadline)
        self.stable(hand(scale=.1));self.assertEqual(self.session.phase,'light_follow')
        self.stable(hand(label='Open_Palm',scale=.1));self.assertEqual(self.session.phase,'light_hold')
        self.assertFalse(self.session.dimmed)

    def test_timeout_ignores_late_fist_requires_fresh_open(self):
        self.follow();last=self.session.last_seen;self.frame()
        self.now=last+5
        self.frame(hand(),dt=0)
        self.assertEqual(self.session.phase,'light_hold')
        self.stable(hand());self.assertEqual(self.session.phase,'light_hold')
        self.follow()

    def test_recovery_at_49_seconds(self):
        self.follow();last=self.session.last_seen;self.frame()
        self.now=last+4.5
        for _ in range(4):self.frame(hand())
        self.assertEqual(self.session.phase,'light_follow')

    def test_continuous_fist_reconfirmed_after_short_loss(self):
        self.follow();self.frame();self.frame(hand())
        self.assertEqual(self.session.phase,'light_lost')
        self.stable(hand());self.assertEqual(self.session.phase,'light_follow')

    def test_old_id_expiry_new_unique_far_fist_can_recover(self):
        self.follow();self.frame(dt=2.3)
        self.stable(hand(name='new',scale=.1))
        self.assertEqual(self.session.selected,'new')
        self.assertEqual(self.session.phase,'light_follow')

    def test_stale_results_hold_and_timeout(self):
        self.follow();self.now+=.3
        self.assertEqual(self.session.directive().mode,'hold')
        self.assertEqual(self.session.phase,'light_lost')
        self.now+=5;self.session.directive();self.assertEqual(self.session.phase,'light_hold')

    def test_nearest_selection_and_no_fallback_to_far_fist(self):
        self.session.request_hand()
        for _ in range(12):self.frame(hand('far',scale=.15),hand('near','Open_Palm',.35))
        self.assertEqual(self.session.phase,'light_acquire')
        for _ in range(8):self.frame(hand('far',scale=.15),hand('near',scale=.35))
        self.assertEqual(self.session.selected,'near')
        for _ in range(8):self.frame(hand('far',scale=.6),hand('near',scale=.2))
        self.assertEqual(self.session.selected,'near')

    def test_ambiguous_hands_do_not_extend_timeout(self):
        self.follow();last=self.session.last_seen;self.frame()
        for _ in range(20):self.frame(hand('b',scale=.3),hand('c',scale=.31))
        self.assertEqual(self.session.phase,'light_lost')
        self.assertTrue(self.session.ambiguous)
        self.now=last+5;self.session.directive();self.assertEqual(self.session.phase,'light_hold')

    def test_tool_timeout_and_uncalibrated(self):
        self.session.request_hand();self.frame(dt=5)
        self.assertEqual(self.session.phase,'light_hold')
        self.session.config=replace(self.config,hand_near_enter_scale=None)
        with self.assertRaises(ValueError):self.session.request_hand()

    def test_wrong_hand_cannot_complete_open_fist(self):
        self.stable(hand('a','Open_Palm'));self.stable(hand('b'))
        self.assertEqual(self.session.phase,'light_hold')

    def test_exit_uses_lock_open_baseline_and_latches_hold(self):
        self.follow()
        self.assertFalse(self.session.exit_history)
        self.stable(hand(label='Open_Palm'))
        self.assertFalse(self.session.exit_requested)
        self.assertEqual(self.session.phase, 'light_hold')
        self.stable(hand())
        self.assertEqual(self.session.phase, 'light_follow')
        self.stable(hand(label='Open_Palm', scale=.1))
        self.assertTrue(self.session.exit_requested)
        for _ in range(5):
            self.assertEqual(self.frame(hand()).mode, 'hold')

    def test_long_pause_does_not_exit_and_fresh_hold_open_can_seed(self):
        self.follow();self.stable(hand(label='Open_Palm'))
        self.now += 3.1
        self.stable(hand());self.assertEqual(self.session.phase,'light_hold')
        self.assertFalse(self.session.exit_requested)
        self.stable(hand(label='Open_Palm'));self.stable(hand())
        self.assertFalse(self.session.exit_requested)
        self.stable(hand(label='Open_Palm'))
        self.assertTrue(self.session.exit_requested)

    def test_far_fist_cannot_resume_or_complete_exit(self):
        self.follow();self.stable(hand(label='Open_Palm'))
        self.stable(hand(scale=.1));self.stable(hand(label='Open_Palm',scale=.1))
        self.assertFalse(self.session.exit_requested)
        self.assertEqual(self.session.phase,'light_hold')

    def test_loss_stop_and_ambiguity_clear_exit_history(self):
        self.follow();self.stable(hand(label='Open_Palm'));self.stable(hand())
        self.assertTrue(self.session.exit_history)
        self.frame()
        self.assertFalse(self.session.exit_history)
        self.stable(hand());self.stable(hand(label='Open_Palm'))
        self.assertFalse(self.session.exit_requested)
        self.frame(hand('b',scale=.3),hand('c',scale=.3))
        self.assertFalse(self.session.exit_history)
        self.session.lock()
        self.assertFalse(self.session.exit_history)
        self.stable(hand());self.assertEqual(self.session.phase,'light_hold')

    def test_hand_change_cannot_complete_old_sequence(self):
        self.follow();self.stable(hand(label='Open_Palm'))
        self.stable(hand('b'))
        self.assertFalse(self.session.exit_requested)
        self.assertEqual(self.session.phase,'light_hold')
        self.assertFalse(self.session.exit_history)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=DemoApp(Path(self.tmp.name))
        self.app.lighting.work_light=Mock()
        await self.app.enter_work_light()
        self.app.vision.hand_visible=False
        await asyncio.sleep(.06)

    async def asyncTearDown(self):
        await self.app.sleep()
        await self.app.close()
        self.tmp.cleanup()

    async def test_lost_light_preserves_latest_user_settings(self):
        s=self.app._work_light_session
        s.phase='light_lost';s.deadline=s.clock()+5
        await asyncio.sleep(.12)
        self.assertEqual(self.app.lighting.work_light.call_args.args[:2],('white',22.5))
        await self.app.update_work_light(tone='warm',brightness_step=25)
        await asyncio.sleep(.12)
        self.assertEqual(self.app.lighting.work_light.call_args.args[:2],('warm',30))
        self.assertEqual(self.app.work_brightness,100)
        s.lock();await asyncio.sleep(.12)
        self.assertEqual(self.app.lighting.work_light.call_args.args[:2],('warm',100))

    async def test_stop_restarts_one_hold_task_and_allows_new_request(self):
        old=self.app.current_motion_task
        await self.app.start_hand_tracking();await self.app.stop_tracking()
        self.assertTrue(old.done())
        self.assertEqual(self.app.current_mode,'work_light')
        self.assertEqual(self.app._work_light_session.phase,'light_hold')
        self.assertFalse(self.app.current_motion_task.done())
        task=self.app.current_motion_task
        self.app._ensure_work_light_runner();self.assertIs(task,self.app.current_motion_task)
        await self.app.start_hand_tracking()
        self.assertEqual(self.app._work_light_session.phase,'light_acquire')

    async def test_temporary_action_restores_actual_position_and_locks(self):
        position={'base_yaw.pos':23,'wrist_pitch.pos':17}
        self.app.motion.read_action=Mock(return_value=position)
        self.app.motion.move_tracking_raw=AsyncMock()
        await self.app.play_motion('nod')
        self.assertEqual(self.app.motion.move_tracking_raw.call_args.args[0],position)
        self.assertEqual(self.app._work_light_session.phase,'light_hold')
        self.assertFalse(self.app.current_motion_task.done())

    async def test_web_brightness_keeps_phase_but_pose_locks(self):
        self.app._work_light_session.phase='light_lost'
        self.app._work_light_session.deadline=self.app._work_light_session.clock()+5
        await self.app.execute_web_action('update_work_light',{'brightness_step':25})
        self.assertEqual(self.app._work_light_session.phase,'light_lost')
        await self.app.update_work_light(pose='low')
        self.assertEqual(self.app._work_light_session.phase,'light_hold')
        self.assertFalse(self.app.current_motion_task.done())

    async def test_sleep_and_maintenance_cannot_restore_old_session(self):
        old=self.app.current_motion_task
        await self.app.begin_web_maintenance()
        self.assertTrue(old.done());self.assertIsNone(self.app._work_light_session)
        self.assertIsNone(self.app.current_motion_task)
        await self.app.end_web_maintenance()
        self.assertIsNone(self.app.current_motion_task)

    async def test_feature_disabled_preserves_original_work_light(self):
        await self.app.exit_work_light()
        self.app.vision.config=replace(self.app.vision.config,work_light_hand_enabled=False)
        await self.app.enter_work_light()
        self.assertIsNone(self.app._work_light_session)
        self.assertIsNone(self.app.current_motion_task)
        with self.assertRaises(ValueError):await self.app.start_hand_tracking()

    async def test_gesture_exit_once_no_self_cancel_or_restore(self):
        from unittest.mock import patch
        session=self.app._work_light_session
        old=self.app.current_motion_task
        with patch.object(self.app, 'exit_work_light', wraps=self.app.exit_work_light) as exit_light:
            session.exit_requested=True
            for _ in range(40):
                if self.app.current_mode == 'standby': break
                await asyncio.sleep(.02)
            self.assertEqual(self.app.current_mode,'standby')
            self.assertEqual(exit_light.await_count,1)
        self.assertTrue(old.done())
        self.assertIsNone(self.app.current_motion_task)
        self.assertIsNone(self.app._work_light_session)
        await self.app.web_release()
        self.assertIsNone(self.app.current_motion_task)

    async def test_sleep_invalidates_queued_gesture_exit(self):
        await self.app._web_lock.acquire()
        session=self.app._work_light_session
        session.exit_requested=True
        self.app._queue_work_light_exit(session)
        task=self.app._work_light_exit_task
        await self.app.sleep()
        self.app._web_lock.release()
        await asyncio.wait_for(task,.5)
        self.assertEqual(self.app.current_mode,'sleep')
        self.assertNotIn('standby',self.app.motion.events)

    async def test_maintenance_and_new_session_invalidate_pending_exit(self):
        for transition in (self.app.begin_web_maintenance, self.app.enter_work_light):
            await self.app._web_lock.acquire()
            session=self.app._work_light_session
            session.exit_requested=True
            self.app._queue_work_light_exit(session)
            task=self.app._work_light_exit_task
            await transition()
            self.app._web_lock.release()
            await asyncio.wait_for(task,.5)
            self.assertNotIn('standby',self.app.motion.events)
            if self.app.maintenance.active:
                await self.app.end_web_maintenance()
                await self.app.enter_work_light()

class CurrentPositionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_pose_hold_never_goes_home_and_safe_entry_is_smooth(self):
        import numpy as np
        from test_vision import VisualTrackingControlTests, FakeMotion
        from lelamp.motion.visual_tracking import VisualTrackingRunner
        from lelamp.vision.types import TrackingDirective
        class Motion(FakeMotion):
            def __init__(self): self.transitions=[];self.sent=[];self.pos=99
            async def tracking_home(self): raise AssertionError('unexpected tracking home')
            def read_action(self):return {'base_yaw.pos':0.,'wrist_pitch.pos':self.pos}
            async def move_tracking_raw(self, action, duration):
                self.transitions.append((dict(action),duration));self.pos=action['wrist_pitch.pos']
            def send_tracking_action(self,action):self.sent.append(dict(action))
        motion=Motion();directive=TrackingDirective('hold',source='hand')
        config=VisualTrackingControlTests().config()
        runner=VisualTrackingRunner(motion,lambda:directive,config=config,
            response_matrix=np.eye(2)*.01,start_from_current=True)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.sleep(.06)
            self.assertEqual(motion.sent,[]);self.assertEqual(motion.transitions,[])
            import time
            directive=TrackingDirective('follow',TrackingTarget('hand','a',(.7,.7),(0,0),time.monotonic(),.9),'hand')
            await asyncio.sleep(.06)
            self.assertEqual(len(motion.transitions),1)
            self.assertEqual(motion.transitions[0][0]['wrist_pitch.pos'],50)
            self.assertGreater(motion.transitions[0][1],.2)
            self.assertTrue(all(-50<=a['wrist_pitch.pos']<=50 for a in motion.sent))
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

class SafetyTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_loss_cancels_safe_entry_before_follow_commands(self):
        import numpy as np
        import time
        from test_vision import VisualTrackingControlTests, FakeMotion
        from lelamp.motion.visual_tracking import VisualTrackingRunner
        from lelamp.vision.types import TrackingDirective
        started, cancelled = asyncio.Event(), asyncio.Event()
        class Motion(FakeMotion):
            def __init__(self): self.sent=[]
            def read_action(self): return {'base_yaw.pos':0.,'wrist_pitch.pos':99.}
            async def tracking_home(self): raise AssertionError('unexpected home')
            async def move_tracking_raw(self, action, duration):
                started.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
            def send_tracking_action(self, action): self.sent.append(action)
        motion=Motion()
        directive=TrackingDirective('follow',TrackingTarget('hand','a',(.7,.7),(0,0),time.monotonic(),.9),'hand')
        runner=VisualTrackingRunner(motion,lambda:directive,config=VisualTrackingControlTests().config(),
            response_matrix=np.eye(2)*.01,start_from_current=True)
        task=asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(started.wait(),.5)
            directive=TrackingDirective('hold',source='hand')
            await asyncio.wait_for(cancelled.wait(),.2)
            await asyncio.sleep(.04)
            self.assertEqual(motion.sent,[])
            self.assertFalse(task.done())
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)


class LightFadeTests(unittest.TestCase):
    def test_feedback_fades_from_current_and_default_fade_is_unchanged(self):
        from unittest.mock import patch
        from lelamp.lighting.controller import LightingController
        light=LightingController(led_brightness=255)
        light._show=Mock()
        with patch('lelamp.lighting.controller.time.sleep'):
            light.fade_to((255,255,255),30,.2,from_current=True)
            values=[call.args[1] for call in light._show.call_args_list]
            self.assertGreater(values[0],.3)
            self.assertAlmostEqual(values[-1],.3)
            self.assertTrue(all(a>=b for a,b in zip(values,values[1:])))
            light._show.reset_mock()
            light.fade_to((255,255,255),75,.2)
            self.assertAlmostEqual(light._show.call_args_list[0].args[1],.75/6)
