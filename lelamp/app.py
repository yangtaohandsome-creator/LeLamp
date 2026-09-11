import asyncio
import os
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
from .voice.kws import make_spotter
from .voice.vad import capture_utterance
from .voice.asr import transcribe
from .voice.tts import speak
from .agent.openclaw import OpenClawError, ask_agent
from .control import ControlServer
from .remote_text import RemoteTextServer
from .tools import ToolExecutor


@dataclass(frozen=True)
class ActionResult:
    """A high-level app result; hardware modules never decide app shutdown."""

    text: str
    status: str = "completed"


@dataclass(frozen=True)
class RemoteActionResult(ActionResult):
    spoken: bool = False

async def run_voice(app) -> None:
    load_voice_config()
    model_dir = Path(os.getenv("KWS_MODEL_DIR", "kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"))
    keywords_file = Path(os.getenv("KWS_KEYWORDS_FILE", str(model_dir / "keywords_xiao_deng.txt")))
    spotter = make_spotter(model_dir, keywords_file)
    capture = start_capture()
    conversation_active = False
    conversation_id: str | None = None
    conversation_turns = 0
    ambient_levels: deque[float] = deque(maxlen=50)
    startup_levels = await asyncio.to_thread(
        measure_noise, lambda: read_capture_block(capture),
        env_float("VAD_CAPTURE_WARMUP_SECONDS", 0.8),
    )
    ambient_levels.extend(startup_levels)
    # Never feed the ALSA startup transient into the first decoder stream.
    stream = spotter.create_stream()
    print("Voice assistant ready. Say: 小灯", flush=True)
    pending_noise_levels: list[float] = []
    conversation_deadline: float | None = None

    async def enter_sleep_waiting(message: str, force_sleep: bool = False) -> None:
        """Park the mechanics, then keep this process listening for the next wake."""
        nonlocal capture, conversation_active, conversation_deadline
        nonlocal conversation_id, conversation_turns
        nonlocal stream, pending_noise_levels
        if capture is not None:
            stop_capture(capture)
            capture = None
        # WORK_LIGHT is a persistent user mode: a voice timeout only resets
        # the conversation and keeps the desk illumination running.
        if force_sleep or app.current_mode != "work_light":
            app.light_state("session_end")
            try:
                await app.sleep()
            except Exception as exc:
                app.light_state("error")
                print(f"进入睡眠姿态失败: {exc}", file=sys.stderr, flush=True)
        conversation_active = False
        conversation_deadline = None
        conversation_id = None
        conversation_turns = 0
        pending_noise_levels = []
        stream = spotter.create_stream()
        capture = start_capture()
        if app.current_mode != "work_light":
            app.light_state("wake_required")
        print(message, flush=True)

    try:
        while True:
            if not conversation_active:
                stereo = await asyncio.to_thread(read_capture_stereo_block, capture)
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
                conversation_id = uuid.uuid4().hex
                conversation_turns = 0
                app.light_state("wake_ack")
                try:
                    await app.enter_standby()
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
            if timeout <= 0:
                await enter_sleep_waiting("会话已结束，请说“小灯”重新唤醒")
                continue
            prompt = "Listening..." if is_first_turn else "连续对话中，请直接说话..."
            utterance = await asyncio.to_thread(
                capture_utterance, lambda: read_capture_block(capture),
                timeout,
                prompt,
                initial_noise_levels=pending_noise_levels,
            )
            pending_noise_levels = []
            listen_seconds = time.perf_counter() - listen_started
            print(f"LISTEN END | 延迟: {listen_seconds:.2f} 秒", flush=True)
            if utterance is None:
                message = ("未检测到问题，继续等待“小灯”唤醒" if is_first_turn
                           else "会话已结束，请说“小灯”重新唤醒")
                await enter_sleep_waiting(message)
                continue
            # Release the capture clock before playback and discard buffered audio.
            stop_capture(capture)
            capture = None
            valid_text_received = False
            shutdown_requested = False
            try:
                app.light_state("thinking")
                asr_started = time.perf_counter()
                text = await asyncio.wait_for(transcribe(utterance), timeout=45)
                asr_seconds = time.perf_counter() - asr_started
                print(f"ASR: {text}", flush=True)
                print(f"ASR 延迟: {asr_seconds:.2f} 秒", flush=True)
                if has_meaningful_text(text):
                    valid_text_received = True
                    llm_started = time.perf_counter()
                    assert conversation_id is not None
                    async with app._interaction_lock:
                        result = await app.handle_text(text, conversation_id)
                        answer = result.text
                        shutdown_requested = result.status == "shutdown_requested"
                        llm_seconds = time.perf_counter() - llm_started
                        tts_first_seconds, tts_stream_seconds, playback_seconds = await app.speak_response(answer)
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
                    app.light_state("turn_done")
            except Exception as exc:
                app.light_state("error")
                print(f"Voice turn failed: {exc}", file=sys.stderr, flush=True)
            if shutdown_requested:
                await enter_sleep_waiting("已进入睡眠，请说“小灯”重新唤醒", force_sleep=True)
                continue

            capture = start_capture()
            pending_noise_levels = await asyncio.to_thread(
                measure_noise, lambda: read_capture_block(capture),
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
            stop_capture(capture)

class LampApp:
    """Application coordination; all app motion sources enter through this object."""
    def __init__(self, motion=None, lighting=None):
        from .motion.controller import MotionController
        from .lighting.controller import LightingController
        self.motion = motion or MotionController(
            port=os.getenv("MOTION_PORT", "/dev/ttyACM0"),
            lamp_id=os.getenv("MOTION_LAMP_ID", "lamppi"),
        )
        self.lighting = lighting or LightingController()
        self.current_mode = "normal"
        self.current_motion_task = None
        self.tracking = False
        self.speaking = False
        self._motion_lock = asyncio.Lock()
        self._interaction_lock = asyncio.Lock()
        self._mode_version = 0
        self._mode_runner = None
        self.mechanically_asleep = False
        self.work_pose = "high"
        self.work_tone = "white"
        self.work_brightness = 75
        self._shutdown_reason: str | None = None
        self._pending_expression: str | None = None
        self._agent_turn_text: str | None = None
        self.tools = ToolExecutor(self)

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

    async def sleep(self):
        async with self._motion_lock:
            if self.mechanically_asleep:
                return
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

    async def stop_tracking(self):
        if self.current_mode == "tracking":
            await self.set_mode("normal")

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
        if self._pending_expression is not None:
            return False, "本轮已经选择了一个情绪动作"
        self._pending_expression = name
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

    def clear_pending_expression(self) -> None:
        self._pending_expression = None

    async def speak_response(self, text: str) -> tuple[float, float, float]:
        """Start a queued expression exactly when TTS playback starts."""
        expression = self._pending_expression
        self._pending_expression = None
        self.speaking = True
        self.light_state("speaking")
        motion_task = None
        try:
            if expression is None:
                return await asyncio.to_thread(speak, text)
            loop = asyncio.get_running_loop()
            playback_started = asyncio.Event()
            tts_task = asyncio.create_task(asyncio.to_thread(
                speak, text, lambda: loop.call_soon_threadsafe(playback_started.set)
            ))
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
        finally:
            self.speaking = False

    def consume_shutdown_request(self) -> str | None:
        reason = self._shutdown_reason
        self._shutdown_reason = None
        return reason

    def get_robot_state(self) -> dict:
        return {
            "current_mode": self.current_mode,
            "motion_active": bool(
                self.current_motion_task is not None
                and not self.current_motion_task.done()
            ),
            "tracking": self.tracking,
            "speaking": self.speaking,
            "mechanically_asleep": self.mechanically_asleep,
            "work_light": self.current_mode == "work_light",
            "work_pose": self.work_pose if self.current_mode == "work_light" else None,
            "work_tone": self.work_tone if self.current_mode == "work_light" else None,
            "work_brightness": self.work_brightness if self.current_mode == "work_light" else None,
        }

    async def handle_text(self, text: str, session_id: str) -> ActionResult:
        command = match_local_command(text)
        if command in ("nod", "headshake"):
            await self.tools.execute("play_motion", {"name": command})
            return ActionResult("点完了。" if command == "nod" else "摇完了。")
        if command == "sleep":
            outcome = await self.tools.execute("sleep", {"reason": "user_request"})
            self.consume_shutdown_request()
            return ActionResult("歇一会儿，有事叫我。", outcome.status)
        if command == "stop_tracking":
            outcome = await self.tools.execute("stop_tracking")
            return ActionResult(outcome.message + "。")
        if command in ("tracking", "reading"):
            return ActionResult("这个功能还没接好。")
        self.clear_pending_expression()
        self._agent_turn_text = text
        try:
            answer = await ask_agent(text, session_id)
        except OpenClawError as exc:
            self.clear_pending_expression()
            print(str(exc), file=sys.stderr, flush=True)
            return ActionResult("脑子暂时连不上，你过会儿再试。", "failed")
        finally:
            self._agent_turn_text = None
        shutdown_reason = self.consume_shutdown_request()
        if shutdown_reason:
            return ActionResult(answer or "回头见。", "shutdown_requested")
        return ActionResult(answer or "刚才没听清，你再说一遍？")

    async def process_remote_text(self, text: str, session_id: str) -> RemoteActionResult:
        """Run remote text through the same Agent, Tools, TTS and sleep path."""
        async with self._interaction_lock:
            if self.mechanically_asleep:
                await self.enter_standby()
            self.light_state("thinking")
            result = await self.handle_text(text, session_id)
            spoken = False
            if result.text:
                await self.speak_response(result.text)
                spoken = True
            if result.status == "shutdown_requested":
                await self.sleep()
                self.light_state("wake_required")
            else:
                self.light_state("turn_done")
            return RemoteActionResult(result.text, result.status, spoken)

    async def close(self):
        try:
            if self.motion.robot is not None:
                await self.sleep()
        finally:
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
    app = LampApp()
    control = ControlServer(app.tools)
    remote = RemoteTextServer(app)
    await control.start()
    await remote.start()
    app.light_state("wake_required")
    try:
        await run_voice(app)
    finally:
        try:
            await remote.close()
        finally:
            try:
                await control.close()
            finally:
                await app.close()


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
