# R1 状态机规则（FSM）

R1 上**一切持续性的姿态、步态、动作都是同一个 FSM 状态**，通过一个 api 切换：

```
topic      rt/api/sport/request
api_id     7101  (SET_FSM_ID)
parameter  {"data": <fsm_id>}
回读       订阅 rt/lf/sportmodestate，读 payload["fsm_id"]
```

R1 的 loco server 实际只注册了 4 个 api：
`7001 (GET_FSM_ID)`、`7101 (SET_FSM_ID)`、`7105 (SET_VELOCITY)`、`7108 (SET_MOTION)`。
**其余 api_id 在固件里存在但没有处理函数，发出去不会有任何回包**（不是拒绝，是沉默）。

## 状态表与语义分组

| fsm_id | 名称 | 语义 | 会动？ | 需要先 Lock？ | 回读可信？ |
|---|---|---|---|---|---|
| 0 | ZeroTorque | 电机失力 | ⚠️ 会掉 | 只能从 Damping | ✅ |
| 1 | Damping | 软停 / **逃生舱** | 否 | 任意状态可进 | ✅ |
| 4 | Lock | 站立姿态（枢纽） | 否 | 任意状态可进 | ✅ |
| 5 | Keep | 保持姿态 | 否 | 是 | ❌ 冻结 |
| 6 | MoveTo | 内部过渡 | 否 | 是 | ❌ 冻结 |
| 7 | SitDown | 坐下 | 否 | 是 | ❌ 冻结 |
| 601/602/603 | Dance1/2/3 | 舞蹈 | ⚠️ | 是 | ✅ |
| 604 | Twist（扭扭舞） | 舞蹈 | ⚠️ | 是 | ✅ |
| 607 | KungFu | 武术 | ⚠️ | 是 | ✅ |
| 608 | JeetKuneDo | 武术 | ⚠️ | 是 | ✅ |
| 701 | StandUp（起身） | 恢复 | ⚠️ | 是 | ✅ |
| 702 | LieDown（躺下） | 恢复 | ⚠️（会倒地） | 是 | ✅ |
| 800 | Motion | 运动总状态 | 否 | 是 | ❌ 冻结 |
| 811 | Run | 行走 | ⚠️ | 是 | ✅ |
| 812 | AmpMotion | 运动 | ⚠️ | 是 | ✅ |
| 813 | WalkStraightKnee | 行走 | ⚠️ | 是 | ❌ 冻结 |
| 814 | Walk | 行走 | ⚠️ | 是 | ❌ 冻结 |
| 815 | AmpLocomotion | 运动 | ⚠️ | 是 | ✅ |
| 816 | ArmSdkLoco | 运动 | ⚠️ | 是 | ✅ |
| 830 | Loco20Dof | 运动（Air 落点） | ⚠️ | 是 | ✅ |
| 831 | LocoArmSdk | 运动 | ⚠️ | 是 | ✅ |

> **Air 差异**：`dances_air.yaml` 只包含 602 / 604 / 607。请求 601 / 603 / 608 可能被拒。
> **830 没有 speed handler**：速度固定 vx 1.0，`SET_VELOCITY` 不能调节快慢，只能改方向。

## 三条必须记住的规则

### 1. Damping(1) 是唯一全局逃生舱

只有 `Damping` 从任何状态都被接受。它也是 `robot_stop` 之外唯一"无条件安全"的目标状态。
但注意：**Damping 会让站立的机器人失去力矩而倒下**——如果只是想让机器人别走，
用 `robot_stop`（软停，保持姿态），不要用 Damping。

### 2. 白名单由机器人裁定，拒绝返回 1001 / 1002

绝大多数动作只能从 `Lock(4)` 进入，**不能从 `Run(811)` 直接进**。
所以标准序列是：

```
robot_stand          → Lock(4)，等待稳定
robot_switch_state   → 目标动作
```

本地守卫会提前拦掉"从当前状态一定进不去"的转换并返回 `guard_rejected` + hint。
被拦了不要原样重试，先按 hint 走。

### 3. 8 个状态会冻结状态上报

`5 / 6 / 7 / 800 / 804 / 805 / 813 / 814`（后两个中 804/805 是 G1 的）。
进入这些状态后，机器人**整体停发** `sportmodestate`，回读停在最后一个值。

对策：MCP 返回 `dispatched + confirmed:false`，并给出 `reporting: "frozen"`。
**任何依赖"等状态变化"的逻辑都会在这些状态下挂死**，不要在它们上面加轮询。

想让状态恢复上报：切到 `Damping(1)`。

## 错误码速查

| 码 | 含义 | 该怎么办 |
|---|---|---|
| `0` | 已接受 | 正常情况下 `confirmed` 取决于回读 |
| `127` | **该 api_id 在当前状态下没有处理函数** | 见下方「127 实测」 |
| `1001` | 当前状态白名单拒绝 | 先 `robot_stand` 进 Lock 再试 |
| `1002` | 当前状态黑名单拒绝 | 同上 |
| `1003` | **本固件没有这个 fsm_id** | 别再重试，查 `robot_list_actions` |
| 无回包（`timed_out`） | 该 api_id 没有处理函数 | 不是失败也不是成功，人工确认机器人状态 |

### 127 实测（2026-09-13，R1 Air / E39N1000Q6G8448E）

在 **830 (Loco20Dof)** 状态下调用 `SET_VELOCITY`(7105)，机器人**立即返回 `127`**：

```json
{"type":"res","topic":"rt/api/sport/response",
 "data":{"header":{"identity":{"api_id":7105,"id":...},"status":{"code":127}},"data":""}}
```

这印证了「830 没有 speed handler」的说法——**不是参数写错，也不是被状态机拒绝**。

后果与对策：

- `robot_move` 检测到 127 会**自动回退到 `rt/wirelesscontroller` 摇杆通路**
  （官方 App 用的就是这条路），返回值里会带 `transport: "joystick_fallback"`。
- 需要精确速度控制时，直接用 `robot_joystick`，并用归一化值。
- 注意 830 是 Air 的**默认开机状态**，所以在 Air 上几乎总是走回退路径。

### `rt/lf/sportmodestate` 的实际字段（实测）

只有 4 个：`fsm_id`、`fsm_mode`、`task_id`、`task_time`。

**没有** `position` / `velocity` / `imu_state`——那些是 Go2 的 `rt/sportmodestate` 才有的。
所以**无法从状态话题读取位移**；要判断机器人走了多远，只能靠 IMU 姿态变化或视觉。

## 手臂手势（与 FSM 独立）

```
topic      rt/api/arm/request
api_id     7106  (SET_ARM_TASK)
parameter  {"data": <action_id>}
```

手势**独立于运动状态**，站立时即可播放，不需要 Lock。
`release`(99) 用于取消当前手势、让手臂回中位。
R1 没有 G1 的两个比心手势（`ArmHeart` 20 / `RightHeart` 21），请求会被拒绝。

## 速度指令

```
topic      rt/api/sport/request
api_id     7105  (SET_VELOCITY)
parameter  {"velocity": [vx, vy, omega], "duration": <秒>}
```

`duration` 由机器人自己执行，到期自动停下，所以 MCP 的 `robot_move` 不需要额外兜底停。
⚠️ 该 api 在仓库里**没有任何可执行示例**，vx/vy/omega 的物理单位未实测确认。
MCP 把它们当"归一化到机器人自身限速"的值处理，并限制在 ±1.0。
上真机时先用小值（0.1~0.2）试探。
