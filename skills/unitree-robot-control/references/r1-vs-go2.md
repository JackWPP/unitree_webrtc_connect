# R1 与 Go2 的能力差异

同一个 MCP server、同一套工具名，但两台机器在下面这些地方**真的不一样**。
写复合计划前先看这张表。

## 一句话区别

- **R1（人形）**= 状态机。一切都归结为"把 fsm_id 切成多少"，有回读、有白名单、有冻结。
- **Go2（四足）**= 指令集。每个动作一个 api_id，**没有状态回读**，动作没有时长。

## 对照表

| 维度 | R1 | Go2 |
|---|---|---|
| 控制模型 | FSM 状态（`SET_FSM_ID` 7101） | sport 指令（`SPORT_CMD`） |
| 状态回读 | 有（`fsm_id`，部分状态冻结）；`lf` 版**只有 fsm_id/fsm_mode/task_id/task_time** | **无** FSM，sport state 里有 mode/gait/position/velocity |
| 切换枢纽 | 必须先 `Lock(4)` | 无，指令直接发 |
| 移动 | `SET_VELOCITY` 7105（自带 duration），但 **830 状态返回 127 → 自动回退摇杆** | `Move` 1008，**无 duration，必须自己停** |
| MCP 里怎么停 | 机器人自己到期停（回退路径下由摇杆归零停） | `robot_move` 在窗口结束后补发 `StopMove`(1003) |
| 手臂 | 13 个手势 + 20 DOF 手臂 | 无 |
| 姿态 | Lock(4) / SitDown(7) / LieDown(702) | BalanceStand(1002) / StandDown(1005) / Sit(1009) |
| LED / 音量 / 亮度 | 无 | `go2_set_led` / `go2_set_volume` / `go2_set_brightness` |
| 避障服务 | 无 | `go2_obstacle_avoid`；开启后会拦截摇杆指令做安全过滤 |
| 雷达 | 无 | `go2_lidar`（点云只给摘要，不给全量） |
| 音频 | 库里 R1 未实现 | 有（示例里有，MCP 未封装） |
| 运动模式切换 | 无 | `go2_motion_mode`：normal / ai / advanced / mcf |
| 危险动作 | 舞蹈、武术、起身、躺下、零力矩 | 空翻、跳跃、handstand、moonwalk |
| 单位语义 | `SET_VELOCITY` 单位**未实测确认** | `Move` 的 x/y/z 语义为 vx/vy/vyaw |

## 对 Agent 的实际影响

### 1. `robot_move` 的行为不同

- **R1**：机器人自己按时长停下。工具返回时动作已经结束。
- **Go2**：工具在窗口结束后**主动补发 `StopMove`**，所以返回时也保证已经停。
  如果中途出错，`finally` 里同样会补停。

两者对 Agent 都是"返回即已停"，**不需要额外调 `robot_stop`**，但调了也无害。

### 2. Go2 没有 `robot_switch_state`

`robot_switch_state` 只在有 FSM 的机型上注册。Go2 上要用：
`robot_stand` / `robot_lie_down` / `robot_play_action`。

### 3. `robot_get_posture` 在 Go2 上返回 n/a

Go2 没有 FSM，`state` 会是 `n/a` 并附一条提示。
想知道 Go2 真实姿态，用 `go2_get_state`（sport state + multi state 聚合）。

### 4. 危险动作的"危险"含义不同

- R1：摔倒是**双足失衡**，通常需要人工扶起；Air 保修只有 6 个月。
- Go2：空翻/跳跃是**主动腾空**，需要软地面和大空间，且对电量敏感。

两边都走 `UNITREE_AGENT_ALLOW_DANGEROUS` 同一个开关，但不要因为"Go2 是四足所以稳"
就放宽 R1 的判断。

## 不要做的事

- ❌ 不要用 Go2 的 `SPORT_CMD` id 去调 R1（两边 id 空间完全不同）。
- ❌ 不要依赖 `LeadFollow` / `FreeWalk`：上游 `SPORT_CMD` 里两者都是 1045，
  名字无法唯一解析，因此 MCP **故意不暴露**它们。需要的话请先确认正确 id。
- ❌ 不要在 Go2 上给 `Move` 传 duration 然后指望它停下——它没有这个参数。
