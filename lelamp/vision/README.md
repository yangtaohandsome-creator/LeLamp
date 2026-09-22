# Vision

LeLamp 摄像头采集与人脸、手势感知模块。正式最新帧采集、YuNet与MediaPipe联合感知、生命周期、单人前景目标保持、双轴图像闭环和健康状态已经接入`LampApp`。

## 文档入口

- [视觉功能设计与开发准则](docs/VISION_DESIGN.md)：后续视觉开发的主要设计依据
- [当前开发路线与交付顺序](docs/ROADMAP.md)
- [历史摄像头选型验证方案](docs/CAMERA_EVALUATION.md)

正式摄像头为固定在灯头上的IMX179 USB UVC摄像头。当前采集基线为640×480@30 FPS MJPEG；安装方向在采集边界逆时针旋转90°，正立模型输入为240×320。正式视觉会话中YuNet和Gesture Recognizer均保持运行，默认各自最高10 Hz。SFace正式激活逻辑暂缓。

## 架构边界

遵循项目根目录的 [archetecture.md](../../../archetecture.md) 和 runtime 的 [ARCHITECTURE.md](../../ARCHITECTURE.md)：

- Vision 负责单一摄像头采集、识别、短期目标保持并输出结果、目标位置或图片，不直接写舵机。
- Motion 负责跟踪产生的机械控制；`LampApp` 负责启停、持续模式恢复和统一运动仲裁。
- Agent 与未来网页使用高层能力，不直接访问舵机、串口、摄像头线程或底层控制参数。
- 正式视觉运行在现有`LampApp`中，不建立第二个视觉app；不引入视觉网关、事件总线或独立运动占用机制。
- 人脸与手势在视觉会话中同时感知；face/hand只表示当前机械关注目标，不产生两个运动控制源。
- WORK_LIGHT长期保留手势感知，手部引导作为其内部子状态复用同一个tracking runner。

## 当前目录

```text
vision/
├── __init__.py
├── README.md                   # 职责、状态和导航
├── config.py                   # vision.conf读取与校验
├── types.py                    # 帧、检测、目标和事件契约
├── camera.py                   # 单一所有者、最新帧采集线程
├── face.py                     # YuNet适配
├── hands.py                    # MediaPipe Gesture Recognizer适配
├── target.py                   # 单人前景选择、短期关联和丢失保持
├── controller.py               # 生命周期、联合调度和健康状态
├── gesture_live_test.py        # 独立手势/人脸组合实验
├── sface_live_test.py          # 独立身份链路实验
└── docs/
    ├── VISION_DESIGN.md        # 正式视觉设计与开发准则
    ├── ROADMAP.md              # 当前交付顺序
    └── CAMERA_EVALUATION.md    # 历史选型验证计划
```

正式依赖位于`pyproject.toml`的`vision` extra。视觉仅在语音连续会话、tracking、WORK_LIGHT或明确按需请求期间启动；普通等待唤醒和sleep状态释放摄像头。人脸tracking通过`LampApp`的统一运动任务向Motion发送流式目标；手势当前仍无副作用。

详细正式方案统一维护在`VISION_DESIGN.md`；[TODO](../../TODO.md)和[HANDOFF](../../HANDOFF.md)只保留阶段摘要与入口。设计会随实测和用户要求更新，不把旧文档当作不可修改的实现约束。后续按实际能力需要添加代码文件，不提前生成空目录和接口。
