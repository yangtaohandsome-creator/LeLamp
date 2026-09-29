# 手势分类模型调研与三维关键点路线

目前的 MediaPipe Gesture Recognizer 能返回手部关键点，但自带分类器在本机手背、俯仰角场景下经常无法给出有效手势。评估新方案时，必须区分「检测出关键点」和「正确分类手势」；后者不能从前者直接推出。

## 已检查的开源候选

| 候选 | 输入与类别 | 目前不能直接替换的原因 |
| --- | --- | --- |
| [hand-gesture-visualizer](https://github.com/KabirRao1/hand-gesture-visualizer) | MediaPipe 关键点、10 类、附带随机森林模型 | 特征只做二维平移、缩放和平面旋转；仓库给出的随机划分精度不能证明手背、俯仰角或跨人泛化。 |
| [edge-ai-gesture](https://github.com/miftahfaridhh/edge-ai-gesture) | 声称 9 类，主要示例使用二维指尖阈值 | 仓库所述预训练权重在检查的代码版本中未找到；示例判断依赖图像坐标方向，倾斜时难以可靠工作。 |
| [hand-gesture-recognition-mediapipe](https://github.com/kinivi/hand-gesture-recognition-mediapipe) | 二维关键点与轻量 TFLite 分类器 | 初始类别少，没有针对本机所需手背、俯仰角泛化的验证。 |
| [HaGRIDv2](https://github.com/hukenovs/hagrid) | RGB 图像，33 类手势及 `no_gesture`，公开模型与数据 | 类别丰富，但不是直接接收 21 个关节点的分类器；需要额外图像推理，Pi5 负载和本机视角效果尚未测量。 |

结论仅限这些候选：没有找到可以直接接入现有 21 点管线、同时已有可信证据证明适用于灯头摄像头各角度场景的预训练模型。HaGRIDv2 可作为以后图像分支的候选，不能据公开数据推断本机效果。

## 当前实现与边界

[MediaPipe 的结果接口](https://ai.google.dev/edge/api/mediapipe/python/mp/tasks/vision/GestureRecognizerResult) 同时提供图像归一化关键点和三维 `hand_world_landmarks`。现有识别器现在保留两组原始三维点，并从 world 点计算：以手腕为原点、以掌骨建立局部坐标轴、按掌宽缩放后的 21 个三维点，以及五指关节弯曲角。计算位于 `hand_pose.py`，预览会显示角度，供手背和俯仰角现场核查。该计算无需增加视觉模型或依赖。

**归一化不是手势分类器。** 当前 `Open_Palm`、`Closed_Fist` 等标签和手部跟随动作仍只取自原 MediaPipe 分类结果；本次没有改变动作触发规则。局部坐标能消去理想三维刚体旋转、平移和尺度变化，但不能修复 MediaPipe 在遮挡、手背视角下缺失或错误的关键点。左右手标签也必须在当前未镜像摄像头上实测，否则不能直接依赖左右手合并后的特征。

下一步在本机相机下分别采集正面、手背、上下俯仰、左右旋转与不同距离的样本，记录原分类、三维点、关节角及是否丢点。按人和采集会话分离训练/测试集，再比较基于三维特征的小分类器、必要时比较 RGB 候选的准确率、误触发率和 Pi5 推理负载。只有新分类器在真实手势切换中通过验收，才能接管跟随状态机；新增类别也需要先确定操作语义和防误触规则。
