# 连接方式与 AES Key

## 三种连接方式怎么选

| 方式 | 值 | 何时用 | 需要什么 |
|---|---|---|---|
| AP | `ap` | 直连机器人热点，最快、最不依赖网络 | 电脑连上机器人 WiFi；地址固定 `192.168.12.1` |
| STA-L | `sta-l` | 机器人和电脑在同一局域网（最常用） | `UNITREE_ROBOT_IP` **或** `UNITREE_ROBOT_SN` |
| STA-T | `sta-t` | 异地远程，走宇树 TURN 中继 | SN + 宇树账号（邮箱/密码）；机器人需在线 |

`UNITREE_AGENT_CONNECTION` 接受 `ap` / `sta-l` / `sta-t`（也接受 `LocalAP`、
`LocalSTA`、`Remote` 这类写法，会自动归一化）。

### 关于组播发现

- 人形（G1/R1）**不在** Go2 的组播组上，必须指定 `device_type`。
- 所有 R1 以及 G1 ≥ 1.5.1 **只回应点名自己 SN 的查询**。
  所以 `robot_discover` 不传 SN 通常是空结果，这是正常的，不是网络问题。
- 发现只能"确认地址"，不能"枚举网络"。想枚举请让操作者告知 SN。

## AES-128 Key（`data2=3`）

### 什么时候需要

- **所有 R1**：必须。
- G1 ≥ 1.5.1、Go2 ≥ 1.1.15：必须。
- G1 < 1.5.1、Go2 < 1.1.15：不需要（固件用静态密钥，库会自动处理）。

没有 key 时 `connect` 会在解密公钥那一步失败，MCP 返回 `aes_key_missing`。

### 怎么拿

```bash
# 单个 SN，输出裸 key（适合管道）
unitree-fetch-aes-key --email you@example.com --sn E21D6000XXXXXXXX --quiet \
  --device-type R1 --region global

# 不带 --sn：列出账号下所有绑定设备（含 key）
unitree-fetch-aes-key --email you@example.com --device-type R1
```

也可以交给 MCP server 自动取：配好 `UNITREE_AGENT_EMAIL` / `UNITREE_AGENT_PASSWORD` 后，
它会登录 → `device/bind/list` → 取 `dev.key`，并**缓存到 `$STATE_DIR/keys.json`（0600）**。
不需要预先知道 SN——只绑了一台设备时自动选它。

三条自动路径，按优先级：

| 输入 | 行为 |
|---|---|
| `UNITREE_AES_128_KEY` | 直接用，**完全不碰云端** |
| `UNITREE_AGENT_ACCESS_TOKEN` | 用 token 调 `device/bind/list`，不需要密码 |
| `UNITREE_AGENT_EMAIL` + `UNITREE_AGENT_PASSWORD` | 登录，缓存 access + refresh token 到 `$STATE_DIR/session.json`（0600） |

**登录态会缓存复用**：第一次给了邮箱密码之后，后续启动即使不再提供密码，也会先用缓存
的 access token；失效则自动用 refresh token 换新的；两个都废了才要求重新登录。
也可以随时用工具 `robot_fetch_key` / `robot_auth_status` 自查。

### ⚠️ region 陷阱（实测踩过）

**账号注册地决定端点，猜错会得到一个很像"密码错误"的报错：**

```
region=global 登录失败: login/email failed: code=1000 msg='email not register'
```

这不是密码错，是**这个账号不在 global 端点**。实测那台 R1 的账号在 **cn**。

默认 `UNITREE_AGENT_REGION=auto`：会依次试 global / cn，**记住成功的那一个**
（写入 `resolved_region`，STA-T 连接也复用它）。只在你有意固定端点时才显式设置。
若想关掉自动回退：`UNITREE_AGENT_REGION_AUTO_FALLBACK=false`。

### key 的性质

- **per-device**：每台机器人一个，换绑/重配对不变。
- **不等于账号密码**：泄露 key 等于泄露这台机器人的控制权，按密钥对待。
- 存于机器人 `/unitree/etc/key/aes_key.bin`（RSA 包裹），云端以 `dev.key` 暴露。
- 缓存失效的条件：换机器人、SN 变了、云端重新签发。**取错了 key 的典型症状是
  `aes_key_rejected`（GCM tag 校验失败）**——不是网络问题，别去查网络。

## 环境变量速查

配置全部走环境变量（也支持 `.env`，查找顺序：`$UNITREE_AGENT_ENV_FILE` → 当前目录 → state dir）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `UNITREE_AGENT_ROBOT` | `R1` | `R1` / `Go2`（`G1` 尚未实现，会明确报错） |
| `UNITREE_AGENT_CONNECTION` | `sta-l` | `ap` / `sta-l` / `sta-t` |
| `UNITREE_ROBOT_IP` | — | STA-L 地址 |
| `UNITREE_ROBOT_SN` | — | 序列号（STA-L 发现、STA-T、取 key 都要） |
| `UNITREE_AES_128_KEY` | — | 32 位十六进制；缺省时会尝试缓存/云端 |
| `UNITREE_AGENT_EMAIL` / `UNITREE_AGENT_PASSWORD` | — | 登录与自动取 key；登录态会被缓存 |
| `UNITREE_AGENT_ACCESS_TOKEN` | — | 直接用 token，不必给密码 |
| `UNITREE_AGENT_REGION` | `auto` | `auto` / `global` / `cn`；auto 会记住成功端点 |
| `UNITREE_AGENT_REGION_AUTO_FALLBACK` | `true` | 关掉则只试配置的那一个端点 |
| `UNITREE_AGENT_IDLE_DISCONNECT_S` | `45` | 空闲多久释放链路；`0` = 不释放（**不推荐**） |
| `UNITREE_AGENT_MAX_MOVE_S` | `10` | 单次移动的硬上限 |
| `UNITREE_AGENT_MOTION_ACCEL` | `1.5` | 速度曲线加速度（摇杆单位/秒²） |
| `UNITREE_AGENT_MAX_TURN_SPEED` | `0.4` | 闭环转向的 rx 峰值 |
| `UNITREE_AGENT_TURN_TOLERANCE_DEG` | `4` | 转向误差小于此值即停止 |
| `UNITREE_AGENT_MAX_TURN_SECONDS` | `20` | 单次转向看门狗 |
| `UNITREE_AGENT_MIN_COMMAND_INTERVAL_S` | `0.1` | 指令最小间隔（限流） |
| `UNITREE_AGENT_SAFE_MODE` | `false` | `true` = 只读，拒绝一切移动工具 |
| `UNITREE_AGENT_ALLOW_DANGEROUS` | `true` | `false` = 禁用舞蹈/起身/零力矩等危险动作 |
| `UNITREE_AGENT_STOP_ON_DISCONNECT` | `soft` | `soft` / `damp` / `none` |
| `UNITREE_AGENT_BACKEND` | `auto` | `auto` / `webrtc` / `mock`（离线自检用） |
| `UNITREE_AGENT_STATE_DIR` | `~/.unitree-agent` | key/会话缓存、审计日志、抓帧归档 |
| `UNITREE_AGENT_AUDIT_LOG` | `<state_dir>/audit.log` | 设为 `off` 关闭 |
| 视觉相关 | 见 `vision.md` | `VIDEO_SOURCE` / `SNAPSHOT_*` / `RTSP_URL` / `CAMERA` |
| `UNITREE_AGENT_TRANSPORT` | `stdio` | `stdio` / `streamable-http` |
| `UNITREE_AGENT_HOST` / `UNITREE_AGENT_PORT` | `127.0.0.1` / `8765` | streamable-http 绑定 |

## 只读安全模式

不确定的话，先用 `UNITREE_AGENT_SAFE_MODE=true` 启动：所有移动类工具都会被拒绝
（返回 `guard_rejected`），只读工具和 `robot_stop` 仍然可用。
确认读到的状态都符合预期后，再去掉这个开关。
