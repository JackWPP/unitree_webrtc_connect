# Unitree Robot MCP — 安装与使用

把宇树机器人（Go2 / R1）的控制能力封装成 MCP server，让 Agent 可以安全地感知与控制。
配套 Skill 见 [`skills/unitree-robot-control/`](../skills/unitree-robot-control/SKILL.md)。

## 它是什么 / 不是什么

✅ **是**：连接管理、状态感知、语义化控制、安全护栏、审计日志、离线自检。
❌ **不是**：unitree_ui 的替代品。需要视觉验收或人手接管时，**让操作者用官方 App**——
本 server 会主动释放连接来配合这一点。

## 一、安装

### 0. 一句话安装（推荐）

**给 Agent 的一句话**（贴进对话框，把 IP 换成你的）：

> 帮我装宇树机器人 MCP 服务：clone `https://github.com/JackWPP/unitree_webrtc_connect.git`
> （分支 `feat/unitree-robot-mcp`），执行 `python3 scripts/install_mcp.py --robot R1 --ip <机器人IP>`，
> 然后把 `robot_doctor` 的结果告诉我。

**给终端的一条命令**（不需要机器人也能跑完）：

```bash
git clone -b feat/unitree-robot-mcp https://github.com/JackWPP/unitree_webrtc_connect.git \
  && cd unitree_webrtc_connect \
  && python3 scripts/install_mcp.py --robot R1 --ip 192.168.10.225
```

安装器做四件事，任一步失败即停：

1. 把本仓库以 `[agent]` extra 装进当前解释器（失败会自动回退成非 editable 安装）
2. 跑**离线**自检（20 项端到端检查，用 mock 后端，不需要机器人）
3. 把 `mcpServers` 条目**合并**进检测到的 MCP 宿主配置——先备份原文件，再原子写入
4. 打印到底改了什么、以及怎么验证

```bash
python3 scripts/install_mcp.py --dry-run               # 只看不写
python3 scripts/install_mcp.py --host codebuddy        # 指定宿主（默认 auto）
python3 scripts/install_mcp.py --host none             # 只打印 JSON 片段
python3 scripts/install_mcp.py --robot Go2 --ip 192.168.8.181
python3 scripts/install_mcp.py --venv .venv            # 装进独立虚拟环境
python3 scripts/install_mcp.py --safe-mode             # 注册为只读模式
```

写入位置：macOS/Linux 上是 `~/.codebuddy/mcp.json`（已实测确认），
另外也认识 Cursor、Claude Desktop、以及工作区级 `.codebuddy/mcp.json`。
其它客户端用 `--host none` 打印片段自行粘贴。

装完后**重启 MCP 宿主**，让 Agent 先调 `robot_doctor`，再调 `robot_status`。

> 合并到 `master` 之后，clone 命令可以去掉 `-b feat/unitree-robot-mcp`。

### 1. 依赖（手动安装）

MCP server 需要 `mcp>=2.0`（也兼容 1.x 的 FastMCP 布局）：

```bash
cd /path/to/unitree_webrtc_connect
pip install -e ".[agent]"
```

机器人侧的核心依赖（aiortc / pycryptodome / requests / curl_cffi）已在主依赖里。
Go2 的雷达、音频等可选能力按需装：

```bash
pip install -e ".[go2]"        # LiDAR 解码（wasmtime + lz4 + numpy）
pip install -e ".[audio]"      # 声卡播放/录音
pip install -e ".[all]"        # 全都要（含 examples 的 cv2/flask/pyaudio）
```

> **惰性导入**：`import unitree_webrtc_connect` **不再**需要 wasmtime / lz4 /
> sounddevice / numpy / pydub。这些只在真正用到对应能力（Go2 雷达、音频、
> AudioHub）时才加载，人形机器人路径完全不需要它们。

### 2. 验证安装（不需要机器人）

```bash
unitree-robot-mcp selftest          # 默认 R1
unitree-robot-mcp selftest --robot Go2
```

两个机型各 16 项检查全绿，说明工具面、护栏、会话生命周期都正常。
这是**离线可跑**的证明——真机不在手边也能验证整套逻辑。

## 二、配置

全部走环境变量，也支持 `.env`（查找顺序：`$UNITREE_AGENT_ENV_FILE` → 当前目录 → `~/.unitree-agent/.env`）。

### 最小配置（R1，局域网）

```bash
export UNITREE_AGENT_ROBOT=R1
export UNITREE_AGENT_CONNECTION=sta-l
export UNITREE_ROBOT_IP=192.168.10.225
export UNITREE_AES_128_KEY=<32位十六进制>
```

AES key 的获取方式见 [connection-and-keys](../skills/unitree-robot-control/references/connection-and-keys.md)：
```bash
unitree-fetch-aes-key --email you@example.com --sn <SN> --device-type R1 --quiet
```

也可以把邮箱密码交给 server 自动取并缓存（`~/.unitree-agent/keys.json`，0600 权限）：
```bash
export UNITREE_AGENT_EMAIL=you@example.com
export UNITREE_AGENT_PASSWORD='...'
# SN 可省：账号下只绑了一台设备时会自动选它
```

**登录态本身也会缓存**（`~/.unitree-agent/session.json`，0600）：第一次登录后，
后续启动可以不再提供密码——会先用缓存的 access token，失效则自动用 refresh token 换新的。

> ⚠️ **region 默认 `auto`**：依次尝试 global / cn 并记住成功的那一个。
> 这是为了绕开一个很误导人的坑——中国大陆注册的账号在 global 端点会返回
> `code=1000 msg='email not register'`，看起来像密码错误，其实是端点不对。
> 想固定端点就显式设 `UNITREE_AGENT_REGION=global|cn`。

### 先只读跑一轮

不确定的时候先开安全模式，确认读到的状态合理再放开控制：

```bash
UNITREE_AGENT_SAFE_MODE=true unitree-robot-mcp serve
```

### 常用调参

| 变量 | 默认 | 说明 |
|---|---|---|
| `UNITREE_AGENT_IDLE_DISCONNECT_S` | `45` | 空闲多久释放链路。**不建议设 0** |
| `UNITREE_AGENT_MAX_MOVE_S` | `10` | 单次移动硬上限 |
| `UNITREE_AGENT_MIN_COMMAND_INTERVAL_S` | `0.1` | 指令限流间隔 |
| `UNITREE_AGENT_SAFE_MODE` | `false` | 只读模式 |
| `UNITREE_AGENT_ALLOW_DANGEROUS` | `true` | 是否允许舞蹈/起身/零力矩 |
| `UNITREE_AGENT_AUDIT_LOG` | `~/.unitree-agent/audit.log` | 设 `off` 关闭 |
| `UNITREE_AGENT_LOG_LEVEL` | `WARNING` | 排查时设 `DEBUG` |

完整清单见 [connection-and-keys](../skills/unitree-robot-control/references/connection-and-keys.md#环境变量速查)。

## 三、运行

### stdio（本地 Agent 直连，默认）

```bash
unitree-robot-mcp serve
```

由 agent host 拉起，一个客户端独占一个机器人连接。

### streamable-http（多客户端共享一个机器人）

```bash
unitree-robot-mcp serve --transport streamable-http --host 0.0.0.0 --port 8765
```

连接端点：`http://<host>:8765/mcp`。

多客户端共享是安全的：会话层有连接互斥锁，谁先调用谁持有链路，空闲 45 s 后释放，
**不会出现两个客户端抢同一个 WebRTC 通道**。

### 其他命令

```bash
unitree-robot-mcp tools             # 这套配置会暴露哪些工具
unitree-robot-mcp doctor            # 自检：配置 / 依赖 / 连通性 / 认证
unitree-robot-mcp auth-status       # 云端认证状态（哪个 region、有无缓存登录）
unitree-robot-mcp discover --sn E21D...   # 组播发现（人形必须带 SN）
unitree-robot-mcp audit-tail -n 40  # 审计日志

# 取并缓存 per-device AES key（区域自动探测，不需要预先知道 SN）
export UNITREE_AGENT_EMAIL=you@example.com UNITREE_AGENT_PASSWORD='...'
unitree-robot-mcp fetch-key --list      # 列出账号下所有设备（key 打码）
unitree-robot-mcp fetch-key -q          # 只打印裸 key，便于管道
```

`fetch-key` 也支持 `--region global|cn|auto`（默认 auto：依次尝试并记住成功的那一个）。

## 四、工具面

R1 暴露 23 个工具，Go2 暴露 29 个（R1 专属 = `robot_switch_state` / `robot_arm_gesture`；
Go2 专属 = `go2_*` 那 8 个）。

| 分类 | 工具 |
|---|---|
| 只读 | `robot_status` `robot_get_posture` `robot_list_actions` `robot_lowstate` `robot_errors` `robot_discover` `robot_audit_tail` `robot_doctor` |
| 认证 | `robot_auth_status`（region/会话状态，无密钥） `robot_fetch_key`（取并缓存 AES key） |
| 停止 | `robot_stop`（软停，无 confirm） `robot_emergency_stop`（Damping，要 confirm） |
| 姿态 | `robot_stand` `robot_lie_down` |
| 移动 | `robot_move` `robot_joystick`（梯形速度曲线） **`robot_turn`（IMU 闭环转向）** |
| 动作 | `robot_switch_state`（R1） `robot_play_action` `robot_arm_gesture`（R1） |
| 视觉 | `robot_snapshot`（返回图片） `robot_vision_probe`（哪条来源可用） `robot_snapshot_history` |
| Go2 | `go2_get_state` `go2_motion_mode` `go2_set_volume` `go2_set_brightness` `go2_set_led` `go2_get_vui` `go2_obstacle_avoid` `go2_lidar` |

还有 3 个 MCP resource（`unitree://robot/status`、`/actions`、`/config`）
和 1 个 prompt（`unitree_safety_briefing`）。

### 运动控制

- `robot_move` / `robot_joystick` 现在按**梯形（长动作）或三角形（短动作）速度曲线**下发，
  曲线末端恒为 0——短动作也能到达指定速度，且窗口结束不会继续走。返回值里的 `profile`
  给出实际曲线与归一化位移。
- `robot_turn(degrees)` 是**闭环**的：读 IMU yaw、按比例控制 rx、误差小于容差即停。
  正数=逆时针。返回 `turned_deg` / `residual_deg`，**以 residual 为准**。
  详见 `skills/unitree-robot-control/references/motion-control.md`。

### 视觉

`robot_vision_probe` 会依次试 4 条来源并报告每条的原因。**不是所有机型都有相机**：
实测 R1 Air 完全没有（无 WebRTC 视频轨、RTSP 无可用挂载点、拍照 API 无 handler）。
没有机器人时可以用本地文件/摄像头把整条视觉链路跑通：

```bash
export UNITREE_AGENT_VIDEO_SOURCE=file
export UNITREE_AGENT_SNAPSHOT_FILE=/path/to/frame.jpg   # 图片 / 视频 / 目录
unitree-robot-mcp serve
```

详见 `skills/unitree-robot-control/references/vision.md`。

## 五、安全护栏（写死在代码里，模型关不掉）

1. 移动类必须 `confirm=true`，否则本地拒绝并附 hint。
2. 时长有硬上限，请求 600 s 会被截断为 10 s 并注明。
3. 本地状态白名单：能确定机器人会拒绝的转换，本地就拒绝（附原因和下一步）。
4. 移动失败 / 超时 / 进程退出，一律回落到**软停**（速度归零、保持姿态）。
5. 指令限流（默认 100 ms 间隔）。
6. 空闲 45 s 自动释放链路，避免占死机器人的单客户端通道。
7. 全部调用写审计日志，AES key / 密码 / token 按字段名脱敏。

想彻底禁掉控制：`UNITREE_AGENT_SAFE_MODE=true`；
只想禁危险动作：`UNITREE_AGENT_ALLOW_DANGEROUS=false`。

## 六、排障

| 现象 | 先看 |
|---|---|
| `robot_busy` | 关掉官方 App / unitree_ui，等几秒 |
| `aes_key_missing` / `rejected` | [connection-and-keys](../skills/unitree-robot-control/references/connection-and-keys.md) |
| `connect_failed` | [troubleshooting](../skills/unitree-robot-control/references/troubleshooting.md) |
| `timed_out` | 是"沉默"不是"拒绝"——先看机器人实际状态 |
| 状态回读不变 | 该状态可能冻结上报，看 `reporting: "frozen"` |

排查第一步永远是 `unitree-robot-mcp doctor`。

## 七、开发

```bash
pip install -e ".[agent,dev]"
python -m pytest -q          # 55 项，全部离线（Mock 后端）
unitree-robot-mcp selftest   # 端到端工具链自检
```

架构分层（详见仓库结构）：

```
MCP 接入层  server.py        双模传输、资源、prompt
工具层      tools/*.py       语义化工具，返回结构化错误
护栏层      core/guards.py   确认、时长、白名单、限流
会话层      core/session.py  按需连接、空闲断开、互斥、状态缓存
机型层      robots/*.py      R1 / Go2 语义（G1 预留）
后端层      backends/*.py    WebRTC 真机 / Mock 离线仿真
```

新增机型 = 加一个 `robots/<name>.py`；新增传输 = 实现 `RobotBackend` 协议。
