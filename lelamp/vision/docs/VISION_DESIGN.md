# LeLamp 视觉功能设计与开发准则

本文是 LeLamp 视觉功能后续开发、评审和验收的主要设计依据。项目级模块边界以仓库上层的 [`archetecture.md`](../../../../archetecture.md) 为准；本文在该边界内规定视觉能力、运行方式、数据契约、机械跟踪、与语音及 Agent 的关系、开发顺序和验收要求。

本文描述当前确认的目标架构，不表示所有能力已经实现，也不是冻结后不可修改的规格。开发时按实际能力创建文件，不预先生成空目录、空接口或占位服务。后续实测、现有代码约束或用户要求与本文不一致时，以新的事实和要求为准；先说明影响并更新本文，再让实现和文档保持一致。

## 1. 已确认的硬件、环境与实测结论

- 主机为 Raspberry Pi 5，2 GB RAM，视觉与语音、Pi Agent、TTS、灯光和运动共用主机资源。
- 正式摄像头为 IMX179 8MP USB Camera (B)，Linux UVC，75° 视场角，自动对焦，固定在灯头上；摄像头朝向可视为小灯视线方向。
- 摄像头原生支持 `640×480 @ 30 FPS MJPEG`，原生 UVC 连续传输实测约 29～30 FPS。
- 固定安装的原始画面侧转90°。正式链路在采集边界逆时针转正，再从正立的480×640画面缩放到240×320作为模型输入；所有下游坐标都使用该正立坐标系。
- 转正和缩放成本远低于模型推理成本，不是当前性能瓶颈。
- OpenCV YuNet、MediaPipe Gesture Recognizer、ONNX Runtime与SFace已经在Pi5 ARM64/Python 3.13独立环境中验证可运行。
- YuNet与Gesture Recognizer组合运行、正式app同时存活的30分钟测试没有摄像头读取失败、swap使用或明显内存泄漏。无散热时会触发Pi5热管理与降频；用户已接受本轮稳定性验证结果，正式部署仍应使用正常散热条件。
- SFace识别链路已独立验证，但当前暂停正式身份注册、激活和业务绑定逻辑。
- 正式最新帧采集、YuNet与Gesture Recognizer联合调度、视觉会话生命周期、单人前景目标保持、双轴人脸机械跟踪和只读健康状态已接入`LampApp`；手势副作用仍未实现，肩/肘慢速姿态回中尚待开发。

正式配置基线为：

```text
Camera capture: 640×480 @ 30 FPS, MJPEG
Model input:    320×240
Face:           YuNet, up to 10 Hz
Hand/Gesture:   MediaPipe Gesture Recognizer, up to 10 Hz
Motion control: 20～30 Hz
```

10 Hz是每项感知能力的调度上限，不是硬性最低吞吐。一次联合感知超过100 ms时不补跑、不积压，下一次直接处理最新帧。

## 2. 总体设计原则

1. 正式系统只有一个`LampApp`进程和一个运动仲裁入口，不创建第二个正式视觉app。
2. Vision回答“看见了什么”，Motion负责“如何运动”，`LampApp`决定“何时运行、关注谁、动作如何交接”。
3. Vision不得直接写舵机、串口、灯光、TTS或Agent；模型类别不得直接产生硬件副作用。
4. Agent和未来网页只能调用高层能力，不能访问摄像头线程、模型实例、像素误差、关节目标或控制增益。
5. 不引入独立Behavior Manager、通用Event Bus、视觉微服务、ROS2或新的硬件占用框架。
6. 摄像头只有一个所有者，采集只保留最新帧；任何推理和机械控制都不得消费持续积压的旧帧。
7. 人脸与手势感知在视觉会话中同时保持可用。当前跟脸或跟手只改变机械目标，不关闭另一条感知链。
8. 单人桌面交互是主要场景。多人能力只实现保证单人体验所需的目标保持和防跳，不为低频场景引入复杂说话人或人体关联系统。
9. 视觉感知频率与舵机控制频率解耦。机械控制使用最新目标、速度估计和结果年龄，不把低频检测伪装成高频新信息。
10. 原始画面默认只在内存中短暂存在，不连续录像、不提交Git；保存快照、诊断数据或身份信息必须有明确用途。
11. WORK_LIGHT长期保留手势感知，并可在该模式内部启用手部引导来调整照明方向；它不建立第二个机械控制源。

## 3. 正式进程与模块关系

```text
IMX179 UVC Camera
        │
        ▼
LatestFrameSource ── 单一采集线程，只保留最新帧
        │
        ▼
VisionController ── 联合感知调度、目标保持、手势状态
   ├── YuNet Face Detector
   ├── MediaPipe Gesture Recognizer
   ├── Face/Hand Target Tracking
   └── Gesture Event Filter
        │
        ├── VisionSnapshot（最新状态）
        ├── TrackingTarget（当前机械目标）
        └── GestureEvent（少量一次性事件）
                │
                ▼
             LampApp
   视觉生命周期、模式、语音会话、事件映射、运动仲裁
                │
                ▼
    Motion Visual Tracking Controller
                │
                ▼
             舵机总线
```

`VisionController`是`LampApp`持有的普通组件，不单独部署。独立摄像头、识别、标定和benchmark程序只用于开发与诊断，不成为第二套正式运行入口。

## 4. 视觉会话与工作负载

视觉会话表示联合感知需要运行，和语音连续会话不是同一状态。以下任一条件成立时可保持视觉会话：

- 语音会话处于唤醒后的连续对话期；
- `current_mode == "tracking"`；
- `current_mode == "work_light"`，用于长期手势感知和按需手部引导；
- app正在执行明确的按需视觉请求，例如短时指向识别或物体查找。

机械sleep时默认停止摄像头和模型。WORK_LIGHT保持视觉会话；无手势、无人或无需移动时仍按固定感知上限运行，不让模型自行切换模式或直接控制硬件。

视觉会话运行时，人脸和手势使用同一最新帧，按照统一周期联合执行：

```text
每100 ms到达一个调度期限
→ 取得此刻最新帧
→ YuNet
→ Gesture Recognizer
→ 发布同源联合结果
→ 超过期限时跳过旧周期，不追赶
```

这保证人脸跟随期间仍能识别切换到手部跟随的手势，也便于进行人脸与手的同帧关系判断。正式默认上限为10 Hz；只有实机8/10/15 Hz对照证明更高频率显著改善跟随误差或动作自然度时，才提高默认值。

舵机控制以20～30 Hz运行，通过目标速度估计、插值、死区、速度和加速度限制平滑两次视觉结果之间的运动。

## 5. 采集、帧语义与队列规则

采集层必须满足：

- 单一线程持续读取`/dev/video0`；设备路径可配置，不能在多个模块重复打开摄像头。
- 显式请求MJPEG、640×480、30 FPS，并记录实际协商结果。
- 内存中只有一个可覆盖的最新帧槽位，不建立无界帧队列。
- 推理通过序号判断是否出现新帧，慢时自然跳过中间帧。
- 停止时先通知采集退出，再等待设备真正释放。
- 摄像头断开、读取连续失败或格式回退必须进入可观测错误状态，不得卡死app主循环。

每帧至少携带：

```text
FramePacket
- sequence: 单调递增帧序号
- captured_at: 应用读取时的单调时间戳
- capture_size: 实际采集宽高
- model_input_size: 模型输入宽高
- rotation / mirrored: 安装方向与镜像信息
- image: 当前BGR或RGB图像
```

软件采集时间戳不等于传感器真实曝光时刻。没有硬件时间戳时，文档和日志必须称为软件链路时间戳。

## 6. 坐标与正式结果契约

所有对外视觉坐标统一为原始采集画面的`0～1`归一化坐标。缩放、镜像、旋转和模型输入坐标转换由Vision内部完成。机械控制不直接接触模型像素坐标。

建议正式数据结构：

```text
FaceObservation
- track_id
- bbox_normalized
- anchor_normalized
- five_landmarks_normalized
- confidence
- velocity_normalized_per_second

HandObservation
- track_id
- handedness
- landmarks_normalized[21]
- palm_center_normalized
- gesture
- gesture_confidence
- velocity_normalized_per_second

VisionSnapshot
- source_frame_sequence
- captured_at
- completed_at
- faces[]
- hands[]
- active_face_target_id
- active_hand_target_id
- camera_status

TrackingTarget
- kind: face | hand
- track_id
- position_normalized
- velocity_normalized_per_second
- captured_at
- confidence

GestureEvent
- gesture
- hand_track_id
- confidence
- confirmed_at
```

`track_id`只在当前视觉会话内表示短期目标连续性，不代表真实身份。

## 7. 人脸检测与单人目标保持

正式人脸检测采用OpenCV YuNet：

- 输出bbox、置信度和五个人脸关键点；
- 人脸机械目标优先使用关键点推导的稳定面部锚点，bbox中心作为回退；
- 检测结果必须保留来源帧和时间戳；
- 不因统一框架而替换成MediaPipe人脸链路。

单人桌面场景的目标规则：

1. 没有当前目标时，选择置信度合格、面积较大且靠近主要桌面交互区域的人脸。
2. 背景小人脸可以计数，但不参与机械控制候选。
3. 锁定后使用预测中心、IoU、尺寸变化和关键点位置关联下一帧。
4. 其他人脸短暂出现或面积略有变化时不得抢走当前目标。
5. 当前目标短暂丢失时保留track ID并减速，不立即选择背景人脸。
6. 持续丢失后解除锁定，再重新选择前景主目标。
7. 两个面积相近的前景人脸同时出现时保持已有目标；没有已有目标且无法稳定选择时允许暂不运动。

当前不开发说话人定位、全身Pose、复杂多人身份优先或严格的人手归属模型。

## 8. 手部检测、手势与目标切换

正式手部模型采用MediaPipe Gesture Recognizer。它同时提供21个关键点、左右手信息和内置静态手势，因此不再并行运行另一套Hand Landmarker。

静态手势从逐帧分类转换为一次性事件，必须满足：

```text
置信度达标
+ 同一hand track持续约300～500 ms
+ 当前app模式允许该手势
+ 未处于同手势已触发锁定
→ 产生一次GestureEvent
```

触发后必须等待手势解除并经过冷却时间，持续保持同一手势不能逐帧重复执行。帧率变化时按时间判断，不按固定帧数判断。

人脸跟随期间仍持续运行手势识别。用于切换目标的稳定手势可以请求：

```text
tracking_target_kind: face → hand
```

切换只修改当前机械关注目标，同一个tracking runner继续运行，不创建第二个运动任务。具体哪些手势负责切换尚未确定，应通过配置或`LampApp`中的明确映射设置，不能硬编码在模型适配器中。

灯头运动期间：

- 静态手势继续感知，以支持跟随模式切换；
- 低置信度或严重运动模糊的结果不能完成确认；
- 动态手势轨迹必须暂停或清空，避免把摄像头自身运动当作手部运动。

这里的“灯头运动”指视觉tracking自身的连续运动。点头、摇头、预设姿态切换等临时动作具有独占权：动作开始后暂停tracking runner，并丢弃期间产生的tracking请求和所有手势事件。动作结束时清空手势确认与动态轨迹，必须观察到新的稳定手势后才能再次触发，旧事件不得排队补执行。

多人、多手且归属不明确时，不执行有副作用的手势。单人场景可以使用目标连续性、出现位置和语音命令“看我的手”绑定稳定手目标，但不得把邻近关系描述为可靠身份归属。

## 9. 人脸与手部机械跟随

人脸跟随和手部跟随复用同一套图像视觉伺服控制器。Vision只输出`TrackingTarget`，机械算法位于Motion。

控制目标是让视觉目标进入舒适区域，而不是把目标严格锁死在单个像素中心。控制器包含：

- 相机内参与安装方向标定；
- 图像误差到视线角误差的转换；
- 常速度目标预测；
- 中心死区与方向反转迟滞；
- 速度、加速度和每周期位移限制；
- 关节软限位和接近限位时的渐进减速；
- 结果年龄检查和失效保护。

关节职责：

- `base_yaw`负责水平跟随；
- `wrist_pitch`负责快速垂直跟随；
- `base_pitch`和`elbow_pitch`缓慢调整整体姿态，使腕部回到舒适区；
- `wrist_roll`保持摄像头水平，除非标定证明需要补偿。

采用基于图像的视觉伺服和标定后的差分关节映射，不引入依赖单目深度的完整三维IK。相机内参和关节小幅运动引起的画面位移应通过固定安装后的实机标定获得。

Motion需要专用流式控制路径，不能每个周期调用现有阻塞式`move_to()`：

- 只发送最新关节目标，不建立运动命令积压；
- 20～30 Hz更新命令，约5～10 Hz读取真实舵机位置用于校正；
- tracking取消后立即停止产生新命令并等待循环真正退出；
- 视觉结果超过约250 ms时不再产生新的追踪位移；
- 故障时保持安全位置，不擅自释放扭矩；
- tracking产生的偏移为临时值，不覆盖持久化的`base_heading_degrees`。

进入普通人脸tracking时先平滑进入独立`tracking_home`。该姿态用于让固定摄像头正对桌前区域并给控制关节保留双向余量，不等同于standby；首版配置即使部分数值相同也必须使用独立配置项。进入时不叠加、不清零也不改写用户保存的`base_heading_degrees`，退出tracking回standby时才恢复该逻辑朝向。固定安装实测后，当前`tracking_home`为base yaw 0、wrist pitch -45，其余关节沿用已确认的待机高度。

目标丢失建议语义：短时使用预测并减速，超过约300 ms停止位移并保持方向。普通tracking超过可配置的无目标期限后退出；若此时语音会话已结束则进入sleep，语音会话仍有效则回standby。WORK_LIGHT手部引导丢失目标时只停止引导并保持最后一个安全照明关节目标，不退出WORK_LIGHT、不关灯，也不因语音会话已结束进入sleep。

## 10. 语音会话、机械唤醒与视觉生命周期

必须区分三个状态：

- `voice_session_active`：是否处于无需再次说唤醒词的连续语音会话；
- `current_mode`：机械持续模式，例如standby、tracking、work_light或sleep；
- `mechanically_asleep`：是否已进入睡眠姿态并释放扭矩。

15秒语音无有效文字只结束语音连续会话和Agent session。处理规则：

| 条件 | 语音超时后的行为 |
|---|---|
| 普通模式 | 进入统一机械sleep |
| tracking | 清空语音会话，但继续视觉与机械跟随 |
| work_light | 清空语音会话，保持照明姿态和灯光 |

tracking期间再次命中“小灯”只创建新的语音会话，不调用会取消tracking的`enter_standby()`。明确睡眠、告别、应用退出仍统一调用`app.sleep()`，先停止tracking和视觉输出，再进入睡眠姿态并释放扭矩。

实现上复用WORK_LIGHT已有的“语音超时只结束会话、保留持续模式”语义，但不要直接扩大`enter_standby()`的早退条件。建议由`LampApp`提供两个窄入口：

```text
keeps_mode_after_voice_timeout()
prepare_for_voice_session()
```

前者在tracking或WORK_LIGHT中阻止语音超时调用机械sleep；后者在新唤醒时保留这两个持续模式，在普通或sleep状态才进入standby。这样`stop_tracking()`仍可明确回到standby，不会被过宽的`enter_standby()`早退条件拦截。

tracking停止时，如果语音会话已经结束，应根据退出原因和配置进入sleep；如果语音会话仍有效，则回standby。目标切换face/hand不退出tracking。

视觉会话不能无限延长Agent对话历史，也不能仅凭看到人脸取消唤醒词要求。

## 11. app运动仲裁与Barge-in

同一时刻只能有一个运动控制源：

```text
通知旧任务停止
→ 等待旧任务停止发送关节命令
→ 启动新任务
```

tracking复用现有`current_motion_task`、`_motion_lock`、`_mode_runner`和模式版本机制：

- tracking → 临时点头/摇头：暂停跟随，动作完成后恢复同一tracking模式；
- 临时动作期间收到sleep、stop tracking或新模式：不得恢复过期tracking；
- 普通tracking进入WORK_LIGHT时停止普通跟随；WORK_LIGHT内部可按需启动手部引导子状态；
- face/hand切换只更新`tracking_target_kind`，不创建并行运动任务；
- 手势产生的动作通过`LampApp`或`ToolExecutor`高层入口执行，不能直接进入Motion。

长期tracking期间，Barge-in继续把存活的`current_motion_task`视为运动状态，只接受运动中的明确停止词。即使目标暂时位于死区，视觉目标随时可能再次驱动舵机；按瞬时关节静止切回自然VAD会重新暴露舵机噪声误打断风险。该语义不要求新增`physically_moving`作为Barge-in门控。

视觉事件进入高层Tool时必须保留调用来源。`ToolExecutor.execute()`增加仅限关键字的`source`参数，并由所有现有调用点明确传入`agent`、`local_voice`、`vision`、`scheduled`或`control_api`等来源。来源由调用适配层设置，不接受模型或普通tool arguments自行声明：现有Pi Agent Control API适配层传`agent`，本地确定性语音传`local_voice`，定时完成回调传`scheduled`，视觉事件消费者传`vision`，未来直接网页控制传`control_api`。只有`source == "agent"`的自主表达参与Agent情绪动作延后；视觉触发动作立即走正常运动仲裁，不能因当时存在Agent轮次而被错误排队。`queue_expression`仍是Agent自主表达专用入口。这个改动只补足调用语义，不能形成绕过`LampApp`的新执行路径。

临时动作执行期间，视觉结果可以继续用于诊断统计，但控制侧直接丢弃tracking请求和手势事件，不延后、不抢占、不在动作结束后补执行。恢复原持续模式后清空手势历史并重新确认。

WORK_LIGHT与普通tracking不是两个并发顶层模式。`current_mode`保持`work_light`，并增加最小的照明视觉子状态：

```text
work_light_visual_state: idle | hand_guidance
work_light_joint_target: 当前自定义照明关节目标，可为空
tracking_target_kind: hand  # hand_guidance期间
```

启用`hand_guidance`时复用同一个tracking runner连续调整照明方向；停止引导时保存软限位内的当前关节目标，灯光和WORK_LIGHT仍保持。用户选择高/低预设时清除自定义目标并停止手部引导。临时动作暂停手部引导，结束后若模式版本未改变则恢复原引导；若当时只是保持自定义照明姿态，则回到保存目标。明确sleep、退出WORK_LIGHT或新持续模式负责停止该子状态。现有`_play_and_restore()`不能再无条件只恢复`work_pose`高/低值，必须按上述保存状态恢复。

现有`tracking`运行标志应改为表示“视觉tracking runner当前是否活动”，不再通过`current_mode == "tracking"`推导；WORK_LIGHT手部引导期间它同样为真。`stop_tracking()`在普通tracking中回standby，在WORK_LIGHT手部引导中则停止引导并保持当前安全照明姿态。`work_light_joint_target`只保存在当前运行期，选择预设、退出WORK_LIGHT或sleep后清除，不覆盖持久化的睡眠姿态、动作CSV或底座逻辑朝向。

## 12. 动态手势与二维指向

动态手势使用最近约0.5～1秒的手掌中心和关键点轨迹：

- 按手掌尺寸归一化位移；
- 使用真实时间戳计算速度；
- Wave要求左右往返和方向反转；
- Swipe要求足够净位移、速度和较少方向反转；
- Move Up/Down要求稳定垂直净位移；
- 机械运动开始或目标切换时清空轨迹。

不为当前动态手势训练LSTM、TCN或其他时序模型。规则方案达不到明确验收指标时再用真实数据评估模型化需求。

“指哪里看哪里”使用食指关键点估计二维方向，并采用意图门控：

```text
用户说“看那里”
→ app开启短时指向窗口并暂停机械跟随
→ Vision确认稳定指向
→ 输出二维方向
→ app执行一次受限转向或恢复跟随
```

二维手指方向不能被描述为真实三维空间落点。需要准确指向桌面物体时，应结合桌面平面标定、深度信息或物体检测。

## 13. 视觉存在、粗略距离、身份与物体

视觉存在只输出可证明的提示：

```text
face_present
hand_present
visual_activity_recent
last_seen_at
```

人脸不可见不等于房间无人。以上结果可用于调整视觉工作负载，但不能直接绕过`LampApp`调用sleep。

粗略距离可依据人脸框比例和固定安装后的实测范围输出`near / normal / far / unknown`，仅用于交互提示和跟随速度调整，不能作为三维控制或安全依据。

SFace正式激活逻辑当前暂停。未来恢复时仍须满足：按新目标或低频确认运行、多帧特征融合、低质量拒绝、连续一致后绑定身份、默认只保存本地特征不保存原图、注册与删除必须由明确用户操作触发。身份识别不能成为普通跟随的依赖。

物体检测是按需能力，不进入持续基础负载。Agent只能调用高层`find_object(name)`之类的能力；Vision在有限时间内加载或唤醒检测器、检查最新帧、返回类别与位置，然后释放或休眠模型。具体模型进入开发时根据Pi5实测选择，本文不预先锁定YOLO或其他实现。

## 14. Agent、网页与高层接口

建议高层接口保持少量、稳定：

```text
start_visual_tracking(target="face" | "hand")
set_tracking_target(target="face" | "hand")
stop_tracking()
start_work_light_hand_guidance()
stop_work_light_hand_guidance(hold_current=True)
get_vision_state()
find_object(name)  # 能力实现后再开放
```

`get_robot_state()`可增加：

```text
vision_running
tracking_target_kind
target_visible
target_age_ms
face_count
hand_count
last_gesture
visual_tracking_active
work_light_visual_state
```

Agent和网页不得取得模型对象、摄像头线程、原始关节目标、控制参数或串口访问权。视觉检测事件不会自动调用Agent；需要语义决策时必须由明确用户请求或app中的确定性规则触发。

## 15. 配置、依赖、模型与隐私

新增`vision.conf`管理摄像头、模型路径、调度频率、检测阈值、手势确认时间、目标丢失时间和视觉会话开关。机械速度、加速度、死区、软限位和关节映射属于`motion.conf`。

正式Python依赖建议放入项目的`vision` optional extra：

```text
opencv-python-headless
mediapipe
```

项目已有NumPy与ONNX Runtime。Pi使用硬件与视觉extra安装；非视觉开发环境不必加载模型依赖。正式模块应延迟导入视觉库，在视觉被禁用或依赖缺失时给出明确状态，不阻止语音app启动。

模型二进制不提交Git。设备模型目录需受同步脚本保护，并记录模型名称、版本、来源、SHA-256和输入规格。身份特征、快照和诊断素材放入受保护的运行状态或诊断目录，不进入代码同步和Git。

## 16. 故障与健康状态

Vision健康状态至少包含：

- 摄像头是否打开及实际协商格式；
- 最新帧序号与年龄；
- Face/Hand实际完成频率；
- 推理P50/P95；
- 主动跳帧数与读取失败数；
- 最新结果年龄；
- 模型加载状态；
- 最近错误。

故障处理原则：

- 摄像头断开或模型失败时停止产生机械目标；
- tracking runner收到目标失效后安全减速并退出或等待恢复；
- app、语音、Agent和灯光继续运行；
- 不进行无限高频重试；
- 恢复摄像头必须重新协商格式并清空所有旧目标和手势历史。

## 17. 文件组织

按实际开发需要创建：

```text
lelamp/vision/
├── __init__.py
├── config.py             # vision.conf读取与校验
├── types.py              # 帧、检测、目标和事件数据结构
├── camera.py             # 单一最新帧采集源
├── face.py               # YuNet适配
├── hands.py              # Gesture Recognizer适配
├── target.py             # 短期关联和目标保持
├── gestures.py           # 静态手势确认与防重复
└── controller.py         # 生命周期与联合调度

lelamp/motion/
└── visual_tracking.py    # 图像视觉伺服与流式关节控制

vision.conf
tests/
├── test_vision_target.py
├── test_gesture_events.py
├── test_visual_tracking.py
└── test_vision_lifecycle.py
```

动态手势、身份和物体检测等文件只在对应能力开始实现时添加。现有`gesture_live_test.py`和`sface_live_test.py`保留为独立实验程序，不被正式app导入。

## 18. 开发流程

### 18.1 正式基础与生命周期

- 建立`vision.conf`、正式数据结构、最新帧采集和VisionController。
- 将`voice_session_active`与机械持续模式分离。
- 复用WORK_LIGHT的持续模式语义，使语音超时保留tracking，明确sleep终止tracking。
- 增加持续模式感知的唤醒入口，确认tracking和WORK_LIGHT中再次唤醒不会调用standby破坏原姿态。
- 为`ToolExecutor`调用补充明确来源，保持所有硬件动作仍经过同一app仲裁。
- 接入视觉健康状态，但不发送舵机命令。

### 18.2 联合感知与单人目标

- 正式接入YuNet与Gesture Recognizer。
- 同帧联合运行，默认各自最高10 Hz。
- 实现单人前景选择、背景小脸过滤、目标锁定和短时丢失。
- 对比8/10/15 Hz的资源与感知结果，记录提高频率是否有实际收益。

### 18.3 完整人脸机械跟随

- 完成相机内参、安装方向和关节图像响应标定。
- 实现Motion流式跟踪控制，并让长期tracking复用现有Barge-in运动任务门控。
- 接入`LampApp`的tracking持续模式。
- 验证目标过期、取消、临时动作、模式恢复和无目标退出。
- 完成视觉满载下的语音、TTS和Barge-in实机回归。

### 18.4 手部跟随与静态手势

- 复用同一`TrackingTarget`和机械控制器。
- 允许在同一tracking任务内切换face/hand。
- 实现手势稳定确认、解除、冷却和模式许可。
- 确认人脸跟随期间手势切换始终可用。
- 接入WORK_LIGHT长期手势感知、手部引导子状态和自定义照明姿态恢复。

### 18.5 动态与按需能力

- 增加规则型Wave、Swipe和Move方向手势。
- 增加带语音意图窗口的二维指向。
- 增加视觉存在与自适应启停。
- SFace激活和物体检测分别等待明确开发指令，不与核心跟随捆绑。

每一步都在同一最终架构内增加能力，不建立一次性正式app，也不在完成后迁移到另一套运行模型。

## 19. 测试与验收

### 19.1 自动测试

- 最新帧覆盖、序号递增、停止释放和摄像头故障状态。
- 坐标转换、镜像、旋转和结果年龄。
- 单人目标保持、背景小脸过滤、短时丢失和重新选择。
- 手势按时间确认、持续只触发一次、解除和冷却。
- face/hand切换不创建第二个运动任务。
- 过期视觉结果不产生新关节位移。
- 关节目标满足软限位、速度和加速度限制。
- tracking → 临时动作 → tracking恢复。
- 临时动作期间tracking请求和手势事件被丢弃，结束后旧事件不补执行。
- tracking中sleep或stop不会被旧任务恢复。
- 语音超时保留tracking，再次唤醒不破坏tracking。
- tracking停止且语音已结束时按规则进入sleep。
- WORK_LIGHT语音超时和再次唤醒均保持照明；手部引导与自定义照明姿态能在临时动作后正确恢复。
- 不同`ToolExecutor`来源不会误入Agent自主情绪动作队列。

### 19.2 实机验收

- 摄像头实际协商为640×480@30 MJPEG。
- Face和Hand平均频率、P50/P95和主动跳帧符合配置语义。
- 不产生帧队列积压，结果年龄不会持续增长。
- 单人常用距离下目标选择稳定，背景小脸不抢目标。
- 人脸出现后约500 ms内锁定；稳定目标中心误差中位数不超过画面宽高的5%，P95不超过10%。
- 跟随无明显高频抖动、突然跳动或关节越界。
- 视觉结果超过250 ms时不继续追逐旧目标。
- 停止跟随和模式切换后约200 ms内停止产生新的跟随命令。
- 人脸跟随过程中静态手势识别和目标切换可用。
- 手势持续保持只触发一次，多人/多手歧义不执行副作用。
- WORK_LIGHT中手势感知长期可用，手部引导能移动并保持任意软限位内的照明方位。
- 运行30分钟无摄像头失败、swap增长、任务积压或明显RSS持续增长。
- 在视觉负载和舵机运动下验证KWS、ASR、TTS、自然Barge-in和运动停止词。

## 20. 明确不采用的方案

- 第二个正式视觉app或独立视觉微服务。
- Vision直接访问舵机或绕过`LampApp`运动锁。
- 为视觉新增通用Event Bus、Behavior Manager或复杂状态机。
- 人脸跟随关闭手势感知，或手部跟随关闭人脸感知。
- 无界帧队列、推理结果队列或运动命令队列。
- 每帧运行身份识别、物体检测或大型视觉模型。
- 将人脸可见性当作可靠人员存在检测。
- 使用单目人脸框推断精确三维坐标并驱动完整IK。
- 在没有明确意图和防抖的情况下让逐帧手势直接控制硬件。
- Face Mesh、Holistic、全身Pose、连续VLM视频推理和为假设需求预建的插件系统。

本文与项目级架构共同记录后续实现当前应遵循的设计。视觉功能的详细设计变更在本文维护；项目模块边界、统一仲裁和跨模块原则在`archetecture.md`维护。开发过程中允许根据实测和用户要求修改两份文档，但不能只改代码而留下相互矛盾的旧设计。
