# Edge TTS 连接诊断

运行目录为 `lelamp_runtime`，需要设备现有 Edge TTS 依赖和网络。使用 ALSA `null` 输出，不发声、不启动 AEC、不操作硬件，不修改用户数据。

```bash
PYTHONPATH=. uv run --no-sync python benchmarks/tts/edge_idle_probe.py --idle 0 15 45 90
PYTHONPATH=. uv run --no-sync python benchmarks/tts/edge_idle_probe.py --retry --idle 20 30 35 45 --output runtime_state/edge_retry_results.json
```

默认每项最多等12秒，这只是诊断程序的上限，不改变正式TTS行为。每个闲置短句后用新连接长句作对照。`--retry` 包含生产重试流程，默认只测单连接。输出记录首包、解码、播放器写入、turn.end及失败阶段；null设备无实际播放耗时，不能用于估计扬声器延迟。

正式Edge路径增加 `EDGE TRACE` 日志：连接年龄、请求发送、首消息/MP3/PCM、首次写播放器、结束及清理。等待期间每5秒打印字节计数，日志不含用户文本或密钥；设 `EDGE_TTS_DIAGNOSTICS=0` 可关闭。只加观测，不改超时、连接重用或AEC。

修复配置（voice.conf）：`EDGE_TTS_PRECONNECT_MAX_AGE_SECONDS=20`、`EDGE_TTS_FIRST_AUDIO_TIMEOUT_SECONDS=5`，已有 `EDGE_TTS_RECEIVE_TIMEOUT_SECONDS=30` 现在用于真实WebSocket收流。过期只影响闲置连接；首包/后续音频计时不被元数据刷新。部分PCM已经提交播放器后，失败不会从头重试或回退远程；主动打断仍为SpeechInterrupted。
