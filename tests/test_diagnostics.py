import asyncio
from types import SimpleNamespace

import numpy as np

from lelamp import diagnostics


def test_audio_monitor_reports_real_channel_and_silence(monkeypatch):
    monitor = diagnostics.AudioHealthMonitor()
    for _ in range(8):
        monitor.observe(np.stack((np.zeros(1600), np.full(1600, 0.05)), axis=1))
    monkeypatch.setenv("KWS_AUDIO_CHANNEL", "1")
    # A constant DC level is not speech; both channels must be centered.
    assert diagnostics._audio_check(monitor.snapshot(), capturing=True)["status"] == "failed"
    x = np.arange(1600)
    for _ in range(8):
        monitor.observe(np.stack((np.zeros(1600), np.sin(x / 10) * 0.01), axis=1))
    assert diagnostics._audio_check(monitor.snapshot(), capturing=True)["status"] == "warning"
    monkeypatch.setenv("KWS_AUDIO_CHANNEL", "0")
    assert diagnostics._audio_check(monitor.snapshot(), capturing=True)["status"] == "failed"
    assert diagnostics._audio_check(monitor.snapshot(), capturing=False)["status"] == "unknown"


def test_stale_capture_and_report_status():
    stale = {"age_seconds": 11.0, "blocks": 30, "rms": [0.004, 0.004]}
    assert diagnostics._audio_check(stale, capturing=True)["status"] == "failed"
    assert diagnostics._report([diagnostics._check("app", "failed", "down")])["overall"] == "failed"


def test_files_check_reports_missing_kws_recording_and_asr(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "ROOT", tmp_path)
    monkeypatch.setenv("ASR_WS_URL", "ws://127.0.0.1:1")
    monkeypatch.setenv("TTS_BACKEND", "remote")
    monkeypatch.setenv("TTS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MOTION_PORT", str(tmp_path / "missing-port"))
    monkeypatch.setenv("HF_LEROBOT_CALIBRATION", str(tmp_path / "missing-calibration"))
    checks = {item["id"]: item for item in diagnostics._files_and_config()}
    for name in ("kws_files", "recordings", "servo_port", "calibration", "asr", "tts"):
        assert checks[name]["status"] == "failed"


def test_live_check_uses_existing_capture_without_opening_device(monkeypatch):
    async def agent():
        return diagnostics._check("agent", "failed", "service down")

    monkeypatch.setattr(diagnostics, "_agent_check", agent)
    monkeypatch.setattr(diagnostics, "_files_and_config", lambda: [])
    monitor = diagnostics.AudioHealthMonitor()
    for _ in range(10):
        monitor.observe(np.random.default_rng(1).normal(0, 0.004, (1600, 2)))
    report = asyncio.run(_run_live_with_fake_app(monitor))
    assert report["overall"] == "failed"
    assert next(c for c in report["checks"] if c["id"] == "microphone")["status"] == "ok"
    assert report["robot_state"]["mechanically_asleep"] is True


async def _run_live_with_fake_app(monitor):
    active = asyncio.create_task(asyncio.sleep(10))
    app = SimpleNamespace(
        voice_task=active, capture_active=True, audio_health=monitor,
        timers=SimpleNamespace(_closed=False),
        alarms=SimpleNamespace(_started=True, _closed=False),
        announcements=SimpleNamespace(_worker=active),
        lighting=SimpleNamespace(_unavailable=False, _rgb=object()),
        get_robot_state=lambda: {"mechanically_asleep": True},
    )
    try:
        return await diagnostics.run_live(app)
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
