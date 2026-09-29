现在开始开发 LeLamp 的灯光状态提示，目标是让灯光直观表示语音交互状态。

按下面方案实现：

- WAKE_REQUIRED：暖橙色极暗慢呼吸，或熄灭
  表示需要重新喊“老灯”

- WAKE_ACK：青蓝色快速闪两下
  表示唤醒成功

- LISTENING：青蓝色低亮常亮或慢呼吸
  表示当前可以继续说话，不需要再次唤醒

- THINKING：紫色慢呼吸
  表示 ASR 已结束，正在等待 LLM / 处理结果

- SPEAKING：暖黄色轻微呼吸
  表示 TTS 正在播报
  第一版不用做音量联动，以后可扩展为随 TTS 音量变化

- TURN_DONE：淡绿色短闪约 300~500ms
  表示本轮回答结束
  随后重新进入 LISTENING

- SESSION_END：暖橙色约 1 秒渐暗
  表示连续对话结束，随后回到 WAKE_REQUIRED

- ERROR：红色
  只用于真正的异常/错误，不作为普通装饰灯效

整体流程：

WAKE_REQUIRED
→ 唤醒词
→ WAKE_ACK
→ LISTENING
→ 用户说完
→ THINKING
→ TTS 开始
→ SPEAKING
→ TTS 结束
→ TURN_DONE
→ LISTENING
→ 连续对话超时
→ SESSION_END
→ WAKE_REQUIRED

颜色语义尽量固定：

青蓝 = 听
紫色 = 想
暖黄 = 说
绿色 = 完成
暖橙 = 待机/睡眠
红色 = 错误

代码上不要在 voice_assistant 里到处直接写 RGB 数值，封装成类似：

light.wake_ack()
light.listening()
light.thinking()
light.speaking()
light.turn_done()
light.session_end()
light.sleep()
light.error()

然后在当前语音流程的对应节点调用。

先结合现有灯光/RGB实现检查当前项目，给出最小改动方案并实现，不要为这个功能额外引入复杂状态机或新框架。