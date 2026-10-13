# 双机器狗 Web 控制台 — 核查、架构与实施计划

**目标** 一个**双狗 Web 控制台**：两台 Go2（固件 ≥ 1.1.15，`data2=3`）在浏览器里同时在线、各自可见、可遥操作、可安全接管与急停；一份共享 fleet 核心同时供 Web 控制台与 MCP agent 复用。

**工作分支** `feat/dual-dog-console`（自 `master`，master 已 fast-forward 到 `feat/unitree-robot-mcp`）
**日期** 2026-10-13 · **基线** = upstream `v2.2.0` + 自有提交
**方法** 只读考古：本地 git 对象比对 · upstream 全包分字节 sha256 比对 · `gh` API · `unitree_ui` 全量 clone 精读。结论均带证据。

---

## 0. 执行摘要与当前进度

### 三个前提的核查结果

| 你的前提 | 结论 |
|---|---|
| ①"去年的工作很初级" | **证实，且更糟：功能性死亡。** 入口 import 已改名的包、人体跟随闭环从未闭上、**连不上你的狗** |
| ②"没跟上作者新更新" | **订正，方向是反的。** 当前分支 = upstream `master` = `v2.2.0`，**零落后**。落后 11 个月的是 `wpp` 分支 |
| ③"`unitree_ui` 可借鉴" | **证实有价值，但多机部分无货**——它自己是硬编码单机。可借的是协议知识、数据表、反面教材 |

### 进度（截至 2026-10-13）

| # | 事项 | 状态 | 证据 |
|---|---|---|---|
| C1 | `master` fast-forward 到 `feat/unitree-robot-mcp` | ✅ | 纯 fast-forward（master 是祖先），非重写历史 |
| C2 | 建工作分支 `feat/dual-dog-console` | ✅ | `4806f3e` |
| C3 | 吸收 upstream PR **#63**（`%Z`→`%z` 时区） | ✅ | 干净落盘，无冲突 |
| C4 | 吸收 upstream PR **#66**（WAF 可诊断 + TLS 指纹可切换） | ✅ | 3 文件干净落盘；README 手工合并 |
| P0.1 | **可运行环境 + 测试全绿** | ✅ | Python 3.14.8 + aiortc 1.15.0；**318 passed**（接手时 collection 阶段即挂） |
| P0.4a | **离线 ICE 并发探针**（全案最大未知） | ✅ | 3 连接：transport 独立、端口零冲突、关一个不影响其余 |
| P1.1 | 身份层：`RobotSpec` + `Roster` | ✅ | `f4679fe`；重复 id / 同 SN / 同 IP 均被拒；每狗派生独立 `state_dir` |
| P1.2 | **per-device key store —— 修掉静默错配 bug** | ✅ | `f4679fe`；含回归测试 + 旧 `keys.json` 格式迁移 |
| P1.4–P1.5 | `robot_id` 进 config / audit；**audit 跨写 bug 修复** | ✅ | `8c9b52a`；每个 `AuditLog` 私有 logger；顺带修好 `to_stderr` 从不生效 |
| P1.7 | `FleetManager`：N 个 Runtime、逐台容错、诚实的 `stop_all` | ✅ | `8c9b52a`；20 个测试 |
| P1.8 | **连接隔离**：进程级串行 + discovery 下线程 | ✅ | `d579183`；实测 0.3s 阻塞扫描期间心跳仍在跑（≥10 tick） |
| P1.10 | **MCP 侧舰队化**：31 个工具加 `robot_id` + 4 个舰队工具 + 舰队资源 | ✅ | `fdeb289`；工具体一行未改（注册层用 ContextVar 绑定）；22 个新测试 |
| P1.11–P1.13 | **Web 操作台**（Starlette + 原生 asyncio + 双栏 UI） | ✅ | `aeeed6d`；端口实测 11/11 通过：estop 锁存→409→解除→200，MJPEG 真帧 |
| P2.1–P2.2 | 舰队急停（逐台结果）+ **锁存式** e-stop | ✅ | `aeeed6d`；舰队锁存不可被单狗解除收窄 |
| P2.x | **切换目标自动归零** + 死手看门狗 + Split 双人键盘 | ✅ | `7cc4549`；驱动归属按 source 分键，覆盖 Both/Split |
| P0.5 | `pyproject.toml`：aiortc 加上界、Python 提到 ≥3.10 | ✅ | `aiortc>=1.9.0,<2`；`requires-python>=3.10` |
| P3.1 | **故障目录 + 快照/增量 store**（从 unitree_ui 借鉴） | ✅ | `134248b`；修掉"快照被当成事件流"——旧实现答不了"现在哪里坏了"；31 个新测试 |
| P3.2 | **控制台故障面板**（逐狗） | ✅ | `134248b`；实机端口实测：dog1 显示 [System]+[Motor 1]，dog2 显示 [Emergency stop]，互不串台 |
| P3.3 | **控制台复用 MCP 工具**（不是第二份实现） | ✅ | `21448d1`；`ToolBridge` + 19 个白名单工具；confirm/safe-mode/审计全部继承 |
| P3.4 | 姿态与动作按钮（站立/趴下/动作下拉） | ✅ | `21448d1`；动作列表来自 `robot_list_actions`，不硬编码 |
| P1.14 | **操作员租约** + 显式连接/断开 | ✅ | `f9d6f36`；页面打开期间不再每 45s 断链；WS 关闭即释放，且**从不主动连接** |
| P1.15 | JS/HTML 契约测试 + JS 语法检查 | ✅ | `test_console_app.py`；`getElementById`/`data-role` 与模板比对；有 node 时 `node --check` |
| P3.5 | **`upload_file` 修复——它一个字节都没发** | ✅ | `e33c584`；修复前实测：`publish()` 进入 0 次、`send()` 0 条、却返回 `"ok"` |
| P0.3 | 真机固件/`data2` 探测 | ⏸ **等硬件** | 局域网扫描 Go2/G1/R1 组均无应答，狗不在线 |
| P0.4b | 真机并发连接 + 阻塞验证 | ⏸ **等硬件** | 同上 |

**P0.4a 的实测结论（最重要的一条）**：`unitree_webrtc_connect/__init__.py:1-17` 把 ICE 凭据钉在**类属性**上，进程内所有 peer connection 共享同一组 ufrag/pwd（实测 3 条连接只有 1 个 ufrag）——**但这不构成阻塞**：3 条连接拿到 3 个独立 transport，36 个 host candidate **端口两两不同**，关掉第 0 条另两条存活。

> ⚠️ 共享 ufrag 只在**各连不同远端**时安全。这意味着机器人"只接受一个 WebRTC 客户端"这条约束必须由 fleet 层**强制**，而不是靠竞态。

### 现在怎么用

一份 `fleet.json` 同时驱动两个前端（Web 控制台与 MCP agent）：

```sh
cat > fleet.json <<'JSON'
{ "default": "dog1",
  "robots": [
    {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "B42D2000AAAA", "role": "lead"},
    {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B42D2000BBBB", "role": "wing"}]}
JSON
export UNITREE_AGENT_ROSTER=$PWD/fleet.json

unitree-fleet-console --backend mock          # Web 控制台，无机器人演示
unitree-fleet-console --backend webrtc        # Web 控制台，真机（狗上线后）
unitree-robot-mcp selftest                    # MCP 离线自检（对 roster 免疫，20/20）
unitree-robot-mcp serve                       # MCP server，工具自动带 robot_id
```

浏览器打开 `http://127.0.0.1:8765`。每只狗一栏：实时画面、电量/状态、**故障面板**、虚拟摇杆、姿态按钮（站立/趴下/动作下拉）、停止/急停。

- 键盘 `WASD` 前进/平移、`QE` 转向；`Split` 模式下第二只狗用 `IJKL+UO`——**一个操作员同时开两只**。
- 急停是**锁存**的，必须显式解除；舰队级锁存不能被单狗解除收窄。
- 姿态/动作按钮走的是**和 MCP 完全相同的受保护函数**（`/api/robot/{id}/tool`，19 个白名单工具），所以 `confirm`、危险动作闸门、safe mode、限流、审计全部继承。按钮发 `confirm: true`——人点按钮本身就是确认；与"人的意图"无关的闸门（safe mode、危险动作白名单）仍然会拒绝。
- 每只狗的 **连接/断开**按钮：从官方 App 手里把链路要回来，或主动交还，都不必先发一条运动指令。
- **页面打开期间链路不会被释放**（操作员租约）：机器人会话默认 45s 空闲就断开好让官方 App 接管，而"页面开着"不是空闲。租约只**阻止释放**，**从不主动连接**；最后一个浏览器关掉后会话照常自行释放。

MCP 侧先调 `robot_roster` 拿 id；每个工具都接受 `robot_id`，省略则作用于名册默认狗。停止全部用 `robot_stop_all`，并**必须检查 `not_stopped`**——链路不通的狗会列在那里，它没有停下。

### 立刻可做的下一步

1. **P0.3/P0.4b**（狗上线时，唯一的硬阻塞）：`scripts/probe_dual_connection.py --ips <dog1>,<dog2>` + 固件探测，然后 `unitree-fleet-console --backend webrtc`。
2. **控制台增强**（不需要机器人）：手柄（Gamepad API）接入——`Split` 模式本来就是为"一个操作员两只狗"设计的，手柄比键盘更合适；以及审计轨迹查看面板。
3. **驱动侧**：`upload_file` 已修，但 `request_static_file`（下载）走的是同一条 fire-and-forget 路径，值得按同样的方式核一遍。

---

## 1. 核查：`wpp` 分支为什么不能直接用

`wpp` 对 SDK **一个文件都没改**（纯追加 52 个文件），所以没有合并冲突，只有"新固件上完全跑不通"：

- **入口已死**：`dual_dog_main.py:166` 还在 import 改名前的 `go2_webrtc_driver` → `missing_deps` 非空 → `sys.exit(1)`。
- **人体跟随闭环从未闭上**：`human_tracking/video_processor.py:117` 在工作线程里调 `asyncio.get_event_loop()`，必然 `RuntimeError`，被 `except` 吞掉，**每条 PID 指令静默丢弃**。且 `if human_bboxes:` **没有 else** → 目标丢失后最后一条速度留在机器人上，**狗会继续走**。
- **连不上你的狗**：`dual_dog_controller.py:113-121` 完全没有 `aes_128_key`；`unitree_auth.py` 只有 `data2 == 2` 分支。你的 Go2 是 `data2=3` → 握手在 `rsa_load_public_key` 就炸，报的还是一个看起来像"网络问题"的 traceback。
- **没有协同层**：所谓"双狗同步" = 同一按钮扇出两条 `create_task`（`:312`）。唯一承诺协同的 `MovementPattern.FOLLOW_LEADER`（`dual_dog_movement.py:24`）**全仓库零引用**，`start_pattern` 直接 `return False`。
- **测试 11 个文件，0 个 `assert`**；文档 5,722 行（比应用代码还多）是 AI 会话报告。

**判决：不 merge，前向移植想法。** 唯一原样带走的结构资产是 `web_server.py:95-127` 的 loop 引导模式（我们改用原生 asyncio，见 §4）。`static/css/style.css`（616 行纯 CSS）可原样复用。

**不要 `git checkout origin/wpp -- .`**：会删掉 `unitree_cloud.py`、`_cli.py`、`LICENSE` 和 10 个 example 目录（`git diff --name-status caf1def origin/wpp` 里**一个 `D` 都没有**——那些是 upstream 在分叉**之后**新增的）。

---

## 2. 关键事实

### 2.1 分支地形

```
caf1def (2025-10-06)  ← wpp 分叉点
   ├──► origin/wpp 8122545 (2026-03-27)   11 commits，+52 文件，SDK 零改动
   └──► origin/master e0abc57 (2026-08-16) = tag v2.2.0 = PyPI 2.2.0   32 commits
             └──► feat/unitree-robot-mcp c325794 (2026-09-14)  4 commits（MCP + skill + 瘦身 + 测试）
                       └──► master（已 FF）► feat/dual-dog-console  ★ 我们在这里
```

`origin/2.x.x` 是上游历史遗留线；本地 `54cecf3` 是 fork 专有的 wpp 合并，**已无价值**。

### 2.2 你的机器狗：`data2=3`

| 机器人 | 固件 | 认证 | 当前分支 | `origin/wpp` |
|---|---|---|---|---|
| Go2 | 1.0.19–1.0.25 / 1.1.1–1.1.14 | `data2=2` 静态 | ✅ | ✅ |
| Go2 | **≥ 1.1.15（你的）** | `data2=3` per-device | ✅ `aes_128_key=` | ❌ |
| G1 / R1 | — | `data2=2` / `data2=3` | ✅ | ❌ |

key 是 per-device、跨重配对稳定，存于机器人 `/unitree/etc/key/aes_key.bin`，云端以 `dev.key` 出现在 `device/bind/list`。

**⚠️ 一个必须修的静默错配**：`unitree_robot_agent/config.py:295-322` 的 key 缓存按 `device_type:sn_or_ip` 索引，**但有 family 兜底**——第二台同型号狗只配 IP 时，family 兜底会把**狗 #1 的 key 交给狗 #2**。失败形态是 GCM tag 校验不过（`AesKeyRejectedError`）。双狗场景下这是**必然踩到**的路径。

### 2.3 当前分支离双狗有多远

**好消息**：单机设计扎实——`RobotSession` 自带 `Guard`/`SessionStats`/connect 锁/holder 计数/空闲看门狗；profile 无实例可变状态；mock backend 658 行全部状态在 `self` 上，能模拟 811→830 重定向、冻结状态上报、1001/1003 拒绝、`RobotBusy`、真 JPEG 帧、yaw 积分闭环。**没有任何东西架构上敌视 2 台。**

**阻挡点是身份，不是机器人接口**：

| 级别 | 差距 | 证据 |
|---|---|---|
| **R1** | audit logger 是进程级单例，`robot=` 只有**型号名** | `core/audit.py:27,116-131`；`context.py:56,98,110`；`session.py:140,164,410,439`。**已实测复现互相污染** |
| **R2** | 一个 `ToolContext`/`Runtime`/31 个工具全部闭包捕获它，**无 target 参数** | `server.py:88-104`、`tools/context.py:27-31` |
| **R3** | 无身份的共享 state dir | `config.py:216-232`（keys.json family 兜底）、`auth.py:175-182`（`clear_session()` **删整个文件**）、`capture.py:186-268`（快照索引无 robot id + `index.jsonl.tmp` 固定名竞态） |
| **R4** | `conn.connect()` 里的阻塞 I/O 直接在 loop 上 | `webrtc_backend.py:115-122`（只构造函数下线程）、`webrtc_driver.py:64-84`、`unitree_auth.py:205-224`、`multicast_scanner.py:68-70`（UDP 10134 单 owner） |
| **R5** | 无舰队急停、无协调 | `locomotion.py:49-66`、`session.py:391-400`（链路断时**不碰机器人就返回 False**）；全包 `collision\|fleet\|coordinat` 零命中 |
| **R6** | `AgentConfig` 运行时被改写 | `config.py:346-376`、`auth.py:200,450-451` |
| **R7** | 视频：回调**串行** await，第一个 `while True: recv()` 会饿死第二个消费者 | `webrtc_video.py:24-33`、`webrtc_backend.py:322-354` |
| **R9** | `FutureResolver` 兜底 key = `f"{type} $ {topic}"`，且一次性 resolve 该 key 下**所有** pending future | `msgs/future_resolver.py:113-120`。**加第二台之前必须先修** |

**约走了 30–40%。** R1–R3、R6、R9 是低风险机械改动；R4 是真项目。

### 2.4 `unitree_ui` 能借什么

**明确不能借**：它**没有**多机支持（`app.ts:2515-2519` 已连接时再连会**拆掉原连接**；`heartbeat.ts:4` 模块级单例定时器；`data-channel.ts:14` 单槽 response handler，其 `docs/slam.md:257` 自认"并发会踩坏响应链"）。**因为它从不复用连接，里面根本没有多机延迟/带宽方案可抄。**

能借的（按性价比）：故障目录 + 每电机合成（我们的 `app_error_messages` 只有 33 条、只到 source 600）· 故障快照/增量语义（我们现在是追加，答不了"现在哪坏了"）· **per-chunk ack**（我们的 `msgs/rtc_inner_req.py:98-141` 的 `upload_file` 是 fire-and-forget，**永远返回 `"ok"`**）· 急停 `header.policy.priority=1` + 按机型分支（Go2 1001 / G1·R1 7101 `{"data":1}`）· 输入源仲裁模式 · BLE 配网（`server/ble_server.py` **本来就是 Python，可直接 vendor**）· USLAM 命令表。

**许可**：MIT，同一个作者 → 复用零障碍。

---

## 3. 范围收紧：N → 2，改变了什么

| 议题 | N 狗方案 | **双狗方案（我们的）** |
|---|---|---|
| 视频 | 只有焦点狗开流 | **两路都默认开**。这是控制台的核心价值：同屏看两只。带宽靠**每路分辨率/fps 上限** + "省流模式"（一路实时 + 一路缩略图）控制 |
| 订阅 | 焦点驱动订阅集 | 两狗都订阅**状态**；视频可独立开关 |
| 互锁 | "同一时刻只允许一个 mover" | **太保守**——双狗的用法大多是同时走。改为：**默认允许同时移动**，配 `STOP ALL` + 逐狗 e-stop + 可选"串联模式"（一条指令发两只） |
| 协同 | 编队原语泛化 | 直接做 **leader/follower + 固定偏移**，以及 Go2 自带 lead-follow（注意 `SPORT_CMD` 把 `LeadFollow`/`FreeWalk` 都映射到 1045，`robots/go2.py:14-16` 因此**故意排除**，需先消歧，不能简单放开） |
| 前端 | 可扩展列表 | **固定双栏**布局，为 2 台优化，代码仍按 dict 写以便日后扩到 N |
| 输入 | 焦点 + 仲裁 | **这是双狗控制台真正的设计难点**，见 §6 |

**架构仍按 N 写，UI 按 2 优化。** 这样不牺牲未来的扩展性。

---

## 4. 目标架构

一份共享核心，两个前端。**不做 package 大搬迁**——避免动那 8,789 行已有 158 个测试覆盖的代码。

```
unitree_webrtc_connect/          ← 上游 vendored 驱动。保持字节对等，只留有记录的补丁
                                    （现有 3 处：懒 pydub / 懒 LiDAR / 删未用 import）
unitree_robot_agent/             ← per-robot 核心 + MCP 面（已有，改动最小化）
  core/ backends/ motion/ vision/ robots/  复用，只加 robot_id / 修 audit / 修 resolver
  fleet/                         ← 【新增】
    spec.py        RobotSpec：id/model/ip/sn/key/role；校验 id·SN·IP 唯一
    roster.py      JSON/env 加载 → list[RobotSpec]；派发 per-robot state_dir
    keys.py        per-device key 缓存（按 SN 索引，**删掉 family 兜底**）
    manager.py     FleetManager：dict[robot_id → Runtime]，get/aclose_all/stop_all/status_all
    connect.py     连接隔离：阻塞段下线程；进程级 discovery 串行化；per-robot semaphore+退避
    media.py       每狗视频开关 + 分辨率/fps 上限 + single-flight 快照
  tools/ server.py cli.py        ← 工具加 `robot_id`；新增舰队工具与资源
unitree_fleet_web/               ← 【新增】FastAPI + 原生 asyncio 控制台
  app.py           FastAPI app，与 WebRTC 同一 loop
  routes/          /api/fleet /api/robot/{id}/* /api/video/{id}.mjpeg
  ws.py            WebSocket 状态/遥测推送
  static/          CSS 复用 wpp 的 style.css；JS 重写（不用 innerHTML）
  templates/
```

**三条决定成败的约束**

1. **每台狗一个 `AgentConfig`、一个 `AuditLog`、一个 `FutureResolver`、一个 `state_dir`。绝不共享。**
2. **Web 层必须是原生 asyncio，与 WebRTC 同 loop。** 这样 `run_coroutine_threadsafe`、`_run_async` 桥、`Future.result()` 全部消失——`wpp` 的 Flask 线程 vs asyncio 线程阻抗失配是它一整类 bug 的根源。
3. **`robot_id` 从第一行代码就带。**

---

## 5. 实施计划

### Phase 0 — 地基与真相

| # | 任务 | 状态 | 验收 |
|---|---|---|---|
| P0.1 | 可运行环境 + 测试全绿 | ✅ | **158 passed**（Python 3.14.8 + aiortc 1.15.0） |
| P0.2 | 密钥通道：吸收 PR #63 + #66 | ✅ | `unitree-fetch-aes-key --impersonate safari` 可用；WAF 拦截可自诊断 |
| P0.4a | 离线 ICE 并发探针 | ✅ | 3 连接 transport 独立、端口零冲突 |
| P0.3 | 真机固件/`data2` 探测 | ⏸ 等硬件 | 每台狗实测 `data2` + SN，产出 `fleet.json` 草稿 |
| P0.4b | 真机并发连接 + 阻塞验证 | ⏸ 等硬件 | 连第 2 台时第 1 台遥测不卡顿 |
| P0.5 | `pyproject.toml`：aiortc 加上界、Python 提到 ≥3.10 | 待做 | 依赖不再漂移 |

### Phase 1 — 共享 fleet 核心（不需要机器人，mock 可全离线验证）

**1A 身份（R1–R3、R6、R9）**

| # | 文件 | 任务 |
|---|---|---|
| P1.1 | `fleet/spec.py` + `roster.py` | `RobotSpec`；`UNITREE_AGENT_ROSTER` 加载 JSON；校验 id/SN/IP 唯一、per-device key 齐备；**派生 `state_dir=<base>/<robot_id>/`**——这一行同时修好 key 缓存、session 缓存、快照归档、audit 路径四处 |
| P1.2 | `fleet/keys.py` | 按 **SN** 索引的 key 缓存；**删掉 family 兜底**（`config.py:314-322`）；`AesKeyRequiredError` → "去跑 fetch-aes-key"，`AesKeyRejectedError` → "SN 与 key 不匹配" |
| P1.3 | `config.py` | 加 `robot_id`；接受 spec dict；`config_from_env` 保持单机兼容；运行时改写隔离到 per-robot 实例 |
| P1.4 | `core/audit.py` | **per-instance logger**（不再往模块级单例加 handler）；所有记录强制 `robot_id` |
| P1.5 | `context.py` / `session.py` / `guards.py` | `robot=` 从型号名 → `robot_id`；`target_label()` 降级为"日志安全的 target" |
| P1.6 | `msgs/future_resolver.py` | **去掉 topic-only 兜底 key**；key = `(robot_id, type, topic, api_id, request_id)`；每 session 一个 resolver。**必须在加第二台之前做** |

**1B 舰队与连接（R2、R4、R7）**

| # | 文件 | 任务 |
|---|---|---|
| P1.7 | `fleet/manager.py` | `FleetManager` 持有 N 个 `Runtime`；`get/status_all/stop_all/aclose_all`；`asyncio.gather` 扇出 + **逐台捕获错误**。**包装 `create_runtime`，不改它** |
| P1.8 | `fleet/connect.py` | 阻塞段（`discover_ip_sn`、云调用、SDP POST）下线程；**进程级 discovery 串行化**（UDP 10134 单 owner）；per-robot semaphore + 退避；`RobotBusyError` → 明确告知"官方 App 占着" |
| P1.9 | `fleet/media.py` | 每狗视频开关 + 分辨率/fps 上限；快照 single-flight；JPEG 编码走**专属** executor，不挤共享默认 executor |
| P1.10 | `tools/*` + `server.py` | 工具加 `robot_id: str = ""`；新增 `robot_roster`/`robot_fleet_status`/`robot_stop_all`/`robot_emergency_stop_all`；资源 `unitree://fleet/status`、`unitree://robot/{id}/…`；重写 `INSTRUCTIONS`。**不要**用 `dog1_move` 前缀 |

**1C Web 控制台**

| # | 文件 | 任务 |
|---|---|---|
| P1.11 | `unitree_fleet_web/app.py` | FastAPI + uvicorn，**与 WebRTC 同 loop**；token 鉴权；**默认绑 `127.0.0.1`**，`--host` 显式开启 |
| P1.12 | `routes/` + `ws.py` | `/api/fleet`、`/api/robot/{id}/*`、`/api/video/{id}.mjpeg`；WebSocket 推状态/遥测（~5Hz） |
| P1.13 | `static/` + `templates/` | 双栏布局；CSS 复用 `wpp` 的 `style.css`；JS 重写，**`textContent` 不用 `innerHTML`**（`wpp` 的 `escapeHtml` 全仓库未定义，狗名可注入 `onclick`） |
| P1.14 | **操作员租约** | 机器人只接受一个 WebRTC 客户端。控制台要常驻，45s idle release 是要把链路让给官方 App。做成**租约**：页面有活跃 WS 连接 + 近期有操作 → 持有；超时/点"释放给 App" → 释放 |

**Phase 1 验收**：两台真机同时在浏览器里可见、可逐台遥操作、可 STOP ALL；一台掉线不影响另一台；audit 能区分两台同型号狗；MCP 能对指定狗下令。

### Phase 2 — 协同与安全

| # | 任务 | 要点 |
|---|---|---|
| P2.1 | 舰队急停 | 一个动作扇出，**返回逐台结果**（delivered / link-down / refused），不是单个 bool——`safe_stop` 在链路断开时是"不碰机器人就返回 False"，必须如实报"狗 2 **没有**停下" |
| P2.2 | e-stop 闭锁 | 急停后**锁定**所有运动指令直到显式解除（`unitree_ui` 的 `emergencyStopped` 模式）；接 `header.policy.priority=1`（`msgs/pub_sub.py:111-115` 已支持）+ 按机型编码 |
| P2.3 | 串联模式（tandem） | 一条指令发两只，**带发送时间戳**、每狗延迟补偿，保证同时起步 |
| P2.4 | 完成反馈 | 替掉 `sleep(3.0)`（`wpp` 靠字符串匹配返回 6.0/4.0/3.0）。用 `sportmodestate`/IMU/状态回读闭环 |
| P2.5 | leader/follower | 固定偏移编队；先消歧 lead-follow api_id（2056 MCF / 1045 冲突） |
| P2.6 | 接近警告（可选） | 需外部定位或 LiDAR；Go2 的 LiDAR 现在只暴露"开/关 + 点数摘要"（`tools/go2.py:270-315`） |

### Phase 3 — 从 `unitree_ui` 收割（按需，各自独立）

故障目录 + 快照/增量 store → **per-chunk ack 修复** → 急停 priority → BLE 配网（可直接 vendor）→ SLAM/示教/地图。

⚠️ 吸收 upstream PR **#64/#67** 时注意：它们都改 `webrtc_audiohub.py`——**我们唯一改过的库文件**（懒 `pydub`）。需手工合并。

---

## 6. 控制台规格

### 6.1 布局

```
┌───────────────────────────────────────────────────────────────────────┐
│  Fleet: 2/2 online        [ ⏹ STOP ALL ]   [ 🔴 E-STOP latching ]     │
├──────────────────────────────┬────────────────────────────────────────┤
│  Dog 1  ● connected          │  Dog 2  ● connected                    │
│  ┌────────────────────────┐  │  ┌────────────────────────┐            │
│  │   live video (MJPEG)   │  │  │   live video (MJPEG)   │            │
│  └────────────────────────┘  │  └────────────────────────┘            │
│  battery · speed · FSM       │  battery · speed · FSM                 │
│  active faults               │  active faults                         │
│  ┌───── virtual sticks ───┐  │  ┌───── virtual sticks ───┐            │
│  └────────────────────────┘  │  └────────────────────────┘            │
│  [stand][sit][hello][stop]   │  [stand][sit][hello][stop]             │
├──────────────────────────────┴────────────────────────────────────────┤
│  输入源 →  [ Keyboard ▾ ] 目标: ( ) Dog1 ( ) Dog2 (•) Both ( ) Split  │
│  Split 映射:  Dog1 = WASD+QE    Dog2 = IJKL+UO                          │
└───────────────────────────────────────────────────────────────────────┘
```

### 6.2 输入仲裁（双狗控制台真正的设计难点）

一把键盘/手柄，两只狗。四种模式：

| 模式 | 行为 | 用途 |
|---|---|---|
| **Focus** | 输入驱动被选中的那只（点击狗面板切换焦点） | 精细操作单只 |
| **Both** | 输入同时驱动两只（正确的"串联"） | 齐步走 |
| **Split** | **Dog1 = WASD+QE，Dog2 = IJKL+UO**（或方向键） | **一个操作员同时手动驾驶两只**——`unitree_ui` 和 `wpp` 都没有的能力 |
| **Per-source** | 手柄 → Dog A，键盘 → Dog B | 双人/双设备协同 |

**关键约束（来自 `unitree_ui` 的教训）**：**切换目标时必须把原目标的摇杆归零。** `unitree_ui` 的 `app.ts:1173-1177` 在切换输入源时会 zero 共享状态；不这么做，被切走的那只狗会**保持最后一条非零速度继续走**。`RobotSession.zero_joystick(repeats=3)`（`core/session.py:375`）已有此机制，需保证它在仲裁切换路径上被调用。

另注意 `unitree_ui` 的 `JOYSTICK_RELEASE_TICKS = 4`：松开后要多发几拍零速，否则空闲抑制会把机器人**留在最后一条非零指令上**。

### 6.3 安全

- **E-STOP 闭锁**：点击后置位并**拒绝所有运动指令**，直到显式解除；发送时带 `priority=1`，按机型分支（Go2 1001 / G1·R1 7101 `{"data":1}`）。
- **STOP ALL**：逐台返回结果，明确列出哪只**没有**停下。
- **每狗独立 e-stop**（面板上的红按钮）。
- **默认绑 `127.0.0.1`**；局域网暴露需 `--host 0.0.0.0` + token。
- `wpp` 的 web 层**零鉴权**、`0.0.0.0`、`SECRET_KEY` 硬编码、CORS `*`、存储型 XSS —— 一律不继承。

---

## 7. 明确不做

- ❌ merge `origin/wpp` 或 `git checkout origin/wpp -- .`（会删 `unitree_cloud.py`/`_cli.py`/`LICENSE`/10 个 example 目录）
- ❌ 照搬 `wpp` 的 `constants.py`/`webrtc_driver.py`/`util.py`/`multicast_scanner.py`/`unitree_auth.py`/`__init__.py`（停在 `caf1def`：无导出、无 R1/LOCO/MCF/避障常量、`handle_error`/`future_resolver` 的 bug 都还在、`discover_ip_sn` 只查 Go2 组）——**能用但静默跑错，最坏的一种**
- ❌ `dog1_move` 式工具名前缀（31×N 会炸掉工具列表）
- ❌ 共享 `AgentConfig` / `AuditLog` / `ToolContext` / `FutureResolver`
- ❌ 复制 `unitree_ui` 的模块级单例 timer、单槽 response handler、单例 cloud client
- ❌ `wpp` 的 `libs/aioice` gitlink 和 `1753683727739-…claude_code_prod.sh`
- ❌ 指望 upstream 或 `unitree_ui` 提供多机地基（两边都是零）

---

## 8. 风险与未知

| 风险 | 级别 | 状态 / 缓解 |
|---|---|---|
| 共享 ICE 凭据在 N 条并发下不可用 | ~~高~~ → **低** | ✅ **已实测排除**（transport 独立、端口零冲突）。真机并发仍待验 |
| **拿不到 AES key**（官方路径被 WAF 拦，issue #65） | **高** | ✅ 已吸收 PR #63/#66。**仍需你实测登录一次** |
| 真机 `connect()` 阻塞共享 loop | 中高 | P1.8 连接隔离；真机用"连第 2 台时第 1 台遥测是否卡顿"验收 |
| 操作台常驻 vs 官方 App 抢链路 | 中 | P1.14 租约模型 |
| **key 缓存的 family 兜底把狗 #1 的 key 给狗 #2** | 中高 | P1.2 必修；双狗同型号时**必然**触发 |
| aiortc 无上界（现 1.15.0 需 Python ≥3.10，我们声明 `>=3.8`）；monkey patch 只分支 `==1.10.0`/`>=1.11.0` | 中 | P0.5；1.15.0 下 158 测试已通过 |
| 多机带宽：两路视频同时解码 | 中 | P1.9 每狗分辨率/fps 上限 + 省流模式 |
| 上游 4 个未合并 PR 未来冲突（#64/#67 打在 `webrtc_audiohub.py`） | 低中 | 保持"上游文件字节对等 + 改动有记录" |

**未验证（诚实标注）**：你每台狗的**实际**固件与 `data2` 值（需 P0.3 探针）· 真机并发连接行为（狗不在线）· `unitree_ui` 的 G1/R1 路径未经硬件验证。

---

## 附录：证据来源

**本地（只读）**：`git log/merge-base/diff --name-status/archive`、`git cat-file`、`grep` 全包扫描、`.venv` 内 `pytest`。
**upstream**：`gh api`（commits/tags/branches/releases/pulls/issues/search）+ `/tmp` shallow clone + 23 个库文件 sha256 逐字节比对 + PyPI JSON。
**`unitree_ui`**：`git clone` @ `590baf6`，精读 `src/`、`server/`、`docs/`。MIT © 2025 Konstantin Severov。
**`origin/wpp`**：`git archive` 到 `/tmp/wpp-tree` 后精读（不在仓库里建 worktree）。
**探针**：`scripts/probe_dual_connection.py --count 3`（离线，3 连接，36 candidates）。
