# LeLamp 常用测试与动作命令（lamppi）

更新：2026-09-14。按新架构入口整理；本文件在项目根目录、runtime 和 Pi5 保持一致。

> **密钥位置**：本文件不写任何 API Key / Token 明文，所有凭据统一存放在本机的 [LELAMP_SECRETS.local.md](../LELAMP_SECRETS.local.md)。
> 该文件不在 Git 仓库内，所以在 GitHub 网页上这个链接打不开——这是有意的，不是坏链。

## 1. 固定配置与进入项目

| 项目 | 当前值 |
| --- | --- |
| SSH | `lamppi@192.168.40.77` |
| 项目目录 | `/home/lamppi/lelamp_runtime` |
| 灯的 ID | `lamppi` |
| 舵机串口 | `/dev/ttyACM0` |
| 默认声卡 | `seeed2micvoicec` |
| RGB | Pi 5 专用驱动，GPIO12，64 颗 LED |

灯 ID 已根据现有的 follower、leader 校准文件 `lamppi.json` 确认，不是仅按用户名猜测。

在电脑上连接：

```bash
ssh lamppi@192.168.40.77
```

登录后先执行一次；以下测试命令均在这个目录运行：

```bash
cd /home/lamppi/lelamp_runtime
```

`--no-sync` 使用已经安装好的环境，不在每次测试时重新同步依赖。普通命令无需 sudo。Pi5 已为 `/dev/ws281x_pwm` 配置 `gpio` 组权限，RGB 状态灯和 RGB 测试可直接由 `lamppi` 用户运行。

### 新架构命令对应关系

| 功能 | 当前入口 |
| --- | --- |
| 完整语音与台灯应用 | `lelamp.app` |
| 录制 | `lelamp.motion.record` |
| 回放 | `lelamp.replay`（内部已接入 app/Motion） |
| 睡眠 | `lelamp.sleep`（内部已接入 app/Motion） |
| 音频、RGB、舵机、VAD 测试 | 原 `lelamp.test.*` 入口继续使用 |
| 校准、居中、列出动作 | 原维护命令继续使用 |

`lelamp.voice_assistant` 和 `lelamp.record` 保留兼容；以下启动和录制示例统一使用新入口。不要直接执行 `motion.controller` 等实现模块，它们没有命令行入口。

### 远程自然语言接口

`lelamp.app` 同时提供局域网文本入口 `POST /api/v1/agent/text`。远程文本跳过 KWS、VAD 和 ASR，之后复用同一个 OpenClaw Agent、高层 Tools、动作仲裁和 TTS 播报。无需部署网站，也不要向局域网公开 OpenClaw Gateway 或底层 Tool 端口。

远程接口监听地址和端口由 `voice.conf` 的 `LELAMP_REMOTE_BIND`、`LELAMP_REMOTE_PORT` 设置；独立 Bearer Token 只放在 `.env` 的 `LELAMP_REMOTE_TOKEN`，值见 [本机凭据汇总](../LELAMP_SECRETS.local.md)。相同 `request_id` 的重试返回缓存结果，不重复执行动作；同一对话持续复用 `session_id`。

## 2. 麦克风和扬声器

### 完整音频测试

```bash
uv run --no-sync -m lelamp.test.test_audio
```

依次播放 3 秒提示音、录音 3 秒、回放录音。看到 `Recording from microphone...` 时说话。

### 只测试扬声器

```bash
speaker-test -D plughw:seeed2micvoicec,0 -c 2 -t sine -f 440
```

循环播放测试音，按 Ctrl+C 停止。

### 查看播放、录音设备和调音量

```bash
aplay -l
arecord -l
alsamixer -c seeed2micvoicec
```

alsamixer 内左右键选通道、上下键调音量、M 切换静音、Esc 退出。调整完成后保存：

```bash
sudo alsactl store seeed2micvoicec
```

## 3. RGB 灯板

```bash
sudo /home/lamppi/.local/bin/uv run --no-sync -m lelamp.test.test_rgb
```

测试红、绿、蓝、彩色图案、优先级，最后熄灭。驱动已修复，红绿蓝已现场确认，官方完整测试已通过。

查看驱动服务：

```bash
systemctl status lelamp-rgb-driver.service --no-pager
journalctl -u lelamp-rgb-driver.service -b --no-pager
```

若服务未启动：

```bash
sudo systemctl start lelamp-rgb-driver.service
```

维护记录：`/home/lamppi/lelamp-drivers/RGB-REPAIR.md`。

### 语音状态灯光

启动新应用后，灯光会按语音阶段自动提示：暖橙表示等待“老灯”，青蓝表示监听，紫色表示思考，暖黄色表示播报，绿色短闪表示本轮完成，红色表示异常。状态灯由 `lelamp/lighting/controller.py` 统一管理，语音流程不直接写 RGB 数值。

```bash
uv run --no-sync -m lelamp.app
```

灯光状态测试不需要单独启动服务；用上面的应用入口测试完整流程。硬件 RGB 自检仍使用 `test_rgb`，不要与语音应用同时运行。若系统重装后设备权限恢复为 `root root`，重新加载 `/etc/udev/rules.d/99-ws281x-pwm.rules`，或暂时使用 `sudo`。

### 书桌办公照明 WORK_LIGHT

普通聊天中直接对老灯说“帮我照桌面”即可进入办公照明。默认移动到高照明姿态，使用中性偏暖白光，亮度 75%。办公模式会保持姿态、扭矩和主照明，语音阶段灯效不会覆盖它。

需要低姿态或暖黄光时，在进入时明确说明；进入后可以说“亮一点”“暗一点”“切换高/低姿态”“换白光/暖黄光”。亮度每次调整 25%，范围为 50%～100%。这些调整只在本次办公模式中有效。

说“关灯”或“退出办公模式”会渐暗并回到普通待机姿态；说“拜拜”会先播报告别，再进入统一睡眠姿态并释放扭矩。办公模式中 15 秒没有有效文字只会结束当前对话，照明继续保持。

对应高层 Agent Tools 为 `enter_work_light`、`update_work_light`、`exit_work_light`，均由 `app.py` 统一协调，OpenClaw 不直接操作舵机或 RGB。

参数位于 runtime 根目录的 `lighting.conf`：

```ini
OFFICE_LIGHT_R=255
OFFICE_LIGHT_G=220
OFFICE_LIGHT_B=180
OFFICE_LIGHT_BRIGHTNESS_PERCENT=75
WORK_LIGHT_FADE_SECONDS=0.8
```

亮度参数按相同比例缩放三个颜色通道，不改变灯光色温。

暖黄照明参数：

```ini
WARM_LIGHT_R=255
WARM_LIGHT_G=140
WARM_LIGHT_B=40
WARM_LIGHT_BRIGHTNESS_PERCENT=100
```

办公模式通过 `LightingController.work_light()` 调用；暖黄参数只作为办公模式的色调选项，不会改变下次进入时的默认白光配置。
系统内核升级后，安装匹配的新内核头文件，再运行：

```bash
bash /home/lamppi/lelamp-drivers/rebuild-rgb-module.sh
```

## 4. 串口与舵机测试

### 查看串口

```bash
ls -l /dev/ttyACM0
ls -l /dev/serial/by-id/
```

如以后编号变化，可以重新查找：

```bash
uv run --no-sync lerobot-find-port
```

查找程序会提示拔插控制板 USB；当前端口已知，不必每次查找。

### 舵机功能测试（会运动）

```bash
uv run --no-sync -m lelamp.test.test_motors --id lamppi --port /dev/ttyACM0
```

这个程序会尝试播放录制目录中的第一个动作，并非只读检测。运行前给灯头留出活动空间，不要同时运行另一份 record、replay 或语音控制程序。

## 5. 录制自己的动作：record

### 录制一段测试动作

```bash
uv run --no-sync -m lelamp.motion.record --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 30
```

1. 等待连接完成，看到提示后按 Enter 开始。
2. 手动引导灯做动作，避免强推到机械限位。
3. 按 Ctrl+C 结束录制。

保存到：

```text
/home/lamppi/lelamp_runtime/lelamp/recordings/my_first_motion.csv
```

**同一个 name 会覆盖已有 CSV。** 下次录不同动作只改 `--name` 后的名字即可，ID 和串口不用改；名字不带 `.csv`。建议给自录动作加 `my_` 前缀，避免覆盖仓库自带动作。

## 6. 回放自己的动作：replay

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 30
```

正常播完一次后进入睡眠姿态、释放扭矩并退出。回放会使舵机主动运动；首次上力时会从当前姿态平滑移动到录制的第一帧。

启动缓动参数位于 `~/lelamp_runtime/motion.conf`：

```ini
# 从瘫痪的当前位置进入第一次动作首帧所需时间（秒）；越大越慢，0 表示立即进入。
MOTION_STARTUP_TRANSITION_SECONDS=1.5

# 已保持扭矩时，从当前姿态进入动作首帧所需时间（秒）。
MOTION_ACTIVE_TRANSITION_SECONDS=0.5

# 缓动控制帧率，通常保持 30。
MOTION_TRANSITION_FPS=30

# 动作结束后进入睡眠姿态所需时间（秒）。
MOTION_SLEEP_TRANSITION_SECONDS=1.5

# 到位后保持扭矩的时间（秒），随后断开。
MOTION_SLEEP_HOLD_SECONDS=1.0

# 唤醒后和普通动作完成后的待机姿态（待机时保持扭矩）。
MOTION_STANDBY_BASE_YAW=0.5826983415508664
MOTION_STANDBY_BASE_PITCH=-44.990548204158785
MOTION_STANDBY_ELBOW_PITCH=58.46221829429962
MOTION_STANDBY_WRIST_ROLL=47.634322373696875
MOTION_STANDBY_WRIST_PITCH=-1.3968775677896446

# 阅读照明模式姿态（当前仅保存，暂未自动切换）。
MOTION_READING_BASE_YAW=-1.56880322725236
MOTION_READING_BASE_PITCH=-23.345935727788287
MOTION_READING_ELBOW_PITCH=28.501988510826322
MOTION_READING_WRIST_ROLL=99.03769045709703
MOTION_READING_WRIST_PITCH=-2.2185702547247246

# 低照明阅读模式姿态。
MOTION_READING_LOW_BASE_YAW=-1.2998655311519514
MOTION_READING_LOW_BASE_PITCH=-39.79206049149339
MOTION_READING_LOW_ELBOW_PITCH=86.7432611577552
MOTION_READING_LOW_WRIST_ROLL=49.71932638331998
MOTION_READING_LOW_WRIST_PITCH=-2.382908792111749
```

修改后，下次启动回放或运动服务时生效。这个时间只控制首次上力到动作首帧的过程，不改变录制动作本身的播放速度。

正常播放结束后，程序会自动移动到 `motion.conf` 保存的睡眠姿态，再释放舵机扭矩。也可以单独执行：

```bash
uv run --no-sync -m lelamp.sleep --id lamppi --port /dev/ttyACM0
```

慢速回放同一段 30 fps 录制：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 15
```

当前代码按指定 fps 逐行发送动作，不按 CSV 的 timestamp 还原节奏。30 fps 录制、15 fps 回放大约是半速；这不会改变首帧的目标姿态。

## 7. 列出和回放已有动作

### 列出动作

```bash
uv run --no-sync -m lelamp.list_recordings --id lamppi
```

当前代码实际上列出整个 `lelamp/recordings/` 下的 CSV，不按 ID 分目录。ID 主要用于选择校准数据。

目前目录中有：`curious`、`excited`、`happy_wiggle`、`headshake`、`idle`、`nod`、`rotation`、`sad`、`scanning`、`shock`、`shy`、`wake_up`。这些是现有文件清单，不代表每段动作都已在本机验证。

点头：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name nod
```

摇头：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name headshake
```

唤醒动作：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name wake_up
```

其他动作仅替换 `--name`，不需要修改灯 ID 或串口。

## 8. 舵机初始化与校准（非日常测试）

目前已存在两套 `lamppi.json` 校准文件。只有首次设置、更换舵机或需要重新校准时才用本节，不要当作每次启动步骤。

### 分配舵机 ID

```bash
uv run --no-sync -m lelamp.setup_motors --id lamppi --port /dev/ttyACM0
```

会写入舵机设置。按程序提示逐个连接指定舵机，不要直接把它当作所有舵机并联后的普通测试。

### 依次校准 follower 与 leader

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0
```

当前用户已有 dialout 权限，使用普通用户运行，以保持校准数据位于 `/home/lamppi/.cache/`；用 sudo 可能改用 root 的校准目录。

当前本机代码还支持单独校准：

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0 --follower-only
```

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0 --leader-only
```

校准文件：

```text
/home/lamppi/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lamppi.json
/home/lamppi/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lamppi.json
```

### 移动到校准中位姿态

先退出应用和其他舵机程序。只读取当前值与校准中位目标：

```bash
uv run --no-sync -m lelamp.move_calibration_center --id lamppi --port /dev/ttyACM0
```

实际移动到中位并保持扭矩，供检查舵盘安装：

```bash
uv run --no-sync -m lelamp.move_calibration_center --id lamppi --port /dev/ttyACM0 --move
```

此维护命令使用自身缓动逻辑，不使用 motion.conf 的启动时长。检查结束后，回到保存的睡眠姿态并释放扭矩：

```bash
uv run --no-sync -m lelamp.sleep --id lamppi --port /dev/ttyACM0
```

## 9. 依赖维护和排错

需要安装或恢复依赖时，普通用户执行：

```bash
uv sync --locked --extra hardware
uv pip check
```

Pi 5 驱动已通过项目配置固定到 `/home/lamppi/lelamp-drivers/rpi-ws281x-python/library`，请保留该目录。

| 现象 | 处理 |
| --- | --- |
| sudo 找不到 uv | 使用上面的完整路径 |
| RGB 报 Hardware revision is not supported | 检查是否被换回旧版驱动，参阅 RGB-REPAIR.md |
| /dev/ttyACM0 不存在 | 检查控制板供电和 USB 数据线，重新查找串口 |
| 找不到录制文件 | 先 list_recordings，name 不带 .csv |
| 校准数据找不到 | 保持 --id lamppi，并在 lamppi 用户下运行舵机命令 |
| 串口忙或舵机通信异常 | 先退出其他占用串口的控制程序 |

## 10. 语音助手：KWS → ASR → LLM → TTS

新主入口（原 `lelamp.voice_assistant` 命令仍可用）：

```bash
cd ~/lelamp_runtime
uv run --no-sync -m lelamp.app
```

完整匹配“点头”“点个头”“摇头”“开灯”“关灯”“睡眠”会执行本地功能；讨论动作或带否定的句子不会被关键词误触发。阅读和跟踪尚未接入实际设备，当前会明确提示未实现。

运动使用 `motion.conf` 的参数；设备默认 `/dev/ttyACM0`、ID 默认 `lamppi`，可用环境变量 `MOTION_PORT`、`MOTION_LAMP_ID` 覆盖。不要同时运行校准、录制或另一份硬件测试。独立 replay 正常结束会进入睡眠；应用中的临时动作恢复持续模式或回待机。语音会话 15 秒没有有效文字后进入睡眠姿态并释放扭矩，但程序、KWS 和暖橙色等待灯效继续运行。

Agent 可以为回答选择一个同步情绪动作：`happy_wiggle`、`excited`、`sad`、`shy`、`shock`、`nod`、`headshake` 或 `curious`。自主表达通过 `lelamp_queue_expression` 登记，在 TTS 第一块音频开始播放时执行；用户明确要求动作时使用即时的 `lelamp_play_motion`。普通回答可以不做动作，每轮最多一个；办公照明默认关闭自主情绪动作。

只检查重构逻辑、不启动麦克风或舵机：

```bash
uv run --no-sync python -m unittest discover -s tests -v
```

常用参数统一修改 runtime 根目录的 `voice.conf`，每项都有中文说明，重启语音助手生效。此文件优先于 `.env` 同名配置；API 密钥仍保留在 `.env`。断句默认使用本地 Silero VAD，并保留 1.0 秒预录缓存；RMS 阈值只作诊断和备用。ASR 默认在 EOF 前快速发送额外 0.5 秒静音，用于补齐识别尾字，不延长本地录音。

Silero VAD 模型路径为 `vad_models/silero_vad.onnx`。缺少时执行：

```bash
mkdir -p vad_models
curl -fL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx \
  -o vad_models/silero_vad.onnx
```

完整语音助手会在 Pi 本地监听唤醒词“小灯”，检测到后录音并通过 ASR 转文字，调用云端 `qwen3.7-flash` 推理，再请求中枢 TTS，最后从台灯扬声器播放回答。

LLM 人格和行为规则采用 OpenClaw 风格文件，位于 runtime 根目录：`IDENTITY.md` 定义身份，`SOUL.md` 定义人格与说话风格，`AGENTS.md` 定义交互规则。语音助手每次调用 LLM 时自动读取这三个文件并组合成系统提示词；修改后重启程序生效。

启动前确认 `~/lelamp_runtime/.env` 已配置 `LLM_BASE_URL`、`OPENAI_API_KEY`、`LLM_MODEL`、`ASR_WS_URL` 和 `TTS_URL`。密钥只放在 Pi 的 `.env` 中，不要提交到 GitHub；各项当前的实际值见 [本机凭据汇总](../LELAMP_SECRETS.local.md)。

连续对话和唤醒参数：

```text
KWS_SCORE=2.0
KWS_THRESHOLD=0.05
CONVERSATION_IDLE_SECONDS=15
CONVERSATION_HISTORY_TURNS=6
```

启动：

```bash
uv run --no-sync -m lelamp.app
```

说“小灯”后再说问题。终端会显示：

```text
WAKE: xiao_deng; listening...
LISTEN END | 延迟: 2.34 秒
ASR: ...
ASR 延迟: 0.86 秒
LLM: ...
LLM 延迟: 1.42 秒
TTS 首包延迟: 0.31 秒
TTS 流传输: 1.10 秒
播放耗时: 2.60 秒
本轮总耗时: 6.81 秒
```

台灯播报完成后会自动进入连续对话，不需要再次说“小灯”。连续 15 秒没有识别到包含汉字、字母或数字的有效文字后，会清空本次上下文并回到等待唤醒状态；噪声触发的空 ASR 或纯标点不会刷新这 15 秒。连续对话期间会保留最近 6 轮问答作为上下文。

停止：

```text
Ctrl+C
```

新应用退出时会停止当前运动；如果连接过舵机，会尝试按配置回睡、保持后释放扭矩。单纯语音运行且没有连接舵机时，不会为了退出而启动舵机。

麦克风使用 ALSA 的 `arecord` 读取 ReSpeaker，扬声器使用 `aplay` 播放。不要同时运行 `test_audio`、其他录音程序或占用声卡的服务。

唤醒词诊断程序会把同一段声音同时送给 ReSpeaker 的 `channel_0`、`channel_1` 和当前默认的 `mean` 三路 KWS，保存三份 WAV 与命中统计；不启动 VAD、ASR、LLM、TTS、灯光或舵机：

```bash
uv run --no-sync -m lelamp.test.test_kws_diagnostic --duration 90
```

在终端显示 `KWS DIAGNOSTIC READY` 后，于 90 秒内自然说 20 次“小灯”，每次间隔约 1 秒。结束后将终端输出和 `voice_debug/kws_diagnostic/` 中最新目录发给我，我会据此选择输入声道和参数。

当前实测 `channel_1=13`、`channel_0=9`、`mean=8`，因此主程序的 KWS 已配置为 `KWS_AUDIO_CHANNEL=1`。该参数只影响唤醒词，VAD 和 ASR 继续使用双声道平均。

旧的单路唤醒词测试命令仍可用于快速检查：

```bash
uv run --no-sync -m lelamp.test.test_kws \
  --model kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01 \
  --keywords kws_models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/keywords_xiao_deng.txt
```

## 11. 语音主程序：后续配置项

语音截断诊断默认开启（`VOICE_DEBUG=1`）。主程序每轮分别保存未补尾的 `*_capture.wav` 和实际发送的 `*_asr_input.wav`，同名 `.jsonl` 保存 ASR 响应。设置 `VOICE_DEBUG=0` 可关闭保存和额外等待。

只测试麦克风和 VAD，不启动唤醒、ASR、LLM 或 TTS：

```bash
uv run --no-sync -m lelamp.test.test_voice_capture --label normal_sentence
```

程序先用 1 秒丢弃声卡启动瞬态并测量环境噪声，随后显示 `TEST READY`。终端每 100ms 显示 RMS、动态启动阈值、当前阈值和连续静音时间。文件保存在 `voice_debug/vad_tests/`：`*_capture.wav` 是原始录音，`*_asr_input.wav` 是追加 ASR 尾部静音后的对照文件，`.csv` 是逐块 VAD 数据。

试听终端打印的录音路径（替换文件名）：

```bash
aplay -D plughw:seeed2micvoicec,0 voice_debug/文件名_capture.wav
```

`main.py` 和 `smooth_animation.py` 保留为旧 LiveKit 示例，不是当前应用入口。日常统一使用 `uv run --no-sync -m lelamp.app`。

## 12. OpenClaw 联网搜索（MCP）

老灯回答天气、新闻等实时信息时，由 OpenClaw 调用 MCP 搜索服务。搜索属于 Agent 层能力，**不经过 `lelamp/` 的 Python 代码**，Pi 上的 app 不需要任何改动；语音入口和局域网文本入口自动共用同一套搜索，不要为它新增 `lelamp_*` 工具或本地网关。

### 可用的搜索后端

| 服务 | 地址 | 鉴权 | 状态 |
| --- | --- | --- | --- |
| 自建 web-search | `http://8.159.128.22:3000/mcp` | 无需 Key | **当前启用** |
| 智谱 Web Search | `https://open.bigmodel.cn/api/mcp-broker/proxy/web-search/mcp` | 需付费账号 | 当前停用（账号欠费） |

自建服务与 LLM provider 同在 `8.159.128.22`，内网直连。它聚合 Baidu、Bing、DuckDuckGo、Sogou、Exa、Brave 等引擎，单个引擎失败会自动降级（返回 JSON 里带 `partialFailures` 字段）。

### 配置位置与易错点

MCP server 配置在 Pi5 的 `~/.openclaw/openclaw.json`，键名是 **`mcp.servers`**（嵌套）。官方文档与社区文档同时存在顶层 `mcpServers` 的写法，在本机 OpenClaw 2026.9.4 上无效，不要照抄。

**上面两个服务实际都用 `streamable-http`。** 智谱那个服务名里带 `-sse`，但用 SSE 连接会被拒绝（HTTP 400）。不要凭服务名猜传输协议，`openclaw mcp add` 会在保存前先探测，猜错不会写进配置。

### 添加自建 web-search（当前生效）

服务端暴露 6 个工具，这里只放行 `search`。其余 5 个是各类文章抓取（GitHub README、CSDN、掘金、linux.do、通用网页），对语音场景是噪声，会白占提示词：

```bash
export PATH="/home/lamppi/.local/bin:$PATH"

openclaw mcp add web-search \
  --url 'http://8.159.128.22:3000/mcp' \
  --transport streamable-http \
  --include 'search' \
  --timeout 20
```

### 添加智谱 Web Search（备用）

需要先给智谱账号充值。Key 到 [本机凭据汇总](../LELAMP_SECRETS.local.md) 的「智谱联网搜索 MCP」一节取：

```bash
export PATH="/home/lamppi/.local/bin:$PATH"
ZHIPU_API_KEY='到凭据汇总文档取智谱 Key'

openclaw mcp add zhipu-web-search-sse \
  --url "https://open.bigmodel.cn/api/mcp-broker/proxy/web-search/mcp?Authorization=$ZHIPU_API_KEY" \
  --transport streamable-http \
  --exclude 'webSearchSogou,webSearchQuark,webSearchPro' \
  --timeout 20
```

> 上面用变量是为了不把 Key 写进命令历史。**真实 Key 只存在 Pi5，不写入本文件、不提交 Git。**

两个服务都配好后，用 `openclaw mcp configure <name> --disable / --enable` 决定哪个生效。**不要同时启用**，否则模型会在两个搜索工具之间犹豫。

`--timeout 20` 把单次搜索限制在 20 秒。默认 60 秒会顶穿 `lelamp/agent/openclaw.py` 里 `httpx` 的 60 秒超时，届时语音端只会播报“脑子暂时连不上”，掩盖真实错误。

### 必须把工具加进白名单

`openclaw.json` 的 `tools.profile` 为 `minimal`，工具靠 `tools.alsoAllow` 放行。**漏加白名单会导致 Agent 完全看不到搜索工具**，与早前 `lelamp_queue_expression` 的问题同源。

工具名带服务名前缀，两个服务分别是 `web-search__search` 和 `zhipu-web-search-sse__webSearchStd`：

```bash
openclaw config patch --stdin <<'JSON'
{"tools": {"alsoAllow": ["lelamp_play_motion", "lelamp_set_light", "lelamp_enter_work_light", "lelamp_update_work_light", "lelamp_exit_work_light", "lelamp_sleep", "lelamp_get_robot_state", "lelamp_queue_expression", "lelamp_create_timer", "lelamp_pause_timer", "lelamp_resume_timer", "lelamp_cancel_timer", "lelamp_add_timer_time", "lelamp_get_timer_remaining", "lelamp_list_timers", "lelamp_create_alarm", "lelamp_cancel_alarm", "lelamp_get_alarm", "lelamp_list_alarms", "web-search__search", "zhipu-web-search-sse__webSearchStd"]}}
JSON
```

`alsoAllow` 是数组，patch 时**整体替换**，必须带上原有全部元素，否则会丢掉已有的 LeLamp 工具。

### 生效与排错

```bash
openclaw mcp list                          # 已配置的 server
openclaw mcp probe --json                  # 探测连接，列出工具名与 timeout
openclaw mcp reload                        # 丢弃 MCP 运行时缓存，下一回合生效
openclaw mcp configure <name> --disable    # 停用
openclaw mcp configure <name> --enable     # 启用
```

改配置不需要重启 Gateway，`config patch` 会热加载。工具不可见时先查白名单，再 `mcp reload`。

日志在 `/tmp/openclaw/openclaw-<日期>.log`。搜索失败的具体原因（鉴权、余额、超时）记在 `[tools] ...` 行内。若该行提示“您的账户已欠费”，那是智谱账号余额问题，与本机配置无关。

### 行为由人格文件约束

`AGENTS.md` 第 6 条规定：实时信息先搜索再回答，搜索失败才说明拿不到，且搜索结果只作要点用一两句话转述，不整段朗读。调整老灯的搜索行为改这里，不必改代码。

### 验证与实测基线

下面这条命令和 `lelamp/agent/openclaw.py` 发出的请求完全一致，是验证真实链路最快的办法——不需要启动 app，不占串口和麦克风：

```bash
set -a; . ~/lelamp_runtime/.env; set +a
curl -sS -X POST http://127.0.0.1:18789/v1/chat/completions \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "x-openclaw-agent-id: lelamp" -H 'Content-Type: application/json' \
  -d '{"model":"openclaw/lelamp","messages":[{"role":"user","content":"今天上海天气怎么样？"}],"user":"lelamp-test","stream":false,"max_tokens":512}'
```

2026-09-14 实测（自建 web-search）：

| 问题 | 耗时 | 结果 |
| --- | --- | --- |
| 今天上海天气怎么样 | 8.8 s | 正常，给出具体天气 |
| 上海的交通状况怎么样 | 7.8 s | 如实说拿不到实时路况 |
| 最近有什么科技新闻 | 20.3 s | 正常，但输出偏长 |

注意 `voice.conf` 的 `OPENCLAW_MAX_TOKENS`（默认 512）是**每次模型调用**的上限，不是整轮上限；OpenClaw 内部会多次调用，整轮 completion 总量可能远超 512。实测天气轮整轮约 350–1150 tokens，不会因此失败。早前“512 全耗在隐藏思考”的问题由 Pi5 侧的 `compat.thinkingFormat` 配置解决，与这个数字无关。

### 已知限制

- **实时路况查不到。** 网页搜索给的是新闻和网页摘要，不是实时交通数据。老灯会如实说拿不到，这是正确行为，不要试图靠提示词让它编一个。
- **宽泛问题输出会变长。** 问“最近有什么科技新闻”时，`AGENTS.md` 要求的一两句话约束会被突破，模型会列清单，语音播报明显变长。需要时在人格文件里进一步收紧。
- **延迟波动大。** 单次搜索约 0.7 秒，但整轮对话 8–20 秒不等，思考灯效期间会静默较久。

## 13. 通用多 Timer

启动 `lelamp.app` 后可直接说“定时25分钟”“还有多久”“暂停第一个计时器”“继续计时”“再加10分钟”“取消计时”或“现在有哪些计时器”。多个目标不明确时，Agent 会先列出计时器再询问。

Timer 仅保存在内存，app 重启后清空。多个 Timer 可以并存；到时后进入统一播报队列，不改变当前姿态或照明模式，也不会打断正在进行的回答。多个同时积压的纯文本提醒会合并播报，带动作或 Agent 任务的提醒仍逐条执行。

Python 功能入口位于 `lelamp/timer/`，Agent 使用 `lelamp_create_timer`、`lelamp_pause_timer`、`lelamp_resume_timer`、`lelamp_cancel_timer`、`lelamp_add_timer_time`、`lelamp_get_timer_remaining` 和 `lelamp_list_timers`。所有时长参数及返回值均以秒为单位。

## 14. Alarm 闹钟

Alarm 用于具体钟表时间，与“25 分钟后”这类 Timer 分开。启动 app 后可以直接说：

```text
明天早上八点提醒我起床
每天晚上十点提醒我休息
工作日下午两点提醒我开会
每周五下午四点半提醒我
现在有哪些闹钟
取消第一个闹钟
```

支持一次、每天、工作日以及每周指定星期重复。闹钟保存于 `runtime_state/alarms.json`，app 或 Pi5 重启后会恢复；关机期间错过的一次性闹钟不会补播，重复闹钟会排到下一次。

所在城市和时区保存在 `runtime_state/location.json`。app 每次启动都会尝试重新定位，成功后覆盖这个配置；如果本次网络定位失败，就继续使用上一次成功保存的城市和时区。

Agent 工具为 `lelamp_create_alarm`、`lelamp_cancel_alarm`、`lelamp_get_alarm` 和 `lelamp_list_alarms`。闹钟到期后进入与 Timer、对话回答共用的统一播报队列；既可播报提醒，也可执行明确指定的高层台灯动作，或重新调用 Agent 完成需要届时查询和判断的任务。

## 来源和验证范围

- [官方 Setup](https://github.com/humancomputerlab/LeLamp/blob/master/docs/4.%20LeLamp%20Setup.md)
- [官方 Control](https://github.com/humancomputerlab/LeLamp/blob/master/docs/5.%20LeLamp%20Control.md)
- [Runtime 仓库](https://github.com/humancomputerlab/lelamp_runtime)
- 模块归属见 `ARCHITECTURE.md`；命令以 app、motion 和保留的 CLI/test 入口为准。
- 本次整理未执行舵机初始化、校准、录制或回放，也未改写已有校准文件。
# 提示音候选试听

试听前先停止语音应用，避免扬声器音效被 KWS/VAD 当成麦克风输入：

```bash
pkill -TERM -f '^/home/lamppi/lelamp_runtime/.venv/bin/python3 -m lelamp.app$'
pkill -TERM -f '^uv run --no-sync -m lelamp.app$'
```

分类试听三组候选：

```bash
cd ~/lelamp_runtime
uv run --no-sync -m lelamp.test.test_sound_candidates wake
uv run --no-sync -m lelamp.test.test_sound_candidates timer
uv run --no-sync -m lelamp.test.test_sound_candidates alarm
```

也可以一次播放全部九个候选：

```bash
uv run --no-sync -m lelamp.test.test_sound_candidates all
```

试听结束后恢复应用：

```bash
cd ~/lelamp_runtime
nohup uv run --no-sync -m lelamp.app >/tmp/lelamp-app.log 2>&1 &
```

候选选择格式示例：`WAKE-B、TIMER-A、ALARM-C`。试听命令只临时播放，不会修改 `sound.conf` 或启用正式提示音。
