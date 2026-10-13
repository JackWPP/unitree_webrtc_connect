# `origin/wpp` 深度调研报告 — 对"两狗 Web 控制台"的可移植价值

> 调研对象：`origin/wpp`（HEAD = `8122545f4d0c9c0ba12047171982f8d13ef8504b`）
> 合并基点：`caf1def86961db2fa880d2aae55494731369e271`（= `master` 与 `wpp` 的 merge-base）
> 调研方式：只读。全程 `git show origin/wpp:<path>`，未修改、未提交、未切换分支。
> 所有结论均可回溯到 `origin/wpp:<path>:<line>`。凡代码无法证实的，明确标注为"未验证"。

---

## 0. 范围界定（先说清楚"哪些算 WPP 的功劳"）

必须区分两组 diff，否则会误判：

| 对比基准 | 规模 | 含义 |
|---|---|---|
| `master..origin/wpp` | 159 files, +16754 / −16605 | **包含 2.x.x 基线分叉**，不是 WPP 的工作 |
| `caf1def..origin/wpp`（merge-base） | **54 files, +16187 / −11** | **这才是 WPP 真正新增的东西** |

WPP 真正改动的库内文件只有 6 个，且全是新文件：

```
$ git diff --name-only caf1def..origin/wpp | grep '^unitree_webrtc_connect/'
unitree_webrtc_connect/human_tracking/__init__.py
unitree_webrtc_connect/human_tracking/pid_controller.py
unitree_webrtc_connect/human_tracking/tracking_manager.py
unitree_webrtc_connect/human_tracking/video_processor.py
unitree_webrtc_connect/human_tracking/yolo11_tracker.py
unitree_webrtc_connect/human_tracking_extension.py
```

**结论：WPP 完全没有改动 `webrtc_driver.py` / `pub_sub.py` / `constants.py` / `webrtc_video.py` 等核心驱动。**
`master..wpp` 里 `unitree_auth.py`(+568/−) `unitree_cloud.py`(−293) 之类的巨大差异，全部是 2.x.x 基线造成的，**不能算作 WPP 的成果**。凡引用库内行为，我引的都是 2.x.x 基线代码（WPP 直接继承），会明确标注。

合并基点里被 WPP **删除**的：`master` 的整套 `tests/`（`git ls-tree -r --name-only origin/wpp | grep -c '^tests/'` → `0`）。这是最严重的负资产，详见 §8。

---

## 强烈建议移植（按对"两狗控制台"的价值排序）

### S1. 服务端 WebRTC → MJPEG 桥（**最高价值**）

这是整个分支唯一真正解决"视频看不见"的东西，也是唯一被实际跑通的浏览器视频路径。

- 服务端收帧 → JPEG → `multipart/x-mixed-replace`：`origin/wpp:web_server.py:574-600`
  ```python
  ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
  yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
  ```
  `origin/wpp:web_server.py:588`、`origin/wpp:web_server.py:599-600`
- 前端用 `<img>` 而不是 `<video>`：`origin/wpp:templates/index.html:175`
  ```html
  <img id="videoElement" style="display: none; width: 100%; height: 100%; object-fit: contain;" alt="视频流">
  ```
  赋值处：`origin/wpp:static/js/app.js:813` `videoElement.src = result.stream_url;`
- 每狗独立帧队列，满了丢最旧帧（不丢新帧）：`origin/wpp:web_server.py:641` `Queue(maxsize=10)`，`origin/wpp:web_server.py:653-657`
- 走的是 `add_track_callback`（库已支持多回调）：`origin/wpp:web_server.py:689`；库侧实现 `origin/wpp:unitree_webrtc_connect/webrtc_video.py:16-23`

**为什么值得移植**：这条路线是踩了两次坑才定下来的，踩坑记录比代码更值钱：

1. 第一版想拼 HTTP URL 直接喂 `<video>`，404：
   `origin/wpp:CAMERA_FIX_SUMMARY.md:20-27`
   > `# 错误的实现：试图构建HTTP视频流URL` / `stream_url = f"http://{ip}:8080/video_stream"` / `# 这个URL实际不存在`
2. 第二版放弃，改成"只开通道，用官方 APP 看"：`origin/wpp:CAMERA_FIX_SUMMARY.md:34-39`
   > `'message': '视频通道已开启，需要专用WebRTC客户端查看',` / `'note': 'WebRTC视频流无法直接通过HTTP URL访问',`
3. 第三版才做成 MJPEG：`origin/wpp:VIDEO_STREAM_FIX.md:13-27`
   > 将WebRTC接收到的视频帧转换为MJPEG（Motion JPEG）流，这是一种浏览器原生支持的视频流格式。
4. 方案对比表（为什么不用 HLS / WebRTC 直连 / RTSP）：`origin/wpp:VIDEO_STREAM_FIX.md:199-215`

**注意**：`CAMERA_FIX_SUMMARY.md` 从未被更新，它至今仍写着"无法在Web界面直接显示视频画面"（`origin/wpp:CAMERA_FIX_SUMMARY.md:160`），与后来的 MJPEG 实现矛盾。**文档不可信，以代码为准。**

### S2. HTTP 线程 → asyncio 线程的桥接范式（`run_coroutine_threadsafe` + `future.result(timeout)`）

任何"Flask 路由要调机器人"的场景都必须过这一关，WPP 把结论写死了。

- 正例（开视频通道）：`origin/wpp:web_server.py:692-708`
  ```python
  if self.asyncio_loop:
      future = asyncio.run_coroutine_threadsafe(enable_video_async(), self.asyncio_loop)
      result = future.result(timeout=5.0)  # 等待最多5秒
  ```
- 正例（关视频通道，3 秒超时，失败只 WARNING 不报错）：`origin/wpp:web_server.py:769-774`、`origin/wpp:web_server.py:776-777`
- 统一包装器：`origin/wpp:web_server.py:122-127`
- **"为什么不能用 `call_soon_threadsafe`"的一手结论**：`origin/wpp:EVENTLOOP_FINAL_FIX.md:13-36`
  > 2. **第二次尝试**：使用 `call_soon_threadsafe()` / ❌ 仍然失败：虽然在正确的线程执行，但同步函数内部访问事件循环时仍会失败 / `call_soon_threadsafe()` 只是调度普通函数，不创建协程上下文
  > 3. **最终方案**：使用 `run_coroutine_threadsafe()` / ✅ 成功：创建完整的协程上下文
- 症状原文：`origin/wpp:EVENTLOOP_FINAL_FIX.md:6-9`
  > `There is no current event loop in thread 'Thread-34 (process_request_thread)'.`
- 每次阻塞调用的超时表（5s / 3s / 10s / 15s / 30s）散落在路由里，值得抄成常量：
  `origin/wpp:web_server.py:269`(30) `:277`(15) `:292`(15) `:341`(10) `:356`(10) `:371`(5) `:399`(10) `:408`(10) `:438`(30) `:485`(10) `:708`(5.0) `:774`(3.0)

### S3. `all / single / selected` 指令路由抽象

两狗控制台的核心就是"这条指令发给谁"，WPP 的抽象干净且可直接搬。

- 控制模式与选中列表：`origin/wpp:dual_dog_controller.py:53-55`
  ```python
  self.control_mode = "all"  # "all", "single", "selected"
  self.selected_dogs: List[str] = []  # 选中的机器狗列表
  ```
- 入口三分：`origin/wpp:dual_dog_controller.py:291-298`
- 扇出实现（**每狗一个 task，并行下发，回执聚合成 `Dict[str, bool]`**）：`origin/wpp:dual_dog_controller.py:300-327`
  ```python
  task = asyncio.create_task(self.send_command_to_dog(name, command, parameters))
  ...  # 并行发送指令
  for name, task in command_tasks:
      results[name] = await task
  ```
- 只把"在线且非 ERROR"的狗纳入目标：`origin/wpp:dual_dog_controller.py:392-409`
- 这个 `Dict[str, bool]` 回执贯穿全分支，是所有"几台成功"判定的基础
  （如 `origin/wpp:dual_dog_movement.py:135-138`、`origin/wpp:dual_dog_movement.py:218-222`）
- 每狗方法生成器（`f"{action}_single"` / `f"{direction}_single"`）：
  `origin/wpp:dual_dog_movement.py:325-333`、`origin/wpp:web_server.py:394-397`、`origin/wpp:keyboard_controller.py:303-317`

**关键设计点**：`Move` 等指令是"广播 + 各自回执"，**不是**任何形式的同步屏障 / 时间对齐。多狗是"同一 tick 内并发发出"，落地上仍然各走各的网络栈。**没有任何 formation / leader-follower / 时间同步代码**（见 §3）。

### S4. 每狗状态契约 + 心跳看门狗 + Socket.IO 推送

WPP 用一个 `connected` 布尔字段统一前后端判断，并为此写了一整篇事故复盘——这类"接口形状不一致导致整类 UI bug"的教训直接适用于我们的控制台。

- 后端唯一权威的状态结构（`connected` 是**计算出来的**，不是状态机里的）：
  `origin/wpp:dual_dog_controller.py:335-352`
  ```python
  # 检查机器狗是否真正连接（不仅仅是connection对象存在）
  is_connected = (dog.connection is not None and dog.status not in [DogStatus.DISCONNECTED, DogStatus.ERROR])
  ```
- WebSocket 推送必须带完整 `dog_info`（而不是只带 status 字符串）：
  `origin/wpp:web_server.py:152-180`，特别是 `origin/wpp:web_server.py:165-170`
  ```python
  self.socketio.emit('status_update', {
      'dog_name': dog_name, 'status': status.value,
      'dog_info': dog_info,  # 包含完整信息（含connected字段）
      'timestamp': datetime.now().isoformat()})
  ```
- 新客户端连上即收到全量快照：`origin/wpp:web_server.py:973-982`
- 前端优先信 `connected`：`origin/wpp:static/js/app.js:758-779`
- **事故复盘（这才是重点）**：`origin/wpp:STATUS_SYNC_COMPLETE_FIX.md:18-56`
  > 1. **WebSocket状态推送不完整** — 后端的`_on_dog_status_change`回调只发送了基本状态信息 / 缺少关键的`connected`字段（布尔值）
  > 2. **前端状态更新函数缺失** — `updateSingleDogStatus`函数几乎为空，只打印日志
- 心跳看门狗：30 秒无 `LOW_STATE` 判 ERROR，5 秒轮询一次
  `origin/wpp:dual_dog_controller.py:202-213`（喂心跳）、`:443-464`（判超时）
  ```python
  if current_time - dog.last_heartbeat > 30:
      self.logger.warning(f"机器狗 {name} 心跳超时")
      dog.status = DogStatus.ERROR; dog.error_message = "心跳超时"
  ```
  阈值也写在配置里（未接线）：`origin/wpp:config.json:7` `"heartbeat_timeout": 30`、`origin/wpp:config.json:6` `"monitor_interval": 5`

### S5. 设备发现 → 自动填表 → 批量添加 → 连接（onboarding 全链路）

- 路由（在独立线程跑阻塞扫描 + 10 秒硬超时）：`origin/wpp:web_server.py:504-541`
  ```python
  with concurrent.futures.ThreadPoolExecutor() as executor:
      future = executor.submit(scan_in_thread)
      devices = future.result(timeout=10)  # 10秒超时
  ```
- 设备名用序列号后 6 位：`origin/wpp:web_server.py:522` ``device_name = f"Unitree-{serial[-6:]}"``
- 扫描器封装（异步 + 连续扫描 + 回调）：`origin/wpp:network_scanner.py:56-89`、`:91-109`
- 连续扫描间隔 10 秒、单次超时 3 秒：`origin/wpp:network_scanner.py:91`（`interval: float = 10.0, timeout: float = 3.0`）
- 前端多选 + 批量 POST：`origin/wpp:static/js/app.js:280-304`
- 已有设备追加到"未被占用"的槽位：`origin/wpp:dual_dog_gui_enhanced.py:428-438`

底层用的是 `discover_ip_sn`（组播 231.1.1.1:10131 查询，10134 收回复）：`origin/wpp:unitree_webrtc_connect/multicast_scanner.py:6-8`、`:29-32`、`:42-52`
**注意**：这是 2.x.x 基线的简单版本（只有 `timeout` 参数、固定查询名 `unitree_dapengche`）；当前 `master` 的同名函数签名更丰富（`device_type="Go2"`, `sn=None`，见 `master:unitree_webrtc_connect/multicast_scanner.py:44`）。**移植时应基于 master 版本，别抄 WPP 这个旧版。**

### S6. 录制：首帧重建 `VideoWriter` 的自适应分辨率技巧

- 先用假分辨率占位：`origin/wpp:web_server.py:826-830`
  ```python
  fourcc = cv2.VideoWriter_fourcc(*'mp4v'); fps = 30.0
  frame_size = (640, 480)  # 默认分辨率，会在第一帧时更新
  writer = cv2.VideoWriter(filepath, fourcc, fps, frame_size)
  ```
- 第一帧到达时 release + 用真实分辨率重建：`origin/wpp:web_server.py:666-677`
  ```python
  # 如果是第一帧，重新创建writer以匹配实际分辨率
  if rec_info['frame_size'] is None:
      height, width = img.shape[:2]
      rec_info['frame_size'] = (width, height)
      writer.release()
      new_writer = cv2.VideoWriter(rec_info['filepath'], fourcc, fps, (width, height))
  ```
- 录制状态查询 / 停止 / 时长统计：`origin/wpp:web_server.py:890-920`、`:922-951`
- 前端：闪烁指示器 + 计时器 + 帧数/时长回显：`origin/wpp:templates/index.html:176-182`、`origin/wpp:static/js/app.js:1007-1029`、`origin/wpp:static/js/app.js:985`
- 关摄像头自动停录制、清空帧队列：`origin/wpp:web_server.py:779-789`

**移植时要修的**（见 §10）：`fps` 硬编码 30.0（`:674`、`:827`）而流实际帧率未知；`mp4v` 不是 H.264，`origin/wpp:VIDEO_RECORDING_COMPLETION.md:263` 写"MP4 (H.264)"是错的；写盘发生在 asyncio 回调里（`:680`），文档 `origin/wpp:VIDEO_RECORDING_COMPLETION.md:277` 声称"异步写入不阻塞视频流"与代码不符。

### S7. 动作/姿态目录 + 难度分级时长表

- 时长表（可直接用）：`origin/wpp:dual_dog_movement.py:144-155`
  ```python
  high_difficulty_actions = ["FrontFlip", "BackFlip", "Handstand"]
  medium_actions = ["Wallow", "Scrape", "FrontPounce"]
  if action in high_difficulty_actions: return 6.0  # 高难度动作时间更长
  elif action in medium_actions:      return 4.0  # 中等难度动作
  else:                               return 3.0  # 常规动作
  ```
- 舞蹈动作序列（12 个）及难度相关的休息间隔：`origin/wpp:dual_dog_movement.py:279-283`、`:298-302`
  ```python
  if current_dance in ["FrontFlip", "BackFlip", "Handstand"]:
      await asyncio.sleep(3)  # 高难度动作休息更久
  else:
      await asyncio.sleep(2)  # 常规动作休息时间
  ```
- Dance1/Dance2 时长区分：`origin/wpp:dual_dog_movement.py:167` `dance_duration = 5.0 if dance_type == "Dance1" else 8.0`
- 第三方文档里的 api_id 对照表（与库内 `SPORT_CMD` 一致，可交叉验证）：
  `origin/wpp:EXTENDED_FEATURES_README.md:79-89`
  ```
  "Stretch": 1017, "Wallow": 1021, "Scrape": 1029, "WiggleHips": 1033,
  "FingerHeart": 1036, "Handstand": 1301, "FrontFlip": 1030,
  "BackFlip": 1044, "FrontPounce": 1032
  ```
  库内权威定义：`origin/wpp:unitree_webrtc_connect/constants.py:110-160`
- 全部姿态的 UI 清单（4 + 4 + 4 布局）：`origin/wpp:templates/index.html:327-399`

### S8. 部署/运行时的硬约束（容易被忽略但会咬人）

- **`use_reloader=False` 是必须的**，否则 Flask 重载器会 fork 第二个进程 → 第二个 asyncio 线程 → 重复连狗：
  `origin/wpp:web_server.py:992-998`
  ```python
  use_reloader=False  # 避免与asyncio线程冲突
  ```
- 目标运行环境是 **WSL2 + Windows 浏览器**，不是裸 Linux：`origin/wpp:start_web_server.sh:26`
  > `echo "💡 可以在Windows浏览器中访问 http://WSL_IP:$PORT"`
  `origin/wpp:demo_web_interface.py:11-18`（`hostname -I` 取 WSL IP）、`origin/wpp:demo_web_interface.py:26-28`
  开发者工作目录：`origin/wpp:start_dual_dog.sh:13` `project_dir="/home/wppjkw/go2_webrtc_connect"`；`origin/wpp:README_DUAL_DOG.md:59` 同。
- 启动脚本会 `kill -9` 占用 5000 端口的进程再启（说明重启是常态）：
  `origin/wpp:start_web_server.sh:6-12`
- `git pull origin wpp` 是文档里的更新方式：`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:1298`
- 单狗场景被当作一等公民反复照顾（见 §10.4），说明**实际只有一条狗**。

### S9. 架构级戒律：CV/推理绝不能跑在 asyncio 主循环里

即使我们现在不做视觉，这条规则决定了以后加任何 CPU 密集功能时的形状。

- `origin/wpp:New_Feature.md:105-107`
  > **性能警告：** `to_ndarray()` 转换以及随后的所有 CV 处理（YOLO 推理、PID 计算）都是**计算密集型和阻塞 (Blocking) 操作**。如果这些操作在主 asyncio 事件循环中执行，它们将*冻结*整个应用程序，导致 WebRTC 连接超时、视频卡顿和命令丢失。
- `origin/wpp:New_Feature.md:229-237`
  > **致命缺陷：** 如果在 asyncio 主线程中（例如在 on_track 回调中）直接调用 `model.track()`，整个事件循环将被冻结。这将导致：* WebRTC 心跳包丢失，连接断开。* 视频流完全卡住。
- 生产者-消费者落地写法（`run_coroutine_threadsafe` 回主循环 + 有界队列）：
  `origin/wpp:New_Feature.md:239-249`、`:251-261`
- 代码里的落地：`origin/wpp:unitree_webrtc_connect/human_tracking/video_processor.py:34`（`queue.Queue(maxsize=10)`）、`:46-48`（daemon 工作线程）、`:57-72`（丢旧帧）

### S10. "必须先发生 X 才能 Y"的次序清单（逐条可验证）

| 次序约束 | 证据 |
|---|---|
| 必须先 `switchVideoChannel(True)`，再 `await track.recv()` 才有帧 | `origin/wpp:web_server.py:695` 与 `:648`；库侧 `origin/wpp:unitree_webrtc_connect/webrtc_datachannel.py:167-173` |
| 必须先切到 `normal` 运动模式，姿态/动作指令才有效 | `origin/wpp:dual_dog_controller.py:215-248`；`origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:22-46` |
| 切模式后必须等狗站起来（**等待时长三处不一致**：库示例 5s / dual_dog 2s / sportsmode 1s） | `origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:46` `await asyncio.sleep(5)  # Wait while it stands up` vs `origin/wpp:dual_dog_controller.py:242` `await asyncio.sleep(2)  # 等待模式切换完成` vs `origin/wpp:sportsmode.py:81` `await asyncio.sleep(1)` |
| 进程必须保活，否则连接断（库示例用 `sleep(3600)` 挂住） | `origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:128` `await asyncio.sleep(3600)` |
| `Move` 会持续运动，必须显式 `StopMove` 才停 | 单狗路径显式补停：`origin/wpp:dual_dog_movement.py:412-414`；库示例连续 Move 之间无 StopMove：`origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:59-79` |
| 录制必须先开摄像头 | `origin/wpp:web_server.py:811-813` `return jsonify({'success': False, 'error': '请先开启摄像头'})` |
| 退出/关闭时要先停运动再断连再停监控 | `origin/wpp:dual_dog_gui_enhanced.py:857-882`、`origin/wpp:dual_dog_gui.py:823-836` |
| 动作之间必须留间隔（三次扑跃中间 3 秒，最后一次不等） | `origin/wpp:web_server.py:953-968`；`origin/wpp:dual_dog_gui_enhanced.py:564-566` `if i < 2:  # 最后一次不需要等待` |
| JOIN/连接阶段并行建连，但**等待是按顺序 await**（不是 gather） | `origin/wpp:dual_dog_controller.py:168-188` vs 断连用 `asyncio.gather`：`:190-200` |

---

## 1. 清单（Inventory）

### 1.1 入口点

| 入口 | 是什么 | 状态 |
|---|---|---|
| `origin/wpp:web_server.py:1001-1014` | **主入口**：Flask + Socket.IO 双狗 Web 控制台，`--host/--port/--debug` | 结构完整，绑 `0.0.0.0:5000` |
| `origin/wpp:dual_dog_main.py:192-237` | tkinter GUI 主入口（带日志/快捷方式/清理） | **已坏**：`check_dependencies()` 导入不存在的 `go2_webrtc_driver`（`origin/wpp:dual_dog_main.py:166-170`），必然 `sys.exit(1)`（`:216-218`） |
| `origin/wpp:start_single_dog.py:295-304` | 单狗 tkinter GUI | 可运行（`start_dual_dog.sh` 不指向它） |
| `origin/wpp:dual_dog_gui_enhanced.py:889-892` | 增强版 GUI（网络扫描 + 键盘） | 有 `emergency_stop` AttributeError（§7） |
| `origin/wpp:dual_dog_gui.py:23` | 原版 GUI（`dual_dog_main.py:18` 实际导入的是这个） | 可运行 |
| `origin/wpp:sportsmode.py:127-158` | 单狗脚本的祖先（正方形走 + 按键触发跳舞） | **残留**：仍 import `go2_webrtc_driver`（`:7-8`、`:143-144`），且硬编码 IP `192.168.31.245`（`:51`、`:146`） |
| `origin/wpp:demo_web_interface.py:53-63` | 只打印访问地址并尝试开浏览器 | 装饰性 |

### 1.2 模块分层

```
入口        web_server.py | dual_dog_main.py | start_single_dog.py | dual_dog_gui*.py
控制核心    dual_dog_controller.py  (连接/状态/指令路由/看门狗)
运动逻辑    dual_dog_movement.py    (模式循环/动作表/单狗便捷方法)
辅助        network_scanner.py      (发现)   keyboard_controller.py (WASD)
前端        templates/index.html + static/js/app.js + static/css/style.css
可选 AI     unitree_webrtc_connect/human_tracking/*  (YOLO + PID, 独立)
文档/脚本   17 个 .md + 12 个 test_*.py + 3 个 .sh（其中 13 个在最后一次提交被删）
```

### 1.3 提交信息说明了什么需求

`git log caf1def..origin/wpp --format='%h %ad %s%n%b'`（11 条）：

- `e73d447` (09-22) `.gitignore` 忽略日志 → 开始长期运行
- `f87d263` (09-22) **选择性控制**："在 DualDogController 中添加控制模式属性和选中机器狗列表 / 实现按全部、单个和选中模式发送指令的方法"
- `95fc2fe` (09-22) **姿态扩展**："将触发跳舞按钮扩展为两排，新增触发伸展、打滚、空翻、扑跃、比心动作按钮"、"细化动作执行时长，区分高难度、中等及常规动作休息时间"
- `3813005` (09-22) **网络扫描 + 键盘 WASD**
- `e59bbc3` (10-15) **视频流 + Web 服务**："Implemented a video stream testing script ... Created a web server (web_server.py) for dual robot dog control, providing RESTful API endpoints for managing robot dogs, controlling movements, and handling video streams."
- `a3b5599` (10-16) **部署指南 + 录制测试脚本**
- `7b7188c` (11-11) 删除 13 个过期文档/脚本（**里面的调试知识被一起删了**，见 §9）
- `8122545` (2026-03-27) **人体追踪扩展**（YOLOv11 + PID）

需求脉络很清楚：**"两台狗 + 一个网页 + 能看画面 + 能一键做动作"**，最后追加"视频找人". `New_Feature.md` 是那份 AI 版需求蓝图（YOLO + FunASR + LLM + edge-tts + ControlArbitrator 状态机）。

### 1.4 分支实际能做什么（功能事实）

能做：多狗增删/连接/断开、LocalSTA/AP/Remote 三种方式、发现设备、按 all/single 模式下发运动指令、正方形走路、舞蹈派对、14 种姿态/特技动作、手动方向+旋转、键盘 WASD、Web 端实时状态与日志推送、每狗 MJPEG 视频、每狗 MP4 录制、紧急停止路由。

不能做：跟随领队（枚举有、实现无，`origin/wpp:dual_dog_movement.py:24` vs `:85-87`）、任何编队/时间同步、任何定位/地图/lidar、任何鉴权、任何持久化配置读取、任何真正的自动化测试。

---

## 2. 控制 / 运动

### 2.1 没有的东西（先声明，避免误判）

**全分支不存在**：
- 速度斜坡 / slew rate limiting（`Move` 的 `x/y/z` 都是阶跃直给，如 `origin/wpp:dual_dog_movement.py:185-192` 直接 `x: 1`）
- 摇杆/手柄支持（无 `pygame`、无 `evdev`、无 SDL 引用；`origin/wpp:web_requirements.txt:1-5` 只有 4 个包）
- 死区 / 响应曲线 / 速度档位（`SpeedLevel` 常量存在但从未被使用）
- 指令频率限制：前端**没有** debounce/throttle（`git show origin/wpp:static/js/app.js | grep -iE 'debounce|throttle'` 无结果）
- 运动看门狗：没有"多久没收到指令就停"的逻辑

### 2.2 有的东西

**(a) 手动控制队列 + 0.1 s 节拍**
`origin/wpp:dual_dog_movement.py:240-274`
```python
command_data = await asyncio.wait_for(self.manual_command_queue.get(), timeout=0.1)
command, parameters, duration = command_data
...
await self.controller.send_command_to_selected(command, parameters)
if duration > 0:
    await asyncio.sleep(duration)
```

**(b) 手动模式门禁**
`origin/wpp:dual_dog_movement.py:312-316`
```python
if self.current_pattern != MovementPattern.MANUAL_CONTROL:
    self.logger.warning("当前不在手动控制模式")
    return False
```
但**单狗路径绕过这道门**：`origin/wpp:dual_dog_movement.py:325-333` 直接 `send_command_to_dog`，无模式检查。→ 同一个 `/api/movement/manual`，带 `dog_name` 时永远生效，不带时依赖先进入 manual 模式。

**(c) 速度/时长默认值**：`0.5` / `1.0`（`origin/wpp:dual_dog_movement.py:336-358`），前端硬编码 `speed: 0.5, duration: 0.5`（`origin/wpp:static/js/app.js:584-585`）。配置里另有一套未接线的值：`origin/wpp:config.json:26-31`（`default_speed 0.5`、`default_turn_speed 1.0`、`square_walk_speed 1.0`）。

**(d) 单狗移动会自动补 `StopMove`，全狗移动不会** —— 这是最关键的一处不对称：
```python
# origin/wpp:dual_dog_movement.py:409-415
async def move_forward_single(self, dog_name, speed=0.5, duration=1.0):
    result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": speed, "y": 0, "z": 0})
    if result and duration > 0:
        await asyncio.sleep(duration)
        await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
    return result
```
```python
# origin/wpp:dual_dog_movement.py:336-338 — 无 StopMove
async def move_forward(self, speed=0.5, duration=1.0):
    return await self.send_manual_command("Move", {"x": speed, "y": 0, "z": 0}, duration)
```
配合"`Move` 持续生效直到被覆盖或 `StopMove`"的库语义（`origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:59-79`），全狗路径的停止完全依赖前端补发 `stop`（`origin/wpp:static/js/app.js:592-601`）。

**(e) 前端输入路径本身很脆**：
- 屏幕按钮：`mousedown` → 发一次，`mouseup`/`mouseleave` → 停（`origin/wpp:static/js/app.js:121-125`）。**没有按住重复**（全文件只有录制计时器一个 `setInterval`，`origin/wpp:static/js/app.js:1010`）。
- 键盘：靠**浏览器按键自动重复**驱动（`origin/wpp:static/js/app.js:156-195`），`keyup` 停。
- **没有 `blur`/`visibilitychange` 处理**：按住 W 时切走标签页 → 收不到 `keyup` → 最后一条 `Move` 无人撤销。

**(f) 键盘模块（tkinter 版）**：映射表 `origin/wpp:keyboard_controller.py:52-63`（W/S/A/D + Q/E + Space/X/Z/H）；0.1 s 动作间隔（`:49`）；20 Hz 循环（`:126` `await asyncio.sleep(0.05)  # 20Hz更新频率`）；移动优先于转向（`:137-153`）。
**同样的缺口**：`on_key_release` 只把键从集合里删掉，**不发停止指令**（`origin/wpp:keyboard_controller.py:105-113`）。

**(g) 状态机**：`MovementPattern`（`origin/wpp:dual_dog_movement.py:18-24`）只有 STOP / SQUARE_WALK / MANUAL_CONTROL / DANCE_PARTY / FOLLOW_LEADER；`start_pattern` 里 FOLLOW_LEADER 落到 `else` → 返回 False（`:85-87`）。`dance_triggered` 是实例属性，`__init__` 里置 False（`:49`），所以**只在进程内泄漏**（每次 `_square_walk_loop` 进入时重置 `square_walk_step`（`:181`）但不重置 `dance_triggered`）—— 若同一进程内 stop 后重启 square_walk，触发标志可能残留。

**(h) 狗的状态机是单向的（新发现）**：
`DogStatus.MOVING` / `DANCING` 只被赋值、**从未被清回 IDLE**：
- 赋值：`origin/wpp:dual_dog_controller.py:263-266`
- 唯一的 `IDLE` 赋值：`origin/wpp:dual_dog_controller.py:208-210`（且条件是 `status == CONNECTED`）
- `stop_movement`（`origin/wpp:dual_dog_movement.py:94-115`）只发 `StopMove`，不改状态
→ 一旦发过一次 `Move`，`get_all_status()['x']['status']` 就永久是 `"moving"`。前端用 `connected` 字段规避了显示问题（`origin/wpp:static/js/app.js:773-775` 把 moving/dancing 映射成 connected），但**状态语义已经失真**。

---

## 3. 多机器人协同

**核心事实：WPP 没有做真正的协同。**

- 指令下发是"同一事件循环 tick 内为每狗建 task，然后按顺序 await"：
  `origin/wpp:dual_dog_controller.py:308-325`
- 没有 barrier / 无 `asyncio.Barrier` / 无时间戳对齐 / 无 `await asyncio.gather` 于下发路径（gather 只用在 `disconnect_all`，`origin/wpp:dual_dog_controller.py:198`）
- 没有任何 leader/follower、编队、相对位姿概念。`FOLLOW_LEADER` 是死枚举（`origin/wpp:dual_dog_movement.py:24`）
- 库里有 `LeadFollow` api_id（`origin/wpp:unitree_webrtc_connect/constants.py:149` `"LeadFollow": 1045`），但分支从未使用；且该值与 `:157` `"FreeWalk": 1045` **重复**，值得在采用前核实
- "同步"靠的是**固定 sleep**：动作 3/4/6 s（`origin/wpp:dual_dog_movement.py:144-155`）、跳舞 5/8 s（`:167`）、三次扑跃中间 3 s（`origin/wpp:web_server.py:962-963`）、舞蹈派对休息 2/3 s（`:299-302`）
- 成功判定是 `sum(results.values()) > 0` —— 只要**任意一台**成功就算成功：
  `origin/wpp:dual_dog_movement.py:135-138`、`:170-173`
- 唯一的"协同"式防御：全狗全失败时暂停循环
  `origin/wpp:dual_dog_movement.py:218-222`
  ```python
  success_count = sum(results.values())
  if success_count == 0:
      self.logger.warning("所有机器狗都未能执行指令，暂停运动")
      await asyncio.sleep(1); continue
  ```

**对两狗控制台的含义**：真要"一起做动作"，WPP 的经验是"能同时发出去就算赢"，两条狗的动作**起止时间并不对齐**。要真正做到同步，得靠指令回执 + 各自状态反馈，WPP 没走到那一步。

`send_command_to_dog` 的指令构造（值得抄）：
```python
# origin/wpp:dual_dog_controller.py:271-279
cmd_data = {"api_id": SPORT_CMD[command]}
if parameters:
    cmd_data["parameter"] = parameters
await dog.connection.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], cmd_data)
```

---

## 4. Web UI

### 4.1 路由清单（23 条 HTTP + 2 个 Socket.IO 事件）

完整清单（`origin/wpp:web_server.py`）：`/`(`:204`)、`GET /api/dogs`(`:209`)、`POST /api/dogs`(`:221`)、`DELETE /api/dogs/<name>`(`:250`)、`POST /api/dogs/connect`(`:258`)、`POST /api/dogs/disconnect`(`:285`)、`POST /api/control/mode`(`:300`)、`POST /api/movement/start`(`:317`)、`POST /api/movement/stop`(`:349`)、`POST /api/movement/emergency_stop`(`:364`)、`POST /api/movement/action`(`:379`)、`POST /api/movement/pounce`(`:416`)、`POST /api/movement/manual`(`:446`)、`GET /api/scan/devices`(`:504`)、`GET /api/logs`(`:543`)、`POST /api/logs/clear`(`:564`)、`GET /api/video/stream/<dog_name>`(`:574`)、`GET /api/status`(`:602`)、`POST /api/video/start`(`:613`)、`POST /api/video/stop`(`:739`)、`POST /api/video/record/start`(`:801`)、`POST /api/video/record/stop`(`:860`)、`GET /api/video/record/status`(`:890`)，加 socketio `connect`(`:973`) / `disconnect`(`:984`)。

### 4.2 界面长什么样

单页 Bootstrap 5 卡片式布局（`origin/wpp:templates/index.html`）：
- 左栏（`col-md-4`）：网络设备扫描（`:37-61`）、机器狗连接状态（`:64-83`）、机器狗管理表单（`:86-110`）、控制模式单选（`:113-136`）
- 右栏（`col-md-8`）：运动状态 alert（`:142-145`）、**实时摄像头**（`:148-191`，含录制按钮与红色闪烁指示器 `:176-182`）、**系统日志**（`:194-218`，等级筛选 + 清空/刷新）、自动运动模式（`:221-264`，正方形走路/舞蹈派对/停止/紧急停止 + 扑跃一次/三次）、手动遥控（`:267-325`，方向十字 + 旋转）、姿态控制（`:328-399`，三组共 12 个按钮）
- 依赖全部 CDN：Bootstrap 5.3.0 / Font Awesome 6.0.0 / Socket.IO 4.0.0（`origin/wpp:templates/index.html:9-11`、`:423-425`）

### 4.3 我们能借鉴的 UI 特性

| 特性 | 证据 |
|---|---|
| **实时日志终端**（分级着色 + 按级别筛选 + 来源标签 + 自动滚底 + 上限 500 条） | `origin/wpp:templates/index.html:194-218`；`origin/wpp:static/js/app.js:332-378`；后端环形缓冲 `origin/wpp:web_server.py:57-58`、`:141-146` |
| 日志同时进 Python logging 与 WebSocket | `origin/wpp:web_server.py:129-150` |
| **紧急停止按钮 + ESC 快捷键 + 红色脉冲动画** | `origin/wpp:templates/index.html:242-244`；`origin/wpp:static/js/app.js:185-187`；`origin/wpp:static/css/style.css:280-298` |
| 按钮 loading 态（旋转 spinner，并发保护） | `origin/wpp:static/css/style.css:143-161`；用法 `origin/wpp:static/js/app.js:547-561` |
| 状态色分（connected/disconnected/connecting/error） | `origin/wpp:static/css/style.css:59-78` |
| 摄像头下拉框按连接状态禁用未连接的狗 | `origin/wpp:static/js/app.js:1087-1106` |
| 录制指示器 + 分秒计时 | `origin/wpp:templates/index.html:177-182`；`origin/wpp:static/js/app.js:1007-1029` |
| 消息统一走 modal，成功/信息 2 秒自动关 | `origin/wpp:static/js/app.js:1122-1147` |

### 4.4 可视化：**没有**

无 lidar、无地图、无曲线、无仪表盘、无 canvas/three.js。`static/css/style.css:266` 定义了 `.keyboard-hint` 但 `templates/index.html` 从未使用（dead CSS）。唯一"图形"是 CSS 动画和 Font Awesome 图标。

**前端 JS 里的一个真实 bug**（移植时会一起带过去）：
`origin/wpp:static/js/app.js:604-605`
```javascript
async executeAction(action) {
    const btn = event.target.closest('.action-btn');   // 依赖全局 window.event（非标准，Firefox 下为 undefined）
```
`origin/wpp:static/js/app.js:747-750`：`updateControlMode()` 会反过来调用 `setControlMode()`，造成一次多余 POST。

---

## 5. Lidar / 感知 / 位姿 —— **明确：没有解决"狗在哪"**

我做了全分支逐文件 grep（`grid_map|robot_pose|utlidar|slam|localization|VoxelMap|LIDAR|point.?cloud|pose`，覆盖所有 `.py/.js/.html/.css/.md`）。在 WPP **新增的 54 个文件里，命中为零**（唯二命中是 `keyboard_controller.py:53` 的 `KeyMapping` 和 `app.js:766` 的 `statusMap`，均为字符串误匹配）。

具体结论：
- WPP 新增代码**从不订阅** `rt/utlidar/voxel_map_compressed`、`rt/utlidar/robot_pose`、`rt/mapping/grid_map`，**从不**绘制任何空间内容
- 这些 topic 名只存在于继承来的库常量里：`origin/wpp:unitree_webrtc_connect/constants.py:66-70`（`ULIDAR_SWITCH/ULIDAR/ULIDAR_ARRAY/ULIDAR_STATE/ROBOTODOM="rt/utlidar/robot_pose"`）、`:80`（`"GRID_MAP": "rt/mapping/grid_map"`）、`:90-104`（`SLAM_ODOMETRY`、`LIDAR_MAPPING_*`、`LIDAR_LOCALIZATION_*`、`LIDAR_NAVIGATION_GLOBAL_PATH`）
- 点云解码器存在（继承自基线）：`origin/wpp:unitree_webrtc_connect/lidar/lidar_decoder_unified.py:1-8`、`origin/wpp:unitree_webrtc_connect/webrtc_datachannel.py:117-146`（二进制帧分流：header 2/0 → lidar 路径，`deal_array_buffer_for_lidar`），`origin/wpp:unitree_webrtc_connect/webrtc_datachannel.py:149` 注释 `#Should turn it on when subscribed to ulidar topic`（对应 `disableTrafficSaving`，`:150-163`）
- 现成示例在基线里：`origin/wpp:examples/go2/data_channel/lidar/lidar_stream.py:28`（`publish_without_callback("rt/utlidar/switch", "on")`）、`:36`（订阅 `rt/utlidar/voxel_map_compressed`）
- 唯一"空间"可视化是 three.js 的示例脚本，**且不在 WPP 新增文件里**：`origin/wpp:examples/go2/data_channel/lidar/plot_lidar_stream.py:4`、`:40`（README 指向 `http://127.0.0.1:8080/`），配图 `origin/wpp:images/plot_lidar_1.png`、`origin/wpp:images/plot_lidar_2.png`

**"狗在哪"这个问题，WPP 完全没有碰。** 它的空间认知上限是"这条 `Move` 发出去成功了没有"。
如果两狗控制台需要位姿，起点应该是基线库的 `ROBOTODOM`/`SLAM_ODOMETRY` 订阅 + `LIDAR_MAPPING_*` 系列，**而不是 WPP**。

---

## 6. 持久化 / 配置 / 数据

### 6.1 落盘的东西

| 数据 | 位置 | 证据 |
|---|---|---|
| 录制视频（MP4/mp4v） | `recordings/<dog>_<YYYYmmdd_HHMMSS>.mp4` | `origin/wpp:web_server.py:66`、`:70-71`、`:821-823` |
| 日志（3 路：main/errors/movement） | `logs/dual_dog_{main,errors,movement}_YYYYMMDD.log` | `origin/wpp:dual_dog_main.py:35-37` |
| 日志清理（>7 天删除） | `logs/*.log` | `origin/wpp:dual_dog_main.py:92-102`（`cleanup_old_logs(days_to_keep=7)`） |
| `.gitignore` 忽略日志与 Claude 配置 | — | `origin/wpp:.gitignore:31-39` |

### 6.2 配置文件存在但**从未被读取**（重要的负面发现）

`config.json`（`origin/wpp:config.json`，40 行）和 `single_dog_config.json`（`origin/wpp:single_dog_config.json`，33 行）都在仓库里，文档也反复让用户去改（`origin/wpp:README_DUAL_DOG.md:64-80`、`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:465`）。

但全分支 grep `config.json|single_dog_config|load_config|json.load` 的结果是：**没有任何代码读取它们**（只有文档引用、`dual_dog_controller.py:229` 解析运动模式响应、以及无关的音频/示例脚本）。
唯一读 `config.json` 的地方是启动脚本里的一句内联 python 打印 —— `origin/wpp:start_dual_dog.sh:88-100`，纯展示。

**因此**：
- `"safety_stop_enabled": true`（`origin/wpp:config.json:32`）**没有任何作用**
- `"heartbeat_timeout": 30`（`:7`）与硬编码的 30 一致纯属巧合（代码在 `origin/wpp:dual_dog_controller.py:452`）
- 而且文档里的 schema 与真实文件**不一致**：`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:467-497` 用的是 `{"server": {...}, "dogs": [ 数组 ], "video": {...}}`，而真实 `config.json:1-40` 用的是 `{"system", "dogs": {dog1,dog2}, "movement", "gui"}`。两套 schema 都没被实现。

### 6.3 无恢复逻辑

- 狗列表、控制模式、选中列表、录制状态、日志缓冲全在内存（`origin/wpp:web_server.py:54-66`）
- **进程重启即全丢**，没有任何持久化/重连/恢复代码
- **录制中崩溃 = 文件损坏**：`cv2.VideoWriter.release()` 只在 `_stop_recording_internal`（`origin/wpp:web_server.py:931`）里调用，没有 `atexit`/`try-finally`/信号处理
- `.gitignore` 里逐条列出 `logs/dual_dog_*_2025092{1,2}.log`（`origin/wpp:.gitignore:32-37`）说明日志曾被误提交过 —— 这是"先踩坑后补规则"的痕迹

---

## 7. 安全

已知的（不复述细节）：无鉴权、`0.0.0.0`、CORS 全开。精确定位供修复参考：

- `origin/wpp:web_server.py:31` `def __init__(self, host='0.0.0.0', port=5000)`
- `origin/wpp:web_server.py:1006` `parser.add_argument('--host', default='0.0.0.0', ...)`
- `origin/wpp:web_server.py:40` `CORS(self.app)`
- `origin/wpp:web_server.py:43` `SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')`
- `origin/wpp:web_server.py:37` `self.app.config['SECRET_KEY'] = 'dual_dog_secret_key'`（硬编码弱密钥）
- 没有任何 `before_request` / token / session 检查；`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:561-576` 只给了一段**示例** `require_auth`，从未落地

### 7.1 除此之外还有什么在"保护"狗（答案：很少，且有一处是坏的）

**存在的**：
- 前端 ESC → 紧急停止（`origin/wpp:static/js/app.js:185-187`）；HTML 上独立的红色紧急停止按钮（`origin/wpp:templates/index.html:242-244`）
- 后端 `/api/movement/emergency_stop` 独立路由，5 秒超时（`origin/wpp:web_server.py:364-377`）
- 关闭 GUI 时先停运动（`origin/wpp:dual_dog_gui_enhanced.py:859-866`、`origin/wpp:dual_dog_gui.py:823-826`）
- 高风险动作有"休息更久"的经验值（`origin/wpp:dual_dog_movement.py:299-302`）
- 三轮文档里的口头警告："高难度动作 (倒立、空翻等) 建议在平整、安全的地面执行"（`origin/wpp:EXTENDED_FEATURES_README.md:106`）、"建议在安全的开阔空间执行扑跃动作"（`origin/wpp:POUNCE_IMPROVEMENT_README.md:145`）

**坏掉的（新发现）**：
GUI 的紧急停止按钮会抛 `AttributeError` —— `DualDogMovement` 没有 `emergency_stop` 方法（只有 `stop_move`，`origin/wpp:dual_dog_movement.py:509`）：
```python
# origin/wpp:dual_dog_gui_enhanced.py:519-523
def _on_emergency_stop(self):
    if self.movement:
        self._run_async(self.movement.emergency_stop())   # AttributeError
```
同一 bug 复制到了测试脚本：`origin/wpp:test_enhanced_gui_recovery.py:175-180`。
Web 路径是对的（`stop_move()`，`origin/wpp:web_server.py:369`）；tkinter 路径（含 `dual_dog_gui.py` 压根没有紧急停止按钮）不设防。

**缺失的**：
- 无速率限制 / 无操作配额（`/api/movement/*` 可被无限调用，前端也无限发）
- 无二次确认对话框（`messagebox.askokcancel` 只用于"退出程序"，`origin/wpp:dual_dog_gui_enhanced.py:859`）
- 无危险动作（空翻/倒立/扑跃）白名单或互锁
- 无"失去浏览器连接 → 停狗"的逻辑：`handle_disconnect` 只打日志（`origin/wpp:web_server.py:984-987`）
- 无 `Move` 超时自动停
- `/api/movement/action` 用未白名单的 `getattr` 反射调用任意同名方法（`origin/wpp:web_server.py:394-409`）—— 对比 `/api/movement/manual` 用了显式 `direction_methods` 白名单（`:461-469`），标准不一致
- 无 HTTPS（只有文档示例，`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:544-559`）
- 无审计日志（谁在什么时候让机器人做了什么）

**其他安全/卫生问题**：
- 仓库里有一个误提交的第三方安装脚本，路径就是原文件名：`origin/wpp:1753683727739-0b3a4f6e84284f1b9afa951ab7873c29.sh?ufileattname=claude_code_prod.sh`（204 行），它会把 `~/.claude/settings.json` 的 `ANTHROPIC_AUTH_TOKEN` 指向智谱（`origin/wpp:<该文件>:15-17`、`:144-172`）。**没有硬编码密钥**（`:145` 是 `read -s -p` 交互输入），但会 `curl | bash` 装 nvm（`:58`）。属于应删除的内容。
- `libs/aioice` 是 gitlink 子模块（`git ls-tree origin/wpp libs/` → `160000 commit ff5755a1...`），克隆后未初始化 → 可复现性风险。

---

## 8. 测试

**结论：分支没有任何自动化测试。它把 master 的测试套件整包删了。**

- `git ls-tree -r --name-only origin/wpp | grep -c '^tests/'` → `0`（master 有 14 个文件：`tests/test_auth.py`、`tests/test_motion.py`、`tests/test_vision.py`、`tests/test_guards.py`、`tests/test_installer.py`、`tests/test_session.py`、`tests/test_mock_e2e.py`、`tests/conftest.py` 等）
- 根目录 12 个 `test_*.py`：**全部 `assert` 计数为 0**（逐文件 `grep -c assert` 均为 0）。它们是硬件在环的手动演示脚本：连真狗、`asyncio.sleep`、`print`。
- 证据：
  - `origin/wpp:test_extended_features.py:24-28` 硬编码 `192.168.31.245`；`:117-118` 只有 `asyncio.run(...)`；无断言
  - `origin/wpp:test_pounce_improvement.py:28-32` 同样硬编码 IP
  - `origin/wpp:test_status_sync.py` 是**唯一部分可脱离硬件**的（`:36-38` 手动触发状态变化、`:63-64` 用字符串冒充 connection 对象），但它靠 `print` + 自己数 `tests_passed`（`:73-90`），从 `main()` 手工汇总（`:218-243`），pytest 收集不到有意义的用例
  - `origin/wpp:test_video_stream.py` 是唯一纯本地的（只测 OpenCV/NumPy/JPEG 编码/队列性能，`grep 192.168` = 0），但仍无断言
  - `origin/wpp:test_enhanced_gui_recovery.py` 是个 tkinter 窗口（`:210-220` `self.root.mainloop()`），点按钮手动看日志
- `origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:1193-1210` 里写了一个 `unittest` 的 `test_web_server.py` **示例**，但该文件在分支里不存在（`git ls-tree` 无此路径）
- `origin/wpp:COMPLETE_FIX_SUMMARY.md:214-223` 声称"自动化测试 ✅"，实际指的是上面这些 print 脚本

**对两狗控制台的含义**：可以放心地说"WPP 的功能没有回归保护"。我们移植任何逻辑，都必须**重新**补测试 —— 尤其 `dual_dog_controller.py` 的扇出与 `_get_target_dogs_by_mode` 这种纯逻辑，是完全可单测的（注入假 connection 即可，`test_status_sync.py:63-64` 已经示范了这个注入技巧）。

---

## 9. 硬功夫（Hard-won details）

这一节是本报告的重点。**注意：最后一次提交 `7b7188c` 删掉了 13 个文件（含 `EVENTLOOP_FIX.md`、`EVENTLOOP_FINAL_FIX.md`、`VIDEO_STREAM_FIX.md`、`POUNCE_IMPROVEMENT_README.md`、`FRONTEND_STATUS_FIX_SUMMARY.md`、`NEW_FEATURES_README.md`、`FEATURE_COMPARISON.md`、`EXTENDED_FEATURES_README.md` 和 3 个 `.sh`），而最值钱的调试记录恰恰在这些被删文件里。** 它们仍可从 `git show e59bbc3:<path>` / `git show 7b7188c^:<path>` 取回。

### 9.1 事件循环：两次失败才找到的写法（最有价值的一条）

`origin/wpp:EVENTLOOP_FINAL_FIX.md:13-36`
> 使用 **`asyncio.run_coroutine_threadsafe()`** 而不是 `call_soon_threadsafe()`
> 1. **初次尝试**：直接在Flask线程调用 `switchVideoChannel()` / ❌ 失败：没有事件循环
> 2. **第二次尝试**：使用 `call_soon_threadsafe()` / ❌ 仍然失败：虽然在正确的线程执行，但同步函数内部访问事件循环时仍会失败 / `call_soon_threadsafe()` 只是调度普通函数，不创建协程上下文
> 3. **最终方案**：使用 `run_coroutine_threadsafe()` / ✅ 成功：创建完整的协程上下文

对照表（`:33-36`）：
> | `call_soon_threadsafe()` | 调度普通函数 | ❌ | ❌ |
> | `run_coroutine_threadsafe()` | 调度协程 | ✅ | ✅ (Future) |

原始报错与修复后的日志（`:107-117`）：
> `❌ 启动摄像头失败: 开启视频通道失败: There is no current event loop in thread 'Thread-34'`
> `✅ [视频管理] 已开启 1212 的视频通道`
> `✅ [视频管理] 开始接收 1212 的视频帧`

（注意日志里的狗名 `1212` —— 与 `WEB_IMPROVEMENTS_README.md` 里用户给的访问地址同源，是真实机器。）

### 9.2 `switchVideoChannel` 是**同步**方法，但仍必须回到事件循环线程执行

```python
# origin/wpp:unitree_webrtc_connect/webrtc_video.py:13-14
def switchVideoChannel(self, switch: bool):
    self.datachannel.switchVideoChannel(switch)
```
它有 `await`（async）版本，但 `publish_without_callback` 内部要访问事件循环。
`origin/wpp:EVENTLOOP_FIX.md:13-19`
> **根本原因**：在Flask的请求处理线程中直接调用WebRTC相关的同步方法时，这些方法内部可能需要访问asyncio事件循环，但Flask的请求线程并没有事件循环。
> 3. `dog_info.connection.video.switchVideoChannel()` 方法虽然是同步的，但内部可能需要访问事件循环

**这是最反直觉、最容易踩的一条**——"同步方法为什么需要事件循环"。

### 9.3 MJPEG 方案的取舍（含带宽/延迟的实测口径）

`origin/wpp:VIDEO_STREAM_FIX.md:203-215`
> | **MJPEG** | ✅ 浏览器原生支持 ✅ 实现简单 ✅ 兼容性好 | ⚠️ 带宽占用较高 ⚠️ 压缩效率低 |
> | **HLS** | ... ❌ 延迟高(5-30秒) ❌ 需要分段处理 |
> | **WebRTC直连** | ✅ 超低延迟 ✅ P2P传输 | ❌ 实现复杂 ❌ 需要信令服务器 |
> | **RTSP/RTMP** | ... ❌ 浏览器不支持 ❌ 需要额外插件 |

调参口径（`:182-197`）：
> `cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])  # 降低质量以提高性能`
> `await asyncio.sleep(0.033)  # 限制为约30fps`
> `self.video_frame_queues[dog_name] = Queue(maxsize=5)  # 减小队列大小降低延迟`

队列丢帧策略（代码里真正生效的是"丢最旧"）：
```python
# origin/wpp:web_server.py:652-657
# 将帧放入队列（如果队列满了，丢弃旧帧）
if self.video_frame_queues[dog_name].full():
    try: self.video_frame_queues[dog_name].get_nowait()
    except: pass
```

### 9.4 首帧才知道真实分辨率 → 先建后毁重建 writer

`origin/wpp:web_server.py:828` 注释：`frame_size = (640, 480)  # 默认分辨率，会在第一帧时更新`
`origin/wpp:web_server.py:666` 注释：`# 如果是第一帧，重新创建writer以匹配实际分辨率`
（细节见 S6）

### 9.5 三条"用户报障 → 根因 → 修复"的完整复盘

**(a) "连上了但前端显示未连接"** —— `origin/wpp:STATUS_SYNC_COMPLETE_FIX.md:10-14`
> 现在最大的问题在于 理论上机器狗已经连接成功了 但是前端依旧显示未连接 也就无法调用摄像头
根因 `:20-28`：
> 1. **WebSocket状态推送不完整** — 缺少关键的`connected`字段（布尔值）
> 2. **前端状态更新函数缺失** — `updateSingleDogStatus`函数几乎为空，只打印日志

**(b) "idle/connected 都显示 disconnected"** —— `origin/wpp:FRONTEND_STATUS_FIX_SUMMARY.md:9-25`
> ### 1. 状态获取逻辑问题 前端的 `getStatusClass()` 方法只接受字符串状态参数，但实际从后端获取的是包含多个字段的对象
> ### 2. 摄像头选择器逻辑错误 `updateCameraSelector()` 方法使用了复杂的状态判断逻辑，而不是直接使用后端已经计算好的 `connected` 字段
> ### 3. 状态显示更新问题 `updateDogStatus()` 方法在调用 `getStatusClass()` 时只传递了 `info.status` 字符串
那里甚至贴了真实的后端响应样本（`:11-19`），含真实 IP `192.168.10.157`：
```json
{"status": "moving", "connected": true, "ip": "192.168.10.157",
 "last_heartbeat": 1758606451.4526227, "error_message": ""}
```

**(c) "单独控制模式下选不到没连上的狗"** —— `origin/wpp:BUG_FIXES_AND_CAMERA_FEATURE.md:5-11`
> **问题描述**: 在单独控制模式下，机器狗选择下拉框只显示已连接的机器狗，导致未连接的机器狗无法被选择进行控制。
> **修复方案**: 修改了`updateDogStatus()`方法，现在无论机器狗是否连接都会在下拉框中显示 / 未连接的机器狗会标注为"(未连接)"并使用灰色字体提示

### 9.6 朴素但真实的性能/资源口径（**未经独立验证，属文档声称**）

`origin/wpp:COMPLETE_FIX_SUMMARY.md:200-210`
> **延迟**: 300-500ms / **帧率**: 20-30 fps / **CPU占用**: 10-15% (单流) / **内存占用**: 50-100MB (单流) / **带宽**: 5-8 Mbps (质量85)
> （状态同步）**延迟**: <100ms / **可靠性**: 100%

`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:1266`
> **A**: 理论上没有限制，但实际受网络带宽和服务器性能影响。建议同时控制2-4个机器狗。

`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:1272`
> **A**: 约50-100 MB/分钟，取决于视频分辨率和画面复杂度。

**没有任何基准测试脚本产出这些数字**（`test_video_stream.py` 只测 JPEG 编码压缩率）。当作量级参考，不要当承诺。

### 9.7 常见错误对照表（运维口述史）

`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:723-729`
> | `Connection refused` | 机器狗未开启或IP错误 | 检查机器狗状态和IP |
> | `No event loop` | 线程同步问题 | **已在v2.2.3修复** |
> | `VideoWriter failed` | OpenCV问题 | 重新安装opencv-python |
> | `Queue is full` | 视频帧积压 | 降低帧率或增加队列大小 |

### 9.8 动作时长的经验值，以及一处需要复核的隐患

时长表与休息间隔见 S7。**下面这条是"看起来对但可能错"的**：

库自带示例给 `BackFlip` 传了参数：
```python
# origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:119-125
# await conn.datachannel.pub_sub.publish_request_new(
#     RTC_TOPIC["SPORT_MOD"],
#     { "api_id": SPORT_CMD["BackFlip"], "parameter": {"data": True} }
# )
```
`StandOut` 同样需要 `{"data": True}`（`:99-100`、`:111-112`）。

而 WPP 的动作下发**不带任何 parameter**：
```python
# origin/wpp:dual_dog_controller.py:271-273
cmd_data = {"api_id": SPORT_CMD[command]}
if parameters:
    cmd_data["parameter"] = parameters   # parameters 在 send_manual_command("BackFlip") 时为 None
```
`origin/wpp:dual_dog_movement.py:396-406`（`front_flip` / `back_flip` / `front_pounce` 都不传参）。

**我无法从代码判定这是否导致空翻无效**（无人能证实它在真机上跑过空翻）。但这是一处与非同源示例的明确不一致，**移植前必须在真机上验证 `BackFlip`/`FrontFlip` 是否需要 `{"data": True}`**。

### 9.9 `Dance1` 约 5 秒 —— 带上注释的经验值

`origin/wpp:sportsmode.py:41`
```python
await asyncio.sleep(5)  # Dance1大约需要5秒
```
这就是 `dual_dog_movement.py:167` 那个 `5.0 if Dance1 else 8.0` 的来源。

### 9.10 三处"切模式后等多久"的不一致

`origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:46`：`await asyncio.sleep(5)  # Wait while it stands up`
`origin/wpp:dual_dog_controller.py:242`：`await asyncio.sleep(2)  # 等待模式切换完成`
`origin/wpp:sportsmode.py:81`：`await asyncio.sleep(1)  # Wait while it stands up`

**三个数字互相矛盾，至少两个是错的。** 库自己的示例用 5 s，两狗控制器只等了 2 s —— 如果 2 s 不够，连上后立刻发动作就会丢。这属于"移植时必须重新标定"的项。

### 9.11 WebSocket/Socket.IO 的工程细节

- `async_mode='threading'`（不是 eventlet）—— 与文档相反：`origin/wpp:web_server.py:43` vs `origin/wpp:WEB_INTERFACE_README.md:48`（"**eventlet** - 异步事件处理"）；`web_requirements.txt` 里也没有 eventlet（`origin/wpp:web_requirements.txt:1-5`）
- `use_reloader=False`（见 S8）
- 日志进 WebSocket 时同时回灌 Python logging，形成统一出口：`origin/wpp:web_server.py:145-150`

### 9.12 其他"踩过才知道"的小痕迹

- 播放器/录制相关：`origin/wpp:VIDEO_RECORDING_COMPLETION.md:263-266` 把 `mp4v` 说成 "MP4 (H.264)"（**错的**，`mp4v` 是 MPEG-4 Part 2；代码见 `origin/wpp:web_server.py:673`、`:826`）。移植时若需要 H.264 兼容性，必须换 `avc1`/ffmpeg。
- `origin/wpp:video_processor.py` 的 TODO 是作者自己承认没做完的目标选择：`origin/wpp:unitree_webrtc_connect/human_tracking/video_processor.py:99`
  > `# TODO: 实现更智能的目标选择策略（如最近、最大、锁定ID等）`
- 人体追踪的"命令回主循环"写法**恰好复现了 `EVENTLOOP_FINAL_FIX.md` 警告过的错误**（在工作线程里 `asyncio.get_event_loop()`）：
  ```python
  # origin/wpp:unitree_webrtc_connect/human_tracking/video_processor.py:116-120
  try:
      asyncio.run_coroutine_threadsafe(self.command_queue.put(command), asyncio.get_event_loop())
  except RuntimeError:
      self.logger.error("无法获取异步事件循环")
  ```
  `video_processor.py:74-84` 的 `_worker_loop` 跑在 `threading.Thread`（`:46-47`）里，该线程没有事件循环 → `asyncio.get_event_loop()` 在 Python 3.10+ 直接 `RuntimeError`（正是 `:119-120` 那条日志），在 3.8/3.9 会拿到错误的循环。**正确写法应是把 loop 引用在创建时注入**（对比 `origin/wpp:web_server.py:122-127` 的 `_run_async` 就是保存 `self.asyncio_loop`）。
- 人体追踪的 PID 参数就是设计文档里的默认值，Ki/Kd 全 0（`origin/wpp:human_tracking/pid_controller.py:12-17`、`origin/wpp:tracking_manager.py:19-25`），**从未实地整定**——而文档明确说"这些参数必须在连接到真实机器狗后进行大量实地调优"（`origin/wpp:New_Feature.md:141`）。量纲上也值得怀疑：角速度误差是像素级（最多 ±640），`Kp=0.005` → 输出 ±3.2 rad/s，被 `output_limits=(-0.8, 0.8)` 直接削平（`origin/wpp:human_tracking/pid_controller.py:29-34`），除非常接近画面中心，否则等价于 bang-bang 控制。
- 模型文件名 `yolov11n.pt`（`origin/wpp:human_tracking/yolo11_tracker.py:13`、`origin/wpp:tracking_manager.py:17`、`origin/wpp:example_human_tracking.py:37`）不是 ultralytics 的官方权重名（官方是 `yolo11n.pt`）。我**无法验证**作者本地是否有改名后的文件；按字面量使用会加载失败。

---

## 10. 不值得移植的部分（逐条给理由）

### 10.1 `tests/` 的删除 —— 必须反向操作
WPP 把 master 的 14 个测试文件删光了（`git ls-tree -r --name-only origin/wpp | grep -c '^tests/'` → 0），换来的 12 个 `test_*.py` 断言数为 0、需要真狗。**净损失。** 我们应保留/扩充 master 的 `tests/`，而不是跟随 WPP。

### 10.2 三个 GUI（`dual_dog_gui.py` 851 行 / `dual_dog_gui_enhanced.py` 895 行 / `start_single_dog.py` 304 行）
理由：
1. 入口已坏 —— `dual_dog_main.py:166-170` 导入不存在的 `go2_webrtc_driver`，`main()` 必然 `sys.exit(1)`（`:216-218`）
2. 紧急停止按钮坏了（`dual_dog_gui_enhanced.py:519-523`）
3. 两个 GUI 之间是 800+ 行近乎逐行重复的复制粘贴（`_on_*` 方法一一对应，见 `FEATURE_COMPARISON.md` 的对照表）；`_on_square_walk` / `_on_start_square_walk`、`_on_dance_party` / `_on_start_dance_party` 同时存在且功能重复（`origin/wpp:dual_dog_gui_enhanced.py:491-511`）
4. 我们的目标本来就是 Web 控制台 —— tkinter 是负资产
但**可以取走**：`_on_closing` 的关停顺序（`origin/wpp:dual_dog_gui_enhanced.py:857-882`）、`DualDogLogger` 的三路日志分流 + 7 天清理（`origin/wpp:dual_dog_main.py:21-102`）。

### 10.3 `web_server.py` 的 `getattr` 动态派发
`origin/wpp:web_server.py:394-409`（action）、`:471-497`（direction）用字符串拼方法名。direction 那侧有白名单（`:461-469`），action 那侧没有。移植时改成显式 command→coroutine 映射表；否则任何拼错的动作名都会静默落到 `{'success': False, 'error': '动作不存在...'}`，且给了反射调用任意无参方法的口子。

### 10.4 整个 `dual_dog_*` 家族的"两狗"叙事 —— 因为实际只有一条狗
这是一处**很重要但很容易漏掉**的发现。证据链：
- 配置里第二台默认关闭：`origin/wpp:config.json:22` `"enabled": false`
- 测试里第二台被注释掉：`origin/wpp:test_selective_control.py:38`
  ```python
  # {"name": "Dog2", "ip": "192.168.31.246"},  # 如果有第二台机器狗，取消注释
  ```
- 快速测试的注释直接说"只添加存在的那一台"：`origin/wpp:quick_test_selective.py:31`
  ```python
  # 添加机器狗（只添加存在的那一台）
  ```
- GUI 用"IP 是否还是默认值"当哨兵来决定加不加第二台：`origin/wpp:dual_dog_gui.py:480`、`origin/wpp:dual_dog_gui_enhanced.py:459`
  ```python
  if dog2_name and dog2_ip and dog2_ip != "192.168.31.246":
  ```
- 所有硬件在环脚本只 add 一台：`origin/wpp:test_extended_features.py:24-28`、`origin/wpp:test_pounce_improvement.py:28-32`、`origin/wpp:quick_test_selective.py:32`
- 专门维护了一份单狗配置和单狗启动器：`origin/wpp:single_dog_config.json`、`origin/wpp:start_single_dog.py`
- FAQ 里"建议同时控制 2-4 个机器狗"是**推测**，不是实测（`origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:1266`）

**含义**：WPP 的双狗并发下发逻辑**很可能从未在两条真狗上同时验证过**。我们要接手"两狗"这个需求时，`send_command_to_targets` 的并发行为在第二台真实设备上属于**未验证代码**，必须自己压测（尤其是两个 `UnitreeWebRTCConnection` 在同一进程、同一事件循环、同一网卡上的相互影响，以及同频指令下的带宽/信令竞争）。

### 10.5 文档体系（17 个 .md）—— 参考，不要当真
具体不准之处：
- `origin/wpp:CAMERA_FIX_SUMMARY.md:160` 说"无法在Web界面直接显示视频画面"（已被 MJPEG 推翻）
- `origin/wpp:CAMERA_FIX_SUMMARY.md:49-66` 贴的 HTML 是 `<video id="videoElement" autoplay muted>`，实际是 `<img>`（`origin/wpp:templates/index.html:175`）
- `origin/wpp:VIDEO_RECORDING_COMPLETION.md:263` "MP4 (H.264)" ≠ `mp4v`
- `origin/wpp:VIDEO_RECORDING_COMPLETION.md:277` "异步写入不阻塞视频流" ≠ 代码在 asyncio 回调里同步 `writer.write`（`origin/wpp:web_server.py:680`）
- `origin/wpp:VIDEO_STREAM_FIX.md:97-100` 说 opencv/numpy "已在web_requirements.txt中"，实际 `web_requirements.txt` 只有 4 行 flask 相关（`origin/wpp:web_requirements.txt:1-5`）
- `origin/wpp:WEB_INTERFACE_README.md:48` 说用 eventlet，实际 `async_mode='threading'`
- `origin/wpp:WEB_CONTROL_SYSTEM_GUIDE.md:465-497` 的 config schema ≠ 真实 `config.json`
- `origin/wpp:COMPLETE_FIX_SUMMARY.md:214-223` 的"自动化测试 ✅"是 print 脚本
- 大量"✅ 通过 / 100% 成功率 / Chrome 90+ Firefox 88+ Safari 14+ 完美支持"（如 `origin/wpp:CAMERA_FIX_SUMMARY.md:141-156`、`origin/wpp:VIDEO_STREAM_FIX.md:247-264`）**没有任何测试产物支撑**，且与"只有一条狗、只有 Chrome"的实际情况不符
- 文档由 AI 撰写：`origin/wpp:CAMERA_FIX_SUMMARY.md:168` `**修复人员**: AI Assistant`；仓库还 `.gitignore` 了 `CLAUDE.md`（`origin/wpp:.gitignore:38`）并误提交了 Claude Code 安装脚本（见 §7.1）

### 10.6 未接线的配置系统
`config.json` / `single_dog_config.json` 无任何代码读取（§6.2），文档里的第二套 schema 也没实现。**不要照抄这两个文件的形状**；要的话自己设计 schema 并真正接线。

### 10.7 `sportsmode.py`（159 行）
纯祖先残留：import 不存在的包（`:7-8`、`:143-144`）、硬编码 IP（`:51`、`:146`）、`except:` 裸吞异常（`:28-29`）、靠 `select.select` 读 stdin（`:24`）。它唯一的存档价值是两条经验注释（`:41` 的 "Dance1大约需要5秒"、`:81` 的 1 秒等待）—— 已经在 §9.9/§9.10 取走。

### 10.8 未完成/不可用的周边
- `FOLLOW_LEADER`：枚举在、实现空（`origin/wpp:dual_dog_movement.py:18-24`、`:83-87`）
- 人体追踪：与 Web 控制台**零集成**（全分支 grep 显示 `human_tracking` 只被 `example_human_tracking.py` / `test_human_tracking.py` / `test_webrtc_tracking.py` 引用），依赖 `ultralytics`/`simple_pid`（不在 `web_requirements.txt` 里），命令链路有事件循环 bug（§9.12），PID 未整定，`yolov11n.pt` 名字可疑。**要做得重写，不是移植。**
- `New_Feature.md` 里的语音/LLM/TTS 部分（FunASR / llama-cpp-python / edge-tts / ControlArbitrator 状态机）**一行都没实现**。它的价值是架构规范（`New_Feature.md:203-261` 的仲裁器状态机 `STATE_STANDBY` / `STATE_TRACKING` / `STATE_MANUAL_OVERRIDE` 与"仲裁器是系统中**唯一**被授权调用 `datachannel.send()` 的模块"，`:223`），以及那条"别阻塞事件循环"的戒律（S9）。
- `libs/aioice` 子模块 gitlink（未初始化无法构建）
- 误提交的 `claude_code_prod.sh`（§7.1）
- 13 个被删文件：**不要恢复为文档**，但内容已在 §9 提炼

---

## 11. 我无法验证的事（明确列出）

1. **两狗是否真的一起跑过**。代码里到处是"只有一条狗"的痕迹（§10.4），但没有任何一份日志/截图能证明或证伪"两台真狗同时受控"。`send_command_to_targets`（`origin/wpp:dual_dog_controller.py:300-327`）在第二台真实设备上的行为属**未验证**。
2. **`BackFlip` / `FrontFlip` 是否需要 `parameter: {"data": True}`**。库示例（`origin/wpp:examples/go2/data_channel/sportmode/sportmode.py:119-125`）需要，WPP 不传（`origin/wpp:dual_dog_controller.py:271-273`）。哪个对，代码无法判定，必须真机验证。
3. **`await asyncio.sleep(2)`（`origin/wpp:dual_dog_controller.py:242`）是否足够**让狗从其它运动模式切到 `normal` 并站稳。库示例用 5 s，`sportsmode.py` 用 1 s；三个值互斥，至少两个错，我无法确定是哪个。
4. **所有性能/兼容性数字**（300-500ms 延迟、20-30fps、CPU 10-15%、50-100MB/流、5-8Mbps、Chrome/Firefox/Safari/Edge "完美支持"、50-100MB/分钟录制）—— 全部来自 `COMPLETE_FIX_SUMMARY.md` / `VIDEO_STREAM_FIX.md` / `CAMERA_FIX_SUMMARY.md` / `WEB_CONTROL_SYSTEM_GUIDE.md` 的声称，**没有对应的基准脚本或原始数据**。`test_video_stream.py` 只测了 JPEG 编码压缩率。
5. **人体追踪是否在真机上闭环跑通过**。能证明的只有"视频帧到达 + YOLO 推理"这半条链（`EVENTLOOP_FINAL_FIX.md:114-116` 的日志是视频通道的，不是追踪的）。命令回传那半条链有明确 bug（`video_processor.py:116-120`），且没有留下任何"追踪生效"的运行记录。我也无法验证 `yolov11n.pt` 这个权重名在作者机器上是否存在。
6. **`/api/scan/devices` 是否成功发现过设备**。`discover_ip_sn` 会 `bind` 到固定端口 10134（`origin/wpp:unitree_webrtc_connect/multicast_scanner.py:21`）且是阻塞的，两次并发扫描会互相冲突；web 路由每次新建线程池调用它（`origin/wpp:web_server.py:515-518`）。没有运行记录，无法验证在 WSL 网络下是否可用。
7. **`emergency_stop` 的 AttributeError 是否被触发过**。它是死代码（`dual_dog_gui_enhanced.py:522` 调用不存在的方法），静态可判定，但我无法证明有人在真机上按过那个按钮。
8. **`MP4 (mp4v)` 录制文件是否真的可播放**。代码路径完整，但没有任何录制成品的样本或验证记录；且崩溃时 `VideoWriter` 不会 release（`origin/wpp:web_server.py:931` 是唯一 release 点），文件必坏。
9. **`DualDogMovement.dance_triggered` 是否真的跨重启泄漏**。它是实例属性并在 `__init__` 置 False（`origin/wpp:dual_dog_movement.py:49`），同进程内 `stop_movement` 不重置它，`_square_walk_loop` 进入时也不重置（只重置 `square_walk_step`，`:181`）。**进程重启必然清零**；"跨重启泄漏"这一说法我无法从代码复现，只能确认"同进程内 stop→start 可能残留"。这一点与既有浅层结论不一致，建议以本节为准。

---

## 附录 A：移植优先级速查表

| 编号 | 项 | 价值 | 立即可用度 | 引用 |
|---|---|---|---|---|
| S1 | WebRTC→MJPEG 视频桥 | ★★★★★ | 高（需补鉴权） | `web_server.py:574-600,644-689` |
| S2 | `run_coroutine_threadsafe` 桥 + 超时表 | ★★★★★ | 高 | `web_server.py:692-708,122-127`；`EVENTLOOP_FINAL_FIX.md:13-36` |
| S3 | all/single/selected 指令路由 | ★★★★★ | 高（纯逻辑，可单测） | `dual_dog_controller.py:291-327,358-409` |
| S4 | 状态契约 + 心跳看门狗 | ★★★★☆ | 高（需修单向状态机） | `dual_dog_controller.py:202-213,335-352,443-464` |
| S5 | 发现→填表→连接 全链路 | ★★★★☆ | 中（改用 master 的 `discover_ip_sn`） | `web_server.py:504-541`；`network_scanner.py:56-109` |
| S6 | 录制首帧重建 writer | ★★★☆☆ | 中（要换编码器/帧率来源） | `web_server.py:662-681,825-843` |
| S7 | 动作目录 + 难度时长表 | ★★★☆☆ | 高 | `dual_dog_movement.py:144-155,279-283` |
| S8 | 部署约束（`use_reloader=False`、WSL） | ★★★☆☆ | 高 | `web_server.py:992-998`；`start_web_server.sh:26` |
| S9 | 不阻塞事件循环的架构戒律 | ★★★☆☆ | 规范 | `New_Feature.md:105-107,229-237` |
| S10 | "先 X 后 Y"次序清单 | ★★★☆☆ | 需逐条复核 | 见 S10 表 |
| — | tkinter GUI ×3 | ☆ | 坏 | §10.2 |
| — | `config.json` schema | ☆ | 未接线 | §6.2 |
| — | 人体追踪模块 | ★★☆☆☆ | 需重写 | §10.8 |
