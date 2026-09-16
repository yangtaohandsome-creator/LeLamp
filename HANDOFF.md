# LeLamp Runtime 交接

本机同步 Pi 必须使用 `scripts/sync_to_pi.sh`。`lelamp/recordings/` 是 Pi 端用户数据，禁止用普通全目录 rsync 覆盖；脚本同时保护密钥、状态、模型、诊断和校准数据。
录制命令覆盖同名动作前，会把旧 CSV 自动保存到 `lelamp/recordings/backups/`。

提示音基础设施位于 `lelamp/audio/`，配置为 `sound.conf`。当前启用用户选定的 `WAKE-B`、`TIMER-B`、`ALARM-B`；提示音与 TTS 共用 `AnnouncementQueue`。

## 先看这些文档

- 总体架构与模块边界：`ARCHITECTURE.md`（项目上层的 `archetecture.md` 是设计来源）
- 可复制的 Pi5 命令、配置和硬件注意事项：`LELAMP_COMMANDS.md`
- 人格、身份、Agent 规则：`SOUL.md`、`IDENTITY.md`、`AGENTS.md`
- 语音参数：`voice.conf`
- 运动参数和睡眠姿态：`motion.conf`
- 灯光参数：`lighting.conf`
- OpenClaw 工具插件：`openclaw-plugin/`
- Pi Agent Core 适配器：`pi-agent/`；与 OpenClaw 共用全部高层 Tool，通过 `AGENT_BACKEND` 切换。当前正式后端是 Pi Agent + 官方 DeepSeek `deepseek-flash`；对比结论及原始结果见 `benchmarks/results/AGENT_COMPARISON.md`。

不要重新设计架构；继续让 `lelamp/app.py` 做总协调器，硬件模块不直接互相控制。

## 当前设备与运行位置

- Pi5：`lamppi@192.168.40.77`
- Pi5 runtime：`/home/lamppi/lelamp_runtime`
- 串口：`/dev/ttyACM0`；灯 ID：`lamppi`
- ReSpeaker 声卡：`seeed2micvoicec`
- 当前唤醒词：`小灯`，词表：`keywords_xiao_deng.txt`
- 当前入口：`uv run --no-sync -m lelamp.app`
- 本机工作副本：`/home/yangyue/LeLamp/lelamp_runtime`

代码、配置和文档通常先在本机修改；用户已经明确要求“修改后自动同步 Pi5”，但不要自动推送 GitHub。同步前保留 Pi5 上用户自己的录音、校准文件和密钥。

## 已实现功能

- KWS → VAD → ASR → OpenClaw Agent → 高层 Tool → TTS 的完整语音流程。
- 唤醒后连续对话；15 秒没有有效文字后机械睡眠、释放扭矩，但程序和 KWS 继续运行。
- `sleep()` 是统一机械睡眠出口；普通临时动作结束回待机或恢复持续模式。
- WORK_LIGHT：高/低姿态、白光/暖黄光、50/75/100% 亮度；Agent 工具为 `enter_work_light`、`update_work_light`、`exit_work_light`。
- 表情动作：`happy_wiggle`、`excited`、`sad`、`shy`、`shock`、`nod`、`headshake`、`curious`。自主表情应通过 `queue_expression`，在 TTS 首包时执行；用户明确要求的动作走 `play_motion`。
- 局域网自然语言入口：`POST /api/v1/agent/text`，由 `lelamp.app` 提供，复用同一 Agent、工具和播报流程；不需要网站。
- 通用多 Timer：`lelamp/timer/` 提供创建、暂停、继续、取消、加时和查询；`on_complete` 保存固定白名单高层 Tool，`agent_task` 保存到期后需要重新搜索、判断或组合工具的自包含任务，二者互斥。到期均由 `LampApp` 排队协调，TimerManager 本身保持硬件无关，可供后续 Focus Mode 直接复用。
- 持久 Alarm：`lelamp/alarm/` 管理带时区的绝对时间，支持单次、每天、工作日、每周指定星期及重启恢复；停机期间错过不补播。城市与 IANA 时区来自 `runtime_state/location.json`，实时定位失败时复用上次成功配置。Alarm 和 Timer 共用 `LampApp` 完成队列及高层 Tool/延迟 Agent 执行路径。
- 统一播报：`lelamp/voice/announcement.py` 是 `LampApp` 持有的单消费者队列。本地语音、远程文本、Timer、Alarm 以及后续通知都从这里进入 TTS；用户说话期间不插播，纯文本提醒可合并，Tool/Agent/情绪事件保持独立。它不是通用 Event Bus，运动仲裁仍由 `LampApp` 负责。
- OpenClaw 插件已注册高层 LeLamp 工具，插件只访问 Pi5 的控制接口，不直接访问舵机参数。

## 最近诊断与修复

Pi5 的 OpenClaw 曾出现两类问题：

1. OpenClaw 配置的 `tools.alsoAllow` 漏掉了 `lelamp_queue_expression`，导致 Agent 实际只能看到 `play_motion`。已在 Pi5 配置中加入该工具，并同步加强了 `AGENTS.md` 的语义映射。
2. Qwen 曾把 512 个 completion token 全耗在隐藏思考，产生空 assistant、OpenClaw `non_deliverable_terminal_turn`/HTTP 500。已在 Pi5 的自定义 provider 模型配置中设置 `compat.thinkingFormat = "qwen"`，使关闭思考真正发送 Qwen 的禁用参数。

已补充的期望映射：身份确认→`nod`，反驳/被损/能力不足→`headshake`，引出故事→`curious`，被夸→`shy`，好消息→`happy_wiggle`/`excited`。

Pi5 当前正式运行 Pi Agent + 官方 DeepSeek `deepseek-flash`，入口为 `lelamp.app`；OpenClaw Gateway 已停止并取消自启动，但配置和 `.env.qwen.backup` 保留，必要时可切回。切换后已用局域网文本入口完成一次真实播报验证。

## 当前已知限制

- 自主表情依赖 Agent 是否选择工具；提示词已加强，但不是硬编码分类器。后续若要求每类语句稳定触发，应增加轻量意图/表情路由器，输出仍复用同一高层工具。
- 云端模型偶发 500 时，语音端会播报“脑子暂时连不上”；需继续观察 Gateway 日志和 OpenClaw transcript，先确认是 provider 失败还是工具失败。
- 摄像头 tracking、reading 等持续模式目前只有架构/模拟交接，真实视觉功能尚未完成。
- OpenClaw 配置含 token，只存在 Pi5 的 `.env`/`~/.openclaw/openclaw.json`，绝不能写入 Git 或交接文档。

## 继续开发前的最小检查

```bash
cd /home/yangyue/LeLamp/lelamp_runtime
uv run --no-sync python -m unittest discover -s tests -v
ssh lamppi@192.168.40.77
cd /home/lamppi/lelamp_runtime
uv run --no-sync -m lelamp.app
```

真实测试时不要同时运行 `test_rgb`、`record`、`replay` 或另一份 app；RGB 和串口都需要单一占用者。修改运动、灯光、语音前先看对应 `.conf`，不要覆盖 Pi5 的校准文件、录音和模型目录。

## 联网搜索

- Agent 只看到固定插件工具 `lelamp_web_search`；它在 `openclaw-plugin/src/search.ts` 中按需调用远程 MCP。
- 主源是 Dashscope WebSearch，失败后自动切换自建 `http://8.159.128.22:3000/mcp`；每源超时 6 秒，连接在 Gateway 进程内复用。
- OpenClaw 原生 `mcp.servers` 全部禁用，避免每次普通请求重新发现远程工具造成 `bundle-tools` 约 15 秒延迟。
- 地址和 Token 只存在 Pi5 的 `~/.openclaw/openclaw.json` 插件配置，不在仓库。`tools.alsoAllow` 只放行 `lelamp_web_search`，旧 MCP 工具名不要加回。
- 搜索规则在 `AGENTS.md` 第 6 条；每轮最多 4 次。安装、验证和排错命令见 `LELAMP_COMMANDS.md` §12。

### 云端关闭思考修正（2026-09-14）
Pi5 的 `~/.openclaw/openclaw.json` 中，设置
`agents.defaults.models["lelamp-qwen/qwen3.7-flash"].params.extra_body.enable_thinking = false`。
这会通过 OpenClaw 自带 extra_body 转发到云端；仅配置 `reasoning:false` 或显示 `thinking=off` 不足以关闭此代理服务的思考输出。无需改 OpenClaw 源码。修改后重启 Gateway，等待端口真正就绪。
14 条完整 Agent 请求验证：计时器、点头摇头、办公模式/姿态/色调/亮度、情绪、天气新闻和告别均调用成功，usage 未再出现 reasoning tokens。情绪规则未修改。一次样本：天气 4.03 秒、新闻两次搜索 10.27 秒；点头/摇头总计约9秒，其中运动工具约6秒。不要将动作播放时长误判为模型延迟。测试用计时器已取消。
### 本地 TTS 候选部署（2026-09-16）

- Pi5 已新增独立目录 `~/tts_local_candidates/`，不改现有 runtime 的远程 TTS 配置，也未改变正在运行的 app。
- MOSS-TTS-Nano 代码在 `~/tts_local_candidates/moss/src`，独立环境为 `~/tts_local_candidates/moss/.venv`；TTS ONNX 模型和 Audio Tokenizer 已下载到 `~/tts_local_candidates/moss/models/`，总占用约 907MB。尚未接入 LeLamp 播报队列。
- Supertonic 3 因公开语言列表没有中文已从 Pi5 删除，释放约 530MB。
- MOSS 的 ONNX 入口是 `src/infer_onnx.py`；Pi5 候选环境已补齐 numpy、ONNX Runtime、sentencepiece、torch/torchaudio 等依赖。使用 `--disable-wetext-processing` 成功生成中文 WAV；常驻同一进程加载一次模型后，3.44 秒音频合成约 16.96 秒，0.88 秒短句约 4.98 秒，6.40 秒长句约 26.50 秒，暖机 RTF 约 4～5.7，当前仍不适合实时对话。试听文件在 `~/tts_local_candidates/moss/samples/warm_0.wav`、`warm_1.wav`、`warm_2.wav`。
- 已在 `~/tts_local_candidates/sherpa/` 独立部署两套轻量中文候选，均未接入系统，也未自动播放：Matcha icefall zh-baker 与 Piper `zh_CN-huayan-medium`。共用 sherpa-onnx 1.13.8 环境，4 线程、22.05kHz。
- Matcha 暖机后：普通句 0.632 秒生成 3.878 秒音频（RTF 0.163），短句 0.157/0.826 秒（0.190），长句 0.706/6.027 秒（0.117）；加载 2.567 秒。试听文件为 `~/tts_local_candidates/sherpa/samples/matcha_1.wav` 到 `matcha_3.wav`。该模型训练数据集注明仅限非商业用途。
- Huayan 暖机后：普通句 0.517 秒生成 3.738 秒音频（RTF 0.138），短句 0.159/0.803 秒（0.198），长句 0.838/5.886 秒（0.142）；加载 1.798 秒。试听文件为 `~/tts_local_candidates/sherpa/samples/huayan_1.wav` 到 `huayan_3.wav`。
- 当前优先试听 Matcha（质量候选）和 Huayan（低延迟、MIT 模型仓库候选）；待人工试听后再决定是否新增 TTS backend。测试脚本位于 Pi5 `~/tts_local_candidates/sherpa/benchmark.py`，只生成 WAV，不调用 `aplay`。
- 后续又部署了 MeloTTS 中文 ONNX 与 ZipVoice Distill INT8，仍未接入正式系统、未自动播放。MeloTTS 加载 4.607 秒；暖机后普通句 3.061/3.218 秒音频（RTF 0.951），短句 0.826/0.805 秒（1.026），长句 4.670/5.410 秒（0.863）。试听文件为 `samples/melo_1.wav` 到 `melo_3.wav`。
- ZipVoice 使用官方新闻女声作为参考音色、4 steps；加载 2.479 秒，但暖机后普通句 15.892/3.780 秒音频（RTF 4.205），短句 10.364/0.768 秒（13.495），长句 20.915/6.624 秒（3.157），不适合 Pi5 实时对话。试听文件为 `samples/zipvoice_1.wav` 到 `zipvoice_3.wav`。
- Edge Neural TTS 的试听和原型仍保留在 `~/tts_local_candidates/edge/`。正式 runtime 已接入可配置的 Edge Xiaoxiao 后端：唤醒后预连接 WebSocket，回答到达后将 MP3 交给 `mpg123` 流式解码并送入 `aplay`；失败快速重试一次，仍失败或预连接未就绪时只回退同事的远程 TTS，不使用本地模型。可用 `TTS_BACKEND=edge|remote` 切换，`TTS_FALLBACK_BACKEND=remote` 控制回退。Pi5 静音实测，预连接 Edge 文本到首个 PCM 约 0.19 秒；未预连接时正确回退远程服务，首包约 0.38 秒。固定回答缓存没有接入，因为 Agent 输出并不固定。
- Edge Xiaoxiao 当前语速为 `EDGE_TTS_RATE=+10%`。Pi Agent 会在 app 日志输出每次模型调用的首字、完整响应、每个 Tool 和总耗时；2026-09-16 三次无 Tool 实测首字 0.94～1.18 秒、完整响应 1.31～1.44 秒。
- 模型下载使用 `HF_ENDPOINT=https://hf-mirror.com`；模型目录不要提交 Git。最终接入应只新增 TTS backend，继续复用 `AnnouncementQueue`，远程 TTS 先保留可回退。

### 其他已完成能力与决策

- 运动与朝向：`lelamp_turn_base(direction, steps)` 每格 30°；逻辑累计范围当前为相对最初正面左右各 60°。方向符号由 `MOTION_BASE_YAW_LEFT_SIGN` 控制，状态写入 `runtime_state/motion_heading.json`。`lelamp_set_base_heading(left|front|right)` 支持绝对朝向，`lelamp_reset_base_heading()` 清除累计偏移。旋转后的朝向会作为待机、照明和录制动作的中立基准，不修改舵机校准文件；base yaw 原始校准仍只覆盖左右约 90°，扩大逻辑范围前必须重新确认机械安全边界。
- 已记录并保留用户动作 CSV：`happy_wiggle`、`excited`、`sad`、`shy`、`shock`、`nod`、`headshake`、`curious`，以及 `idle`、`wake_up`、`rotation`、`scanning` 等；覆盖同名动作前会备份到 `lelamp/recordings/backups/`，禁止用同步脚本覆盖 Pi5 用户录音。
- 运动安全：睡眠姿态、待机姿态、高/低阅读照明姿态均在 `motion.conf`；`sleep()` 移动到睡眠姿态、保持 `MOTION_SLEEP_HOLD_SECONDS` 后释放扭矩。普通动作结束回待机或恢复持续模式，不自动 sleep；从释放扭矩进入首帧用 `MOTION_STARTUP_TRANSITION_SECONDS`，已上电切换用 `MOTION_ACTIVE_TRANSITION_SECONDS`。
- 灯光：RGB 使用 Pi5 专用 `rpi-ws281x` 驱动路径；灯板曾因电源线断线无法显示，硬件修复后才可判断灯效。灯光状态与机械睡眠解耦；`lighting.conf` 保存工作照明白光/暖黄光、亮度和高低姿态参数，默认 WORK_LIGHT 为高姿态、白光、75%。
- Agent 迁移：原先使用 OpenClaw + Qwen（配置备份仍在 Pi5），后部署 Pi Agent Core 并完成对比后切换为正式后端 `AGENT_BACKEND=pi`，模型改为 DeepSeek 官方 OpenAI 兼容接口 `deepseek-flash`。Pi Agent 只监听 `127.0.0.1:18792`，仍通过 `LampApp` 的统一高层 Tool 和 Control API 控制硬件；不要让 Agent 直接接触串口、舵机角度或 RGB。
- DeepSeek 配置位置：模型地址、模型名和非密钥参数在 `voice.conf`；API key、Pi Agent token、控制接口 token 只在 Pi5 `.env`。不要把任何密钥写入文档、代码或 Git。
- 远程自然语言入口：同事可直接 POST Pi5 `http://<pi-ip>:18791/api/v1/agent/text`，由 app 进入同一 Agent、Tool 和 AnnouncementQueue；不需要先建网站。请求字段是 `request_id`、`session_id`、`text`、可选 `locale`，返回 `answer`、`status`、`spoken`。真实地址和 Bearer token 只从 Pi5 `.env` 获取，不在交接文档记录。
- 搜索与天气：Agent 通过固定 `lelamp_web_search` 按需搜索，主源为 DashScope WebSearch，备用为自建 MCP；每源 6 秒、每轮最多 4 次。app 启动时一次性用公网 IP 查询城市和 IANA 时区并缓存到 `runtime_state/location.json`，天气请求不重复定位。
- 提醒能力：Timer 是内存多实例，Alarm 是持久绝对时间提醒；二者都可保存到期高层动作或 `agent_task`，到期由 app 重新执行 Tool/Agent。Timer 重启清空，Alarm 从 `runtime_state/alarms.json` 恢复；完成提醒、远程回答和本地回答都走统一播报队列，队列不打断已开始的播报。
