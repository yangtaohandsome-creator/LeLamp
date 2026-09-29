import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from lelamp.app import LampApp
from lelamp.voice.kws import KeywordSpotterCache
from lelamp.vision.controller import VisionController
from lelamp.web.demo import DemoApp
from test_vision import make_config, FakeFrameSource, FakeFace, FakeHands


def wait_for(predicate):
    deadline=time.monotonic()+2
    while not predicate():
        if time.monotonic()>deadline: raise AssertionError('timeout')
        time.sleep(.01)


class ResourceTests(unittest.TestCase):
    def test_kws_cache_settings_paths_and_file_changes(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root); keywords=path/'keywords';keywords.write_text('lamp')
            factory=Mock(side_effect=lambda *_: Mock())
            cache=KeywordSpotterCache()
            first,reused=cache.get(path,keywords,factory)
            self.assertFalse(reused)
            self.assertIs(cache.get(path,keywords,factory)[0],first)
            self.assertTrue(cache.get(path,keywords,factory)[1])
            keywords.write_text('new keyword')
            self.assertIsNot(cache.get(path,keywords,factory)[0],first)
            with patch.dict('os.environ',{'KWS_NUM_THREADS':'7'}):
                self.assertFalse(cache.get(path,keywords,factory)[1])
            self.assertFalse(cache.get(path/'other',keywords,factory)[1])
            cache.clear();self.assertFalse(cache.get(path,keywords,factory)[1])

    def test_hand_only_lazy_face_load_and_toggle_without_camera_restart(self):
        face=FakeFace(None);face.detect=Mock(wraps=face.detect)
        factory=Mock(return_value=face)
        camera=Mock(side_effect=FakeFrameSource)
        hands=Mock(side_effect=FakeHands)
        c=VisionController(make_config(),camera_factory=camera,face_factory=factory,hands_factory=hands)
        c.set_face_detection_enabled(False);c.start()
        try:
            wait_for(lambda:c.latest_snapshot() is not None)
            self.assertEqual(len(c.latest_snapshot().hands),1)
            pixels,snapshot,enabled,generation=c.latest_model_preview()
            self.assertFalse(pixels.flags.writeable)
            self.assertIs(snapshot,c.latest_snapshot())
            self.assertEqual(pixels.shape[:2],c.config.model_size[::-1])
            self.assertIs(pixels,c.latest_model_preview()[0])
            factory.assert_not_called()
            c.set_face_detection_enabled(True)
            wait_for(lambda: bool(c.latest_snapshot().faces))
            c.set_face_detection_enabled(False)
            self.assertEqual(c.latest_snapshot().faces,())
            self.assertIsNone(c.latest_face_target())
            # Allow the one already in flight to finish before checking no new calls.
            time.sleep(.15);count=face.detect.call_count
            time.sleep(.2);self.assertEqual(face.detect.call_count,count)
            c.set_face_detection_enabled(True)
            self.assertEqual(c.latest_snapshot().faces,())
            wait_for(lambda:bool(c.latest_snapshot().faces))
            factory.assert_called_once();camera.assert_called_once();hands.assert_called_once()
        finally:c.stop()

    def test_inflight_face_result_cannot_reappear_after_policy_change(self):
        entered=threading.Event();release=threading.Event()
        class Face(FakeFace):
            def detect(self,image):
                entered.set();release.wait(2);return super().detect(image)
        c=VisionController(make_config(),camera_factory=FakeFrameSource,face_factory=Face,hands_factory=FakeHands)
        c.start()
        try:
            self.assertTrue(entered.wait(2))
            c.set_face_detection_enabled(False);c.set_face_detection_enabled(True)
            c.set_face_detection_enabled(False);release.set()
            wait_for(lambda:c.latest_snapshot() is not None)
            self.assertEqual(c.latest_snapshot().faces,())
            self.assertFalse(c.state()['face_detection_enabled'])
            self.assertIsNone(c.latest_face_target())
        finally:release.set();c.stop()


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transition_retains_vision_until_voice_ready_or_sleep(self):
        with tempfile.TemporaryDirectory() as root:
            app=DemoApp(Path(root));app._started=True
            app._sync_vision_lifecycle=LampApp._sync_vision_lifecycle.__get__(app)
            app.current_mode='work_light';session=Mock(exit_requested=True)
            app._work_light_session=session
            app.vision.stop=Mock(wraps=app.vision.stop)
            await app._sync_vision_lifecycle()
            self.assertFalse(app.vision.state()['face_detection_enabled'])
            await app._exit_work_light_to_conversation(session,app._mode_version)
            self.assertTrue(app._voice_transition_valid())
            app.vision.stop.assert_not_called()
            self.assertTrue(app.vision.state()['face_detection_enabled'])
            await app.set_voice_session_active(True)
            await app._finish_voice_transition()
            app.vision.stop.assert_not_called()
            await app.sleep()
            self.assertIsNone(app._voice_transition)
            app.vision.stop.assert_called_once()
            await app.announcements.close()

    async def test_invalid_transition_cannot_keep_camera_alive(self):
        with tempfile.TemporaryDirectory() as root:
            app=DemoApp(Path(root));app.current_mode='standby'
            app._mode_version=1;app._voice_transition=(0,0,0,None)
            self.assertTrue(app._vision_should_run())
            app._web_epoch+=1
            await app._sync_vision_lifecycle()
            self.assertIsNone(app._voice_transition)
            self.assertFalse(app.vision.running)
            await app.announcements.close()
