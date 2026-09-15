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

验证过普通 OpenClaw 请求可以返回文本，且推理 token 已显著下降。为诊断而启动的 Pi5 `lelamp.app` 当前已停止；需要继续现场测试时手动启动它。OpenClaw Gateway 本身保持运行。

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

## 联网搜索（2026-09-14 接入，语音链路未验证）

功能已接通，**但完整语音链路一次都没跑过**——最后一步需要人在灯旁实测，见本节末尾。

- 能力在 Agent 层，`lelamp/` 里没有任何搜索代码；`lelamp/agent/openclaw.py` 一行未改。
- 当前服务：自建 `http://8.159.128.22:3000/mcp`，streamable-http，**无需 Key**，聚合 Bing/Baidu/Sogou 等引擎。与 LLM provider 同机、内网直连。
- 备用智谱服务已配置但 `enabled: false`（账号欠费，充值后 `openclaw mcp configure zhipu-web-search-sse --enable` 可恢复，但要先停用自建那个，别同时开两个）。
- **配置只在 Pi5 的 `~/.openclaw/openclaw.json`**（`mcp.servers` + `tools.alsoAllow`），本仓库里没有。重建步骤见 `LELAMP_COMMANDS.md` §12；真实密钥见本机 `LELAMP_SECRETS.local.md`。
- 工具名带服务名前缀：`web-search__search`。**漏加 `alsoAllow` 就完全看不见工具。**
- `AGENTS.md` 第 6 条（实时信息先搜索，失败才说拿不到）、第 20 条（列表类问题只讲一条，用户追问"再说几条"才展开）。注意第 18、19 条是 Timer 规则，不要插队改号。

实测方式（与 `lelamp/agent/openclaw.py` 发出的请求完全一致，不占串口和麦克风，不需要启动 app）：

```bash
set -a; . ~/lelamp_runtime/.env; set +a
curl -sS -X POST http://127.0.0.1:18789/v1/chat/completions \
  -H "Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN" \
  -H "x-openclaw-agent-id: lelamp" -H 'Content-Type: application/json' \
  -d '{"model":"openclaw/lelamp","messages":[{"role":"user","content":"今天上海天气怎么样？"}],"user":"lelamp-test","stream":false,"max_tokens":512}'
```

实测结果：天气 8.8 s 有具体数据；交通 7.8 s 如实说拿不到；科技新闻 20.3 s 正常。

已知问题：

- **实时路况查不到。** 网页搜索给不了实时交通数据，老灯会如实拒绝，这是正确行为；要真做需接地图 API。
- **整轮延迟波动 8–38 秒**，语音静默期偏长。
- `OPENCLAW_MAX_TOKENS`（512）是**每次模型调用**的上限，不是整轮上限；整轮 completion 累计可超 512，属正常，不要为它调参。
- 两个易错点：配置键是 `mcp.servers`（**不是** `mcpServers`）；transport 必须 `streamable-http`（服务名里的 `-sse` 是误导）。`alsoAllow` 是数组、**整体替换**，patch 时要带全量，否则会丢掉已有工具。

**待做**：启动 `lelamp.app`，人对着灯说"小灯，今天天气怎么样"，看终端日志里的 KWS/ASR/LLM/TTS 各阶段延迟和实际播报内容。重点确认搜索的 8～38 秒静默期会不会误唤醒、长回答会不会被 TTS 截断。

**未提交**：`AGENTS.md` 的改动还在工作区，用户要求暂不推 GitHub。

### 云端关闭思考修正（2026-09-14）
Pi5 的 `~/.openclaw/openclaw.json` 中，设置
`agents.defaults.models["lelamp-qwen/qwen3.7-flash"].params.extra_body.enable_thinking = false`。
这会通过 OpenClaw 自带 extra_body 转发到云端；仅配置 `reasoning:false` 或显示 `thinking=off` 不足以关闭此代理服务的思考输出。无需改 OpenClaw 源码。修改后重启 Gateway，等待端口真正就绪。
14 条完整 Agent 请求验证：计时器、点头摇头、办公模式/姿态/色调/亮度、情绪、天气新闻和告别均调用成功，usage 未再出现 reasoning tokens。情绪规则未修改。一次样本：天气 4.03 秒、新闻两次搜索 10.27 秒；点头/摇头总计约9秒，其中运动工具约6秒。不要将动作播放时长误判为模型延迟。测试用计时器已取消。
