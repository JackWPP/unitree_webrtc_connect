# 真机验证清单

> 本仓库的所有代码路径都已在**离线 Mock 后端**上跑通（`unitree-robot-mcp selftest`
> 两个机型各 20/20）。但 Mock 复刻的是*已知行为*，不是固件。
> **第一次接真机时，请按下表逐条实测，并把结果记回来。**

## 已实测回填（2026-09-13，R1 **Air**，SN `E39N1000Q6G8448E`，alias R1_04846）

连接方式：STA-L / `192.168.31.237:9991`（`con_notify` 路径）/ region `cn`。

| 结论 | 状态 |
|---|---|
| `con_notify` 返回 `data2=3` → 必须用 per-device key | ✅ 实测确认 |
| 账号 region 为 **cn**（global 报 `email not register`） | ✅ 实测确认 |
| **开机默认状态就是 `fsm_id=830` (Loco20Dof)** —— Air 上 811→830 的重定向**真实存在** | ✅ 实测确认 |
| 电机数组 20 个有效槽：0-11 / 15-18 / 22-25，`lost` 全 0 | ✅ 实测确认，与 `R1_MOTOR_INDICES` 完全一致 |
| 电机温度是 `[casing, winding]` 双元素，28~34 °C | ✅ 实测确认 |
| 电池：SOC 25%、pack 30.77 V、9 节 cell 有效、极差 10 mV | ✅ 实测确认（**低电量，见下**） |
| 主板温度 43 °C | ✅ 实测确认 |
| IMU 温度上报 **81 °C**（躯干与骨盆一致） | ⚠️ 实测值，偏高但两侧一致，疑似固件以固定偏置上报，暂按正常对待 |
| `robot_stop` 软停（SET_VELOCITY [0,0,0]）返回 code 0，无位移 | ✅ 实测确认 |
| 不带 `confirm` 的 `robot_move` 被**本地**拒绝（`confirm_required`），未向机器人发出任何指令 | ✅ 实测确认 |
| 空闲自动释放链路 + 下次调用自动重连 | ✅ 实测确认 |
| 机器人自身端口：`9991`(信令) + `8551`(**GStreamer RTSP 相机**)，其余全关 | ✅ 实测确认 |
| 组播发现：显式绑定 en0 发 9 条查询仍**无应答** | ✅ 实测确认 R1 只回应点名自己 SN 的查询 |
| 手臂手势 `release` / `handshake` / `release` 全部 `code=0` | ✅ 实测通过 |
| `robot_joystick(ly=0.1, duration_s=0.3)` 成功，动作后姿态稳定、自动归零 | ✅ 实测通过 |
| **`SET_VELOCITY`(7105) 在 830 状态返回 `127`**（无 speed handler） | ✅ 实测确认，已加自动回退 |
| `robot_move` 命中 127 后**自动回退摇杆通路**，返回 `transport=joystick_fallback` | ✅ 实测通过 |
| `rt/lf/sportmodestate` 只有 `fsm_id / fsm_mode / task_id / task_time` 四个字段 | ✅ 实测确认（**没有** position/velocity，无法读位移） |
| **无可用相机**：WebRTC 20s 内无 track 到达；RTSP `8551` 只声明 ANNOUNCE/RECORD（推流端），所有挂载点 404 | ✅ 实测确认（Air 无摄像头或未启动） |
| 姿态/tilt：动作后 IMU 显示躯干 pitch ≈ 8.4°、两个 IMU 一致、同会话内 yaw 稳定 | ⚠️ 实测记录，需人眼确认是否为正常站姿 |

### 低电量警告

实测时 SOC 仅 **25%**，接近本 skill 设的 20% 下限。**做大幅度动作（舞蹈/起身/武术）
前先充电**：这些动作电流冲击大，低电量下有触发掉电保护、直接摔落的风险。

### 仍未确认（下次动起来时补）

- [ ] `SET_VELOCITY` 的**物理单位**：因为 830 直接回 127，本次没能测到。
      需要先切到有 speed handler 的状态（如 `811`/`815`/`816`）再试
- [ ] `duration` 单位是否为秒（同上，未验证）。注意：现在窗口结束会**显式发停**
      （R1 发 `SET_VELOCITY [0,0,0]`），所以该单位只影响窗口内的速度，不再决定何时停
- [ ] 830 的「固定 vx 1.0」是否真会**无视摇杆的幅度**（本次 `ly=0.1` 未见异常速度，
      但缺少位移量测，无法定量）
- [ ] `robot_joystick` 的 **`lx` / `rx` 真机符号**：代码按 `[ly, -lx, -rx] -> [vx, vy, omega]`
      映射（`lx` 正数 = 右移，`rx` 正数 = 顺时针），与文档一致，但真机方向未实测
- [ ] Air 上 601 / 603 / 608 是否被拒（`dances_air.yaml` 只有 602/604/607）。
      在确认之前，`UNITREE_AGENT_CHASSIS=air` 只在**显式声明**时隐藏这三个动作；
      默认 `unknown` 保持全量目录
- [ ] `robot_switch_state('Run')` 在真机上的回读（当前已在 830，需先切走再验证）
- [ ] `robot_snapshot` 在**有相机的机型**上是否可用（本机 Air 无相机，无法验证）

### 建议的下一轮测试顺序

1. **先充电到 50% 以上**（本轮从 25% 掉到 22%，大幅动作有掉电风险）。
2. 人眼确认机器人站姿是否正常（IMU 显示 8.4° 前倾）。
3. `robot_arm_gesture("handshake")` 再确认一次手臂动作符合预期。
4. `robot_switch_state("Lock")` → 观察是否从 830 平顺切到 4（顺便验证 `Lock` 可达性）。
5. 在 Lock 下尝试 `robot_switch_state("AmpLocomotion" / "ArmSdkLoco")` 等，找到**有 speed
   handler 的状态**，再回填 `SET_VELOCITY` 的单位。
6. 舞蹈类：先 602 → 604 → 607（Air 白名单内），再试 601/603/608 看是否被拒。

## 准备

- [ ] 已读 `safety.md`，现场净空 ≥ 2 m，有人在场，急停可达
- [ ] `unitree-robot-mcp doctor` 输出 `verdict: connected`
- [ ] 若条件允许：**悬挂试机**（轮/脚离地）

## A. 只读（零风险，先全部跑一遍）

| # | 调用 | 期望 | 实测 |
|---|---|---|---|
| A1 | `robot_status` | `connected: true`，机型正确 | ☐ |
| A2 | `robot_get_posture` | 有合理的 `state` / `semantic` | ☐ |
| A3 | `robot_list_actions` | R1 34 项 / Go2 22 项左右，无异常缺失 | ☐ |
| A4 | `robot_lowstate` | `soc` 合理；R1 电机下标 = 0-11/15-18/22-25 | ☐ |
| A5 | `robot_lowstate(include_motors=true)` | 温度是**个位数到几十度**，不是 250 这种值 | ☐ |
| A6 | `robot_errors` | 空列表或能看懂的已解码错误 | ☐ |
| A7 | `robot_audit_tail` | 能看到前面几次调用 | ☐ |

## B. 连接与生命周期

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| B1 | 静置 60 s（默认 45 s 空闲） | server 自动断开；**官方 App 此时能连上** | ☐ |
| B2 | 断开后再调 `robot_status` | 自动重连成功，`stats.connects` +1 | ☐ |
| B3 | 保持 App 连接时调工具 | 返回 `robot_busy` 且 hint 正确 | ☐ |
| B4 | server 关闭（Ctrl-C） | 连接释放，无残留占线 | ☐ |

## C. R1 状态机（核心）

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| C1 | `robot_stand` | 进入 Lock(4)，姿态站稳 | ☐ |
| C2 | `robot_switch_state("Run")` | 返回值 `redirected: true`，回读 830 或 811 | ☐ |
| C3 | 在 Run 时直接 `robot_switch_state("Dance1")` | 本地 `guard_rejected` 或机器人 1001 | ☐ |
| C4 | 先 `robot_stand` 再 Dance1 | 接受并开始跳舞 | ☐ |
| C5 | `robot_switch_state("Damping")` | 接受（无需 confirm） | ☐ |
| C6 | `robot_switch_state("Keep")` | 接受；随后 `robot_get_posture` 显示 `frozen` | ☐ |
| C7 | 进入 Keep 后立刻 Damping | 状态上报恢复 | ☐ |
| C8 | `robot_switch_state("ZeroTorque")`（从 Lock） | 本地拒绝并提示只能从 Damping | ☐ |
| C9 | `robot_play_action("stand_up")` | 起身成功（R1 Air 用专用 ONNX） | ☐ |
| C10 | **请求一个不存在的状态** | 返回 `1003`，不是挂死 | ☐ |
| C11 | **记录 830 是否真的忽略速度调节** | 见 D3 | ☐ |

## D. 移动（从最保守开始）

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| D1 | `robot_move(vx=0.1, duration_s=0.3, confirm=true)` | 只迈一小步就停 | ☐ |
| D2 | 同上的返回 | `duration_s` 未被截断；机器人已停 | ☐ |
| D3 | 在 830 状态下 `robot_move(vx=0.2)` | 记录实际速度是否可调（预期**不可调**） | ☐ |
| D4 | `robot_move(duration_s=600)` | 返回 `duration_s: 10`（被截断）+ clamped 说明 | ☐ |
| D5 | `robot_joystick(ly=0.3, duration_s=0.5, confirm=true)` | 平滑起步、结束归零 | ☐ |
| D6 | 移动中途 Ctrl-C 关掉 server | 机器人停止移动（不是继续走） | ☐ |
| D7 | `robot_stop` | 立即停，**姿态不变**（没有倒下） | ☐ |
| D8 | `robot_emergency_stop(confirm=true)` | 进入 Damping；**记录实际是否倒下** | ☐ |

## E. Go2 专属

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| E1 | `go2_get_state` | 有 sport state 与 multi state | ☐ |
| E2 | `go2_set_led(color="cyan", seconds=3)` | 灯变青色 3 秒 | ☐ |
| E3 | `go2_set_volume(3)` → `go2_get_vui()` | 读回 volume=3 | ☐ |
| E4 | `go2_obstacle_avoid(enable=true)` → 再读 | 读回 enabled=true | ☐ |
| E5 | 开启避障后 `robot_joystick(ly=0.6)` 朝障碍物 | 被避障服务拦截/减速 | ☐ |
| E6 | `robot_move(vx=0.3, duration_s=1)` | 走完 1 s 后**自己停下**（StopMove 生效） | ☐ |
| E7 | `go2_motion_mode("ai", confirm=true)` | 切到 AI 模式（机器人站起，等待 ~10 s） | ☐ |
| E8 | `go2_motion_mode("normal", confirm=true)` | 切回，sport 指令恢复可用 | ☐ |
| E9 | `go2_lidar(enable=true)` | 返回点云摘要（point_count / resolution） | ☐ |

## F. 媒体

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| F1 | `robot_vision_probe` | 明确列出哪条来源可用、其余为何不可用 | ☐ |
| F2 | `robot_snapshot` | 返回一张能看懂的 JPEG，不是花屏 | ☐ |
| F3 | 连续抓 3 帧 | 都能出图，不卡死 | ☐ |
| F4 | `robot_snapshot_history` | 列出刚才的帧（含路径与尺寸） | ☐ |
| F5 | 抓图后立刻 `robot_move` | 视频通道不阻塞控制指令 | ☐ |
| F6 | 本地源 | 设 `UNITREE_AGENT_VIDEO_SOURCE=file` + `SNAPSHOT_FILE` 后仍能出图 | ☐ |

## I. 运动控制（新增）

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| I1 | `robot_turn(45, confirm=true)` | 实测转过角度接近 45°，`residual_deg` 在容差内 | ☐ |
| I2 | `robot_turn(-45, confirm=true)` | 反向转，IMU yaw 减少 | ☐ |
| I3 | 连续 `robot_turn(90)` 两次 | 累计约 180°，不跑偏 | ☐ |
| I4 | `robot_turn(0)` | 直接返回 `skipped`，不需要 confirm | ☐ |
| I5 | `robot_joystick(ly=0.3, duration_s=0.5)` | 返回 `profile.shape`，起步/收尾平滑、结束归零 | ☐ |
| I6 | 对比 `duration_s=0.2` 与 `0.4` 的实际位移 | 短动作也不再"几乎没动"（曲线改造的验收点） | ☐ |
| I7 | 转向中途 Ctrl-C | 摇杆归零，机器人停转 | ☐ |
| I8 | 读不到 IMU 时 `robot_turn` | 报 `no_imu` 而不是瞎转 | ☐ |

## G. 安全护栏回归

| # | 操作 | 期望 | 实测 |
|---|---|---|---|
| G1 | `UNITREE_AGENT_SAFE_MODE=true` 启动 | 移动类返回 `guard_rejected`；`robot_stop` 仍可用 | ☐ |
| G2 | `UNITREE_AGENT_ALLOW_DANGEROUS=false` | 舞蹈返回 `guard_rejected`；`hello` 手势可用 | ☐ |
| G3 | 审计日志 | 每次调用一条记录，**不包含 AES key / 密码** | ☐ |
| G4 | 无 AES key 启动 | `robot_doctor` 明确提示如何取 key | ☐ |

## H. 需要回填的未知项

这几项在离线阶段**无法确认**，实测后请更新文档：

- [ ] `SET_VELOCITY`(7105) 的 vx/vy/omega **物理单位**（m/s? rad/s? 归一化？）
- [ ] `SET_VELOCITY` 的 `duration` 是秒还是别的单位
- [ ] 830 (Loco20Dof) 是否真的完全忽略速度调节
- [ ] Air 上 601 / 603 / 608 是否被拒（`dances_air.yaml` 只有 602/604/607）
- [ ] `rt/sportmodestate`（无 `lf/`）与 `rt/lf/sportmodestate` 的字段差异
- [ ] `robot_errors` 的 `errors` 快照是否在连接时主动推送
- [ ] 冻结状态清单是否还有遗漏（本次列为 5/6/7/800/813/814）

> 发现与文档不符时，**先信机器人**，然后同时更新
> `unitree_robot_agent/robots/*.py` 与技能文档，并补一个测试用例。
