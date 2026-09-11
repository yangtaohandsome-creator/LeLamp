# LeLamp Runtime 交接

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

