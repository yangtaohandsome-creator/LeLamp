# 官方手势模型拆包与关节点接入验证

本记录只分析已部署的 `gesture_recognizer.task`；没有修改正式识别、手势切换或机械控制。模型来自 Pi5 的 `~/lelamp_vision_eval/models/gesture_recognizer.task`，SHA256 为 `97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482`。本地与 Pi5 使用的 MediaPipe 版本均为 1.0.1。

## 模型与特征链

外层 `.task` 包含 `hand_landmarker.task` 和 `hand_gesture_recognizer.task`；后者包含两个 TFLite 模型：

| 模型 | 输入 | 输出 |
| --- | --- | --- |
| `gesture_embedder.tflite` | `hand` `[1,21,3]`、`handedness` `[1,1]`、`world_hand` `[1,21,3]`，全部 `float32` | `hand_embedding` `[1,128]` |
| `canned_gesture_classifier.tflite` | `hand_embedding` `[1,128]`，`float32` | 8 类分数 `[1,8]` |

这与[官方手势子图](https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/cc/vision/gesture_recognizer/hand_gesture_recognizer_graph.cc)一致：手势子图向 embedder 提供图像关节点、三维 world 关节点和左右手分数，不直接提供 RGB 像素。RGB 在之前的手检测和关键点估计阶段使用。但**输入张量存在，不等于模型实际使用了它**；见下文实测。

关节点不能直接以 `GestureRecognizerResult` 的原始值喂给 embedder。按[官方 `LandmarksToMatrixCalculator` 实现](https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/cc/vision/gesture_recognizer/calculators/landmarks_to_matrix_calculator.cc)，输入先经历：

1. 图像关节点的 `x/y` 按图像长宽比缩放；world 关节点不做这一步。
2. 如有 `NORM_RECT` 旋转，绕图像中心旋转两组点的 `x/y`。
3. 两组点分别减去手腕点 0，并分别除以本组 `x/y` 包围盒跨度的较大者加 `1e-5`；`z` 使用同一缩放因子。
4. 左右手张量为“右手概率”，不是字符串或固定的 0/1。随后依序输入 embedder 的三个张量，再将其 128 维输出输入分类器。

进一步检查本机这份 `gesture_embedder.tflite` 的运算图：`handedness` 和 `world_hand` 张量没有被任何算子引用，只有 `hand` 被引用。固定 `hand`，将左右手概率由 0.01 改成 0.99、同时将 world 点全部替换，128 维输出的最大绝对差仍为 **0**；改用另一副 `hand` 点时，输出明显变化。也就是说，**这份具体模型的实际分类只依赖图像关节点张量 `hand`**。其他版本或重新训练的模型不一定如此；实验入口用模型 SHA256 锁定这一结论。

## 隔离验证结果

拆出的模型在临时目录通过 LiteRT 单独运行。使用[官方 Model Maker 示例图片](https://developers.google.com/edge/mediapipe/solutions/customization/gesture_recognizer)和上面的前处理，完整任务与拆开的模型链得到相同类别，分数精确到小数点后五位：

| 示例图片 | 完整 MediaPipe | 拆出模型复现 |
| --- | --- | --- |
| `paper/77.jpg` | `None` 0.74520 | `None` 0.74520 |
| `rock/88.jpg` | `Thumb_Down` 0.71046 | `Thumb_Down` 0.71046 |
| `scissors/604.jpg` | `Victory` 0.56956 | `Victory` 0.56956 |

又将当前 `hand_pose.py` 的局部三维坐标投影为正立手骨架，作为两个关节点张量送入 embedder。三个样本都能运行并输出有限概率；当前独立脚本中 `rock/88.jpg` 变为 `Closed_Fist` 0.804，`scissors/604.jpg` 仍为 `Victory` 0.903，`paper/77.jpg` 仍为 `None` 0.732。这只证明**接口可用、摆正会改变分类结果**，不能证明准确率提高。这里的两个张量使用了同一组投影点，尚未满足官方图像坐标与 world 坐标各自的真实数据分布。

对同三张图片，仅镜像图像关节点 `x`，`rock/88.jpg` 由 `Thumb_Down` 0.710 变为 `None` 0.614。可见**去掉左右手概率输入不等于对左右镜像不敏感**；手骨架在图像中的几何朝向仍影响分类。

## 结论与后续验证

**可以把我们变换后的关节点直接输入官方的独立 embedder，再输入原分类器**，无需把骨架重新渲染为 RGB，也无需改正式 MediaPipe 图。当前公开 Python `GestureRecognizer` API 没有暴露这个中间入口，因此若要正式使用，应封装独立的推理适配器，并保留现有图像→关节点管线。

独立实验链现已实现于 `lelamp/vision/gesture_classifier_experiment.py`，图片比较入口为 `scripts/compare_gesture_classifier.py`。它只读取指定图片和 `.task` 模型，不访问摄像头、app 或舵机。对每只手输出官方分类、拆包复现分类、手掌坐标归一化实验分类、各类分数和两段模型的耗时，支持将结果保存为 JSONL。为了避免类别顺序或输入签名在模型升级时悄悄改变，当前只接受本页记录的模型 SHA256。

实验脚本需要在**独立环境**中提供 `mediapipe`、`numpy` 和 `ai-edge-litert`。用法示例：

```bash
python scripts/compare_gesture_classifier.py \
  --model /path/to/gesture_recognizer.task \
  --output /tmp/gesture_compare.jsonl \
  /path/to/test-image.jpg
```

其中 `stock_reproduced=true` 是前处理与拆出模型连通性的检查；若为 `false`，不得解读摆正分支的结果。`palm_aligned_experiment` 目前是正交投影的实验输入，图像与 world 两路使用同一组局部关节点，故尚未是已标定的相机几何重建。脚本报告的耗时只覆盖 embedder 和分类器，不包括图片读取、MediaPipe 关节点估计及归一化。

正式接入前须优先验证三维旋转后生成的**图像关节点 `hand`**：投影、尺度、`z` 符号和左右镜像约定，以及原模型对这种分布外输入的反应。`world_hand` 与左右手概率虽然需要按当前模型签名提供合法张量，本版本不会影响输出；后续若换模型，必须重新检查。应先离线比较不同朝向的张开、握拳和易混淆姿态，再在不发送机械命令的预览中并行显示新旧结果，统计漏检、误触发和耗时。若关节点本身错误或丢失，摆正不能修复。验收前不让新输出驱动跟随状态机。

## Pi5 实时独立验收

`scripts/gesture_classifier_live.py` 复用设备 `vision.conf` 的最新帧采集与 MediaPipe 手部感知，在显示器并排标注 Original 和 Aligned。不启动 app、人脸模型或机械跟随，故这次帧率不代表正式组合负载。`baseline_match` 应为 true；摆正结果仅供人工比较，不产生控制事件。日志每秒输出一次分类、分数、结果年龄和模型分类耗时。

Pi5 的 LiteRT 放在独立的 `~/lelamp_vision_eval/litert_packages`，不修改项目依赖。摄像头空闲时运行：

```bash
cd ~/lelamp_runtime
DISPLAY=:0 PYTHONPATH=~/lelamp_vision_eval/litert_packages \
  .venv/bin/python -u scripts/gesture_classifier_live.py
```

按 Q/Esc 或 Ctrl-C 结束并释放摄像头；`--seconds 15` 可做定时冒烟测试。测试时同一姿态保持数秒，对照掌心、手背、俯仰、左右手和过渡姿态，不将某个标签的高分直接当成准确率。

### 2026-09-24 旋转一致性修正（palm-axes-v2）

移除摆正后根据原图食指根/小指根 X 顺序选择镜像的分支。该分支会在同一只手旋转180°时改变输出 X 符号，接近90°时也可能跳变。现在仅用掌骨定义的局部坐标轴，固定将手指方向映射为向上；不根据屏幕左右关系改变镜像。新增图像与 world 同步旋转的回归测试，覆盖倒置及90°两侧。旧测试只旋转 world，未覆盖该问题。

此修正确保理想刚体旋转的输入一致，不保证真实关键点估计或分类准确率；左右手的局部 Z 差异和模型对正交投影的适应性仍待验收。用户已确认完整入镜仍出现倒置张开漏识别，因此不能将裁切作为该问题的解释。正式视觉与运动链保持不变。

2026-09-24 实机反馈：palm-axes-v2 导致正向手背识别退化，已回滚至上一版屏幕左右分支。理想刚体旋转一致性不能作为分类效果验收；上一版仍有倒置不稳定的已知问题。后续优先验证保留原始 normalized XYZ 的平面旋转，再单独研究三维投影与左右手约定。

### 回滚后的候选：保留图像 XYZ，只校正平面旋转

官方 `landmarks_to_matrix_calculator.cc` 的 `RotateLandmarks` 对 X/Y 做旋转、Z 原样保留，之后才做 wrist-origin 和尺度归一化。来源：https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/cc/vision/gesture_recognizer/calculators/landmarks_to_matrix_calculator.cc 。这里的旋转能力不代表官方已经自动消除了每只手的姿态。

下一独立实验优先使用 normalized landmarks：先校正图像宽高比，再以 wrist(0) 指向 middle MCP(9) 的向量计算平面角度，绕 wrist 旋转至手指向上；保留点编号、左右手几何关系和原始 Z，再执行官方平移/尺度前处理。不能用 world 的局部 Z 直接替代图像 Z，也不能通过每帧挑最高分类分数选择镜像。原始分支与上一版摆正分支保留作对照。平面旋转不解决掌心/手背翻转和出平面俯仰，这两项必须单独验证。

验证需录制同一手势正立/倒立/侧转的原始 normalized XYZ、world XYZ、左右手及完整类别分数，重放同一数据比较分支；还应包含 None 与其他手势负样本。先核对校正后骨架和深度分布，再评估准确率、跳变和耗时，不靠时间平滑隐藏分类错误。新方法尚未部署。

### SSH 终端引导采样

无需 Pi5 键盘，在 SSH 中前台运行（Pi5 显示器保留预览）：

```bash
cd ~/lelamp_runtime
DISPLAY=:0 PYTHONPATH=~/lelamp_vision_eval/litert_packages \
  .venv/bin/python -u scripts/gesture_classifier_live.py \
  --capture-dir runtime_state/gesture_capture --terminal-capture
```

输入数字后回车：1=掌心正立张开，2=手背正立张开，3=手背倒立张开；每次先准备3秒、采集10秒后自动暂停。不自动切换下一组，可任意顺序和重复采样。0立即暂停，q保存退出，Ctrl-C同样关闭文件并释放摄像头。每次启动创建独立JSONL，不覆盖旧数据；原始坐标、模型输入及类别分数记录于每个采样帧，未检测到手的帧也保留。采样程序仍不连接运动控制。

### 原始图像骨架旋转与 Z 反射对照分支

新增 `image_roll_inputs`：宽高比校正后，按 wrist→middle MCP 的平面方向旋转，保留图像骨架的相对 XYZ；另输出固定翻转 Z 的诊断分支。两次归一化与299帧回放实验一致。Z反射只是对照，不代表已经判断掌心/手背，不能将新标签接入正式运动。

实时预览保留 Original、旧 Aligned，并增加 Roll+Z (test)。采样JSONL同时记录 image_roll 和 image_roll_z 的输入、全部类别分数及模型耗时。终端采样1～3仍为原三组张开；4=掌心正立握拳，5=手背正立握拳，6=手背倒立握拳，7=自然半弯非目标姿态，8=剪刀手，9=竖拇指。每组选定后准备3秒、采集10秒并暂停。7不强制标为None，而是单独检查是否误报Open_Palm/Closed_Fist。新文件不覆盖旧样本。

待用户采集负样本和握拳后，再比较各分支的目标召回、非目标误报、类别跳变与有效手帧数；本次几何测试不等价于识别精度验收。还需后续不同手、距离和角度的独立数据，不能根据同一批张开样本选择最高分分支作为最终算法。

无图形会话时增加 `--no-preview`，不创建任何OpenCV窗口，SSH终端控制和数据记录不变：

```bash
PYTHONPATH=~/lelamp_vision_eval/litert_packages \
  .venv/bin/python -u scripts/gesture_classifier_live.py \
  --terminal-capture --no-preview --capture-dir runtime_state/gesture_capture
```

新实现回放原299帧：Roll+Z分支掌心正立99/100、手背正立99/99、手背倒立100/100，复现此前结果。握拳与负样本精度尚待补采；上述比例不能代表通用准确率。

### 逐帧并集兜底（仅独立预览）

按用户最新要求取消固定分工和冲突拒绝：三路非None类别取并集去重，唯一例外是Thumb_Up只采纳Original。不新增置信度门槛，不做计时确认或历史保持；分支没有输出不否决其他分支。同帧不同类别并存时，Fused用竖线展示全部候选，JSON的labels数组保留各类别，不将其当作一个新的手势类别。不以最高分选赢家。

该实验没有接入正式app或运动状态机。并集会保留各分支误判，包括半弯手误报张开；后续若用于控制，需要另行确认多候选语义，不能直接把多个候选当成连续手势事件。HandTargetManager的ID仅用于记录，不给融合增加等待。
