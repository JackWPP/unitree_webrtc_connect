# R1 能力封装：MCP + Skill 方案

日期：2026-09-12　目标：把 Unitree R1（R1 Air 优先）的控制能力封装成 MCP Server 与 Skill

---

## 一、决断（先说结论）

**MCP Server 只能基于 `unitree_webrtc_connect`（Python）封装，`unitree_ui` 进不了 MCP。**

理由不是"Python 更好"，而是 **UI 压根没有可编程接口**：

| 检查项 | 结果 |
|---|---|
| UI 有没有对外控制 API | **没有**。全仓库唯一对外 HTTP 接口是 `server/scanner.mjs` 的 `GET /scan`（端口 3001），**只做设备发现** |
| UI 的 WebRTC 跑在哪 | **浏览器原生 `RTCPeerConnection`**。`test-connection.mjs` 只做信令握手（`con_notify` → `con_ing`），**不建数据通道**，且硬编码的是 Go2 的静态 GCM key |
| 要程序化驱动 UI 只能怎么办 | Playwright/CDP 点按钮 —— 慢、脆、无状态语义、且会跟 MCP 抢同一个 WebRTC 通道 |

**所以两者的关系不是"二选一"，而是分工：**

- **MCP = 程序化控制面**（LLM/脚本调用）→ 封装 Python 库
- **UI = 人在环调试台**（点按钮、看图传、验收）→ 保持独立，**不进 MCP**

**Skill 的定位再区分一层**：Skill 不是"能力"，是**知识 + 流程 + 安全规程**。MCP 给 LLM 一双手，Skill 给它规矩。

---

## 二、两者能力对照（R1 视角）

**不完全相同。UI 是功能超集，Python 库是接口超集。**

| 能力 | Python 库 | UI | 说明 |
|---|---|---|---|
| FSM 状态切换（7101） | ✅ | ✅ | 两者都全量暴露，**都没按 Air 过滤** |
| 手臂手势（7106） | ✅ 13 个 | ✅ 13 个 | 一致（Air 无 ArmHeart/RightHeart） |
| 速度 / 摇杆遥操作 | ✅ `rt/wirelesscontroller`，**20 Hz** | ✅ 虚拟摇杆 + BLE 遥控器中继 | UI 多了物理遥控器中继 |
| lowstate 订阅 | ✅ 20 槽 | ✅ status page | 一致 |
| 图传 | ✅（要自己解码） | ✅ 原生 `<video>` | 库需要 OpenCV/PyAV 显示 |
| 音频 | ❌ **R1 未实现** | ⚠️ 有 UI，**R1 是否可用未验证** | 库里 R1 音频表是 `—` |
| 错误解码 | ✅ `msgs/error_handler` | ✅ errors-page + `R1_FSM_ERRORS` | 需实测比对 |
| 服务管理（MCF） | ❌ | ✅ services-page | |
| 账号 / 云端（取 AES key） | ✅ `UnitreeCloud` + CLI | ✅ Account page | |
| 组播发现 | ✅ Python | ✅ `/scan` HTTP + Vite 插件 | |
| 3D 关节可视化 | ❌ | ✅ Three.js | |
| BLE 配网 / 遥控器中继 / gamepad | ❌ | ✅ | |
| 点云 / SLAM | ❌（R1 无） | ✅（仅 Go2） | 与 R1 无关 |
| **可被脚本调用** | ✅ | ❌ | ← 决定性差异 |

---

## 三、MCP 封装的三个硬约束（实测得出）

### 3.1 依赖链被强制拖长

实测（隔离 venv，Python 3.13.12，逐层补包直到 `import unitree_webrtc_connect` 成功）：

```
aiortc + pycryptodome + requests + packaging
  → 缺 curl_cffi   （unitree_cloud.py 顶层导入）
  → 缺 numpy
  → 缺 wasmtime    ← 最重的一个
  → 缺 lz4
  → 缺 sounddevice
= IMPORT_OK
```

**为什么 R1 用不到还是被拖进来**：

```
__init__ → webrtc_driver → webrtc_datachannel → lidar/lidar_decoder_unified
                                                  ├─ libvoxel → wasmtime   (Go2 雷达)
                                                  └─ native  → lz4, numpy  (Go2 雷达)
                        → webrtc_audio → sounddevice, numpy  (R1 无音频)
```

**可裁剪，但要动源码**：

| 依赖 | R1 是否需要 | 裁剪方式 |
|---|---|---|
| `aiortc` | ✅ 必需 | — |
| `pycryptodome` | ✅ 必需（RSA/AES 握手） | — |
| `requests` | ✅ 必需（本地信令 HTTP） | — |
| `packaging` | ✅ 必需（aiortc 版本分支） | — |
| `curl_cffi` | ⚠️ 仅"云端取 AES key"需要 | key 缓存到本地后可不装 |
| `wasmtime` | ❌ | 把 `webrtc_datachannel` 的 lidar 导入改惰性 |
| `lz4` / `numpy` | ❌ | 同上 |
| `sounddevice` | ❌ | 把 `webrtc_driver` 的 audio 导入改惰性 |
| `pyaudio` / `pydub` / `flask-socketio` / `opencv-python` | ❌ | **包内零引用**，纯声明冗余，直接不装 |

> 已验证环境已建好：`/Users/jackwang/.workbuddy/binaries/python/envs/unitree_r1`（`import` 通过）。

### 3.2 单客户端互斥 —— MCP 最大的设计陷阱

机器人同一时刻**只接受一个 WebRTC 客户端**，第二个连接会被 SDP 回 `reject` → `RobotBusyError`。

**如果 MCP Server 常驻连接，UI 就永久连不上了**，他自己没法手动接管。

必须做：**按需连接 + 空闲自动断开**（建议 30–60 s 无调用即断），而不是启动时连一次保持。

### 3.3 状态语义不可信

| 现象 | 后果 | 对策 |
|---|---|---|
| Air 上 811 被固件重定向为 **830** | 回读 ≠ 请求，LLM 会以为失败并重试 | 返回值归一化：把 811/816/830/831 映射成一个语义组 |
| 8 个状态回读**冻结**（5/6/7/800/804/805/813/814） | 工具"等状态确认"会挂死 | 改为"已下发 + `confirmed: false`"，不让 LLM 依赖回读 |
| 可达性由机器人裁定，拒 → **1001**；固件没有 → **1003** | LLM 瞎撞会触发大量 1001 | 本地复刻白名单，非法转换**在本地就拒绝**，并返回原因 |
| `Damping(1)` 是唯一全局逃生舱 | —— | 作为急停工具，且**任何异常路径都回落到它** |

---

## 四、建议的 MCP 工具面（面向 LLM 设计）

**原则：不要暴露 `set_fsm_id(int)`。** 暴露语义名，把 23 个原始状态收敛成少数可理解的意图。

### 只读（无风险，可自由调用）
| 工具 | 说明 |
|---|---|
| `r1_status()` | 连接状态、固件版本、电池、`fsm_id`（含重定向归一化结果） |
| `r1_get_posture()` | 当前姿态语义（standing / lying / damping / unknown） |
| `r1_list_actions()` | **按 chassis 过滤后**的可用动作清单（Air 会少掉 601/603/608） |
| `r1_lowstate()` | 20 槽电机温度/位置/IMU（只读快照） |

### 低风险控制
| 工具 | 说明 |
|---|---|
| `r1_damp()` | 软停。**任何时刻可调，无需确认** |
| `r1_lock()` | 进入 Lock(4)，动作枢纽 |
| `r1_arm_gesture(name)` | 13 个手势选一，白名单枚举 |

### 高风险控制（需显式确认 + 自动超时保护）
| 工具 | 说明 |
|---|---|
| `r1_run(vx, vy, omega, duration_s)` | 移动。**必须带 duration，到期自动 `r1_damp()`** |
| `r1_play_action(name)` | 舞蹈/武术/起身/躺下，需 `confirm=true` |
| `r1_stand_up()` / `r1_lie_down()` | 需 `confirm=true` |

### 全局护栏（写死在 server 里，不可被 LLM 关闭）
1. 所有移动类调用**强制带超时**，超时回落 `Damping`
2. 危险动作要求 `confirm=true` 参数，缺省拒绝
3. 本地状态机白名单校验，非法转换直接返回 `reason`
4. 断连/异常时**主动发一次 `Damping`**
5. 频率限制（例如移动指令 ≥ 100 ms 间隔）
6. 所有调用写审计日志（时间、工具、参数、机器人返回码）

---

## 五、Skill 该写什么（不是能力，是规矩）

Skill 里应该装这些**MCP 装不了**的东西：

1. **前置流程**：怎么拿 per-device AES key —— 宇树账号登录 → `device/bind/list` 的 `dev.key` → 或 `unitree-fetch-aes-key --device-type R1`；key 缓存位置与失效重取条件
2. **连接选择**：AP（`192.168.12.1`）/ 局域网 STA-L（按 IP 或 `device_type="R1"` + SN）/ Remote（需账号，走 TURN）
3. **状态机规则**：白名单机制、`Damping` 逃生舱、8 个冻结状态、1001 vs 1003 的区别
4. **Air 差异清单**：811→830 重定向、`dances_air.yaml` 只有 602/604/607、830 无 speed handler（固定 vx 1.0）、起身策略是 Air 专用 ONNX
5. **安全规程**：人机安全距离、悬挂试机、Air 保修仅 6 个月、摔倒会损坏
6. **降级路径**：MCP 连不上 / 被占用 / 需要视觉验收时 → 让 agent 提示人去开 `unitree_ui` 接手（这是 UI 真正的价值位）
7. **致命风险提示**：固件 OTA 可能改协议（G1 1.5.1 改 `data2=3` 曾击穿所有第三方客户端），升级固件前先隔离复测

---

## 六、落地路线

| 阶段 | 做什么 | 说明 |
|---|---|---|
| 0（已完成） | 隔离环境 + 依赖摸清 | `envs/unitree_r1`，`import` 通过 |
| 1 | **vendored fork**：复制一份库到本地，把 lidar / audio 导入改惰性 | 甩掉 wasmtime / lz4 / sounddevice，安装体积和失败面大降 |
| 2 | 打 **Air 补丁**：动作清单按 chassis 过滤 + 状态归一化 | 同时可提 PR 回上游 |
| 3 | 写 `r1_core.py` 适配层（连接生命周期 / 守卫 / 审计） | 业务代码只碰这一层，不散落调库 |
| 4 | 包 MCP Server（stdio），实现第四章的工具面 | 先只上只读 + `damp`，跑稳再加移动 |
| 5 | 写真机验证脚本，**逐条实测状态可达性** | 别信文档表，机器人自己裁定 |
| 6 | 写 Skill（第五章内容） | 创建时需过安全审计 |

**顺序建议：4 之前必须做完 5 的只读部分。** 没有真机验证的 MCP 工具面，等于给 LLM 一把不知道能不能拔的枪。

---

## 附：本次实测数据

- 源码：`unitree_webrtc_connect` v2.2.0（commit 2026-08-16/17），已拉到 `./unitree_webrtc_connect`
- 环境：`/Users/jackwang/.workbuddy/binaries/python/envs/unitree_r1`
- 最终可 import 的依赖集合（24 个包）：
  `aioice, aiortc, av, certifi, cffi, charset-normalizer, cryptography, curl_cffi, dnspython, google-crc32c, idna, ifaddr, lz4, numpy, packaging, pycparser, pycryptodome, pyee, pylibsrtp, pyOpenSSL, requests, sounddevice, typing_extensions, urllib3, wasmtime`
- 声明但**包内零引用**：`pyaudio`、`pydub`、`flask-socketio`、`opencv-python`
