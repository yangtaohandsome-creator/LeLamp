# Barge-in 固定回归集

这个目录保存测试清单和评测程序说明，音频仍放在 Pi5 的
`~/lelamp_runtime/voice_debug/`，不会提交 Git。

当前 V0 不包含舵机运动样本。2026-09-18 又加入了水瓶移动误触发、短词“停”成功、自然插话丢失和两条完全未触发日志案例。数据分为：

- `no_interrupt`：只有小灯播报和现场环境声，预期不打断。
- `false_interrupt`：正式运行中已确认的误打断，触发后 ASR 为空。
- `correct_interrupt`：真人插话被正确保留并识别。
- `missed_interrupt`：检测到了插话，但保存的语音或 ASR 结果不完整。
- `needs_segmentation`：旧的长时间真人双讲实验，保留在清单中但暂不计分。
- `log_only`：已知漏触发事实，但当时没有连续保存音频，只登记而不参与自动计分。

清单中的 `evaluation` 有两种主要类型：

- `vad_replay`：可以把保存的 AEC clean 音频重新送入当前 Silero VAD，比较参数。
- `retention_record`：用于统计触发之后是否保住了人声，不能用来重新评价 AEC
  之前是否应该触发。

在 Pi5 上验证文件并运行当前参数：

```bash
cd ~/lelamp_runtime
uv run --no-sync python scripts/evaluate_barge_in_dataset.py --validate-only
uv run --no-sync python scripts/evaluate_barge_in_dataset.py \
  --threshold 0.40 --min-speech 0.20 \
  --json-output voice_debug/barge_benchmark/latest.json
```

比较新参数时只修改命令行参数，使用同一份清单。评测不会播放声音、启动
`lelamp.app`、调用 ASR/Agent 或控制硬件。

运行固定参数网格并与当前 `0.40 / 0.20 秒`基线比较：

```bash
uv run --no-sync python scripts/tune_barge_in_dataset.py \
  --output voice_debug/barge_benchmark/grid-v0.json
```

网格会把已触发录音开头 0.5 秒的 AEC clean 预录作为近似触发证据。它适合
排除明显更差的参数，但真人正样本仍很少，不能单独决定正式配置。

2026-09-18 的 V0 首次网格结果：当前 `0.40 / 0.20 秒`检出 3/4 条真人
证据，同时在 32 条负样本中触发 7 条；扫描的 56 组参数里没有一组在保持
或提高真人召回的同时减少误触发。`0.30 / 0.20 秒`可检出 4/4，但误触发
升至 14/32；把最短语音提高到 0.30 秒则真人召回降至 1/4。完整结果位于
Pi5 `voice_debug/barge_benchmark/grid-v0.json`，因此正式参数暂不修改。

## V0 的限制

- 纯播报负样本较多，真人成功插话样本太少。
- 现有长录音缺少逐句开始时间和原句标签，不能可靠自动切分。
- 缺少不同插话时机、音量和句长的成组样本。
- 当前只能可靠比较 VAD 参数；修改 AEC 算法或 delay 时仍需用 raw/reference
  重新生成 clean 音频。
