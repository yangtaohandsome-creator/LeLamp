# 当前架构与迁移说明

最终设计以项目上层的 archetecture.md 为准。runtime 已按一个 app 总协调器和五个功能模块整理，不引入网关、事件总线或独立仲裁框架。

| 位置 | 已有实现 |
|---|---|
| lelamp/app.py | 语音会话流程、OpenClaw 调用、运动任务交接、WORK_LIGHT 与持续模式恢复 |
| lelamp/agent/qwen.py | 原云端 Qwen 调用与人格文件读取 |
| lelamp/voice/ | config、audio、kws、vad、asr、tts，保留已有算法与参数 |
| lelamp/motion/ | 配置、可取消播放、睡眠、录制 |
| lelamp/lighting/ | RGB 驱动、语音状态灯效和持续 WORK_LIGHT 照明 |
| lelamp/vision/ | 目录与职责说明，实际视觉功能尚未实现 |
| lelamp/service/ | 旧服务路径的兼容适配；运动服务转入 app，共用 Motion |
| lelamp/follower、leader | 继续复用的舵机驱动 |
| tests/ | 不依赖真实硬件的迁移回归测试 |

## 兼容与配置

- 新入口是 `uv run --no-sync -m lelamp.app`。
- 原 voice_assistant、record、replay、sleep 命令继续使用；校准、居中、诊断入口保留。
- voice.conf、motion.conf、.env 和根目录人格文件仍按原位置读取。用户录制、校准、模型不迁移、不覆盖。
- 睡眠姿态仅在 motion.conf 中维护；播放与机械睡眠分开。普通临时动作回待机或恢复原持续模式，不自行睡眠。
- 原 AnimationService 构造参数保留，缓动时间统一由 motion.conf 控制。
- sleep 是机械休眠的统一出口：会话超时、明确睡眠指令、独立 replay 正常完成和应用正常退出才会调用。睡眠后程序与 KWS 继续运行，再次唤醒会平滑进入待机姿态。
- 运动任务失败或取消后不自动立即释放扭矩；正常睡眠才释放。关闭语音应用会取消运动并尝试睡眠。
- WORK_LIGHT 由 app 持有姿态、色调和亮度状态；办公模式中的语音状态灯效被屏蔽，语音超时不收灯。所有办公灯调整通过 OpenClaw 高层 Tool 进入 app。
- Agent 自主情绪通过 `queue_expression` 每轮最多登记一个高层动作；app 在 TTS 第一块音频开始播放时执行动作，结束后恢复原持续模式。用户明确要求的动作仍走即时 `play_motion`。办公照明默认拒绝自主情绪动作。
- 新应用与兼容播放服务使用简单串口占用锁；原维护工具仍应单独运行。

## 后续能力边界

app 的 set_mode 接收模式及其执行协程，临时动作结束后恢复该协程。tracking、reading 的交接与恢复已用模拟任务测试，实际摄像头跟踪和阅读姿态尚未实现。

OpenClaw 只有模块位置和说明，没有创建虚假的接入实现。当前 Qwen 保持文本响应；高层动作可由本地明确指令触发。未来 Agent 工具同样调用 app，不能绕过它写舵机。

## 验证

```bash
uv run --no-sync python -m unittest discover -s tests -v
```

模拟检查覆盖动作独占、取消、模式恢复、睡眠保持顺序、整句匹配、采音转换、短句与静音。模拟通过不等于真实舵机、麦克风和云端服务已经现场验证。
