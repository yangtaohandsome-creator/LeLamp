# LeLamp Runtime 交接文档

这份文档是新对话的入口。先读本文件，再按下面的引用查看细节；不要根据旧聊天记录重新设计项目。

## 必读文档

- 最终架构与模块边界：`ARCHITECTURE.md`。上层设计来源在项目根目录 `archetecture.md`。
- Pi5 操作、测试和排错命令：`LELAMP_COMMANDS.md`。
- 待办状态及“舵机噪声分类器”等暂缓方案：`TODO.md`。
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
- Pi Agent 仅调用统一 Control API 和 `ToolExecutor`。动作、灯光、转向、Timer、Alarm、Todo、搜索、睡眠都使用同一套高层工具。
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
- `lelamp/todo/` 是持久快速记事，保存于 Pi5 `runtime_state/todos.json`，支持创建、列出、修改、完成和删除。Todo 不定时播报；明确要求主动提醒才使用 Timer/Alarm。
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

摄像头选型、独立依赖环境、YuNet、MediaPipe Gesture Recognizer、SFace链路及30分钟app并行负载测试已经完成。正式摄像头为固定在灯头上的IMX179 USB UVC摄像头；采集基线为640×480@30 FPS MJPEG，固定安装画面旋转180°转正后的模型输入为320×240。

2026-09-21 已完成第一批正式视觉代码：`vision.conf`、正式数据契约、单一最新帧采集、YuNet/MediaPipe同源联合调度、`VisionController`生命周期、只读状态与健康信息均已接入`LampApp`。视觉在语音会话和WORK_LIGHT中运行，普通等待唤醒与sleep时释放摄像头；当前不发送舵机命令、不执行手势副作用。Pi5正式环境实测10.0 Hz，推理P50/P95为38.8/39.8 ms，摄像头读取失败0。

同日已增加`target.py`：按归一化人脸面积筛除背景小脸控制候选，使用预测位置、IoU和尺寸变化关联当前前景目标；新出现的人脸不会立即抢占，目标短时丢失时保留ID并在250 ms后拒绝向Motion提供旧结果，1.2秒后才释放选择权。

人脸机械跟踪已接入正式路径。`tracking_home`独立于standby，但复用持久底座朝向作为底层偏移；跟随软限位与校准范围预留余量后的边界取交集。Motion以25 Hz运行最新目标闭环，包含死区、预测、限速、限加速度、软限位和过期停更。正式app无人运行41秒时视觉9.99 Hz、背景未误选、舵机命令0。真实前景人物的方向、平滑度、目标短失和临时动作恢复仍需现场验收。

同日已实现嵌套式手部跟随代码：稳定手ID、掌骨尺度、MediaPipe手势时间确认，以及`face_follow / hand_follow / hand_hold`状态共用同一个Motion runner。普通握拳→张开锁定；锁定时复用人脸模式的张开→握拳转换，同一只近距离手必须在1.5秒内完成动作才恢复，单独握拳不恢复。退出手部覆盖只接受同一只手在3秒内完成的既有连续序列，不再保留无限期的`hand_follow_exit_armed`状态。`start_face_tracking`、`start_hand_tracking`、`stop_tracking`和`get_vision_state`已接入app、`ToolExecutor`、Pi Agent与OpenClaw schema；精简提示词仅增加一行跟脸、跟手和停止映射。引导标定把近距离进入/退出和手势置信度阈值保存到Pi的`runtime_state/vision_hand_calibration.json`；重新运行会原子覆盖，代码同步不会删除。文件缺失时Tool返回未标定，自动手势不驱动舵机。手背识别改善已记入TODO，本轮没有自定义分类逻辑。

Pi5当前手部设备标定来自边界与稍远位置各张开/握拳约60帧：`enter=0.221882`、`exit=0.189495`、`gesture_confidence=0.50308`，完整样本摘要保存在同一JSON。后续重新运行`DISPLAY=:0 .venv/bin/python -u scripts/vision_preview.py --hand-calibration`会按终端12步引导、重新计算并原子覆盖旧标定。

2026-09-22 修正腕部关节命名：物理 ID 4 对应 `wrist_pitch`，ID 5 对应 `wrist_roll`。Follower/Leader 映射、各自校准、5 组固定姿态和 16 份录制动作已成套迁移；逐帧检查确认迁移前后发送到两个物理舵机的原始目标值相同。旧视觉响应测的是物理 ID 5，已留作历史备份；加载器要求新标定带 `controlled_motor_ids=[1,4]`。

同日用户确认的新视觉跟随标准初始姿态已写入 `motion.conf`：以待机为基准，物理 `wrist_pitch` 朝上 30°，`wrist_roll` 略向下 2°，其余关节保持待机目标。普通待机姿态没有改变。新图像响应已在 Pi5 重新实测：输出画面480×640，`base_yaw`正向使内容约上移12.05像素/控制单位，`wrist_pitch`正向使内容约右移10.87像素/控制单位，矩阵条件数1.21。标定脚本收尾已改为进入睡眠姿态后才释放扭矩；实时真人跟随的方向、平滑和边界仍待现场验收。

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

## 局域网网页控制台（已部署 Pi5，部分验收）

视觉网页接入交接：先读 `lelamp/web/HANDOFF_VISION.md`，包含代码入口、白名单、仲裁注意事项和验收边界。

- 实现和启动方法见 [网页说明](lelamp/web/README.md)。首页、对话、控制、Timer/Alarm/Todo 管理、动作录制和 follower/leader 校准均已提供；控制页视觉跟随按钮与状态已接入，视频和网页标定不在本轮范围内。
- 正式网页复用 app 的远程 HTTP 监听端口，免登录、同源校验；旧同事接口继续使用 Bearer Token。网页按钮调用高层能力，由 LampApp 抢占旧对话/运动，维护模式独占设备；提醒保留到维护结束。
- 本机模拟入口：`uv run --no-sync -m lelamp.web --demo`，访问 `http://127.0.0.1:18793`。模拟不连接 Agent、麦克风、串口或 TTS，数据退出后删除。
- 2026-09-24 已同步 Pi5 和本机编译的 Pi Agent；控制/维护只读/抢占等已实测。用户恢复发声测试后，网页回答、独立 Timer/Alarm 和维护积压合并播报均完成；先前 Edge 间歇失败未复现但未确认根治。自检发现 ASR 端口暂不可连接。测试结束 app 已停止，Pi Agent 保留。真实录制、校准写入仍待现场操作。详见网页说明末尾验收记录。OpenClaw 回退尚无同等跨请求轮次隔离，不作为网页抢占验收后端。

## 接手后的最小检查

```bash
cd /home/yangyue/LeLamp/lelamp_runtime
uv run --no-sync python -m unittest discover -s tests -v

ssh lamppi@192.168.40.77
cd ~/lelamp_runtime
pgrep -af 'lelamp.app|pi-agent|server.js'
```

运行实机前确认 Pi Agent 已启动，再单独启动一份 app。不要同时运行 `test_rgb`、record、replay、AEC 诊断或第二份 app；RGB、声卡和串口需要单一占用者。测试结束后停止自己启动的进程，并让小灯进入安全睡眠状态。


### 2026-09-28 三路手势并集正式接入

MediaPipeHands默认使用官方原始、旧world摆正、原始XYZ平面旋转+翻Z三路逐帧并集；Thumb_Up仅采纳官方原始。gesture_candidates保留类别及各自分数，gesture用于显示完整并集。融合不增加计时，机械状态机继续使用既有StableGestureDetector：仅一个有效控制类别时确认Open_Palm/Closed_Fist，同帧两者并存清除候选，不把并列结果当作先后转换。Motion与姿态/限位不变。

正式分类模块为gesture_classifier.py、gesture_fusion.py；旧experiment模块保留兼容导入。vision可选依赖新增ai-edge-litert==2.2.0，锁文件同步。独立三路对比脚本显式使用fused=False，避免把融合结果当作官方原始输出。全套152项测试通过；用户已确认当前真人跟随效果，半弯误触发仍作为后续优化项。


### 2026-09-28 用户实机验收：确认为当前正式方案

用户确认当前视觉跟随效果良好，三路手势逐帧并集作为当前正式方案，替代此前仅官方原始分类的方案。保持Thumb_Up仅接受官方原始、其余类别三路并集、融合无额外延时；机械控制复用原有手势确认及单一Motion控制链。此次验收为用户现场体验确认，不等同于所有角度、所有用户和负样本的全面量化验收。半弯手误报与多候选情况继续作为后续优化项，不阻塞当前版本采用。本次只更新状态记录，不改变运行参数、不重启预览。


### 当前工作切换：视觉延迟（2026-09-28）
振荡优化暂缓，完整暂停点见 `lelamp/vision/docs/ROADMAP.md` 末尾。显示阻塞导致的抖动已解决；增益1.8、速度80/40、加速度210/140、预测0、制动开启时仍有停手回摆，用户反馈制动只略有改善。下一步先分段测量视觉/控制/显示延迟，不继续调整运动参数。


### 2026-09-28 分段延迟测量完成
详见 [分段测量报告](lelamp/vision/docs/LATENCY_MEASUREMENT.md)。热状态下官方手链P50约99ms，采集返回到首次写入158ms、到显示提交171ms；两条补充分类约1.4ms。当前测试已停止并睡眠释放扭矩；运动参数未变。优先恢复散热复测，再优化推理/结果发布及采集链。


### 2026-09-28 实际关节反馈的灯头运动补偿（待真人快速移停验收）

延迟优化暂停。保持增益1.8、速度80/40、加速度210/140、目标速度外推0和原制动开关。
新增 `MOTION_TRACKING_EGO_COMPENSATION_ENABLED=1`：开启后实际反馈采样至少达到控制频率（当前25Hz），原5Hz配置保留供关闭补偿时使用。

Motion保存最多128条真实关节位置和单调时间戳（串口读起止中点近似采样时间），用包围帧采集时刻的两条反馈线性插值，得到拍摄时位置。`修正位置 = 原图目标位置 + response_matrix @ (最近实测位置 - 拍摄时插值位置)`。不将指令位置视作实际位置，不对最新反馈之后的关节运动外推。历史不足、采集时间越界、反馈间隔或陈旧时间超过2.5个反馈周期时回退原控制；脸短失时已预测的位置、启用目标速度外推时也跳过补偿，避免重复推算。

补偿后制动只扣除最近反馈至当前的残余时间，不再重复扣掉已补偿的整段图像年龄。所有速度、加速度、底座朝向、软硬限位仍走原控制链。hold只读取位置，仍不发送移动命令。临时动作重建runner时重建历史。

状态增加ego_compensation_active和ego_image_shift；诊断cycle增加ego_joint_delta、ego_image_shift、ego_feedback_at、corrected_position、braking_age。关闭开关即可回退原视觉误差控制及5Hz反馈。图像时间仍是read返回，不是真实曝光时刻，响应矩阵是局部近似，不能宣称消除了全部延迟或振荡。反馈采样提高后的周期耗时需实测，最终效果由快速移手后停止验收。


### 2026-09-28 死区边缘连续过渡试验
增加 `MOTION_TRACKING_DEADZONE_TRANSITION_WIDTH=0.04`：在原死区之外4%画面宽/高的误差区间内，用smoothstep(u)=u²(3-2u)乘原误差；死区内仍为0，区间外原误差完全保留。水平误差达到0.085、竖直达到0.075后恢复原增益2.8关系。无时间状态/额外等待，不改死区0.045/0.035、setpoint(0.5,0.5)、增益、速度/加速度、补偿与限位。此为局部误差整形，不新增可变增益配置或全局滤波。宽度设0回退原硬死区。小偏差追赶更柔和，可能增加低速稳态偏差，须实际验收；不能仅凭函数连续断言舵机微动已完全平滑。
本地165项测试通过，新增覆盖边界连续、对称、单调、远端保持及回退。同步Pi5启动全屏跟随预览，待用户验证慢速小幅与快速移停。


2026-09-28 独立低速轨迹确认：绕过视觉后目标寄存器平滑且读回正确，实际位置仍有数百毫秒平台。详见ROADMAP末尾及servo-slow-1790579546777937612.jsonl；执行端内部控制/机械负载仍待区分。本轮未改底层PID/校准，已睡眠释放扭矩。


2026-09-28 完成低速执行端A/B/A诊断：[报告](lelamp/vision/docs/SERVO_LOW_SPEED_DIAGNOSIS.md)。启动力16→48明显减小误差，恢复16复现；base中位7→1→7tick，pitch14→7→14tick。主要因素为小误差驱动能力与负载匹配，pitch残余停顿未解决，供电版本待核对。试验值已全部恢复，两轴P/I/D16/0/32、启动力16，sleep且Torque_Enable=0；没有将试验参数作为正式方案。


### 2026-09-28 启动力48实机试用（可回退）
用户授权应用：motion.conf新增MOTION_SERVO_STARTUP_FORCE_EXPERIMENT=1及BASE_YAW/WRIST_PITCH两项48。follower.configure在启动力写入后读回校验，仅改这两个关节；这是全局底层设置，会影响姿态/录制动作/跟随，并非视觉专属。其他PID、视觉参数、校准及姿态不变。
回退：将MOTION_SERVO_STARTUP_FORCE_EXPERIMENT设0并同步、重启运动进程；configure将两轴显式写回16。不能只删除新代码而忽略寄存器残留。修改前本机备份runtime_state/startup_force_backup/1790580713095385597，Pi5备份runtime_state/startup_force_backup/before48。
本地168项测试、Pi5配置3项测试通过。全屏跟随预览已启动待用户观察慢速/快速/静止表现；尚未验收其他动作、长时温升或噪声，当前仍为试用值。


### 2026-09-28 启动力48真人验收失败，已回滚
用户反馈正式跟随抖动明显加重。停止预览PID4908，完成睡眠后通过独占总线将base_yaw/wrist_pitch Minimum_Startup_Force写回16并读回确认；P/I/D仍16/0/32，Torque_Enable=0。motion.conf试验开关已设0并同步。保留回退支持代码，不保留活动试验设置；未重启运动。
纠正此前结论：参数A/B/A证明启动力影响低速误差/停走，不足以证明“16错误”或“48可改善正式跟随”。此前只测缓慢确定性轨迹，未覆盖视觉噪声与闭环频繁修正。base单周期实际位置跳动在48下仍可达6tick，并未因平均误差变小而消失；pitch残余停顿明显。末尾2秒静止基线无明显自振，这也不能代表含噪视觉闭环稳定。
合理机制是假设：提高最小输出后小误差更易克服负载，但也可能造成更强的小幅冲击，对视觉误差噪声/反向修正更敏感。此次真实预览没有逐周期诊断日志，不能断言全部抖动由该机制解释，更不能已确认硬件故障。后续保持启动力16，以真实视觉轨迹/静止噪声闭环数据分析，评价必须同时包含位置误差、实际速度变化与抖动，不再单凭误差下降采用更大启动力。


### 2026-09-28 纠正死区过渡局部增益过大
复查tracking-1790579238486102501：旧公式error*smoothstep((abs(error)-deadzone)/0.04)虽连续，局部导数导致等效P水平峰值8.81、竖直7.84，而配置P仅2.8。移动窗口水平相邻期望速度跳变>2有77次，48次对应新视觉帧；竖直50次、37次对应新帧。加速度限幅仅2/3次，意味着这些小速度跳变几乎全部通过。不能再把抖动主要归为硬件而忽略控制器自身的放大；独立舵机测试只证实执行端存在非线性，未证明其为视觉顿挫主因。
修改为先扣除死区，再在max(配置宽度,3*deadzone)范围内smoothstep恢复偏置；单轴映射斜率<=1.5，无额外时间状态。水平偏差>=0.18、竖直>=0.14完全恢复原误差和P2.8；中间范围会更柔和，不承诺所有中偏差速度与旧版一致。硬死区大小不变，启动力保持16，试验开关0。
固定历史观测的离线重算：相邻期望速度变化P95水平4.7567→2.4719、竖直3.2272→2.0001，最大水平9.4538→5.3261、竖直6.2024→3.8126。这不是闭环重放或实机改善证明，目标轨迹与实际舵机会随控制变动。169项本地测试通过，新增映射斜率边界测试。同步Pi5预览等待用户慢速及快速验收。


### 2026-09-28 自适应目标滤波实机试验（未验收）
用户仍反馈慢速小幅不顺，启动力保持16。新增独立Motion目标滤波模块target_filter.py；仅视觉follow调用，不改变姿态/录制动作、手势确认或感知输出。以现有局部响应矩阵R和拍摄时实际关节q构造stable=p-R*q_capture，在此坐标估计速度和One Euro滤波，再加R*q_latest恢复当前图像位置。响应矩阵仅局部近似，非完整世界坐标；采集时间仍非曝光时间。
仅新captured_at更新滤波；25Hz重复观测复用输出，但每周期重新加实际相机运动。换源/换ID、hold/home、无目标、预测目标、反馈不足或帧间隔>0.3s重置，禁止跨目标拖尾。保留P2.8、速度80/40、加速度210/140、死区与已有制动。
配置MOTION_TRACKING_TARGET_FILTER_ENABLED=1，MIN_CUTOFF=1.5Hz、BETA=8、ERROR_GAIN=20；截止频率=min_cutoff+beta*稳定坐标速度模长+error_gain*未滤波图像误差模长，导数低通1Hz。后项是本项目扩展，避免大误差但目标静止时过度平滑。均为待实机验证的试验值，不宣称消除抖动。滤波不可避免增加延迟，需同时评价慢速抖动、快速追赶与停止回摆。回退开关设0并重启跟随即可；不需要恢复底层寄存器。
诊断增加filter_input/output/cutoff/target_speed，状态target_filter_active。本地173测试通过（新增重复帧、目标切换/时间断档、噪声/速度自适应、自运动消除测试）。使用既有tracking_diagnostic.py的6/7慢速及4/5快速移停记录做闭环验收，单元测试不能代替实机。


### 2026-09-28 自适应目标滤波回退
用户真人反馈没有明显改善。MOTION_TRACKING_TARGET_FILTER_ENABLED恢复0，重启预览回到滤波试验前的控制行为；保留未启用的试验代码及参数以供诊断。P2.8、速度80/40、加速度210/140、死区与补偿均不改。下一候选是控制端期望速度的连续轨迹/jerk约束，尚未实现；先用原诊断记录验证命令速度跳变与实测停走的对应关系，不把滤波无效当作硬件原因的证明。


### 2026-09-28 视觉指令速度jerk约束试验（待真人验收）
自适应目标滤波无明显改善，保持TARGET_FILTER_ENABLED=0。此次只在VisualTrackingRunner的follow输出使用velocity_profile.py，保留当前P2.8、速度80/40、加速度210/140、原死区与补偿，启动力16；不改姿态/录制动作/home/Agent。
采用单轴解析速度过渡：由当前指令速度、加速度，规划加速度斜坡—可选恒加速度—加速度归零三段，对每段精确积分得到位置增量；每周期从既有状态重规划，不把每个视觉观测当作停止位置。没有安装Ruckig/ROS或其他库，不是Ruckig实现。双轴独立，现有软件/硬件限位优先。只保证正常follow指令轨迹的jerk界，不保证物理舵机jerk；hold、目标丢失、切源与限位停止会清零状态并打断连续性。
MOTION_TRACKING_JERK_LIMIT_ENABLED=1，MAX_JERK_BASE_YAW=1000、WRIST_PITCH=700，单位为校准关节单位/s³而非度。回退开关设0并重启即恢复原加速度限幅和原制动计算。为了不继续用无限jerk的停车假设，开启时制动额外计入各轴max_acceleration/max_jerk的保守过渡时间；接近目标的跟随速度可能因此降低，不能声称响应完全不变。
纯指令模型：base从0到80约0.591s（原0.381s），pitch到40约0.486s（原0.286s）；base的2单位/s小速度阶跃展开到约90ms。新增运动诊断profile_acceleration_before/profile_acceleration/profile_velocity/profile_displacement。全屏预览新增可选--tracking-log路径，使用原异步有界写入器，仅记录运动，不额外开启全链延迟日志。
本地178测试通过：含2500次随机速度切换/可变周期的v/a/jerk界，精确积分、分段等价、静止及反向收敛、锁定/丢失不发命令、限位强制停止。尚无真人验收结论，需比较慢速微动、快速反向和快速移停，并检查实际位置是否仍平台后跳动。


### 2026-09-28 jerk试验仍未改善主观顿挫
用户反馈追赶未变慢，但顿挫无明显改善。已分析运行中的377秒运动快照，详见[结果](lelamp/vision/docs/JERK_TRIAL_RESULT.md)。输出速度变化收敛，但存在发送位置持续移动、实测约0.4秒不变的手部跟随片段。不可仅靠反馈静止比例判定摩擦或硬件原因。未追加参数修改，当前预览继续运行记录。


### 2026-09-28 两种平滑试验全部回退，补充执行端原参数实测
用户要求回退jerk并自行调查，暂不应用候选。JERK_LIMIT_ENABLED=0、TARGET_FILTER_ENABLED=0已同步并确认实机；保留代码但不启用。新基线servo-feedback-1790584470317008449共902样本、丢失0，沿用±4校准单位的原轨迹，增加速度/负载/电流/电压读取，未改变调参寄存器。已完成睡眠并逐轴读回Torque_Enable=0，当前没有预览运行。
详细证据、原P/启动力A/B重新评价、整数目标量化离线测试及未实施的候选见[低速执行端复核](lelamp/vision/docs/LOW_SPEED_ACTUATION_REVIEW.md)。新候选是位置模式下同包发送轨迹匹配的速度/加速度；只提出独立A/B，未实现或应用。不能将驱动占空比增加而位置不动直接诊断为硬件损坏，也不再拿位置误差降低证明平滑。

### 2026-09-28 网页视觉跟随接入

控制页增加人脸/手部/停止跟随，复用 ToolSource.WEB 与原网页仲裁；只读 `/api/v1/web/vision` 展示真实状态并过滤过期检测。照明期间必须先退出照明，维护禁止控制，未标定禁止手部启动。修正从睡眠开启持续模式后机械睡眠标记残留。没有改动运动参数、手势和模型；低速抖动优化仍暂停，两个平滑试验开关仍关闭。范围和验收记录见 [网页说明](lelamp/web/README.md#视觉跟随控制2026-09-28)，后续视觉网页入口见 [交接](lelamp/web/HANDOFF_VISION.md)。

本轮本地186项测试、Pi5网页21项测试通过；实机验证睡眠/开始/停止、临时动作恢复、等待手超时、照明/维护互斥；真人手部锁定及观感未验收。浏览器本机模拟与Pi5正式站点手机/电脑检查通过。实机已睡眠并停止本轮测试App，未推送GitHub。

### 2026-09-28 办公照明独立手势调向

新增WorkLightHandSession，仅照明内使用；原NestedTrackingSession不改。5秒丢失暂停保持低亮度，只有握拳恢复，张开忽略；超时锁定，下一次重新张开→握拳。用户最新色温亮度独立保存，调向/暂停用30%，锁定恢复。网页照明开放手部开始和停止；人脸仍受照明限制。原手优先，多手明显近者可接管；阈值与回退开关见 [网页说明](lelamp/web/README.md#办公照明手势调向)。高照明pitch99超出跟随软限95，首次跟手时平滑进入边界，保持及丢失不自行回home。普通跟随、运动参数与平滑开关不改。真人效果未验。

2026-09-29收尾：本地207项、Pi Agent4项、Pi5照明20项与网页22项无硬件测试通过，手机/电脑浏览器模拟通过。模拟维护锁已隔离到临时目录，真实串口锁不变。文件已同步，当前用户App6273/Agent6278未重启，仍待加载新逻辑及真人验收。motion.conf校验保持09a62a1d…，两个平滑开关仍0，无GitHub推送。

### 2026-09-29 连续手势退出照明

照明复用当前普通跟随的纯退出序列判断（ExitGestureSequence），各自持有历史，普通跟随的状态转换保持不变。首次张开→握拳只启动；调向中同手3秒内握拳→张开→握拳→张开退出，锁定张开也可作为张开→握拳→张开的起点。第一次张开仅锁定，限时起点可用于近距离恢复调向；最终张开不新增距离限制。停止、丢失、超时、换手/多手歧义、临时动作清除未完成序列，丢失暂停的张开仍无效。

序列完成先保持当前位置，App独立任务核对会话和模式版本，经既有仲裁调用exit_work_light：渐暗关灯、回待机，不恢复人脸或进入机械睡眠。状态读取不触发退出。未新增Tool、接口或Agent提示词；未改模型、运动参数、校准和动作。真人手势验收仍待进行。

本轮验证：本地及Pi5完整216项测试通过，包含12,288组共享序列与旧算法对比、退出只执行一次及睡眠/维护/新会话失效测试。网页JS语法检查通过。已按文件清单同步；检查时Pi5已无正式App/Agent进程，本轮未启动它们，真人连续手势效果待验收。motion.conf校验未变，未推送GitHub。

### 2026-09-29 手势退出照明后恢复交流

`_queue_work_light_exit` 继续由App独立任务协调，经现有锁调用内部 `_exit_work_light_to_conversation`。先核对照明会话/模式版本，暂停唯一语音任务，取消该本地Agent轮次及Tool资格，清理local_reply/wake和旧打断音频；独立通知保留。随后复用 `exit_work_light`，提交一次性 `VoiceResumeRequest`（模式版本、网页epoch、睡眠代次、本地会话ID/轮次）。原语音监督器重启采音，热身完成并再次核对请求有效性后才激活免唤醒监听和完整倒计时。普通语音退出、网页及定时退出均不走此手势专用取消路径。

Voice的 `capture_call` 屏蔽后台线程任务的直接取消：先关闭采音设备，再等待读取线程结束，重复取消也不能跳过收尾。共享/旧式采音均应用；半段音频不迁移。`AnnouncementQueue.pause_for_control` 可选择只中断指定来源，原网页全中断默认行为保持不变；Timer/Alarm不因本次手势丢弃。

实现没有新服务或公开Tool，没有调整Agent提示词、模型、运动/灯光配置、校准和动作。测试与现场验收状态见网页视觉交接最新记录。

### 2026-09-29 照明资源与退出等待优化

Voice新增App持有的单实例KeywordSpotterCache；模型/关键词路径、文件大小和mtime、线程数、score/threshold共同决定复用，识别流每次重新建立，App关闭清理缓存。手势退出使用绑定原照明会话、模式版本、网页epoch和睡眠代次的短暂视觉保留标记，避免关摄像头再开；监听实际就绪、取消、失败或更高优先级模式使标记清除。旧采音线程仍先关闭并等待结束，0.8秒预热、真实监听灯及完整15秒计时保持不变。VOICE_RESUME日志输出旧语音收尾、关灯回待机、模型准备（reused）、采音准备和监听总耗时。

Vision增加内部set_face_detection_enabled，由App将work_light设为False，其余视觉场景为True。照明只跳过YuNet和人脸关联，保留手势与采集；人脸模型按需初建并保留到视觉停止。切换立即清除旧人脸目标和结果，epoch隔离在途结果；重新启用只接受切换后采集的帧。只读状态/网页增加face_detection_enabled，区分停用与无人脸。没有改普通跟随、手势规则、模型文件或运动参数。

本次Pi5隔离短测（无正式App、无舵机控制、不保存图像）：唤醒模型+新流首次1236.57ms，复用0.63/0.50ms；同一视觉进程按人脸开→关→开各预热3秒、测量8秒，CPU66.2%→48.3%→69.0%，推理9.98→9.99→9.98Hz，RSS约269MiB。CPU100%为一个核；是当前画面短测，不承诺其他场景收益。未测得明确结果年龄改善，目的为省计算与减少重复初始化，不提高固定10Hz频率。实际整段退出等待仍需真人正式App验收。

### Todo 自然完成与编号（2026-09-29）

Agent 对用户本人“充电线带好了／水已经喝过了”等表达先查待办，唯一匹配后完成；否定、计划、疑问、转述和歧义不自动完成。网页默认未完成，可切换查看已完成，显示序号从1重排；内部 todo_id 保持稳定，旧数据无需迁移。隔离 Pi Agent 自然语言用例验证完成/不误完成和序号定位；未改用户待办。

Todo/Timer/Alarm 语义修正：明确做完并要求删除/不用提醒时，Agent 根据上下文及列表匹配真实类型，取消 Timer/Alarm 或删除 Todo；仅报告完成待办则保留历史。删除已完成需查询 include_completed=true；明确清理所有已完成只删除完成项。暂不自动清理历史。

### 2026-09-29 首次唤醒及语音告别等待

Pi5 独立计时：首次导入 follower 驱动约 2.116 秒；视觉启动返回立即，但两次停止分别耗时 5.023/5.008 秒。原先驱动在唤醒提示后首次动作才加载，语音结束则在低头前等待视觉停止；网页接管更早关闭视觉，因此表现不同。`LampApp.start()` 现提前调用 `MotionController.prepare_driver()`，只导入，不打开串口或上电；语音结束直接走统一 `app.sleep()`，先停止运动源、完成睡眠动作，再清理视觉。持续照明/跟随超时保留模式，已睡眠情况仍清理视觉。未改运动速度、姿态、校准或视觉实现。

本机完整回归 244 项及 10 项子测试通过，新增测试覆盖驱动预加载不连接硬件、语音休眠先动作后等待视觉清理。代码已选择性同步 Pi5；完整真人唤醒/告别耗时需下次正式启动验收，不能把独立耗时当作实际整轮改善。视觉 stop 仍可能等待约 5 秒，只是移到低头之后；后续若恢复唤醒慢，再与视觉开发核查其退出线程。未推送 GitHub。

### 网页原始像素预览

现有WebConsole增加只读vision/preview接口和单工作线程输出器；Vision新增latest_model_preview，借用已发布只读模型输入图像，不改变latest_preview供显示器使用的接口。网页最多3 FPS、单请求、原始BGR/Canvas绘制，不触发Tool或App接管。缓存和在途数据有界，资源或网络异常只暂停预览。维护、停止、模式或感知代次切换使旧结果失效。详见lelamp/web/README.md与HANDOFF_VISION.md最新记录。

### 2026-09-29 告别工具漏调用提示修正

`AGENT_RUNTIME.md`（当前 Pi 实际加载）与 `AGENTS.md` 加强告别必须先成功调用一次 sleep 再回复，覆盖“拜拜吧你”等语气词；引用、否定、单纯停止播报不休眠。未新增本地规则或改工具/硬件链路。Pi5 隔离 Agent 使用当前模型及真实工具定义、假 Control API 验收26/26：四种告别各三次、否定引用、普通聊天、点头、情绪、照明、Timer、Todo、条件为假不建提醒和休眠失败如实回复。原始记录 `benchmarks/results/farewell_prompt_20260929.json`；工具模拟不代表实机验收，也不保证概率模型永不漏调用。本机252项及10项子测试通过。提示文件每轮请求重读，同步后无需重启现有app/Agent；未推送GitHub。

### 2026-09-29 Edge TTS 无声等待诊断

见 `benchmarks/tts/REPORT_20260929.md` 与同目录复现命令/原始记录。Pi5无声测试16请求：45/90秒闲置连接失效但closed仍False；35/45秒生产重试可恢复。未复现120秒卡顿，不能把闲置失效断言为唯一原因。已核实aiohttp默认WebSocket接收无限等待，现有HTTP sock_read=30不生效。本轮只加默认开启的EDGE TRACE阶段/字节计数（EDGE_TTS_DIAGNOSTICS=0关闭），不改正式行为；代码选择性同步Pi5，用户App未重启，下次启动加载诊断日志。

### 2026-09-29 Edge TTS 连接及超时修复

仅修改voice下Edge TTS及TTS回退判断：闲置预连接20秒有效，使用时按需重建；预连接调用不会关闭正在合成的长句连接。WebSocket首个音频包限5秒，后续音频/turn.end按已有30秒配置，元数据不刷新期限。无PCM输出的失败沿用一次重试；已提交PCM后的失败禁止重播或远程回退，主动打断不算故障。参数中文说明在voice.conf；保留阶段诊断。

本机261项及10项子测试通过。Pi5真实Edge、解码、ALSA null无声8次全部成功，覆盖20/30/35/45秒旧连接与新连接长句；超龄连接发送前重建。Pi5模拟无音频、使用真实解码/播放子进程约5.13秒退出且无残留；故障单测覆盖缺失结束、部分播出、主动取消和后续恢复。记录见benchmarks/tts/fixed_results_20260929.json与fixed_trace_20260929.log。未测试真实扬声器/AEC，未修改Agent/动作/视觉/睡眠。代码及配置选择性同步，用户App未重启，新代码下次启动生效；未推送GitHub。
