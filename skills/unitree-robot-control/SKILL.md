---
name: unitree-robot-control
description: "Control one or more real Unitree robots (Go2 quadruped or R1 humanoid) through the unitree-robot-mcp server - read status, battery, motor and IMU telemetry, switch FSM states, walk, play dances and gestures, toggle Go2 LEDs and obstacle avoidance, and grab camera stills. Drives a whole roster: robot_roster lists the ids, every tool takes robot_id, and robot_stop_all stops the fleet. This skill should be used whenever the user asks to move, drive, stand, sit, dance, wave, inspect or debug a Unitree robot, or when a task depends on what the robot physically is doing. Trigger phrases: 控制机器人、让机器人动、让它走、跳舞、挥手、起身、躺下、机器人现在什么状态、机器人电量、宇树 Go2、宇树 R1、查看机器人、多台机器人、双狗、两只狗、机队、一起走. Do NOT use for simulated robots or for the Unitree mobile app UI."
---

# 宇树机器人控制（Go2 / R1）

## 这个 skill 是什么

它给 Agent 一套**规程**，配合 `unitree-robot-mcp` 这个 MCP server 使用。
MCP 提供能力（一双手），本 skill 提供规矩（何时动手、怎么动手、怎么收手）。

机器人是真实物理设备。**一次错误的调用可能让机器人摔倒、撞人或损坏硬件。**
本 skill 里的安全规则不是建议，是硬约束，MCP server 里也写死了同样的规则。

## 使用前提

1. MCP server 已连接到 agent host（stdio 或 streamable-http）。
2. 首次使用先跑 `robot_doctor`，确认：机型、连接方式、AES key 是否就绪、可选依赖是否装齐。
3. 如果不确定周围环境，**先停下来问操作者**。Agent 看不到机器人周围有什么。

## 标准工作流

```
robot_roster            # 0. 我在驱动谁？配了几台？默认是哪台？（多机时必做）
robot_doctor            # 1. 自检：配置/依赖/连通性/认证，所有问题一次性暴露
robot_status            # 2. 连上了吗？现在什么状态？安全模式开着吗？
robot_list_actions      # 3. 这台机器的动作清单（已按机型过滤）
robot_get_posture       # 4. 当前姿态，以及「这个读数可不可信」
robot_stand             # 5. 进入已知的站立姿态（R1→Lock，Go2→BalanceStand）
... 执行动作 / 移动 / 转向 ...
robot_stop              # 6. 收手：停止移动，保持姿态
robot_vision_probe      # 7. 有相机吗？（失败/无结果时先跑这个，再决定怎么抓图）
robot_snapshot          # 8. 抓一帧确认现场
```

**每一步都要看返回值再决定下一步。** 不要连续盲发指令。

### 多台机器人

`robot_roster` 返回几台，就说明这个 server 在驱动几台。**超过一台时，每个工具都要带
`robot_id`**——省略它作用在名册的**默认那台**上，而操作者说"让另一只过来"时，默认那台
几乎肯定不是他要的。id 不是机型名：两只 Go2 叫 `dog1` 和 `dog2`，不叫 `Go2`。

```
robot_roster            # 先看 id 和默认值，不要猜
robot_fleet_status      # 一次拿到所有机器人的状态，不必逐台轮询
robot_status robot_id=dog2
robot_stop_all          # 停下所有正在移动的，逐台返回结果
```

**`robot_stop_all` 必须检查 `not_stopped`**：链路不通的机器人会列在那里——它**没有**停下。
把 fleet 报告成"已安全"之前先看这个列表。

连接不上、或提示缺密钥时：

```
robot_auth_status       # 云端认证状态（哪个 region、有无缓存登录）
robot_fetch_key         # 取并缓存 per-device AES key（安全、可重复）
```

## 工具地图

### 舰队（多台机器人）

| 工具 | 用途 |
|---|---|
| `robot_roster` | 我配了哪几台：id、机型、角色、地址、默认是哪台。**多机时先调这个** |
| `robot_fleet_status` | 所有机器人的状态一次拿全（`live=true` 会真的去读机器人） |
| `robot_preflight` | 每台可达吗？固件要不要 per-device key（`data2=3`）？手上的 key 对不对？**只读**：发 `con_notify` 但从不发 SDP，**不占用机器人唯一的 WebRTC 客户端槽**，官方 App 连着时也能跑 |
| `robot_stop_all` | 停下所有正在移动的机器人，保持姿态。逐台返回结果 |
| `robot_emergency_stop_all` | 让所有已连接机器人阻尼（会瘫倒）。需要 `confirm=true` |

### 只读（随时可调，零风险）

| 工具 | 用途 |
|---|---|
| `robot_status` | 连接、机型、姿态、安全模式、能力清单 |
| `robot_get_posture` | 语义化姿态 + `reporting` 字段告诉你读数是否可信 |
| `robot_list_actions` | 可用动作与状态表（按机型过滤） |
| `robot_lowstate` | 电池 / IMU / 主板温度 / 电机表 |
| `robot_errors` | 当前活动错误（已解码成人话） |
| `robot_discover` | 局域网组播发现（**必须带 SN**，见下） |
| `robot_audit_tail` | 最近的操作审计记录 |
| `robot_doctor` | 自检报告，无真机也能跑 |

### 低风险（会动，但不需要 `confirm`）

| 工具 | 说明 |
|---|---|
| `robot_stop` | **软停**：速度归零，**保持姿态**。任何时候都可以调，这是默认的安全动作 |
| `robot_arm_gesture` | R1 手臂手势（握手/挥手/击掌/拥抱…）。注意手臂活动范围内的障碍物 |

### 高风险（需要 `confirm=true`，移动类还会被时长上限截断）

| 工具 | 说明 |
|---|---|
| `robot_move` | 定时移动。`vx` 前进 / `vy` 左移 / `omega` 左转，归一化到机器人自己的限速 |
| `robot_joystick` | 归一化虚拟摇杆，按梯形速度曲线下发 |
| **`robot_turn`** | **按角度转向，IMU 闭环**。正数 = 逆时针。需要"转 90 度"时用它，别用 rx 猜时长 |
| `robot_stand` / `robot_lie_down` | 站立 / 趴下 |
| `robot_switch_state` | R1：按语义名切任意 FSM 状态（Lock / Run / Dance1 / StandUp…） |
| `robot_play_action` | 两个机型通用的动作目录（舞蹈/武术/运动指令） |
| `robot_emergency_stop` | **Damping，机器人会失去力矩而倒下** |

### 视觉（只读）

| 工具 | 说明 |
|---|---|
| `robot_vision_probe` | **抓图失败时先跑这个**：哪条来源能用、为什么不能用 |
| `robot_snapshot` | 抓一帧并返回图像；同时归档 |
| `robot_snapshot_history` | 之前抓的帧在哪（时间、来源、尺寸、路径） |

> ⚠️ **不是所有机器人都有相机。** 实测的 R1 Air 完全没有：没有 WebRTC 视频轨、
> RTSP 没有可用挂载点、固件拍照 API 也没 handler。详见 `references/vision.md`。

### 账号与密钥（只读 / 本地）

| 工具 | 说明 |
|---|---|
| `robot_auth_status` | 云端认证状态：哪个 region 接受了账号、有无缓存登录、配了哪些凭据（不含密钥） |
| `robot_fetch_key` | 从云端取本机 per-device AES key 并缓存（0600）。安全、可重复调用 |

### Go2 专属

`go2_get_state`、`go2_motion_mode`、`go2_set_volume`、`go2_set_brightness`、
`go2_set_led`、`go2_get_vui`、`go2_obstacle_avoid`、`go2_lidar`。

## 不可绕过的硬规则

1. **移动必须有 `confirm=true`**，缺省直接拒绝并返回 `confirm_required`。
2. **时长有硬上限**（默认 10 s，`UNITREE_AGENT_MAX_MOVE_S`）。请求 600 s 会拿到 10 s +
   一条 `clamped` 说明，不会真的走 10 分钟。
3. **本地状态白名单**：能从当前状态判定机器人一定会拒绝的转换，本地就拒绝并给出原因。
   收到 `guard_rejected` 时不要原样重试，先按 `hint` 换状态。
4. **单客户端互斥**：机器人同时只接受一个 WebRTC 客户端。server 会在空闲约 45 s 后
   主动释放链路，好让操作者用官方 App 接管。**不要试图长时间占住连接。**
5. **任何异常路径都会回落到软停**（速度归零），而不是 Damping——因为 Damping 会让
   站立的机器人直接倒下，比「站着不动」更糟。
6. **多机时 `robot_id` 不是可选项**。省略它 = 作用于名册默认那台。要动"另一只"却省略
   id，就是动了错的那只。名字认不出来时先 `robot_roster`，**不要猜**。

## 关键语义陷阱（最容易误判的四件事）

### 1. R1 Air 上 811 → 830 是**成功**，不是失败

请求 `Run`(811)，机器人在 Air 底盘上会被固件重定向到 `Loco20Dof`(830)。
返回值里 `redirected: true, confirmed: true` —— **不要重试**。

### 2. 部分状态下回读会**冻结**

R1 的 5/6/7/800/813/814 等状态会整体停发 `sportmodestate`，回读停留在最后一次上报值。
`robot_get_posture` 的 `reporting: "frozen"` 就是这个意思。
**不要用「等状态变化」来判断命令是否生效**，那会永远等下去。

### 3. `1001` 和 `1003` 含义完全不同

| 码 | 含义 | 处理 |
|---|---|---|
| `1001` / `1002` | 当前状态的白/黑名单拒绝 | 先 `robot_stand`（进 Lock），再重试 |
| `1003` | 这个固件**没有**这个状态 | 别再试了，用 `robot_list_actions` 看真实清单 |

### 4. `timed_out` 是**沉默**，不是拒绝

部分 api_id（例如 R1 的 7002/7007/7107/7109）在固件里没有处理函数，请求发出去不会有任何回包。
收到 `timed_out` 时：**先让操作者确认机器人物理状态**，再决定是否重试。
不要把它当成失败，也不要当成成功。

### 5. `127` = 该状态没有这个命令的处理器（实测）

R1 在 **830 (Loco20Dof)** —— 也就是 Air 的**默认开机状态** —— 调 `SET_VELOCITY` 会立刻返回 `127`，
**不是参数错误也不是状态机拒绝**，而是这个状态压根没有 speed handler。

`robot_move` 遇到 127 会**自动改用摇杆通路**（官方 App 走的那条，`rt/wirelesscontroller`），
返回值里带 `transport: "joystick_fallback"`。看到这个字段说明已经替你换了路，**不要重试**。
需要精细控制速度/方向时，直接调 `robot_joystick`。

另外实测确认：R1 的 `rt/lf/sportmodestate` **只有** `fsm_id / fsm_mode / task_id / task_time`
四个字段，**读不到位移和速度**。想判断它走了多远，只能靠 IMU 姿态变化或视觉。

## 被占用 / 连不上时的降级路径

| 现象 | 处理 |
|---|---|
| `robot_busy` | 机器人被别的客户端占用。请操作者关掉官方 App / unitree_ui，等几秒再试 |
| `aes_key_missing` | R1 与 Go2≥1.1.15 需要 per-device key，见 `references/connection-and-keys.md` |
| `connect_failed` | 检查电源、网段、是否连到机器人 AP；详见 `references/troubleshooting.md` |
| 需要视觉验收 / 人手接管 | **让操作者打开 unitree_ui**——这是 UI 真正的价值位，不要抢连接 |

## 参考文档

按需加载，不要一次性全读：

| 文件 | 什么时候读 |
|---|---|
| `references/connection-and-keys.md` | 配置连接方式、取 AES key、环境变量清单 |
| `references/fsm-rules.md` | 状态机白名单、逃生舱、错误码细节 |
| `references/r1-vs-go2.md` | 两机型能力差异与工具映射 |
| `references/motion-control.md` | **移动/转向前**读：速度曲线、闭环转向、三种移动方式怎么选 |
| `references/vision.md` | 抓图相关：四条来源、离线开发、失败排查 |
| `references/safety.md` | **动手前**读：人机安全、试机流程、保修与硬件风险 |
| `references/troubleshooting.md` | 连不上、被占用、超时、认证、异常排查 |
| `references/verification-checklist.md` | 第一次上真机的逐条验证清单 |
