import asyncio
import os
import signal
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import numpy as np
from .voice.config import SAMPLE_RATE, env_float, has_meaningful_text, load_voice_config
from .voice.audio import (
    start_capture, stop_capture, read_capture_block, read_capture_stereo_block,
    select_kws_audio, measure_noise,
)
from .voice.shared_capture import SharedCapture
from .voice.kws import make_spotter
from .voice.vad import CaptureInterrupted, capture_utterance
from .voice.asr import transcribe
from .voice.tts import close_tts, preconnect_tts, speak
from .voice.playback import PlaybackControl, SpeechInterrupted
from .voice.barge_in import BargeInController, BargeInUtterance, strip_stop_prefix
from .voice.announcement import (
    Announcement, AnnouncementInterrupted, AnnouncementPriority, AnnouncementQueue,
)
from .agent import AgentError, ask_agent, clear_session
from .agent.common import AgentConnectionError
from .agent.pi_service import PiAgentService
from .control import ControlServer
from .remote_text import RemoteTextServer
from .tools import ToolExecutor, ToolSource
from .timer import TimerManager, TimerSnapshot
from .alarm import AlarmManager, AlarmSnapshot
from .location import LocationError, resolve_location
from .audio import SoundPlayer
from .diagnostics import AudioHealthMonitor


@dataclass(frozen=True)
class ActionResult:
    """A high-level app result; hardware modules never decide app shutdown."""

    text: str
    status: str = "completed"
    expression: str | None = None


@dataclass(frozen=True)
class RemoteActionResult(ActionResult):
    spoken: bool = False

async def run_voice(app) -> None:
    load_voice_config()
    shared_capture_enabled = os.getenv("VOICE_SHARED_CAPTURE", "0") == "1"

    def read_mono():
        if shared_capture_enabled:
            return capture.read_mono()
        return read_capture_block(capture, app.audio_health.observe)

    def read_stereo():
        if shared_capture_enabled:
            return capture.read_stereo()
        return read_capture_stereo_block(capture, app.audio_health.observe)

    def pause_capture() -> None:
        nonlocal capture
        if capture is None:
            return
        if shared_capture_enabled:
            capture.pause()
            return
        app.capture_active = False
        stop_capture(capture)
        capture = None

    def resume_capture() -> None:
        nonlocal capture
        if shared_capture_enabled:
            capture.resume()
            app.capture_active = True
            return
        capture = start_capture()
        app.capture_active = True

    # Prepare Edge while KWS is waiting. Wake detection calls this again, so a
    # failed startup connection gets another chance without delaying speech.
    preconnect_tts()
    model_dir = Path(os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"))
    keywords_file = Path(os.getenv("KWS_KEYWORDS_FILE", str(model_dir / "keywords_xiao_deng.txt")))
    spotter = make_spotter(model_dir, keywords_file)
    if shared_capture_enabled:
        capture = SharedCapture(app.audio_health.observe)
        capture.resume()
        capture.start()
        app.shared_capture = capture
        print("持续麦克风采音已启用", flush=True)
    else:
        capture = start_capture()
    app.capture_active = True
    conversation_active = False
    conversation_id: str | None = None
    conversation_turns = 0
    ambient_levels: deque[float] = deque(maxlen=50)
    try:
        startup_levels = await asyncio.to_thread(
            measure_noise, read_mono,
            env_float("VAD_CAPTURE_WARMUP_SECONDS", 0.8),
        )
    except Exception as exc:
        if not shared_capture_enabled:
            raise
        print(f"持续采音启动失败，回退旧采音方式: {exc}", file=sys.stderr, flush=True)
        capture.close()
        app.shared_capture = None
        shared_capture_enabled = False
        capture = start_capture()
        startup_levels = await asyncio.to_thread(
            measure_noise, read_mono,
            env_float("VAD_CAPTURE_WARMUP_SECONDS", 0.8),
        )
    ambient_levels.extend(startup_levels)
    # Never feed the ALSA startup transient into the first decoder stream.
    stream = spotter.create_stream()
    print("Voice assistant ready. Say: 小灯", flush=True)
    pending_noise_levels: list[float] = []
    conversation_deadline: float | None = None
    pending_barge_in: BargeInUtterance | None = None
    sleep_after_barge_in = False

    async def play_pending_announcements(*, restore_light: bool = True) -> None:
        """Pause ordinary listening while the sole TTS consumer is active."""
        nonlocal capture, stream, pending_noise_levels, conversation_deadline
        nonlocal pending_barge_in
        if not app.announcement_pending():
            return
        pause_capture()
        await app.drain_announcements()
        # One simple rule: every completed or interrupted announcement opens a
        # fresh idle window.  Old elapsed time is irrelevant; only 15 seconds
        # after the latest TTS end can park the lamp.
        if conversation_deadline is not None:
            conversation_deadline = time.monotonic() + env_float(
                "CONVERSATION_IDLE_SECONDS", 15.0
            )
        interrupted = app.take_barge_in_utterance()
        if interrupted is not None:
            pending_barge_in = interrupted
            print(
                f"TTS cancelled | listening resumed | trigger={interrupted.trigger}",
                flush=True,
            )
            return
        resume_capture()
        if shared_capture_enabled:
            pending_noise_levels = []
        else:
            pending_noise_levels = await asyncio.to_thread(
                measure_noise, read_mono,
                env_float("VAD_CAPTURE_WARMUP_SECONDS", 0.5),
            )
        stream = spotter.create_stream()
        if restore_light:
            app.light_state("listening" if conversation_active else "wake_required")
        ended_at = app.last_playback_ended_at
        if ended_at is not None:
            print(
                f"AUDIO RESUME | 播放结束到可监听: {time.monotonic() - ended_at:.3f} 秒",
                flush=True,
            )

    async def enter_sleep_waiting(message: str, force_sleep: bool = False) -> None:
        """Park the mechanics, then keep this process listening for the next wake."""
        nonlocal capture, conversation_active, conversation_deadline
        nonlocal conversation_id, conversation_turns
        nonlocal stream, pending_noise_levels
        nonlocal sleep_after_barge_in, pending_barge_in
        pause_capture()
        await app.set_voice_session_active(False)
        if force_sleep or not app.keeps_mode_after_voice_timeout():
            app.light_state("session_end")
            try:
                await app.sleep()
            except Exception as exc:
                app.light_state("error")
                print(f"进入睡眠姿态失败: {exc}", file=sys.stderr, flush=True)
        conversation_active = False
        conversation_deadline = None
        if conversation_id is not None:
            await clear_session(conversation_id)
        conversation_id = None
        conversation_turns = 0
        sleep_after_barge_in = False
        pending_barge_in = None
        pending_noise_levels = []
        stream = spotter.create_stream()
        resume_capture()
        if app.current_mode != "work_light":
            app.light_state("wake_required")
        print(message, flush=True)

    try:
        while True:
            if pending_barge_in is not None and not conversation_active:
                conversation_active = True
                await app.set_voice_session_active(True)
                conversation_id = uuid.uuid4().hex
                conversation_turns = 0
                conversation_deadline = time.monotonic() + env_float(
                    "CONVERSATION_IDLE_SECONDS", 15.0
                )
            if not conversation_active:
                stereo = await asyncio.to_thread(read_stereo)
                if app.announcement_pending():
                    await play_pending_announcements()
                    continue
                samples = select_kws_audio(stereo)
                centered = stereo.mean(axis=1)
                centered = centered - centered.mean()
                rms = float(np.sqrt(np.mean(np.square(centered))))
                if rms < env_float("VAD_START_RMS_THRESHOLD", 0.008):
                    ambient_levels.append(rms)
                stream.accept_waveform(SAMPLE_RATE, samples)
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                if not spotter.get_result(stream):
                    continue
                spotter.reset_stream(stream)
                conversation_active = True
                await app.set_voice_session_active(True)
                conversation_id = uuid.uuid4().hex
                conversation_turns = 0
                preconnect_tts()
                app.light_state("wake_ack")
                if app.sound_configured("wake"):
                    await app.submit_announcement(
                        source="wake",
                        text="",
                        priority=AnnouncementPriority.LOCAL_REPLY,
                        sound_before="wake",
                    )
                    await play_pending_announcements()
                try:
                    await app.prepare_for_voice_session()
                except Exception as exc:
                    app.light_state("error")
                    print(f"待机姿态失败: {exc}", file=sys.stderr, flush=True)
                pending_noise_levels = list(ambient_levels)
                ambient_levels.clear()
                conversation_deadline = time.monotonic() + env_float(
                    "CONVERSATION_IDLE_SECONDS", 15.0
                )
                print("WAKE: xiao_deng; listening...", flush=True)
                app.light_state("listening")

            turn_started = time.perf_counter()
            listen_started = time.perf_counter()
            is_first_turn = conversation_turns == 0
            assert conversation_deadline is not None
            timeout = conversation_deadline - time.monotonic()
            # A detected interruption already owns the next turn.  Never park
            # the mechanics before transcribing it, even if the previous idle
            # deadline elapsed while the assistant was speaking.
            if timeout <= 0 and pending_barge_in is None:
                await enter_sleep_waiting("会话已结束，请说“小灯”重新唤醒")
                continue
            prompt = "Listening..." if is_first_turn else "连续对话中，请直接说话..."
            barge_trigger = None
            if pending_barge_in is not None:
                utterance = pending_barge_in.audio
                barge_trigger = pending_barge_in.trigger
                pending_barge_in = None
                app.local_speech_started()
                print(
                    f"BARGE-IN CAPTURE | 时长: {len(utterance) / SAMPLE_RATE:.2f} 秒",
                    flush=True,
                )
            else:
                try:
                    utterance = await asyncio.to_thread(
                        capture_utterance, read_mono,
                        timeout,
                        prompt,
                        initial_noise_levels=pending_noise_levels,
                        interrupt_before_speech=app.announcement_interrupted,
                        on_speech_start=app.local_speech_started,
                    )
                except CaptureInterrupted:
                    await play_pending_announcements()
                    continue
                pending_noise_levels = []
                listen_seconds = time.perf_counter() - listen_started
                print(f"LISTEN END | 延迟: {listen_seconds:.2f} 秒", flush=True)
            if utterance is None:
                app.local_speech_finished()
                message = ("未检测到问题，继续等待“小灯”唤醒" if is_first_turn
                           else "会话已结束，请说“小灯”重新唤醒")
                await enter_sleep_waiting(message)
                continue
            # Release the normal capture clock before playback. Barge-in audio
            # already came from the duplex AEC session, so capture is None.
            pause_capture()
            valid_text_received = False
            shutdown_requested = False
            reply_handle = None
            try:
                app.light_state("thinking")
                asr_started = time.perf_counter()
                text = await asyncio.wait_for(transcribe(utterance), timeout=45)
                asr_seconds = time.perf_counter() - asr_started
                print(f"ASR: {text}", flush=True)
                print(f"ASR 延迟: {asr_seconds:.2f} 秒", flush=True)
                # A real utterance during the farewell cancels the deferred
                # sleep.  Empty ASR or an ASR failure leaves it armed.
                if sleep_after_barge_in and has_meaningful_text(text):
                    sleep_after_barge_in = False
                control_only = False
                if barge_trigger == "keyword":
                    remaining, removed = strip_stop_prefix(text)
                    if removed and remaining.strip("，,。！？!?、 ") in {
                        "", "一下", "吧", "好了", "好啦",
                    }:
                        control_only = True
                    elif removed:
                        text = remaining
                await app.restore_after_barge_in()
                if control_only:
                    valid_text_received = True
                    print("BARGE-IN CONTROL: 已停止播报，继续监听", flush=True)
                    app.light_state("listening")
                elif has_meaningful_text(text):
                    valid_text_received = True
                    llm_started = time.perf_counter()
                    assert conversation_id is not None
                    async with app._interaction_lock:
                        result = await app.handle_text(text, conversation_id)
                        answer = result.text
                        shutdown_requested = result.status == "shutdown_requested"
                        llm_seconds = time.perf_counter() - llm_started
                        reply_handle = await app.submit_announcement(
                            source="local_reply",
                            text=answer,
                            priority=AnnouncementPriority.LOCAL_REPLY,
                            # The voice loop owns local farewell shutdown so it
                            # can move straight from speaking to session_end.
                            # Remote/scheduled announcements still use the
                            # queue-level sleep_after path.
                            callback_info={},
                            expression=result.expression,
                        )
                    await play_pending_announcements(
                        restore_light=not shutdown_requested
                    )
                    spoken = await reply_handle.wait()
                    if not spoken.success:
                        raise RuntimeError(f"播报失败: {spoken.error}")
                    if spoken.interrupted:
                        print("播报已被用户打断。", flush=True)
                    tts_first_seconds, tts_stream_seconds, playback_seconds = spoken.metrics
                    print(f"LLM: {answer}", flush=True)
                    print(f"LLM 延迟: {llm_seconds:.2f} 秒", flush=True)
                    conversation_turns += 1
                    print(f"TTS 首包延迟: {tts_first_seconds:.2f} 秒", flush=True)
                    print(f"TTS 流传输: {tts_stream_seconds:.2f} 秒", flush=True)
                    print(f"播放耗时: {playback_seconds:.2f} 秒", flush=True)
                    print(
                        f"本轮总耗时: {time.perf_counter() - turn_started:.2f} 秒",
                        flush=True,
                    )
                    if not shutdown_requested:
                        app.light_state("turn_done")
            except Exception as exc:
                app.light_state("error")
                print(f"Voice turn failed: {exc}", file=sys.stderr, flush=True)
            finally:
                app.local_speech_finished()
            if sleep_after_barge_in:
                sleep_after_barge_in = False
                await enter_sleep_waiting(
                    "已进入睡眠，请说“小灯”重新唤醒", force_sleep=True
                )
                continue
            if shutdown_requested:
                if pending_barge_in is not None:
                    # Farewell was interrupted.  Transcribe that one captured
                    # utterance before making the final sleep decision.
                    sleep_after_barge_in = True
                    continue
                await enter_sleep_waiting("已进入睡眠，请说“小灯”重新唤醒", force_sleep=True)
                continue

            if pending_barge_in is not None:
                # Process the captured interruption before any older queued
                # notification or a newly opened ALSA capture.
                continue

            if app.announcement_pending():
                await play_pending_announcements()
            elif capture is None or shared_capture_enabled:
                resume_capture()
                if shared_capture_enabled:
                    pending_noise_levels = []
                else:
                    pending_noise_levels = await asyncio.to_thread(
                        measure_noise, read_mono,
                        env_float("VAD_CAPTURE_WARMUP_SECONDS", 0.5),
                    )
                stream = spotter.create_stream()
            if valid_text_received:
                conversation_deadline = time.monotonic() + env_float(
                    "CONVERSATION_IDLE_SECONDS", 15.0
                )
            elif time.monotonic() >= conversation_deadline:
                await enter_sleep_waiting("会话已结束，请说“小灯”重新唤醒")
                continue
            if conversation_active:
                app.light_state("listening")
                print("连续对话中，请直接说话...", flush=True)
            else:
                app.light_state("wake_required")
                print("会话已结束，请说“小灯”重新唤醒", flush=True)
    finally:
        if capture is not None:
            app.capture_active = False
            if shared_capture_enabled:
                capture.close()
                app.shared_capture = None
            else:
                stop_capture(capture)

class LampApp:
    """Application coordination; all app motion sources enter through this object."""
    def __init__(self, motion=None, lighting=None, sound_player=None, vision=None):
        from .motion.controller import MotionController
        from .motion.config import motion_port
        from .lighting.controller import LightingController
        self.motion = motion or MotionController(
            port=motion_port(),
            lamp_id=os.getenv("MOTION_LAMP_ID", "lamppi"),
        )
        self.lighting = lighting or LightingController()
        self.sound_player = sound_player or SoundPlayer()
        if vision is None:
            from .vision import VisionController
            vision = VisionController()
        self.vision = vision
        self._started = False
        self.voice_session_active = False
        self.current_mode = "normal"
        self.current_motion_task = None
        self.tracking = False
        self._visual_tracking_runner = None
        self.speaking = False
        self._motion_lock = asyncio.Lock()
        self._interaction_lock = asyncio.Lock()
        self._mode_version = 0
        self._mode_runner = None
        self.mechanically_asleep = False
        self.work_pose = "high"
        self.work_tone = "white"
        self.work_brightness = 75
        from .motion.heading import load_heading
        self.base_heading_degrees = load_heading()
        self._apply_base_heading(self.base_heading_degrees)
        self._shutdown_reason: str | None = None
        self._active_agent_turn_id: str | None = None
        self._pending_expression: tuple[str, str] | None = None
        self._agent_turn_text: str | None = None
        import threading
        self._local_speech_active = threading.Event()
        self.barge_in = BargeInController()
        self._barge_in_utterances: deque[BargeInUtterance] = deque()
        self._barge_restore_needed = False
        self.announcements = AnnouncementQueue(self._process_announcement_batch)
        self.audio_health = AudioHealthMonitor()
        self.capture_active = False
        self.shared_capture = None
        self.last_playback_ended_at: float | None = None
        self.voice_task = None
        self.timers = TimerManager(self._on_timer_complete)
        self.alarms = AlarmManager(self._on_alarm_complete)
        self.tools = ToolExecutor(self)
        self.agent_service: PiAgentService | None = None

    async def start(self) -> None:
        await self.announcements.start()
        await self.alarms.start()
        await asyncio.to_thread(self.barge_in.prepare)
        self._started = True
        await self._sync_vision_lifecycle()

    def keeps_mode_after_voice_timeout(self) -> bool:
        return self.current_mode in {"tracking", "work_light"}

    async def prepare_for_voice_session(self) -> None:
        if self.current_mode not in {"tracking", "work_light"}:
            await self.enter_standby()

    async def set_voice_session_active(self, active: bool) -> None:
        self.voice_session_active = bool(active)
        await self._sync_vision_lifecycle()

    def _vision_should_run(self) -> bool:
        return self.voice_session_active or self.current_mode in {
            "tracking", "work_light"
        }

    async def _sync_vision_lifecycle(self) -> None:
        if not self._started:
            return
        if self._vision_should_run():
            self.vision.start()
        else:
            await asyncio.to_thread(self.vision.stop)

    def get_vision_state(self) -> dict:
        state = self.vision.state()
        runner = self._visual_tracking_runner
        state["motion_tracking"] = dict(runner.state) if runner is not None else None
        return state

    async def _on_timer_complete(self, timer: TimerSnapshot) -> None:
        """Turn a hardware-neutral Timer completion into one speech event."""
        has_work = bool(
            timer.callback_info.get("action")
            or str(timer.callback_info.get("agent_task", "")).strip()
        )
        await self.submit_announcement(
            announcement_id=timer.timer_id,
            source="timer",
            text=timer.message.strip() or "计时结束了。",
            priority=AnnouncementPriority.NOTIFICATION,
            mergeable=not has_work,
            callback_info=timer.callback_info,
            sound_before="timer",
        )
        print(f"TIMER COMPLETED: {timer.timer_id}", flush=True)

    async def _on_alarm_complete(self, alarm: AlarmSnapshot) -> None:
        """Turn an Alarm trigger into the same app-level speech event."""
        has_work = bool(
            alarm.callback_info.get("action")
            or str(alarm.callback_info.get("agent_task", "")).strip()
        )
        await self.submit_announcement(
            announcement_id=alarm.alarm_id,
            source="alarm",
            text=alarm.message.strip() or "闹钟时间到了。",
            priority=AnnouncementPriority.NOTIFICATION,
            mergeable=not has_work,
            callback_info=alarm.callback_info,
            sound_before="alarm",
        )
        print(f"ALARM TRIGGERED: {alarm.alarm_id}", flush=True)

    async def submit_announcement(
        self,
        *,
        source: str,
        text: str,
        priority: int,
        announcement_id: str | None = None,
        mergeable: bool = False,
        callback_info=None,
        expression: str | None = None,
        sound_before: str | None = None,
    ):
        return await self.announcements.submit(
            announcement_id=announcement_id or f"{source}-{uuid.uuid4().hex}",
            source=source,
            text=text,
            priority=priority,
            mergeable=mergeable,
            callback_info=callback_info,
            expression=expression,
            sound_before=sound_before,
        )

    def sound_configured(self, cue: str) -> bool:
        return self.sound_player.has_cue(cue)

    def announcement_pending(self) -> bool:
        return self.announcements.has_pending()

    def announcement_interrupted(self) -> bool:
        return (
            not self._local_speech_active.is_set()
            and self.announcements.interruption_requested()
        )

    async def drain_announcements(self) -> None:
        await self.announcements.drain_and_pause()

    def take_barge_in_utterance(self) -> BargeInUtterance | None:
        return self._barge_in_utterances.popleft() if self._barge_in_utterances else None

    def local_speech_started(self) -> None:
        self._local_speech_active.set()

    def local_speech_finished(self) -> None:
        self._local_speech_active.clear()

    async def _process_announcement_batch(
        self, batch: list[Announcement]
    ) -> tuple[str, tuple[float, float, float]]:
        """Resolve and speak one queue batch under the app interaction lock."""
        async with self._interaction_lock:
            if len(batch) > 1:
                message = self._merge_announcement_texts([item.text for item in batch])
                ids = ",".join(item.announcement_id for item in batch)
                print(f"ANNOUNCEMENT MERGED: {ids} | {message}", flush=True)
                cue = "alarm" if any(item.sound_before == "alarm" for item in batch) else next(
                    (item.sound_before for item in batch if item.sound_before), None
                )
                await self._play_sound_now(cue)
                metrics = await self._speak_now(message, None) if message else (0.0, 0.0, 0.0)
                print(f"ANNOUNCEMENT END: {ids}", flush=True)
                return message, metrics

            item = batch[0]
            message = item.text
            expression = item.expression
            sleep_after = bool(item.callback_info.get("sleep_after"))
            action = item.callback_info.get("action")
            agent_task = str(item.callback_info.get("agent_task", "")).strip()
            print(
                f"ANNOUNCEMENT START: {item.announcement_id} | {item.source} | {message}",
                flush=True,
            )
            await self._play_sound_now(item.sound_before)
            if agent_task:
                try:
                    print(f"SCHEDULE AGENT START: {item.announcement_id}", flush=True)
                    result = await self.handle_text(
                        agent_task,
                        f"scheduled-{item.announcement_id}-{uuid.uuid4().hex}",
                    )
                    message = result.text or message
                    expression = result.expression
                    sleep_after = result.status == "shutdown_requested"
                    print(
                        f"SCHEDULE AGENT END: {item.announcement_id} | {result.status}",
                        flush=True,
                    )
                except Exception as exc:
                    message = "定时任务执行失败了。"
                    self.clear_pending_expression()
                    print(
                        f"SCHEDULE AGENT FAILED: {item.announcement_id} | {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            elif isinstance(action, dict):
                tool_name = str(action.get("tool", ""))
                arguments = action.get("arguments", {})
                try:
                    outcome = await self.tools.execute(
                        tool_name, arguments, source=ToolSource.SCHEDULED
                    )
                    if outcome.status == "shutdown_requested":
                        self.consume_shutdown_request()
                        sleep_after = True
                    print(
                        f"SCHEDULE ACTION END: {item.announcement_id} | "
                        f"{tool_name} | {outcome.status}",
                        flush=True,
                    )
                except Exception as exc:
                    message = "定时任务执行失败了。"
                    print(
                        f"SCHEDULE ACTION FAILED: {item.announcement_id} | "
                        f"{tool_name} | {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            metrics = await self._speak_now(message, expression) if message else (0.0, 0.0, 0.0)
            if sleep_after:
                await self.sleep()
            print(f"ANNOUNCEMENT END: {item.announcement_id}", flush=True)
            return message, metrics

    async def _play_sound_now(self, cue: str | None) -> float:
        if not cue:
            return 0.0
        try:
            seconds = await asyncio.to_thread(self.sound_player.play, cue)
        except Exception as exc:
            print(f"提示音 {cue} 播放失败，继续文字播报: {exc}", file=sys.stderr, flush=True)
            return 0.0
        if seconds > 0:
            print(f"SOUND: {cue} | {seconds:.2f} 秒", flush=True)
        return seconds

    @staticmethod
    def _merge_announcement_texts(texts: list[str]) -> str:
        parts = [text.strip().rstrip("。；;，,") for text in texts if text.strip()]
        return "；".join(parts) + ("。" if parts else "")

    def light_state(self, name: str) -> None:
        """Set a semantic status effect without RGB values in app logic."""
        if self.current_mode == "work_light":
            return
        method = getattr(self.lighting, name, None)
        if method is None:
            raise ValueError(f"未知灯光状态: {name}")
        try:
            method()
        except Exception as exc:
            print(f"灯光状态 {name} 失败: {exc}", file=sys.stderr, flush=True)

    def light_interrupted(self) -> None:
        """Confirm a real barge-in without exposing the effect as an Agent Tool."""
        method = getattr(self.lighting, "interrupted", None)
        if method is None:
            return
        try:
            method(work_light=self.current_mode == "work_light")
        except Exception as exc:
            print(f"打断灯效失败: {exc}", file=sys.stderr, flush=True)

    async def _stop_motion(self):
        task = self.current_motion_task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"上一个运动任务失败: {exc}", file=sys.stderr, flush=True)
            finally:
                self.current_motion_task = None
        self.tracking = False

    def _motion_active(self) -> bool:
        task = self.current_motion_task
        return task is not None and not task.done()

    async def restore_after_barge_in(self) -> None:
        """Restore the persistent mode only after interruption capture/ASR."""
        if not self._barge_restore_needed:
            return
        self._barge_restore_needed = False
        async with self._motion_lock:
            if self.current_mode == "work_light":
                await self.motion.work_pose(self.work_pose)
            elif self._mode_runner is not None:
                runner = self._mode_runner
                self.tracking = self.current_mode == "tracking"
                self.current_motion_task = asyncio.create_task(runner())
            else:
                await self.motion.standby()
            self.mechanically_asleep = False

    async def play_motion(self, name, force_startup=False):
        async with self._motion_lock:
            await self._stop_motion()
            version = self._mode_version
            from .motion.config import active_transition_seconds, startup_transition_seconds
            transition = startup_transition_seconds() if force_startup else (
                startup_transition_seconds() if self.mechanically_asleep
                else active_transition_seconds()
            )
            task = asyncio.create_task(self._play_and_restore(name, version, transition))
            self.current_motion_task = task
            self.mechanically_asleep = False
        await task

    def _apply_base_heading(self, logical_degrees):
        from .motion.config import base_yaw_left_sign
        setter = getattr(self.motion, "set_base_yaw_offset_degrees", None)
        if setter is not None:
            setter(float(logical_degrees) * base_yaw_left_sign())

    async def turn_base(self, direction="left", steps=1):
        """Turn in fixed increments and make the result the new motion neutral."""
        from .motion.config import base_yaw_step_degrees

        if direction not in ("left", "right"):
            raise ValueError("direction 必须是 left 或 right")
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise ValueError("steps 必须是正整数")
        delta = base_yaw_step_degrees() * steps * (1 if direction == "left" else -1)
        return await self._move_to_base_heading(self.base_heading_degrees + delta)

    async def set_base_heading(self, position="front"):
        """Move to an absolute logical heading without Agent-side arithmetic."""
        from .motion.config import base_yaw_max_offset_degrees
        limit = base_yaw_max_offset_degrees()
        targets = {"left": limit, "front": 0.0, "right": -limit}
        if position not in targets:
            raise ValueError("position 必须是 left、front 或 right")
        return await self._move_to_base_heading(targets[position])

    async def _move_to_base_heading(self, target_heading):
        from .motion.config import (
            active_transition_seconds, base_yaw_max_offset_degrees,
            startup_transition_seconds,
        )
        from .motion.heading import save_heading

        limit = base_yaw_max_offset_degrees()
        if target_heading < -limit - 1e-6 or target_heading > limit + 1e-6:
            raise ValueError(f"转向会超过安全范围，当前只允许 {-limit:g}°～{limit:g}°")
        if abs(target_heading - self.base_heading_degrees) < 1e-6:
            return self.base_heading_degrees

        async with self._motion_lock:
            self._mode_version += 1
            await self._stop_motion()
            previous_heading = self.base_heading_degrees
            was_asleep = self.mechanically_asleep
            previous_mode = self.current_mode
            runner = self._mode_runner
            self._apply_base_heading(target_heading)
            try:
                transition = (
                    startup_transition_seconds() if was_asleep
                    else active_transition_seconds()
                )
                if previous_mode == "work_light":
                    await self.motion.work_pose(self.work_pose, transition)
                else:
                    await self.motion.standby(transition)
                    if previous_mode == "sleep":
                        self.current_mode = "standby"
                        self._mode_runner = None
                self.base_heading_degrees = target_heading
                self.mechanically_asleep = False
                save_heading(target_heading)
                if runner is not None and previous_mode != "sleep":
                    self.tracking = previous_mode == "tracking"
                    self.current_motion_task = asyncio.create_task(runner())
            except BaseException:
                self._apply_base_heading(previous_heading)
                raise
        return self.base_heading_degrees

    async def reset_base_heading(self):
        return await self.set_base_heading("front")

    async def _play_and_restore(self, name, version, transition_seconds):
        await self.motion.play(name, transition_seconds)
        if version != self._mode_version:
            return
        if self.current_mode == "work_light":
            await self.motion.work_pose(self.work_pose)
        elif self._mode_runner is not None:
            self.tracking = self.current_mode == "tracking"
            runner = self._mode_runner
            async def restore():
                try:
                    await runner()
                finally:
                    self.tracking = False
            self.current_motion_task = asyncio.create_task(restore())
        else:
            await self.motion.standby()

    async def enter_standby(self):
        if self.current_mode == "work_light":
            return
        async with self._motion_lock:
            self._mode_version += 1
            await self._stop_motion()
            self.current_mode = "standby"
            self._mode_runner = None
            await self.motion.standby()
            self.mechanically_asleep = False
        await self._sync_vision_lifecycle()

    async def enter_work_light(self, pose="high", tone="white"):
        if pose not in ("high", "low"):
            raise ValueError("办公姿态必须是 high 或 low")
        if tone not in ("white", "warm"):
            raise ValueError("办公色调必须是 white 或 warm")
        async with self._motion_lock:
            self._mode_version += 1
            await self._stop_motion()
            self.current_mode = "work_light"
            self._mode_runner = None
            self.work_pose = pose
            self.work_tone = tone
            self.work_brightness = 75
            self.mechanically_asleep = False
            await self.motion.work_pose(pose)
            await asyncio.to_thread(
                self.lighting.work_light,
                tone,
                self.work_brightness,
                self._work_light_fade_seconds(),
            )
        await self._sync_vision_lifecycle()

    async def update_work_light(self, pose=None, tone=None, brightness_step=None):
        if self.current_mode != "work_light":
            raise ValueError("当前不在办公照明模式")
        if pose is None and tone is None and brightness_step is None:
            raise ValueError("至少指定一个办公照明参数")
        if pose is not None and pose not in ("high", "low"):
            raise ValueError("办公姿态必须是 high 或 low")
        if tone is not None and tone not in ("white", "warm"):
            raise ValueError("办公色调必须是 white 或 warm")
        if brightness_step is not None and brightness_step not in (-25, 25):
            raise ValueError("brightness_step 必须是 -25 或 25")
        async with self._motion_lock:
            if self.current_mode != "work_light":
                raise ValueError("当前不在办公照明模式")
            new_brightness = self.work_brightness
            if brightness_step is not None:
                new_brightness = max(50, min(100, self.work_brightness + brightness_step))
            new_pose = pose or self.work_pose
            new_tone = tone or self.work_tone
            tone_changed = new_tone != self.work_tone
            if new_pose != self.work_pose:
                self.work_pose = new_pose
                await self.motion.work_pose(new_pose)
            self.work_tone = new_tone
            self.work_brightness = new_brightness
            if tone_changed or brightness_step is not None:
                await asyncio.to_thread(
                    self.lighting.work_light,
                    self.work_tone,
                    self.work_brightness,
                    self._work_light_fade_seconds(),
                )

    async def exit_work_light(self):
        if self.current_mode != "work_light":
            return False
        async with self._motion_lock:
            if self.current_mode != "work_light":
                return False
            self._mode_version += 1
            await self._stop_motion()
            await asyncio.to_thread(self.lighting.fade_off, self._work_light_fade_seconds())
            self.current_mode = "standby"
            self._mode_runner = None
            self.work_pose = "high"
            self.work_tone = "white"
            self.work_brightness = 75
            await self.motion.standby()
            self.mechanically_asleep = False
        await self._sync_vision_lifecycle()
        return True

    @staticmethod
    def _work_light_fade_seconds():
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parents[1] / "lighting.conf", override=True)
        return max(0.0, float(os.getenv("WORK_LIGHT_FADE_SECONDS", "0.8")))

    async def set_mode(self, mode, runner=None):
        # Future reading/tracking implementations supply their existing coroutine.
        if mode == "sleep":
            await self.sleep()
            return
        if mode == "normal" and runner is None:
            await self.enter_standby()
            return
        if runner is None:
            raise NotImplementedError(f"{mode} 模式尚未接入")
        async with self._motion_lock:
            self._mode_version += 1
            await self._stop_motion()
            self.current_mode = mode
            self._mode_runner = runner
            self.tracking = mode == "tracking"
            async def run():
                try:
                    if runner is not None:
                        await runner()
                finally:
                    self.tracking = False
            self.current_motion_task = asyncio.create_task(run())
            task = self.current_motion_task
        # Tracking is long-lived; mode selection should return once it is started.
        # Persistent modes return once their runner has started.
        await self._sync_vision_lifecycle()

    async def sleep(self):
        async with self._motion_lock:
            if self.mechanically_asleep:
                return
            self.voice_session_active = False
            self._mode_version += 1
            await self._stop_motion()
            if self.current_mode == "work_light":
                await asyncio.to_thread(self.lighting.fade_off, self._work_light_fade_seconds())
                self.work_pose = "high"
                self.work_tone = "white"
                self.work_brightness = 75
            self.current_mode = "sleep"
            self._mode_runner = None
            await self.motion.sleep()
            self.mechanically_asleep = True
        await self._sync_vision_lifecycle()

    async def stop_tracking(self):
        if self.current_mode == "tracking":
            await self.set_mode("normal")

    async def start_face_tracking(self):
        """Enter the persistent face-tracking mode through app arbitration."""
        from .motion.visual_tracking import VisualTrackingRunner

        runner = VisualTrackingRunner(
            self.motion, self.vision.latest_tracking_target
        )
        self._visual_tracking_runner = runner
        await self.set_mode("tracking", runner.run)

    def set_light(self, color=(255, 255, 255), brightness=255):
        self.lighting.set_light(color, brightness)

    def request_shutdown(self, reason: str) -> None:
        self._shutdown_reason = reason

    def queue_expression(self, name: str) -> tuple[bool, str]:
        from .motion.config import auto_expression_enabled, auto_expression_in_work_light
        if not auto_expression_enabled():
            return False, "自动情绪动作已关闭"
        if self.current_mode == "work_light" and not auto_expression_in_work_light():
            return False, "办公照明模式不执行自动情绪动作"
        turn_id = self._active_agent_turn_id
        if turn_id is None:
            return False, "当前没有可绑定的 Agent 播报轮次"
        if self._pending_expression is not None:
            return False, "本轮已经选择了一个情绪动作"
        self._pending_expression = (turn_id, name)
        print(f"EXPRESSION QUEUED: {name}", flush=True)
        return True, "情绪动作已加入本轮播报"

    def should_defer_agent_motion(self, name: str) -> bool:
        """Convert Agent-selected emotions to reply-synchronous motion.

        A direct motion request remains immediate. This only arbitrates timing;
        it never creates an action that the Agent did not request.
        """
        text = self._agent_turn_text
        if text is None:
            return False
        motion_terms = {
            "nod": ("点头",),
            "headshake": ("摇头",),
            "happy_wiggle": ("开心动作", "开心地动", "扭一扭", "扭一下"),
            "excited": ("兴奋动作", "激动动作"),
            "sad": ("伤心动作", "难过动作"),
            "shy": ("害羞动作",),
            "shock": ("震惊动作", "惊讶动作"),
            "curious": ("好奇动作", "疑惑动作", "困惑动作"),
        }
        explicitly_named = name in text.lower() or any(
            term in text for term in motion_terms.get(name, ())
        )
        command_word = any(
            word in text for word in ("做", "来一个", "来个", "表演", "动一下", "执行")
        )
        return not (explicitly_named and command_word)

    def clear_pending_expression(self, turn_id: str | None = None) -> None:
        if turn_id is None or (
            self._pending_expression is not None
            and self._pending_expression[0] == turn_id
        ):
            self._pending_expression = None

    def take_pending_expression(self, turn_id: str) -> str | None:
        pending = self._pending_expression
        if pending is None or pending[0] != turn_id:
            return None
        self._pending_expression = None
        return pending[1]

    async def _speak_now(
        self, text: str, expression: str | None
    ) -> tuple[float, float, float]:
        """Sole app-level TTS path, called only by AnnouncementQueue."""
        self.speaking = True
        self.last_playback_ended_at = None
        self.light_state("speaking")
        motion_task = None
        session = None
        control = PlaybackControl()
        session = self.barge_in.create_session(
            motion_active=self._motion_active,
            cancel_playback=control.cancel,
            shared_capture=self.shared_capture,
        )
        control.on_playback_end = lambda: setattr(
            self, "last_playback_ended_at", time.monotonic()
        )
        if session is not None:
            def start_aec(sample_rate: int, channels: int) -> None:
                nonlocal session
                try:
                    session.start(sample_rate, channels)
                except Exception as exc:
                    print(f"AEC 不可用，回退播完再听: {exc}", file=sys.stderr, flush=True)
                    session.close()
                    session = None

            def feed_aec(pcm: bytes, sample_rate: int, channels: int) -> None:
                if session is not None:
                    session.feed_pcm(pcm, sample_rate, channels)

            control.on_format = start_aec
            control.on_pcm = feed_aec
        try:
            if expression is None:
                if session is None:
                    return await asyncio.to_thread(speak, text)
                return await asyncio.to_thread(speak, text, None, control)
            loop = asyncio.get_running_loop()
            playback_started = asyncio.Event()
            callback = lambda: loop.call_soon_threadsafe(playback_started.set)
            args = (text, callback) if session is None else (text, callback, control)
            tts_task = asyncio.create_task(asyncio.to_thread(speak, *args))
            started_task = asyncio.create_task(playback_started.wait())
            done, _ = await asyncio.wait(
                {tts_task, started_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if playback_started.is_set():
                print(f"EXPRESSION START: {expression}", flush=True)
                motion_task = asyncio.create_task(self.play_motion(expression))
            else:
                started_task.cancel()
            metrics = await tts_task
            if motion_task is not None:
                try:
                    await motion_task
                    print(f"EXPRESSION END: {expression}", flush=True)
                except Exception as exc:
                    print(f"情绪动作 {expression} 失败: {exc}", file=sys.stderr, flush=True)
            return metrics
        except SpeechInterrupted:
            print("TTS cancelled", flush=True)
            self.light_interrupted()
            had_motion = self._motion_active()
            if motion_task is not None and not motion_task.done():
                motion_task.cancel()
                await asyncio.gather(motion_task, return_exceptions=True)
            if had_motion:
                self._barge_restore_needed = True
            await self._stop_motion()
            result = None
            if session is not None:
                result = await asyncio.to_thread(
                    session.wait_result, env_float("VAD_MAX_SECONDS", 15.0) + 2.0
                )
            if result is not None and len(result.audio):
                self._barge_in_utterances.append(result)
            raise AnnouncementInterrupted
        finally:
            cleanup_started = time.monotonic()
            if session is not None:
                await asyncio.to_thread(session.close)
            self.speaking = False
            if self.last_playback_ended_at is not None:
                print(
                    "AUDIO CLEANUP | "
                    f"播放结束到AEC关闭: {time.monotonic() - self.last_playback_ended_at:.3f} 秒 "
                    f"| AEC关闭耗时: {time.monotonic() - cleanup_started:.3f} 秒",
                    flush=True,
                )

    def consume_shutdown_request(self) -> str | None:
        reason = self._shutdown_reason
        self._shutdown_reason = None
        return reason

    def get_robot_state(self) -> dict:
        if self.base_heading_degrees > 1e-6:
            base_heading_position = "left"
        elif self.base_heading_degrees < -1e-6:
            base_heading_position = "right"
        else:
            base_heading_position = "front"
        vision_state = self.get_vision_state()
        return {
            "current_mode": self.current_mode,
            "motion_active": bool(
                self.current_motion_task is not None
                and not self.current_motion_task.done()
            ),
            "tracking": self.tracking,
            "voice_session_active": self.voice_session_active,
            "vision_running": vision_state["running"],
            "vision_status": vision_state["status"],
            "vision_target_visible": bool(
                vision_state.get("tracking_target", {}).get("visible")
                if vision_state.get("tracking_target") else False
            ),
            "vision_target_id": (
                vision_state["tracking_target"]["track_id"]
                if vision_state.get("tracking_target") else None
            ),
            "vision_target_age_ms": (
                vision_state["tracking_target"]["age_ms"]
                if vision_state.get("tracking_target") else None
            ),
            "speaking": self.speaking,
            "mechanically_asleep": self.mechanically_asleep,
            "base_heading_degrees": self.base_heading_degrees,
            "base_heading_position": base_heading_position,
            "work_light": self.current_mode == "work_light",
            "work_pose": self.work_pose if self.current_mode == "work_light" else None,
            "work_tone": self.work_tone if self.current_mode == "work_light" else None,
            "work_brightness": self.work_brightness if self.current_mode == "work_light" else None,
        }

    async def handle_text(self, text: str, session_id: str) -> ActionResult:
        # A local exact command owns no Agent reply turn and must never consume
        # an expression left by a stale or bypassed request.
        self.clear_pending_expression()
        command = match_local_command(text)
        if command in ("nod", "headshake"):
            await self.tools.execute(
                "play_motion", {"name": command}, source=ToolSource.LOCAL_VOICE
            )
            return ActionResult("点完了。" if command == "nod" else "摇完了。")
        if command == "sleep":
            outcome = await self.tools.execute(
                "sleep", {"reason": "user_request"},
                source=ToolSource.LOCAL_VOICE,
            )
            self.consume_shutdown_request()
            return ActionResult("歇一会儿，有事叫我。", outcome.status)
        if command == "stop_tracking":
            outcome = await self.tools.execute(
                "stop_tracking", source=ToolSource.LOCAL_VOICE
            )
            return ActionResult(outcome.message + "。")
        if command == "tracking":
            outcome = await self.tools.execute(
                "start_face_tracking", source=ToolSource.LOCAL_VOICE
            )
            return ActionResult(outcome.message + "。")
        if command == "reading":
            return ActionResult("这个功能还没接好。")
        turn_id = uuid.uuid4().hex
        self._active_agent_turn_id = turn_id
        self._agent_turn_text = text
        try:
            try:
                answer = await ask_agent(text, session_id)
            except AgentConnectionError:
                if self.agent_service is None:
                    raise
                await self.agent_service.ensure_ready()
                answer = await ask_agent(text, session_id)
            expression = self.take_pending_expression(turn_id)
        except (AgentError, RuntimeError) as exc:
            self.clear_pending_expression(turn_id)
            print(str(exc), file=sys.stderr, flush=True)
            return ActionResult("脑子暂时连不上，你过会儿再试。", "failed")
        finally:
            self.clear_pending_expression(turn_id)
            if self._active_agent_turn_id == turn_id:
                self._active_agent_turn_id = None
            self._agent_turn_text = None
        shutdown_reason = self.consume_shutdown_request()
        if shutdown_reason:
            return ActionResult(
                answer or "回头见。", "shutdown_requested", expression
            )
        return ActionResult(
            answer or "刚才没听清，你再说一遍？", expression=expression
        )

    async def process_remote_text(self, text: str, session_id: str) -> RemoteActionResult:
        """Run remote text through the same Agent, Tools, TTS and sleep path."""
        async with self._interaction_lock:
            if self.mechanically_asleep:
                await self.enter_standby()
            self.light_state("thinking")
            result = await self.handle_text(text, session_id)
            handle = None
            if result.text:
                handle = await self.submit_announcement(
                    source="remote_reply",
                    text=result.text,
                    priority=AnnouncementPriority.REMOTE_REPLY,
                    callback_info={
                        "sleep_after": result.status == "shutdown_requested"
                    },
                    expression=result.expression,
                )
        spoken = False
        if handle is not None:
            announcement = await handle.wait()
            spoken = announcement.success and not announcement.interrupted
        if result.status == "shutdown_requested":
            self.light_state("wake_required")
        else:
            self.light_state("turn_done")
        return RemoteActionResult(
            result.text, result.status, result.expression, spoken
        )

    async def close(self):
        self._started = False
        try:
            await asyncio.to_thread(self.vision.stop)
        finally:
            try:
                await self.timers.close()
                await self.alarms.close()
                await self.announcements.close()
                if self.motion.robot is not None:
                    await self.sleep()
            finally:
                close_tts()
                self.motion.close()
                self.lighting.close()


def match_local_command(text):
    normalized = text.strip().rstrip("。！？!?，,.、").strip()
    return {
        "点头": "nod", "点个头": "nod", "摇头": "headshake",
        "看着我": "tracking", "别跟了": "stop_tracking",
        "阅读模式": "reading", "睡眠": "sleep",
    }.get(normalized)




async def run():
    load_voice_config()
    app = None
    agent_service = None
    control = None
    remote = None
    voice_task = None
    signal_task = None
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(signum, stop_requested.set)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        try:
            location, refreshed = await asyncio.to_thread(resolve_location)
            print(
                ("公网 IP 定位完成" if refreshed else "公网 IP 定位失败，复用历史配置")
                + f": {location.city} | 时区: {location.timezone}",
                flush=True,
            )
        except LocationError as exc:
            print(f"公网 IP 定位不可用: {exc}", file=sys.stderr, flush=True)

        app = LampApp()
        await app.start()
        control = ControlServer(app.tools)
        remote = RemoteTextServer(app)
        await control.start()
        await remote.start()
        if os.getenv("AGENT_BACKEND", "openclaw").strip().lower() == "pi":
            agent_service = PiAgentService()
            await agent_service.ensure_ready()
            app.agent_service = agent_service
        app.light_state("wake_required")

        voice_task = asyncio.create_task(run_voice(app), name="lelamp-voice")
        app.voice_task = voice_task
        signal_task = asyncio.create_task(stop_requested.wait(), name="lelamp-stop-signal")
        done, _ = await asyncio.wait(
            (voice_task, signal_task), return_when=asyncio.FIRST_COMPLETED
        )
        if voice_task in done:
            await voice_task
    finally:
        for task in (voice_task, signal_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = tuple(task for task in (voice_task, signal_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            try:
                if remote is not None:
                    await remote.close()
            finally:
                try:
                    if control is not None:
                        await control.close()
                finally:
                    try:
                        if app is not None:
                            await app.close()
                    finally:
                        if agent_service is not None:
                            await agent_service.close()
        finally:
            for signum in installed_signals:
                loop.remove_signal_handler(signum)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
