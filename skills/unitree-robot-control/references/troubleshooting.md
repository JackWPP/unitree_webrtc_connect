# 故障排查

先跑 `robot_doctor`。它一次性检查：配置、依赖、连接、安全模式，并给出 `findings`。
下面按错误码展开。

## `robot_busy`

**含义**：机器人已经有一个 WebRTC 客户端，第二个连接会被 SDP 回 `reject`。

**为什么重要**：这是设计限制，不是 bug。机器人同一时刻只服务一个客户端。

**处理**：
1. 让操作者关闭官方 App / unitree_ui / 任何正在连的脚本。
2. 等几秒（旧连接需要时间释放）。
3. 重试。

**预防**：不要设置 `UNITREE_AGENT_IDLE_DISCONNECT_S=0`。默认 45 s 的空闲释放就是为了
把链路还给操作者。**永远不要试图长期占用连接。**

## `aes_key_missing` / `aes_key_rejected`

| 错误 | 含义 | 处理 |
|---|---|---|
| `aes_key_missing` | 这台机器人需要 per-device key，但没有配 | 调 `robot_fetch_key`，或按 `connection-and-keys.md` 取 |
| `aes_key_rejected` | key 解密失败（GCM tag 不匹配） | **key 取错了**（多半是别的机器人的），按当前 SN 重取 |

`aes_key_rejected` 看起来像握手失败，但**根因一定是 key 内容不对**，
不要去查网络、端口或防火墙。

## 登录报 "email not register"

**这不是密码错，是 region 错。** 账号注册地决定端点：

- 中国大陆注册 → `robot-api.unitree.com`，需要 `region=cn`
- 其他地区 → `global-robot-api.unitree.com`

实测案例：账号在 cn，用 global 端点登录得到 `code=1000 msg='email not register'`，
切换后立刻成功。

默认 `UNITREE_AGENT_REGION=auto` 会自动依次尝试并记住成功的那一个，
所以正常情况下你不需要管这件事。如果看到这个报错，说明自动回退被关掉了
（`UNITREE_AGENT_REGION_AUTO_FALLBACK=false`）或显式指定了错误端点。

先跑 `robot_auth_status` 看 `region_configured` / `region_resolved` / `cached_session`。

## 抓不到图 / `no_frame`

**先跑 `robot_vision_probe`**，它会逐条报告每条来源的状态和失败原因。
详细处置见 `vision.md`，最常见的两种情况：

- **这台机型根本没有相机**。实测 R1 Air 就是如此：WebRTC 无 track、RTSP 无挂载点、
  拍照 API 无 handler。此时唯一能出图的办法是用**本地源**（`UNITREE_AGENT_SNAPSHOT_FILE`）
  离线开发。换一台有相机的机型才能真正验证机器人侧抓帧。
- **配置没给**：`rtsp` 需要 `UNITREE_AGENT_RTSP_URL`，`file` 需要
  `UNITREE_AGENT_SNAPSHOT_FILE`，`device_camera` 需要 `UNITREE_AGENT_CAMERA`。

## `connect_failed`

按连接方式分别查：

**AP**
- 电脑是否真的连到了 `Unitree_*` 热点？地址固定 `192.168.12.1`。
- 有些系统连上热点后仍走蜂窝网络 → 关掉移动数据或调整路由优先级。

**STA-L**
- 机器人和电脑在同一网段吗？`ping <ip>` 通吗？
- 端口：`:9991`（新固件，走 `con_notify`）或 `:8081`（老 Go2，走 `offer`）。
  两个都不通 → 网络或防火墙问题。
- 用 `robot_discover --sn <SN>` 确认机器人在线（不传 SN 基本查不到人形）。

**STA-T**
- 机器人侧是否打开了"远程/互联网接入"？云端 `webrtc/connect` 会返回 `code 1000`
  （设备不在线）如果没打开。
- 账号 `region` 对不对？国内注册的账号要用 `cn`。

## `command_timeout` / 返回 `timed_out`

**关键区分**：
- `timed_out = true` 表示**没有收到任何回包**。
- `robot_refused`（带 `robot_code`）表示**机器人明确拒绝了**。

**为什么会没有回包**：
1. 该 api_id 在这台固件上**没有处理函数**（例：R1 的 7002 / 7007 / 7107 / 7109）。
   这是最常见的原因，属于"沉默"而不是"失败"。
2. 数据通道已关闭 / 对端掉线。
3. 响应与请求的 id 配对失败（库内部按 id 匹配）。
4. 链路拥塞（视频通道开着时会明显变慢）。

**处理**：
- **先问操作者机器人实际在干什么**，再决定重试。
- 不要自动重试 3 次——同一个"沉默"调用重试 3 次还是沉默。
- 如果伴随视频卡顿，先关掉视频（不要调 `robot_snapshot`）再试。

## `guard_rejected` / `confirm_required`

这些是**本地守卫**拒绝的，请求根本没发出去。

| 返回 | 原因 | 处理 |
|---|---|---|
| `confirm_required` | 缺 `confirm=true` | 确认环境安全后带 `confirm=true` 重发 |
| `guard_rejected` + `safe_mode` | 只读模式开着 | 去掉 `UNITREE_AGENT_SAFE_MODE` 重启 server |
| `guard_rejected` + `allow_dangerous` | 危险动作被禁用 | 去掉 `UNITREE_AGENT_ALLOW_DANGEROUS=false` 重启 |
| `guard_rejected` + 提到 Lock | 当前状态进不去目标状态 | 先 `robot_stand`，再重试 |
| `unknown_action` / `unknown_state` | 名字拼错或该机型没有 | 查 `robot_list_actions` 的返回 |

## R1 状态回读不变化

**如果机器人处于 5 / 6 / 7 / 800 / 813 / 814**：这是**设计行为**，这些状态整体停发状态。
返回值里 `reporting: "frozen"` 已经说明了。不要轮询，不要重试。

想恢复上报：切到 `Damping(1)`（`robot_switch_state state="Damping"`，无需 confirm）。

## 请求 `Run` 但回读是 830

Air 底盘上这是**正常的固件重定向**，返回值里 `redirected: true`。
不是失败，不要重试。

## 机器人没反应 / 动作没执行

排查顺序：
1. `robot_status` → 还连着吗？`connected: false` 说明空闲释放了（这个正常，下次调用会自动重连）。
2. `robot_get_posture` → 在哪个状态？`frozen` 的话回读没有参考价值。
3. `robot_errors` → 有没有过温/欠压/通信错误？过温会静默拒绝运动。
4. 电量 `robot_lowstate` → 低于 20% 先充电。
5. 都不是 → **看机器人本体**（有没有红灯、异响、卡住）。

## MCP server 连不上（agent host 侧）

| 现象 | 检查 |
|---|---|
| host 报告找不到 server | `command` 路径是否是绝对路径？用的是不是装了依赖的那个 venv 的 python？ |
| server 启动即退出 | 用 `unitree-robot-mcp doctor` 手跑一次，错误会直接打印出来 |
| 工具列表为空 | `unitree-robot-mcp tools` 看注册结果；`mcp` 依赖没装会直接报 ImportError |
| streamable-http 连不上 | `--host 0.0.0.0 --port 8765`；确认防火墙；默认只绑 `127.0.0.1` |

## 什么时候该找 unitree_ui

UI（官方 App / unitree_ui 调试台）不是替代品，是**降级路径**：

- 需要**视觉验收**（看关节角度、3D 姿态）→ 让人开 UI。
- MCP 连不上又怀疑是软件问题时 → 让人用 UI 验证机器人本身是好的。
- 需要**人手接管**做危险操作 → 让操作者拿遥控器/UI 上。

**主动让出连接**：等 45 s 空闲自动释放，或直接停掉 server。不要抢。
