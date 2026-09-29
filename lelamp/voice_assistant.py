"""Compatibility entrypoint; implementation lives in app, voice and agent."""
from .voice.config import SAMPLE_RATE, BLOCK_SAMPLES, env_float, voice_debug, has_meaningful_text, load_voice_config
from .voice.audio import start_capture, read_capture_block, measure_noise, stop_capture, write_mono_wav, scale_pcm_s16le
from .voice.kws import make_spotter
from .voice.vad import make_vad, capture_utterance
from .voice.asr import transcribe
from .voice.tts import speak
from .agent.qwen import ask_llm, load_agent_prompt
from .app import main

if __name__ == "__main__":
    main()
