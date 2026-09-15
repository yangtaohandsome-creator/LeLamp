# 当前架构与迁移说明

最终设计以项目上层的 archetecture.md 为准。runtime 已按一个 app 总协调器和五个功能模块整理，不引入网关、事件总线或独立仲裁框架。

| 位置 | 已有实现 |
|---|---|
| lelamp/app.py | 语音会话流程、OpenClaw 调用、运动任务交接、WORK_LIGHT 与持续模式恢复 |
| lelamp/agent/qwen.py | 原云端 Qwen 调用与人格文件读取 |
| lelamp/voice/ | config、audio、kws、vad、asr、tts，以及 app 持有的轻量 AnnouncementQueue |
| lelamp/audio/ | 本地短提示音解析与 ALSA 播放；不判断业务场景 |
| lelamp/motion/ | 配置、可取消播放、睡眠、录制 |
| lelamp/lighting/ | RGB 驱动、语音状态灯效和持续 WORK_LIGHT 照明 |
| lelamp/vision/ | 目录与职责说明，实际视觉功能尚未实现 |
| lelamp/timer/ | 独立的内存多计时器；只管理时间与完成事件，不依赖硬件或具体模式 |
| lelamp/alarm/ | 持久化绝对时间提醒；支持单次、每天、工作日和每周指定星期，不依赖硬件或具体模式 |
| lelamp/location.py | 应用启动时刷新公网 IP 城市与 IANA 时区配置；成功则覆盖持久配置，失败则复用上次结果；不作为 Agent Tool，不参与功能仲裁 |
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
- TimerManager 由 app 持有，支持多个计时器、暂停、恢复、取消、加时和查询。完成事件由 app 排队处理：固定延迟动作通过统一 ToolExecutor 执行，需要届时搜索或判断的任务由 app 重新调用 Agent。Timer 本身只保存回调信息，不绑定语音、灯光、动作、Agent 或 Focus Mode。
- AlarmManager 同样由 app 持有，使用定位缓存中的 IANA 时区管理绝对时间，持久化未来提醒并支持单次、每天、工作日和每周指定星期重复。Alarm 与 Timer 共用 app 的完成通知通道；停机期间错过的 Alarm 不补播。
- 所有 app 内的 TTS 都经过同一个 AnnouncementQueue。本地语音回答优先于同时积压的远程回答和到期通知；用户已经开口时不会被插播。纯文本 Timer/Alarm 通知可按到达顺序合并，带 Tool、Agent 任务或情绪动作的事件保持独立。队列只串行播报，不是 Event Bus，业务处理和硬件仲裁仍在 app。
- 短提示音同样由 AnnouncementQueue 串行输出。`LampApp` 为唤醒、Timer、Alarm 等场景选择语义音效名，`SoundPlayer` 只读取 `sound.conf` 并播放本地 WAV；提示音和其后的 TTS 属于同一事件，期间不会插入其他播报。
- location 是一次性的启动基础动作：app 启动时查询城市与 IANA 时区，成功后原子覆盖持久配置，失败时读取上一次有效配置。Agent 客户端附加该上下文，Alarm 直接读取同一时区；天气或 Alarm 请求不会再次调用定位服务，也不增加新的 Tool 轮次。
- 新应用与兼容播放服务使用简单串口占用锁；原维护工具仍应单独运行。

## 后续能力边界

app 的 set_mode 接收模式及其执行协程，临时动作结束后恢复该协程。tracking、reading 的交接与恢复已用模拟任务测试，实际摄像头跟踪和阅读姿态尚未实现。

OpenClaw 只有模块位置和说明，没有创建虚假的接入实现。当前 Qwen 保持文本响应；高层动作可由本地明确指令触发。未来 Agent 工具同样调用 app，不能绕过它写舵机。

## 验证

```bash
uv run --no-sync python -m unittest discover -s tests -v
```

模拟检查覆盖动作独占、取消、模式恢复、睡眠保持顺序、整句匹配、采音转换、短句与静音。模拟通过不等于真实舵机、麦克风和云端服务已经现场验证。
