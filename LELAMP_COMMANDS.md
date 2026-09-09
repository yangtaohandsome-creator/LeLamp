# LeLamp 常用测试与动作命令（lamppi）

更新：2026-09-09。命令已按树莓派现有代码和校准文件核对。

## 1. 固定配置与进入项目

| 项目 | 当前值 |
| --- | --- |
| SSH | `lamppi@192.168.40.77` |
| 项目目录 | `/home/lamppi/lelamp_runtime` |
| 灯的 ID | `lamppi` |
| 舵机串口 | `/dev/ttyACM0` |
| 默认声卡 | `seeed2micvoicec` |
| RGB | Pi 5 专用驱动，GPIO12，64 颗 LED |

灯 ID 已根据现有的 follower、leader 校准文件 `lamppi.json` 确认，不是仅按用户名猜测。

在电脑上连接：

```bash
ssh lamppi@192.168.40.77
```

登录后先执行一次；以下测试命令均在这个目录运行：

```bash
cd /home/lamppi/lelamp_runtime
```

`--no-sync` 使用已经安装好的环境，不在每次测试时重新同步依赖。普通命令无需 sudo；RGB 需要 sudo 并使用 uv 完整路径。

## 2. 麦克风和扬声器

### 完整音频测试

```bash
uv run --no-sync -m lelamp.test.test_audio
```

依次播放 3 秒提示音、录音 3 秒、回放录音。看到 `Recording from microphone...` 时说话。

### 只测试扬声器

```bash
speaker-test -D plughw:seeed2micvoicec,0 -c 2 -t sine -f 440
```

循环播放测试音，按 Ctrl+C 停止。

### 查看播放、录音设备和调音量

```bash
aplay -l
arecord -l
alsamixer -c seeed2micvoicec
```

alsamixer 内左右键选通道、上下键调音量、M 切换静音、Esc 退出。调整完成后保存：

```bash
sudo alsactl store seeed2micvoicec
```

## 3. RGB 灯板

```bash
sudo /home/lamppi/.local/bin/uv run --no-sync -m lelamp.test.test_rgb
```

测试红、绿、蓝、彩色图案、优先级，最后熄灭。驱动已修复，红绿蓝已现场确认，官方完整测试已通过。

查看驱动服务：

```bash
systemctl status lelamp-rgb-driver.service --no-pager
journalctl -u lelamp-rgb-driver.service -b --no-pager
```

若服务未启动：

```bash
sudo systemctl start lelamp-rgb-driver.service
```

维护记录：`/home/lamppi/lelamp-drivers/RGB-REPAIR.md`。
系统内核升级后，安装匹配的新内核头文件，再运行：

```bash
bash /home/lamppi/lelamp-drivers/rebuild-rgb-module.sh
```

## 4. 串口与舵机测试

### 查看串口

```bash
ls -l /dev/ttyACM0
ls -l /dev/serial/by-id/
```

如以后编号变化，可以重新查找：

```bash
uv run --no-sync lerobot-find-port
```

查找程序会提示拔插控制板 USB；当前端口已知，不必每次查找。

### 舵机功能测试（会运动）

```bash
uv run --no-sync -m lelamp.test.test_motors --id lamppi --port /dev/ttyACM0
```

这个程序会尝试播放录制目录中的第一个动作，并非只读检测。运行前给灯头留出活动空间，不要同时运行另一份 record、replay 或语音控制程序。

## 5. 录制自己的动作：record

### 录制一段测试动作

```bash
uv run --no-sync -m lelamp.record --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 30
```

1. 等待连接完成，看到提示后按 Enter 开始。
2. 手动引导灯做动作，避免强推到机械限位。
3. 按 Ctrl+C 结束录制。

保存到：

```text
/home/lamppi/lelamp_runtime/lelamp/recordings/my_first_motion.csv
```

**同一个 name 会覆盖已有 CSV。** 下次录不同动作只改 `--name` 后的名字即可，ID 和串口不用改；名字不带 `.csv`。建议给自录动作加 `my_` 前缀，避免覆盖仓库自带动作。

## 6. 回放自己的动作：replay

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 30
```

正常播完一次后退出。回放会使舵机主动运动，开始时可能先移动到录制的第一帧姿态。

慢速回放同一段 30 fps 录制：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name my_first_motion --fps 15
```

当前代码按指定 fps 逐行发送动作，不按 CSV 的 timestamp 还原节奏。30 fps 录制、15 fps 回放大约是半速；这不会改变首帧的目标姿态。

## 7. 列出和回放已有动作

### 列出动作

```bash
uv run --no-sync -m lelamp.list_recordings --id lamppi
```

当前代码实际上列出整个 `lelamp/recordings/` 下的 CSV，不按 ID 分目录。ID 主要用于选择校准数据。

目前目录中有：`curious`、`excited`、`happy_wiggle`、`headshake`、`idle`、`nod`、`rotation`、`sad`、`scanning`、`shock`、`shy`、`wake_up`。这些是现有文件清单，不代表每段动作都已在本机验证。

点头：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name nod
```

摇头：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name headshake
```

唤醒动作：

```bash
uv run --no-sync -m lelamp.replay --id lamppi --port /dev/ttyACM0 --name wake_up
```

其他动作仅替换 `--name`，不需要修改灯 ID 或串口。

## 8. 舵机初始化与校准（非日常测试）

目前已存在两套 `lamppi.json` 校准文件。只有首次设置、更换舵机或需要重新校准时才用本节，不要当作每次启动步骤。

### 分配舵机 ID

```bash
uv run --no-sync -m lelamp.setup_motors --id lamppi --port /dev/ttyACM0
```

会写入舵机设置。按程序提示逐个连接指定舵机，不要直接把它当作所有舵机并联后的普通测试。

### 依次校准 follower 与 leader

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0
```

当前用户已有 dialout 权限，使用普通用户运行，以保持校准数据位于 `/home/lamppi/.cache/`；用 sudo 可能改用 root 的校准目录。

当前本机代码还支持单独校准：

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0 --follower-only
```

```bash
uv run --no-sync -m lelamp.calibrate --id lamppi --port /dev/ttyACM0 --leader-only
```

校准文件：

```text
/home/lamppi/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lamppi.json
/home/lamppi/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lamppi.json
```

## 9. 依赖维护和排错

需要安装或恢复依赖时，普通用户执行：

```bash
uv sync --locked --extra hardware
uv pip check
```

Pi 5 驱动已通过项目配置固定到 `/home/lamppi/lelamp-drivers/rpi-ws281x-python/library`，请保留该目录。

| 现象 | 处理 |
| --- | --- |
| sudo 找不到 uv | 使用上面的完整路径 |
| RGB 报 Hardware revision is not supported | 检查是否被换回旧版驱动，参阅 RGB-REPAIR.md |
| /dev/ttyACM0 不存在 | 检查控制板供电和 USB 数据线，重新查找串口 |
| 找不到录制文件 | 先 list_recordings，name 不带 .csv |
| 校准数据找不到 | 保持 --id lamppi，并在 lamppi 用户下运行舵机命令 |
| 串口忙或舵机通信异常 | 先退出其他占用串口的控制程序 |

## 10. 语音主程序：后续配置项

官方还有 `main.py download-files`、`main.py console` 和 `smooth_animation.py console`，但当前主程序仍有 `lamp_id="lelamp"` 硬编码，与现有 `lamppi` 校准 ID 不一致；语音服务凭据和 sudo 下的校准路径也尚未完成核对。它们不是上述硬件测试的前置条件，完成这些配置后再启动。本次只整理命令，没有修改或启动语音主程序。

## 来源和验证范围

- [官方 Setup](https://github.com/humancomputerlab/LeLamp/blob/master/docs/4.%20LeLamp%20Setup.md)
- [官方 Control](https://github.com/humancomputerlab/LeLamp/blob/master/docs/5.%20LeLamp%20Control.md)
- [Runtime 仓库](https://github.com/humancomputerlab/lelamp_runtime)
- 参数和具体行为以本机 `lelamp/*.py`、`lelamp/test/*.py` 的实际代码为准。
- 本次整理未执行舵机初始化、校准、录制或回放，也未改写已有校准文件。

