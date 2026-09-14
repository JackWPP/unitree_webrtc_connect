# 运动控制

## 三种移动方式，怎么选

| 工具 | 闭环？ | 何时用 |
|---|---|---|
| `robot_move(vx, vy, omega, duration_s)` | 部分（时长由服务端强制） | 默认选择。R1 走 `SET_VELOCITY`，Go2 走 `Move`+`StopMove`，Air 上会自动换成摇杆 |
| `robot_joystick(lx, ly, rx, duration_s)` | 否 | 需要原始摇杆控制，或要配合 Go2 避障服务（它拦截摇杆消息）时。轴向符号：`lx` 正数 = 右移，`rx` 正数 = 顺时针（右转），`ly` 正数 = 前进——即下面的 `[ly, -lx, -rx]` 映射 |
| `robot_turn(degrees)` | **是**（IMU yaw 反馈） | 需要"转 90 度"这种**角度**目标，而不是"转一小会儿" |

**优先用 `robot_turn` 而不是"给 rx 猜一个时长"**：后者依赖速度标定，而标定会随地面、
电量、负载变化。

## 速度曲线（2026-09 改进）

`robot_joystick` 和 `robot_move` 的摇杆回退路径现在按**梯形/三角形速度曲线**下发：

```
速度
  ^
  |      ______
  |     /      \          trapezoid（时长足够）
  |    /        \
  +---/----------\-------> 时间
      0          T

  |       /\
  |      /  \             triangle（时长短，冲不到峰值）
  +-----/----\---------> 时间
```

关键参数（环境变量可调）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `UNITREE_AGENT_MOTION_ACCEL` | `1.5` | 加速度（摇杆单位/秒²）。1.0 = 满速 |
| `UNITREE_AGENT_MAX_MOVE_S` | `10` | 单次移动硬上限 |
| `UNITREE_AGENT_MAX_TURN_SPEED` | `0.4` | 转向时 `rx` 的峰值 |
| `UNITREE_AGENT_TURN_TOLERANCE_DEG` | `4` | 转角误差小于此值即停止 |
| `UNITREE_AGENT_MAX_TURN_SECONDS` | `20` | 单次转向看门狗 |

**为什么改**：旧实现用固定 0.15/帧的斜坡，20 Hz 下需要 ~0.35 s 才到目标速度。
于是 `duration_s=0.3` 的"小步走"**根本到不了指定速度**就结束了，实际位移与请求值无关。
现在短动作会走三角形曲线（依然到达峰值），长动作走梯形，并且**曲线末端恒为 0**，
所以不需要额外的减速阶段，也不会出现"窗口结束后机器人还在走"。

返回值里带 `profile`，可以看到这次实际用的曲线：

```json
{"peak": 1.0, "duration_s": 0.4, "ramp_s": 0.2, "shape": "triangle", "travel_normalised": 0.2}
```

`travel_normalised` 是曲线积分，也就是**归一化位移**——乘上机器人自身限速就是大致距离。

## 闭环转向 `robot_turn`

```
输入：degrees（正 = 逆时针/向左）
过程：读 IMU yaw → 算最短角误差 → 比例控制 rx → 循环至误差 < 容差
输出：turned_deg（实测转过的角度）、residual_deg（剩余误差）、within_tolerance
```

实测（Mock）：请求 90° → 实测 86.4°，残差 3.6°，72 帧（约 3.6 s）。

要点：

- **正数 = 逆时针/向左**（IMU yaw 正方向）。机器人侧的映射是
  `[ly, -lx, -rx] -> [vx, vy, omega]`，所以代码里用 `rx = -kp * error`。
- **读 `residual_deg`，不要假设转到了**。`ok` 只表示"误差在容差的 2 倍以内"；
  `within_tolerance` 才是严格的。
- **180° 方向是模糊的**：±180 都是同一个朝向，`wrap_angle` 会选最短路径，
  实测可能往任一方向转。要指定方向就分成两次 90°。
- 读不到 IMU 时**直接报错**（`no_imu`），不会瞎猜——退回 `robot_joystick` 手动控制。
- 转向期间会 `hold()` 连接，空闲回收不会把链路断掉。
- 结束（含异常路径）一定把摇杆归零。

## 归一化速度的含义

`vx/vy/omega` 与 `lx/ly/rx` 都是**归一化值**，范围 ±1.0，由机器人乘以自身限速
（默认前进 1.0 m/s）。所以 1.0 = "这台机器人的满速"，不是 1 m/s。

实测对照（R1 Air）：

- `ly=0.1` 约等于"很慢的一小步"。
- `SET_VELOCITY` 的**物理单位仍未确认**——因为 Air 在 830 状态直接返回 127，
  本次真机测试没能测到。需要切到有 speed handler 的状态后回填。

## 安全边界

1. 所有移动类调用都要 `confirm=true`（`robot_turn(0)` 除外，它什么也不做）。
2. `duration_s` 有硬上限，请求 600 s 会被截断为 10 s 并在返回值里注明。
3. 任何异常路径都会**归零摇杆**；`robot_stop` 是软停（保持姿态），不会 Damping。
4. 移动类工具在只读安全模式（`UNITREE_AGENT_SAFE_MODE=true`）下直接拒绝。
5. 转向看门狗到点即停，不会无限循环。

## 组合动作的正确姿势

```
robot_stand                                      # 进入已知姿态
robot_turn(90, confirm=True)                     # 闭环转到目标朝向
robot_move(vx=0.3, duration_s=1.0, confirm=True) # 走一段
robot_stop                                       # 收手
robot_snapshot                                   # 看一眼确认
```

**一步一确认，每步看返回值**。不要写"转 90° 再走 2 米再转回来"这种复合计划——
中间任何一步的实际结果都可能与预期不同，而 Agent 看不到现场。
