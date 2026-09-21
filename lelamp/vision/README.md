# Vision

LeLamp 摄像头采集与人脸、手势感知模块。当前仅完成目录和开发路线规划，尚未安装视觉依赖、下载模型、编写视觉程序或运行摄像头实验；下述计划不代表已有能力。

## 文档入口

- [开发路线与阶段交付条件](docs/ROADMAP.md)
- [第一阶段：摄像头选型验证执行方案及结果模板](docs/CAMERA_EVALUATION.md)

第一阶段使用现有 UGREEN USB 摄像头验证购买需求；第二阶段在新摄像头到货、安装并验证实际视角后，再细化正式视觉设计与开发。MediaPipe Gesture Recognizer、YuNet、SFace 都是待验证候选。

## 架构边界

遵循项目根目录的 [archetecture.md](../../../archetecture.md) 和 runtime 的 [ARCHITECTURE.md](../../ARCHITECTURE.md)：

- Vision 负责采集、识别并输出结果、目标位置或图片，不直接写舵机。
- Motion 负责跟踪产生的机械控制；`LampApp` 负责启停、持续模式恢复和统一运动仲裁。
- Agent 与未来网页使用高层能力，不直接访问舵机、串口、摄像头线程或底层控制参数。
- 不引入视觉网关、事件总线或独立运动占用机制；不提前创建空的代码子目录、抽象接口或模拟实现。

## 当前目录

```text
vision/
├── __init__.py                 # 保持现有占位
├── README.md                   # 职责、状态和导航
└── docs/
    ├── ROADMAP.md              # 完整路线与阶段边界
    └── CAMERA_EVALUATION.md    # 选型验证计划
```

详细方案只在本目录维护；[TODO](../../TODO.md) 和 [HANDOFF](../../HANDOFF.md) 保留阶段摘要与入口。后续按真实实现需要添加代码文件，不预先确定完整程序结构。
