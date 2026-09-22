# LeLamp Runtime 交接文档

这份文档是新对话的入口。先读本文件，再按下面的引用查看细节；不要根据旧聊天记录重新设计项目。

## 必读文档

- 最终架构与模块边界：`ARCHITECTURE.md`。上层设计来源在项目根目录 `archetecture.md`。
- Pi5 操作、测试和排错命令：`LELAMP_COMMANDS.md`。
- 待办及“舵机噪声分类器”等暂缓方案：`TODO.md`。
- Agent 当前精简运行提示：`AGENT_RUNTIME.md`；人格和完整规则见 `IDENTITY.md`、`SOUL.md`、`AGENTS.md`。
- 当前配置：`voice.conf`、`motion.conf`、`lighting.conf`、`sound.conf`。
- Pi Agent 与 OpenClaw 对比：`benchmarks/results/AGENT_COMPARISON.md`。
- 视觉模块入口及当前状态：`lelamp/vision/README.md`。
- 灯光设计来源：`lelamp/lighting/idea.md`。

架构原则：`lelamp/app.py` 是总协调器；Motion、Voice、Vision、Lighting、Timer、Alarm 各自实现自己的能力；硬件冲突和持续模式恢复由 `LampApp` 轻量仲裁；Agent 只能调用高层 Tool，不能直接控制串口、舵机角度或 RGB。不要提前引入 Event Bus、ROS2、复杂状态机或新的网关层。

## 环境与保护规则

- 本机工作副本：`/home/yangyue/LeLamp/lelamp_runtime`
- Pi5：`lamppi@192.168.40.77`
- Pi5 runtime：`/home/lamppi/lelamp_runtime`
- 串口：`/dev/ttyACM0`；灯 ID：`lamppi`
- ReSpeaker：`seeed2micvoicec`
- 唤醒词：`小灯`，词表 `keywords_xiao_deng.txt`
- 正式入口：`uv run --no-sync -m lelamp.app`

修改完成后按用户长期要求自动同步 Pi5，但不要自动推送 GitHub。同步必须用 `scripts/sync_to_pi.sh`；禁止普通全目录 rsync 覆盖 Pi5。脚本会保护 `.env`、模型、运行状态、校准、诊断数据和用户录制动作。

`lelamp/recordings/` 是重要用户数据。当前自录动作包括 `happy_wiggle`、`excited`、`sad`、`shy`、`shock`、`nod`、`headshake`、`curious`，以及 `idle`、`wake_up`、`rotation`、`scanning` 等。禁止擅自替换；同名重录前会自动备份到 `lelamp/recordings/backups/`。

密钥、Bearer Token、模型 API Key 只放 Pi5 `.env`，不能写入代码、文档或 Git。

## 当前运行状态与版本

- 正式 Agent：Pi Agent Core + DeepSeek 官方 `deepseek-flash`，`AGENT_BACKEND=pi`。
- Pi Agent 监听 `127.0.0.1:18792`，Control API 监听 `127.0.0.1:18790`，远程自然语言入口监听 `:18791`。
- Pi Agent 是独立进程；只启动 `lelamp.app` 而未启动 Pi Agent 会出现 `All connection attempts failed`。启动/重启命令见 `LELAMP_COMMANDS.md`。
- OpenClaw + Qwen 配置仍保留用于回退，但不是当前正式后端。
- 当前 TTS：Edge Xiaoxiao，`TTS_BACKEND=edge`、`TTS_FALLBACK_BACKEND=none`。目前禁止自动回退同事的远程 TTS；以后需要时把 fallback 改为 `remote`。
- 进程状态随现场测试变化，不把文档中的状态当真；接手前先检查，避免重复启动。2026-09-18 最近一次只读检查时 app 与 Pi Agent 均在运行。
- GitHub 上最后的 AEC 前基线：`565042a10cc8b3ea7357e7f6d58bdc3c8e6a382b`。此后的 Barge-in 工作已同步 Pi5，但尚未提交或推送。
- 最近本机完整测试：88 项通过。

## 已完成能力

### 语音与对话

- KWS、VAD、ASR、可替换 Agent、TTS 的完整语音链。
- 唤醒后连续对话；有效回答播报结束后重新计算 15 秒无有效文字期限。
- 15 秒超时进入机械睡眠并释放扭矩，但程序、麦克风和 KWS 继续运行。
- Agent 会话在睡眠或超时后清空，下次唤醒是新会话。
- 人格名已统一为“小灯”，性格仍是嘴有点损但靠谱的桌面机器人灯。

### Agent 与高层 Tool

- Agent 后端通过 `lelamp/agent/` 薄接口切换，`LampApp` 不依赖 Pi Agent 或 OpenClaw 的具体实现。
- Pi Agent 仅调用统一 Control API 和 `ToolExecutor`。动作、灯光、转向、Timer、Alarm、搜索、睡眠都使用同一套高层工具。
- 当前模型关闭思考，精简稳定 system 前缀，保留 6 轮历史；日志会分解模型首字、完整响应、Tool 和总耗时。
- 搜索工具使用 DashScope WebSearch 为主源、自建 MCP 为备用源；城市和时区从公网 IP 定位后持久化到 `runtime_state/location.json`，失败时复用上次成功值。
- 条件提醒由 Agent 先搜索和判断；条件为 false 或 unknown 时不得创建 Timer。

### 运动与持续模式

- `sleep()` 是唯一机械休眠出口：进入睡眠姿态、保持配置时间、释放扭矩；不会关闭 Pi 或停止唤醒监听。
- 唤醒后平滑进入待机并保持扭矩；临时动作结束后回待机，或恢复 WORK_LIGHT、tracking 等此前持续模式。
- 从释放扭矩进入动作使用 `MOTION_STARTUP_TRANSITION_SECONDS`；已上电状态切换动作使用 `MOTION_ACTIVE_TRANSITION_SECONDS`。
- Base yaw 支持每格 30°相对转向、left/front/right 绝对朝向和回正。累计偏移写入 `runtime_state/motion_heading.json`，后续待机、照明和录制动作都以新朝向为中立基准，不改校准文件。
- `LampApp.current_motion_task` 是当前统一运动源状态，后续视觉跟踪和 Barge-in 必须复用它，不能另建舵机占用系统。

### 灯光与办公照明

- RGB 状态灯和 WORK_LIGHT 已实现。灯板曾因电源线断裂失效，硬件现已修好。
- WORK_LIGHT 默认高姿态、白光、75%；支持高/低姿态、白/暖黄光、50/75/100% 亮度。
- WORK_LIGHT 是持续模式，聊天状态灯效不能覆盖主照明；临时动作后恢复其姿态和灯光。
- 退出 app 的灯光清理逻辑已恢复；真实灯板测试确认能够熄灭。

### Timer、Alarm 与播报队列

- `lelamp/timer/` 是 asyncio 多 Timer 基础能力，支持创建、暂停、继续、取消、加时、查询；Timer 仅驻留内存。
- `lelamp/alarm/` 是持久绝对时间提醒，支持单次、每天、工作日和每周，并从 `runtime_state/alarms.json` 恢复。
- Timer/Alarm 可保存普通 message、白名单高层 `on_complete`，或到期后重新交给 Agent 推理的 `agent_task`；Timer/Alarm 本身不直接控制硬件。
- `lelamp/voice/announcement.py` 是统一单消费者播报队列，管理本地回答、远程回答、Timer、Alarm 和未来通知。它只解决 TTS 顺序、合并和取消，不是通用 Event Bus。
- 当前提示音选择：`WAKE-B`、`TIMER-B`、`ALARM-B`，配置见 `sound.conf`。

### 远程自然语言入口

同事或未来网页可直接调用现有入口，不需要先做网站：

```http
POST http://<pi-ip>:18791/api/v1/agent/text
Authorization: Bearer <PI5 .env 中的 LELAMP_REMOTE_TOKEN>
Content-Type: application/json
```

```json
{
  "request_id": "每次请求唯一ID，重试复用",
  "session_id": "连续对话保持相同",
  "text": "进入照明模式",
  "locale": "zh-CN"
}
```

返回包含 `answer`、`status`、`spoken`。该入口绕过 KWS/VAD/ASR，之后复用同一个 Agent、ToolExecutor、硬件仲裁和 AnnouncementQueue；HTTP 默认等待播报完成后返回。

## Barge-in 当前状态

Barge-in 是 `voice/` 基础能力，不是 Agent Tool。实现位于 `lelamp/voice/barge_in.py`、`playback.py` 和 `keywords_interrupt.txt`，由 `LampApp` 协调 TTS 取消、动作停止、ASR 和会话期限。

- 舵机静止：WebRTC AEC（channel 0、80 ms、low）+ 独立 Silero VAD 检测自然插话。
- 舵机运动：复用 `current_motion_task`，只用本地 sherpa KWS 接受“停、等等、行了、别说了、闭嘴”，避免舵机噪声触发自然 VAD。
- TTS PCM 同时送扬声器和 AEC reference；命中后终止 `aplay`/Edge 解码，保留约 0.5 秒预录并继续收完整句子，再进入原 ASR/Agent 流程。
- 已修复正式 GStreamer appsrc caps、reference 实时节奏、Edge 取消死锁、raw pre-roll 导致自识别、打断后立刻错误睡眠等问题。
- 当前自然插话参数：`BARGE_IN_IDLE_VAD_THRESHOLD=0.40`、`BARGE_IN_IDLE_MIN_SPEECH_SECONDS=0.20`；停止词参数与“小灯”唤醒参数独立。
- 告别仲裁：正常播完告别直接睡眠；告别播报被打断且 ASR 有有效新文字则取消睡眠并回答；打断后 ASR 为空或失败仍执行睡眠。
- TTS 正常结束或被打断都把连续会话截止时间重置为“此刻 + 15 秒”。
- `BARGE_IN_DEBUG=0` 是仓库默认；排错时可临时设为 1。
- 确认真实打断时会触发约 180 ms 的高亮青蓝反馈；WORK_LIGHT 中只对当前照明做约 10% 的轻微亮度脉冲并恢复，不切换色调。该反馈由 `LampApp` 调用 Lighting，不是 Agent Tool；参数在 `lighting.conf`。

正式 Barge-in 已能停止播放并进入 ASR，但最新告别仲裁和误触发参数仍需实机回归。重点验证：自然插话、空 ASR 后仍等待 15 秒、告别三种分支、运动中五组停止词、舵机单独运动不误停、WORK_LIGHT 状态恢复。不要重新接回旧的 `BARGE_IN_VAD_*` 参数。

2026-09-20 已把普通监听改为持续采音：`VOICE_SHARED_CAPTURE=1` 时使用 ALSA `dsnoop` 保持底层 ReSpeaker 常驻，普通监听和每轮新建的 WebRTC AEC 共享该底层流。播报前后只暂停/恢复普通音频消费，不再关闭麦克风或等待 0.8 秒预热；`VOICE_SHARED_CAPTURE=0` 可回退旧方式。Edge 播放结束后不再等待远端 WebSocket 关闭握手。Pi5 完整远程文本链实测“播放结束→AEC 关闭→恢复可监听”为 `0.047 秒`，纯回声为 `0/3` 误打断，数据在 `voice_debug/aec/shared_capture_dsnoop_smoke2/`。第一版 Python `appsrc` 转发方案因破坏 ALSA/GStreamer 共同时间基准已废弃，不要恢复。

独立 AEC、动态打断和舵机噪声诊断脚本保留在 `lelamp/test/`。固定频率 notch 已证实不能消除多种舵机噪声；以后继续分类器实验时直接读 `TODO.md`。

正式 TTS 声学延迟诊断为 `lelamp/test/test_tts_acoustic_latency.py`：它旁路保存实际写给 `aplay` 的 Edge PCM，同时以 20 ms 块采集 ReSpeaker 双通道，再用互相关计算偏移。2026-09-18 使用两句不同时长文本共测 12 轮：channel 0/1 均稳定在约 `8.31～8.50 ms`，总中位约 `8.4 ms`。原始数据位于 Pi5 `voice_debug/tts_latency/20260918-124444/` 和 `20260918-124645/`。当前 `BARGE_IN_AEC_DELAY_MS=80` 是早期 AEC 抑制扫描的工作参数，并非物理声学延迟；在修改它之前，应基于正式 appsrc+AEC 链路围绕 `8 ms` 做细粒度抑制和双讲回归。

随后用 `test_formal_aec_delay.py` 对正式 Edge/aplay/Barge-in 链做纯回声扫描。物理延迟 `8 ms` 的 AEC 抑制反而弱于 80 ms；40～120 ms 各值均可能偶发误触发，120 ms 虽平均抑制较稳定，9 次仍有 1 次失败，且未验证真人双讲。失败 clean 与 TTS reference 相似度很低，更像环境类人声噪音被 Silero 误判。因此正式值暂时保持 80 ms；不要继续靠扫固定 delay 解决误触发，下一步应在现有 VAD 后增加轻量二次确认，同时保留真人插话灵敏度。诊断数据在 Pi5 `voice_debug/aec/formal_delay/`。

固定回归集位于 `benchmarks/barge_in/dataset.jsonl`，评测入口是 `scripts/evaluate_barge_in_dataset.py`；音频仍只存于 Pi5 `voice_debug/`。V0 排除了舵机运动，共 44 条：27 条可重放的纯播报/环境负样本、6 条已知误打断、2 条成功插话、5 条漏打断/检测后 ASR 为空、4 段待切分旧真人实验；其中三条完全未触发案例因为没有连续音频，仅作日志标签。当前 `0.40/0.20 秒` 在全部负样本上误触发 5/27，只看录制 delay 为 80 ms 的样本为 1/6，结果在 `voice_debug/barge_benchmark/v0-current.json`。真人正样本明显不足，后续需要带明确原句和插话时刻的定向补录；必须连续保存 raw/reference/clean，才能把未触发的失败也纳入评测。

`scripts/tune_barge_in_dataset.py` 已对 56 组 Silero 参数做离线对照。把已触发录音的 0.5 秒 AEC clean 预录也纳入近似判定后，当前 `0.40/0.20 秒`为真人 3/4、负样本误触发 7/32；没有任何候选同时保持真人召回并降低误触发。`0.30/0.20 秒`虽达到 4/4，但误触发为 14/32；最短语音提高到 0.30 秒则只剩 1/4。简单 RMS、过零率和频带比例也出现真人与误触发重叠，暂不适合作为硬阈值二次确认。正式参数继续保持不变，结果在 Pi5 `voice_debug/barge_benchmark/grid-v0.json`。

## TTS 决策与历史候选

- 正式采用 Edge Xiaoxiao，唤醒后预连接，MP3 由 `mpg123` 流式解码后送 `aplay`；当前语速 `EDGE_TTS_RATE=+10%`。
- Edge 暖连接首个 PCM 约 0.19 秒；固定回答缓存未接入，因为 Agent 输出不固定。
- 本地候选都在 Pi5 `~/tts_local_candidates/`，没有接入正式系统：Huayan/Matcha 很快但音质一般，MeloTTS 较慢，MOSS/ZipVoice 远慢于实时。Supertonic 因无中文已删除。
- 当前禁止远程 TTS fallback。不要因 Edge 主动取消而把它当故障重新播放远程 TTS。

## 下一步：视觉开发

摄像头选型、独立依赖环境、YuNet、MediaPipe Gesture Recognizer、SFace链路及30分钟app并行负载测试已经完成。正式摄像头为固定在灯头上的IMX179 USB UVC摄像头；采集基线为640×480@30 FPS MJPEG，固定安装画面逆时针转正90°后的模型输入为240×320。

2026-09-21 已完成第一批正式视觉代码：`vision.conf`、正式数据契约、单一最新帧采集、YuNet/MediaPipe同源联合调度、`VisionController`生命周期、只读状态与健康信息均已接入`LampApp`。视觉在语音会话和WORK_LIGHT中运行，普通等待唤醒与sleep时释放摄像头；当前不发送舵机命令、不执行手势副作用。Pi5正式环境实测10.0 Hz，推理P50/P95为38.8/39.8 ms，摄像头读取失败0。

同日已增加`target.py`：按归一化人脸面积筛除背景小脸控制候选，使用预测位置、IoU和尺寸变化关联当前前景目标；新出现的人脸不会立即抢占，目标短时丢失时保留ID并在250 ms后拒绝向Motion提供旧结果，1.2秒后才释放选择权。

人脸机械跟踪已接入正式路径。`tracking_home`独立于standby和持久底座朝向，当前实测为base yaw 0、wrist pitch -45；设备本地`lelamp/motion/calibration/visual_response.json`保存双轴图像响应，条件数1.59。Motion以25 Hz运行最新目标闭环，包含死区、预测、限速、限加速度、软限位和过期停更。正式app无人运行41秒时视觉9.99 Hz、背景未误选、舵机命令0。真实前景人物的方向、平滑度、目标短失和临时动作恢复仍需现场验收。

2026-09-22 修正腕部关节命名：物理 ID 4 对应 `wrist_pitch`，ID 5 对应 `wrist_roll`。Follower/Leader 映射、各自校准、5 组固定姿态和 16 份录制动作已成套迁移；逐帧检查确认迁移前后发送到两个物理舵机的原始目标值相同。上段记录的 `wrist pitch -45` 是修正前的旧名称，实际对应现在的 `wrist_roll -45`。旧 `visual_response.json` 测的是物理 ID 5，不能用于新 `wrist_pitch`；加载器会拒绝没有新 `controlled_motor_ids=[1,4]` 的结果。重新实机运行 `scripts/calibrate_visual_response.py` 并检查跟踪方向与软限位之前，不要启动人脸机械跟踪。

正式架构已经确定：只使用现有`LampApp`，不建立第二个视觉app；摄像头只保留最新帧；视觉会话中人脸与手势同时以最高10 Hz运行；单人前景目标优先；face/hand只切换同一个tracking runner的机械关注目标。SFace正式激活逻辑暂缓。

详细方案统一维护在视觉目录：

- [模块职责与目录导航](lelamp/vision/README.md)
- [视觉功能设计与开发准则](lelamp/vision/docs/VISION_DESIGN.md)
- [当前开发路线](lelamp/vision/docs/ROADMAP.md)
- [历史摄像头选型验证方案](lelamp/vision/docs/CAMERA_EVALUATION.md)

后续正式开发遵守以下边界：

1. Vision只负责采集、识别、短期目标保持和输出结果，不直接写舵机。
2. tracking机械输出必须经过现有运动锁、`current_motion_task`和取消等待；同一时刻只能有一个运动控制源。
3. 语音会话超时与机械持续模式分离：复用WORK_LIGHT语义让tracking继续运行，再次唤醒只建立语音会话、不进入standby；明确sleep终止tracking。
4. 长期tracking任务在Barge-in中按运动状态处理，继续复用`current_motion_task`门控；不要按瞬时关节静止切回自然VAD。
5. `ToolExecutor`调用必须携带`agent`、`local_voice`、`vision`、`scheduled`或`control_api`等来源，避免视觉动作被误判为Agent情绪动作。
6. 临时动作期间丢弃tracking请求和手势事件，恢复后重新确认；WORK_LIGHT长期保留手势感知，并在内部支持手部引导和自定义照明姿态。
7. Agent和网页只使用高层能力，不能看到摄像头线程、控制器增益、关节目标或串口参数。

视觉设计文档和项目架构记录的是当前最佳方案，允许按后续实测和用户要求更新；变更时要同步修正文档和实现，不能机械执行已经过时的条目。

## 下一步：网页开发

网页可以先复用 `POST /api/v1/agent/text` 做自然语言控制和完整回答展示。需要按钮或状态面板时，在 `LampApp` 现有轻量 HTTP 服务上增加少量高层端点，内部仍调用 `ToolExecutor`/`get_robot_state()`；网页不能直接请求串口、动作 CSV 或 RGB 驱动。

- 浏览器和 Pi5 在同一局域网时使用 Pi 的 LAN 地址；token 从部署环境提供，不能硬编码到公开前端仓库。
- 如果网页需要实时进度，优先增加简单任务查询或 SSE；没有实际需求前不要引入新的 FastAPI/MCP Gateway。
- 远程文本、网页和本地语音必须共享同一 `LampApp` 实例，才能复用动作仲裁、模式状态和统一播报队列。
- 网页开发前先明确是“只发自然语言”还是“还需要直接按钮和实时状态”，再决定最小接口集合。

## 接手后的最小检查

```bash
cd /home/yangyue/LeLamp/lelamp_runtime
uv run --no-sync python -m unittest discover -s tests -v

ssh lamppi@192.168.40.77
cd ~/lelamp_runtime
pgrep -af 'lelamp.app|pi-agent|server.js'
```

运行实机前确认 Pi Agent 已启动，再单独启动一份 app。不要同时运行 `test_rgb`、record、replay、AEC 诊断或第二份 app；RGB、声卡和串口需要单一占用者。测试结束后停止自己启动的进程，并让小灯进入安全睡眠状态。
