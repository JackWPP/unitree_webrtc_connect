

# **宇树机器狗（Unitree）的集成式人工智能控制系统：技术架构与实施蓝图**

## **第 1 部分：系统概述与架构**

### **1.1 引言与目标**

本技术文档旨在为基于 PC 客户端的宇树机器狗（Unitree）高级控制系统提供一个全面、详尽的架构设计和实施蓝图。项目的核心目标是开发一个多模态、人工智能驱动的控制界面，该界面完全在客户端（PC）上运行，并通过 WebRTC 协议与机器狗进行实时通信。

该系统将实现两大核心控制回路，以满足用户（开发者）的快速开发需求：

1. **视觉跟踪回路：** 客户端接收机器狗回传的 WebRTC 视频流，利用 YOLO（You Only Look Once）等先进的计算机视觉（CV）模型对视频流进行实时处理，实现高精度的人体跟踪。  
2. **对话式命令回路：** 客户端采集本地麦克风输入，通过本地化的自动语音识别（ASR）和大型语言模型（LLM）进行指令理解和意图分析，生成相应的运动指令或语音反馈。

系统将根据 YOLO 跟踪结果或 LLM 理解的指令，生成结构化的运动命令，并通过 WebRTC 数据通道（Data Channel）发送给机器狗执行。此外，系统还将通过本地文本转语音（TTS）引擎提供实时的语音响应。本文档将详细阐述实现这一目标所需的技术栈、系统架构、数据流和关键集成点。

### **1.2 概念架构**

本系统的核心架构将基于 Python 的 asyncio 库构建 1。这是由 WebRTC 的 Python 实现库 aiortc 的异步特性所决定的。整个应用程序将作为一个 asyncio 事件循环运行，协调多个独立的、并发的子系统。

系统包含两个主要的、并行的控制流程：

1. **反应式视觉回路（高频，低延迟）：**  
   * 流程：WebRTC 视频输入 \-\> 视频帧解码 (av.VideoFrame) \-\> YOLO 目标跟踪 \-\> PID 控制逻辑 \-\> 运动指令生成 \-\> WebRTC 数据通道输出。  
   * 特性：此回路需要以视频帧率（例如 15-30Hz）持续运行，对延迟高度敏感，用于实现平滑的自动跟踪。  
2. **主动式对话回路（低频，事件驱动）：**  
   * 流程：麦克风音频输入 (PyAudio) \-\> 实时 ASR \-\> LLM 意图解析 \-\> 指令/TTS 文本生成 \-\> (运动指令输出 / 本地 TTS 播放)。  
   * 特性：此回路由用户的语音指令触发，属于事件驱动型，用于下达明确的、高级的命令（例如“站起来”、“跟着我”）。

这两个回路的输出（即运动指令）将由一个“控制仲裁器”（Control Arbitrator）模块统一管理，该模块根据系统当前状态（例如“待命”、“跟踪中”）决定最终发送给机器狗的命令。

### **1.3 核心组件与技术栈**

为了实现“快速开发”这一关键约束，技术栈的选择将优先考虑那些具有成熟 Python API、强大社区支持和高性能的库。一个核心的设计决策是，我们将**直接利用 legion1581/go2\_webrtc\_connect 库** 2 作为连接和通信的基础，而不是从头构建底层的 aiortc 客户端。go2\_webrtc\_connect 封装了 Unitree 特定的、非公开的信令（Signaling）和连接逻辑 3，直接解决了项目中最复杂、最耗时的部分。

以下是为构建此系统而选定的技术栈：

| 组件 | 选用库/技术 | 理由 |
| :---- | :---- | :---- |
| **WebRTC 通信** | go2\_webrtc\_connect 2 | 封装了 Unitree Go2 特定的 WebRTC 连接和信令逻辑，是实现“快速开发”的关键。它本身基于 aiortc 1。 |
| **视觉感知** | ultralytics (YOLOv8) 4 | 提供 SOTA 级的实时目标检测与跟踪性能。其 Python API 简洁，且内置了 model.track() 方法，可直接输出带 ID 的跟踪结果 6。 |
| **图像处理** | opencv-python 8 | 标准的 CV 库。用于在 av.VideoFrame 9 和 YOLOv8 的 numpy.ndarray 输入之间进行必要的格式转换（例如 BGR/RGB 切换） 10。 |
| **视觉控制** | simple-pid | 一个轻量级的 PID 控制器库。用于将 YOLO 的像素空间误差平滑地转换为机器狗的运动速度指令 11。 |
| **音频输入** | PyAudio | 事实上的标准库，用于从 Python 中捕获麦克风的实时音频流 13。 |
| **自动语音识别 (ASR)** | FunASR 15 | 阿里达摩院的开源 ASR 工具。相比 whisper.cpp 17，它专为流式识别 18 设计，内置 VAD（语音活动检测）16，极大简化了实时语音流的处理，开发速度更快。 |
| **自然语言理解 (NLU)** | llama-cpp-python 19 | 业界领先的库，用于在本地 CPU/GPU 上高效运行 GGUF 格式的量化大模型（如 Llama 3 8B）。 |
| **文本转语音 (TTS)** | edge-tts 21 | 一个轻量级、高质量的 TTS 库。其关键优势是提供了原生的 asyncio API 23，可以在不阻塞 WebRTC 事件循环的情况下异步生成和播放音频。 |

## **第 2 部分：WebRTC 通信层与机器人接口**

### **2.1 连接层抽象**

本系统的架构将围绕 go2\_webrtc\_connect 2 库进行构建。我们将创建一个中心控制类（例如 UnitreeController），它封装或继承自 Go2WebRTCConnection。这个类将负责处理所有底层的 WebRTC 事务，包括信令、STUN/TURN 协商以及数据通道管理 2。

我们的主要任务是实现该库提供的回调（Callbacks），特别是用于视频和数据通道的回调，然后将我们的 AI 逻辑（YOLO、LLM）挂载到这些回调上。

### **2.2 接收视频与音频流**

我们将通过重写或订阅 go2\_webrtc\_connect 提供的 on\_track 事件处理器来实现视频接收。当一个媒体轨道（Track）建立时，此回调会被触发。

Python

*\# 示例代码概念*  
controller \= Go2WebRTCConnection(...)

**@controller.on\_track**  
**async** **def** **on\_track**(track):  
    **if** track.kind \== "video":  
        *\# 成功接收到视频流*  
        *\# track 是一个 aiortc.MediaStreamTrack 实例*   
        *\# 我们现在可以启动一个异步任务来处理它*  
        asyncio.create\_task(handle\_video\_stream(track))

在 handle\_video\_stream 任务中，我们将进入一个异步循环，不断地从轨道中拉取视频帧。  
关键调用是 await track.recv() 24。此调用将返回一个 av.VideoFrame 对象 9，这是 aiortc 用来封装单个视频帧的数据结构。这个 VideoFrame 对象将是第 3 部分中视觉处理管线的起点。

### **2.3 WebRTC 数据通道命令 API 定义**

这是整个系统中最关键、也是最不透明的部分。机器狗的运动控制是通过一个特定的 WebRTC 数据通道 10 发送的。这些命令的**确切格式**（例如，JSON 结构、键值对）并未公开记录。

由于分析 main.js 和 main.py 的尝试未能访问到目标文件（49），我们必须做出一个关键假设：go2\_webrtc\_connect 2 库的 Python 源代码中 *必须* 包含用于格式化和发送这些命令的逻辑。

因此，**项目的第一个实施步骤（参见 6.2 节）必须是**：克隆 go2\_webrtc\_connect 仓库，并对其源代码进行逆向工程，以找出发送运动指令（例如 move, gait, stance）的确切函数和其消息载荷（Payload）格式。

基于机器人技术和 WebRTC 的通用实践 27，我们假定该 API 是通过数据通道发送的 JSON 字符串。PID 和 LLM 模块的输出必须被格式化为这种结构。

**表 2.3：假定的运动命令 API（待验证）**

| 命令 | 假定的 JSON 结构 | 描述 |
| :---- | :---- | :---- |
| **连续运动 (Gait)** | {"cmd": "move", "type": "gait", "linear": \[x, y, z\], "angular": \[r, p, yaw\]} | 用于平滑的运动控制。linear.x（前/后）和 angular.yaw（左/右转）将由 YOLO-PID 回路高频更新。 |
| **离散动作 (Stance)** | {"cmd": "action", "type": "stance", "value": "stand\_up"} | 用于 LLM 指令，如“站立”或“趴下”。 |
| **模式切换 (Mode)** | {"cmd": "action", "type": "mode", "value": "balance\_mode"} | 用于 LLM 指令，如“切换到平衡模式”。 |
| **发送消息 (Ping)** | {"cmd": "ping"} | 可能需要一个心跳包来保持数据通道的活跃 28。 |

## **第 3 部分：视觉感知与控制回路（人体跟踪）**

### **3.1 实时视频处理管线**

视觉回路始于从 on\_track 回调中获取的 av.VideoFrame 对象 9。此对象存在于 asyncio 事件循环中。

**关键转换点：** 为了将此帧用于 OpenCV 和 YOLO，我们必须将其转换为 numpy.ndarray。这通过调用 frame.to\_ndarray(format="bgr24") 9 来实现。这个 BGR24 格式是 OpenCV 的标准格式 24。

**性能警告：** to\_ndarray() 转换以及随后的所有 CV 处理（YOLO 推理、PID 计算）都是**计算密集型和阻塞 (Blocking) 操作**。如果这些操作在主 asyncio 事件循环中执行，它们将*冻结*整个应用程序，导致 WebRTC 连接超时、视频卡顿和命令丢失。

**架构解决方案：** 必须将整个视觉处理管线（从 to\_ndarray 开始）移出一个单独的工作线程（Worker Thread）。asyncio 主循环将作为生产者，将 VideoFrame 对象放入一个线程安全的队列（asyncio.Queue）30，而 CV 工作线程将作为消费者。详见 5.2 节。

### **3.2 YOLOv8 对象跟踪实现**

一旦 CV 工作线程获得了 numpy 格式的图像帧，我们将使用 ultralytics 库进行跟踪。

我们将使用 model.track() 方法，而不是 model.predict() 6。  
选择 track() 的原因是其提供了跨帧的持久化对象 ID 31。在多人场景中，predict() 只能告诉我们“这里有三个人”，而 track() 可以告诉我们“这是 ID 1、ID 3 和 ID 7”。这使我们能够实现“锁定”功能：一旦 LLM 指令（或默认逻辑）选择跟踪“ID 3”，系统就可以在后续所有帧中专门跟踪“ID 3”的边界框 33，即使用户被部分遮挡或其他人进入视野。  
CV 线程中的逻辑将是：

1. results \= model.track(frame, persist=True)  
2. 在 results 中搜索我们当前“锁定”的目标 ID。  
3. 如果找到，提取该目标的边界框坐标 \[xmin, ymin, xmax, ymax\]。  
4. 将这些坐标传递给 PID 控制器。

### **3.3 基于 PID 的视觉伺服控制**

**目标：** 将来自 YOLO 的像素空间误差（即目标不在图像中心）转换为平滑的、现实世界的机器狗运动速度指令 35。直接将误差映射到速度会导致系统振荡和不稳定。

**实现：** 我们将实例化**两个**独立的 PID 控制器 12，均在 CV 工作线程中运行：

1. pid\_angular (角速度 / 偏航 / 转向)：  
   * **目标：** 保持目标在图像的水平中心。  
   * **设定点 (Setpoint)：** image\_center\_x \= frame.width / 2  
   * **过程变量 (Process Variable)：** bbox\_center\_x \= (xmin \+ xmax) / 2  
   * **误差：** setpoint \- process\_variable  
   * **输出：** angular\_velocity\_yaw (一个 \-1.0 到 1.0 之间的值，映射到表 2.3 中的 angular.yaw)  
2. pid\_linear (线速度 / 前进 / 后退)：  
   * **目标：** 与目标保持一个恒定的理想距离。  
   * **设定点 (Setpoint)：** DESIRED\_BBOX\_AREA (一个需要调整的恒定值，例如 50000 像素，代表“理想距离”)  
   * **过程变量 (Process Variable)：** current\_bbox\_area \= (xmax \- xmin) \* (ymax \- ymin)  
   * **误差：** setpoint \- process\_variable  
   * **输出：** linear\_velocity\_x (一个 \-1.0 到 1.0 之间的值，映射到表 2.3 中的 linear.x)

**调优（Tuning）：** PID 的 Kp, Ki, Kd 参数 11 将是实现平滑跟踪的最关键挑战。Kp（比例）提供即时响应。Ki（积分）将帮助消除稳态误差（例如目标总是稍微偏左）。Kd（微分）将抑制超调（Overshoot）并减少振荡 12。这些参数必须在连接到真实机器狗后进行大量实地调优。

**表 3.3：PID 控制逻辑摘要**

| 控制器 | 目标 | 设定点 (SP) | 过程变量 (PV) | 输出变量 (映射至) | 调优说明 |
| :---- | :---- | :---- | :---- | :---- | :---- |
| pid\_angular | 水平居中 (偏航控制) | image\_width / 2 | (xmin \+ xmax) / 2 | angular.yaw | 先调 P。增加 D 以减少振荡。最后加 I 以消除稳态偏差。 |
| pid\_linear | 保持距离 (前进/后退) | DESIRED\_BBOX\_AREA (常量) | (xmax \- xmin) \* (ymax \- ymin) | linear.x | 同样从 P 开始。小心 I 的“积分饱和” (wind-up) 导致的前冲。 |

## **第 4 部分：对话式控制回路（语音命令）**

### **4.1 音频输入与实时 ASR**

对话回路将由一个专用的 asyncio 任务管理。此任务将使用 PyAudio 13 打开一个到默认麦克风的音频输入流。

**ASR 模型选型：** 我们选择 FunASR 15 而不是 whisper.cpp 17。原因是 FunASR 是为流式服务器架构设计的 18，并提供了包含 VAD（语音活动检测）的流式客户端 API 16。这意味着我们可以简单地将 PyAudio 的音频块（chunks）连续喂给 FunASR 客户端。FunASR 将在内部处理静音检测，并在检测到用户一句话说完后，自动返回完整的转录结果。这极大地简化了开发，避免了我们手动实现 VAD 和音频缓冲逻辑。

**流程：**

1. 启动一个 PyAudio 录音流。  
2. 在录音回调中，将音频块喂给 FunASR 流式客户端。  
3. FunASR 客户端在检测到句子结束时，返回转录文本（例如 "跟着我"）。  
4. 此文本被放入一个 asyncio.Queue，供 LLM 任务消费。

### **4.2 LLM 意图识别**

**目标：** 将来自 ASR 的非结构化文本（例如“停下”、“跟着那个穿红衣服的人”）转换为第 2.3 节中定义的机器可读的 JSON 命令 39。

**实现：** 我们将使用 llama-cpp-python 19 在本地加载一个量化模型（例如 Llama-3-8B-Instruct.Q4\_K\_M.gguf）。

**关键设计：强制 JSON 输出。** LLM 的输出必须是 100% 可靠的 JSON，否则系统将崩溃。我们绝不能依赖 LLM 的“自愿”格式化。我们将通过以下方式实现：

1. **系统提示 (System Prompt)：** 提供一个详细的系统提示，定义机器狗的 API、可用命令以及严格的 JSON 输出格式。  
2. **结构化输出：** 利用 llama-cpp-python 的 grammar（语法）功能，或 Jsonformer 41、Outlines 等库，将输出严格约束为一个 Pydantic 模型（Schema）42。这将强制 LLM 只能生成符合预定义 JSON 结构的 token。

**流程：**

1. 从 ASR 队列中收到文本："跟着我"。  
2. 构建一个包含系统提示、JSON 约束和用户文本的完整提示。  
3. 调用 llm.create\_chat\_completion(...)。  
4. 解析返回的、保证有效的 JSON 对象。

**表 4.2：LLM 意图 JSON 模式（示例）**

| 用户语音 | 输出 JSON | 描述 |
| :---- | :---- | :---- |
| "停下" / "站住" | {"action": "stop"} | 一个全局停止命令，将使系统进入 STATE\_STANDBY。 |
| "跟着我" / "锁定目标" | {"action": "track\_person", "target": "nearest"} | 激活视觉跟踪，使系统进入 STATE\_TRACKING。 |
| "前进" / "后退两步" | {"action": "move\_discrete", "linear\_x": 0.3, "duration\_s": 2.0} | 一个离散的运动命令。 |
| "你看到什么了？" | {"action": "speak", "response": "我正在跟踪一个目标。"} | 一个非运动命令，将触发 TTS 响应。 |
| "站起来" | {"action": "robot\_command", "payload": {"cmd": "action", "type": "stance", "value": "stand\_up"}} | 直接映射到表 2.3 的机器人 API。 |

### **4.3 本地 TTS 语音响应**

当 LLM 生成一个包含 {"action": "speak",...} 的 JSON 时，控制仲裁器需要将 response 字符串转换为语音。

TTS 模型选型： 我们选择 edge-tts 21。  
核心优势： edge-tts 是为 asyncio 而生的 21。我们可以在 asyncio 主循环中安全地调用 await communicate.save() 或（更优）直接流式传输到音频播放器 44，而不会阻塞 WebRTC 事件循环。这对于保持系统响应性至关重要。  
如果需要更高的语音质量或语音克隆，可以考虑 Coqui XTTS v2 45，但这需要本地 GPU 资源和更复杂的流式处理实现 45，与“快速开发”目标相悖。

## **第 5 部分：系统集成与控制仲裁**

### **5.1 控制仲裁器设计**

**目标：** 这是系统的“大脑”，一个状态机，用于防止 YOLO-PID 回路（高频、反应式）和 LLM 回路（低频、主动式）争夺机器狗的控制权。

**核心状态 (States)：**

1. STATE\_STANDBY (待命状态)：  
   * 默认状态。  
   * 仲裁器向机器狗高频发送“空指令”（例如 {"cmd": "move", "linear": , "angular": }）以保持连接。  
   * 在此状态下，它**只接受**来自 LLM 的命令。  
   * 如果收到 {"action": "track\_person"}，则切换到 STATE\_TRACKING。  
2. STATE\_TRACKING (跟踪状态)：  
   * 由 LLM 激活。  
   * 在此状态下，仲裁器**完全忽略**来自 YOLO-PID 回路的运动指令，并将这些指令（linear.x, angular.yaw）转发到机器狗的数据通道。  
   * 它**忽略**所有来自 LLM 的运动指令，**只侦听**一个 LLM 命令：{"action": "stop"}。  
   * 如果收到 {"action": "stop"}，则切换回 STATE\_STANDBY。  
3. STATE\_MANUAL\_OVERRIDE (手动覆盖状态)：  
   * 当处于 STATE\_STANDBY 并收到来自 LLM 的离散运动指令（如“前进”）时触发。  
   * 仲裁器执行该离散命令，然后立即返回 STATE\_STANDBY。

这个仲裁器类将是系统中**唯一**被授权调用 datachannel.send() 的模块。

### **5.2 并发与异步管理（核心架构）**

这是本系统设计中技术上最具挑战性、也是最关键的部分。

**问题：** aiortc 1 依赖于单线程的 asyncio 事件循环。YOLO 推理 6 和 LLM 推理 19 都是耗时（几十到几百毫秒）的**阻塞 (Blocking)** 操作。

**致命缺陷：** 如果在 asyncio 主线程中（例如在 on\_track 回调中）直接调用 model.track()，整个事件循环将被冻结。这将导致：

* WebRTC 心跳包丢失，连接断开。  
* 视频流完全卡住。  
* 所有 asyncio 任务（包括 LLM 任务）全部饿死。

**架构解决方案：** 必须采用一个解耦的、多线程的“生产者-消费者”模型，并使用 asyncio 的线程安全原语（如 asyncio.Queue 30）作为“胶水”。

**视觉回路（高频）的详细数据流：**

1. MainThread (Asyncio): on\_track 回调被触发。它**唯一**的工作是：await frame\_queue.put(frame)。此操作非阻塞且极快。  
2. CV\_WorkerThread (Blocking): 这是一个独立的、永久运行的 threading.Thread。  
3. CV\_WorkerThread: 它在一个 while True 循环中运行：frame \= frame\_queue.get() (这是一个阻塞调用，等待主线程的帧)。  
4. CV\_WorkerThread: img \= frame.to\_ndarray(format="bgr24") (阻塞的 CPU 任务)。  
5. CV\_WorkerThread: results \= yolo\_model.track(img) (阻塞的 GPU 任务)。  
6. CV\_WorkerThread: cmd \= pid.compute(...) (阻塞的 CPU 任务)。  
7. CV\_WorkerThread: asyncio.run\_coroutine\_threadsafe(command\_queue.put(cmd), main\_loop)。这是**关键的桥梁**：它安全地将计算结果（cmd）从工作线程提交回主线程的 asyncio 队列中。  
8. MainThread (Asyncio): ControlArbitrator 任务（一个 asyncio.create\_task）正在 await command\_queue.get()。  
9. MainThread (Asyncio): ControlArbitrator 收到 cmd，检查 if state \== STATE\_TRACKING: datachannel.send(format(cmd))。此发送是非阻塞的。

**对话回路（低频）的详细数据流：**

1. Audio\_WorkerThread (Blocking): 另一个独立的 threading.Thread，运行 PyAudio \-\> FunASR 的阻塞循环。  
2. Audio\_WorkerThread: 当 FunASR 返回转录文本 text 时：asyncio.run\_coroutine\_threadsafe(asr\_queue.put(text), main\_loop)。  
3. MainThread (Asyncio): 一个 LLM\_Task（asyncio.create\_task）正在 await asr\_queue.get()。  
4. MainThread (Asyncio): LLM\_Task 收到 text。现在它必须调用阻塞的 LLM。  
5. MainThread (Asyncio): LLM\_Task 调用：json\_cmd \= await asyncio.to\_thread(llm.create\_chat\_completion, prompt,...)。  
6. MainThread (Asyncio): **关键点：** asyncio.to\_thread 会在一个**新的、独立的线程池**中运行阻塞的 LLM 推理，而 LLM\_Task 自身会 await，**释放主线程**，使其可以继续处理 WebRTC 流量。  
7. MainThread (Asyncio): LLM 推理完成后，LLM\_Task 恢复，并将 json\_cmd 提交给 ControlArbitrator 进行状态处理和执行。

这种架构将 I/O 密集型任务（WebRTC、音频）与计算密集型任务（CV、LLM）完全分离，是构建响应式实时 AI 系统的唯一可靠方法。

## **第 6 部分：实施规划与蓝图**

### **6.1 环境设置与依赖项**

1. **Python:** 推荐 3.10+。  
2. **PyTorch:** 必须安装支持 CUDA 的版本（如果使用 NVIDIA GPU）。  
3. **系统依赖：** PortAudio (用于 PyAudio)。  
4. **llama-cpp-python:** 根据硬件安装，例如 pip install llama-cpp-python\[server\] 并设置 CMAKE\_ARGS 以启用 CUDA 或 Metal 加速。  
5. **Python 依赖 (requirements.txt):**  
   go2\_webrtc\_connect  
   ultralytics  
   opencv-python-headless  
   simple-pid  
   pyaudio  
   funasr  
   llama-cpp-python  
   edge-tts  
   asyncio

6. **模型文件：**  
   * YOLO: yolov8n.pt (或更重的模型)。  
   * LLM: 下载一个 GGUF 格式的指令微调模型（如 Llama-3-8B-Instruct.Q4\_K\_M.gguf）。  
   * FunASR: 模型将在首次运行时自动下载。

### **6.2 分步开发路径（里程碑）**

此开发路径旨在首先解决最高风险和最不确定的部分。

* **里程碑 1：建立连接与控制（Go/No-Go 阶段）**  
  1. 克隆 legion1581/go2\_webrtc\_connect 2。  
  2. 运行其 example 目录中的示例，确保可以与机器狗建立 WebRTC 连接。  
  3. **关键任务：** 深入研究其 Python 源代码，**找出发送运动指令的确切函数和 JSON 格式**（参见 2.3 节）。  
  4. 编写一个最简单的 Python 脚本，不包含任何 AI，只使用 asyncio 和该库，发送一个硬编码的“原地旋转”命令：datachannel.send('{"cmd": "move", "angular": \[0,0,0.5\]}')。  
  5. **验证：** 机器狗必须旋转。如果此步骤失败，项目将无法继续。  
* **里程碑 2：实现视频接收**  
  1. 基于里程碑 1，添加 on\_track 回调。  
  2. 实现 av.VideoFrame \-\> numpy.ndarray 的转换 9。  
  3. 在一个**单独的线程**中（避免阻塞 asyncio）使用 cv2.imshow("Video", frame) 6 来显示实时视频流。  
  4. **验证：** PC 上能以低延迟显示机器狗的第一视角。  
* **里程碑 3：构建视觉跟踪回路（离线调优）**  
  1. 将 model.track() 6 集成到里程碑 2 的 cv2 显示线程中。  
  2. 在 cv2 窗口上绘制 YOLOv8 的边界框，验证跟踪 34。  
  3. 实现 pid\_angular 和 pid\_linear 类 12。  
  4. **不要**将 PID 输出发送给机器狗。而是将计算出的 linear\_velocity 和 angular\_velocity 实时 print() 到控制台。  
  5. **验证：** 手动在摄像头前移动，观察控制台输出。调整 PID 参数（Kp, Ki, Kd）11，直到输出的 linear 和 angular 速度值看起来平滑、稳定、响应迅速且没有剧烈振荡。  
* **里程碑 4：构建对话回路（独立测试）**  
  1. 编写一个**完全独立**的 asyncio 脚本，不涉及任何 WebRTC 或 Unitree。  
  2. 实现 PyAudio \-\> FunASR (流式) \-\> LLM (llama-cpp-python) (带 JSON 强制) \-\> edge-tts (异步播放) 的完整流水线 15。  
  3. **验证：** 对着麦克风说“站起来”，程序应在控制台打印出 {"action": "robot\_command",...}，并用 edge-tts 的声音说出“好的，正在站起”。  
* **里程碑 5：最终集成（仲裁器与并发）**  
  1. 将里程碑 1、3、4 的代码合并到一个项目中。  
  2. 严格按照 5.2 节的描述，实现 asyncio 主线程与 CV\_WorkerThread、Audio\_WorkerThread 之间的多线程架构（使用 asyncio.Queue 和 asyncio.to\_thread）。  
  3. 实现 5.1 节中描述的 ControlArbitrator 状态机（STATE\_STANDBY, STATE\_TRACKING）。  
  4. **最终系统测试：**  
     * 启动系统（应处于 STATE\_STANDBY）。  
     * 对着麦克风说：“跟着我”。  
     * **验证：** 系统应语音回应“好的，正在跟踪”，并切换到 STATE\_TRACKING。  
     * **验证：** 机器狗现在开始实时跟随 PID 回路的输出，自动跟踪目标。  
     * 对着麦克风说：“停下”。  
     * **验证：** 系统应语音回应“已停止”，机器狗停止运动，系统切换回 STATE\_STANDBY。

#### **引用的著作**

1. aiortc/aiortc: WebRTC and ORTC implementation for Python using asyncio \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/aiortc/aiortc](https://github.com/aiortc/aiortc)  
2. legion1581/unitree\_webrtc\_connect: Unitree Go2 and G1 WebRTC driver \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/legion1581/go2\_webrtc\_connect](https://github.com/legion1581/go2_webrtc_connect)  
3. tfoldi/go2-webrtc: WebRTC API for Unitree GO2 Robots \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/tfoldi/go2-webrtc](https://github.com/tfoldi/go2-webrtc)  
4. Explore Ultralytics YOLOv8, 访问时间为 十一月 11, 2025， [https://docs.ultralytics.com/models/yolov8/](https://docs.ultralytics.com/models/yolov8/)  
5. Ultralytics YOLO \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/ultralytics/ultralytics](https://github.com/ultralytics/ultralytics)  
6. How to output a live broadcast with object detection Yolov8 \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/77222165/how-to-output-a-live-broadcast-with-object-detection-yolov8](https://stackoverflow.com/questions/77222165/how-to-output-a-live-broadcast-with-object-detection-yolov8)  
7. Object Tracking with YOLOv8 and Python \- PyImageSearch, 访问时间为 十一月 11, 2025， [https://pyimagesearch.com/2024/06/17/object-tracking-with-yolov8-and-python/](https://pyimagesearch.com/2024/06/17/object-tracking-with-yolov8-and-python/)  
8. OpenCV python modeling server with WebRTC \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/63549278/opencv-python-modeling-server-with-webrtc](https://stackoverflow.com/questions/63549278/opencv-python-modeling-server-with-webrtc)  
9. Real-time object detection with WebRTC and YOLO | Modal Docs, 访问时间为 十一月 11, 2025， [https://modal.com/docs/examples/webrtc\_yolo](https://modal.com/docs/examples/webrtc_yolo)  
10. aiortc — aiortc documentation, 访问时间为 十一月 11, 2025， [https://aiortc.readthedocs.io/](https://aiortc.readthedocs.io/)  
11. PID Controller in Robotics—A Practical Deep Dive with Python and C++, 访问时间为 十一月 11, 2025， [https://machinelearningsite.com/pid-controller-in-robotics/](https://machinelearningsite.com/pid-controller-in-robotics/)  
12. Python PID Controller Example: A Complete Guide | by UATeam | Medium, 访问时间为 十一月 11, 2025， [https://medium.com/@aleksej.gudkov/python-pid-controller-example-a-complete-guide-5f35589eec86](https://medium.com/@aleksej.gudkov/python-pid-controller-example-a-complete-guide-5f35589eec86)  
13. python \- How to convert live real time audio from mic to text? \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/57268372/how-to-convert-live-real-time-audio-from-mic-to-text](https://stackoverflow.com/questions/57268372/how-to-convert-live-real-time-audio-from-mic-to-text)  
14. Real-time Speech Recognition with PyAudio and Faster Whisper (Issue with Temporary Files) \#827 \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/SYSTRAN/faster-whisper/discussions/827](https://github.com/SYSTRAN/faster-whisper/discussions/827)  
15. modelscope/FunASR: A Fundamental End-to-End Speech Recognition Toolkit and Open Source SOTA Pretrained Models, Supporting Speech Recognition, Voice Activity Detection, Text Post-processing etc. \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/modelscope/FunASR](https://github.com/modelscope/FunASR)  
16. funasr \- PyPI, 访问时间为 十一月 11, 2025， [https://pypi.org/project/funasr/0.7.5/](https://pypi.org/project/funasr/0.7.5/)  
17. ggml-org/whisper.cpp: Port of OpenAI's Whisper model in C/C++ \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/ggml-org/whisper.cpp](https://github.com/ggml-org/whisper.cpp)  
18. Fun-ASR Technical Report \- arXiv, 访问时间为 十一月 11, 2025， [https://arxiv.org/html/2509.12508v3](https://arxiv.org/html/2509.12508v3)  
19. Llama.cpp Python Examples: A Guide to Using Llama Models with Python \- Medium, 访问时间为 十一月 11, 2025， [https://medium.com/@aleksej.gudkov/llama-cpp-python-examples-a-guide-to-using-llama-models-with-python-1df9ba7a5fcd](https://medium.com/@aleksej.gudkov/llama-cpp-python-examples-a-guide-to-using-llama-models-with-python-1df9ba7a5fcd)  
20. llama.cpp: The Ultimate Guide to Efficient LLM Inference and Applications \- PyImageSearch, 访问时间为 十一月 11, 2025， [https://pyimagesearch.com/2024/08/26/llama-cpp-the-ultimate-guide-to-efficient-llm-inference-and-applications/](https://pyimagesearch.com/2024/08/26/llama-cpp-the-ultimate-guide-to-efficient-llm-inference-and-applications/)  
21. Edge TTS: The Ultimate Guide for Developers \- VideoSDK, 访问时间为 十一月 11, 2025， [https://www.videosdk.live/developer-hub/ai/edge-tts](https://www.videosdk.live/developer-hub/ai/edge-tts)  
22. edge-tts \- PyPI, 访问时间为 十一月 11, 2025， [https://pypi.org/project/edge-tts/](https://pypi.org/project/edge-tts/)  
23. Adding a simple example of how to use the application in Python · Issue \#20 · rany2/edge-tts, 访问时间为 十一月 11, 2025， [https://github.com/rany2/edge-tts/issues/20](https://github.com/rany2/edge-tts/issues/20)  
24. Building a Real-Time Streaming Application Using WebRTC in Python \- Medium, 访问时间为 十一月 11, 2025， [https://medium.com/@malieknath135/building-a-real-time-streaming-application-using-webrtc-in-python-d34694604fc4](https://medium.com/@malieknath135/building-a-real-time-streaming-application-using-webrtc-in-python-d34694604fc4)  
25. Using aiortc to stream live video feed instead of a video file \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/78013750/using-aiortc-to-stream-live-video-feed-instead-of-a-video-file](https://stackoverflow.com/questions/78013750/using-aiortc-to-stream-live-video-feed-instead-of-a-video-file)  
26. API Reference \- aiortc documentation, 访问时间为 十一月 11, 2025， [https://aiortc.readthedocs.io/en/latest/api.html](https://aiortc.readthedocs.io/en/latest/api.html)  
27. python \- Send and receive objects through WebRTC data channel \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/72160370/send-and-receive-objects-through-webrtc-data-channel](https://stackoverflow.com/questions/72160370/send-and-receive-objects-through-webrtc-data-channel)  
28. Can't add a second data channel while handling message events separately : r/WebRTC, 访问时间为 十一月 11, 2025， [https://www.reddit.com/r/WebRTC/comments/oe78v4/cant\_add\_a\_second\_data\_channel\_while\_handling/](https://www.reddit.com/r/WebRTC/comments/oe78v4/cant_add_a_second_data_channel_while_handling/)  
29. Sending opencv frame to webrtc · Issue \#447 · aiortc/aiortc \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/aiortc/aiortc/issues/447](https://github.com/aiortc/aiortc/issues/447)  
30. Seems datachannel.send() blocked by reading channel · Issue \#40 \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/aiortc/aiortc/issues/40](https://github.com/aiortc/aiortc/issues/40)  
31. YOLOv8 Object Detection & Tracking Guide \- Ultralytics, 访问时间为 十一月 11, 2025， [https://www.ultralytics.com/blog/object-detection-and-tracking-with-ultralytics-yolov8](https://www.ultralytics.com/blog/object-detection-and-tracking-with-ultralytics-yolov8)  
32. Yolov8 object tracking 100% native | Object detection with Python | Computer vision tutorial, 访问时间为 十一月 11, 2025， [https://www.youtube.com/watch?v=uMzOcCNKr5A](https://www.youtube.com/watch?v=uMzOcCNKr5A)  
33. How to convert Yolo format bounding box coordinates into OpenCV format \- Stack Overflow, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/64096953/how-to-convert-yolo-format-bounding-box-coordinates-into-opencv-format](https://stackoverflow.com/questions/64096953/how-to-convert-yolo-format-bounding-box-coordinates-into-opencv-format)  
34. Real-time Object Tracking with OpenCV and YOLOv8 in Python, 访问时间为 十一月 11, 2025， [https://thepythoncode.com/article/real-time-object-tracking-with-yolov8-opencv](https://thepythoncode.com/article/real-time-object-tracking-with-yolov8-opencv)  
35. Visual Servoing of a Moving Target by an Unmanned Aerial Vehicle \- MDPI, 访问时间为 十一月 11, 2025， [https://www.mdpi.com/1424-8220/21/17/5708](https://www.mdpi.com/1424-8220/21/17/5708)  
36. Reetika12795/Visual\_Servoing: THis project will contain all documentation for MSFT project \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/Reetika12795/Visual\_Servoing](https://github.com/Reetika12795/Visual_Servoing)  
37. BurakDmb/DifferentialDrivePathTracking: A simple goal-to-goal PID controller to control a Differential Drive Robot using Python \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/BurakDmb/DifferentialDrivePathTracking](https://github.com/BurakDmb/DifferentialDrivePathTracking)  
38. Faster Whisper transcription with CTranslate2 \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper)  
39. a intent parser using llama.cpp and mistral-7b \- GitHub Gist, 访问时间为 十一月 11, 2025， [https://gist.github.com/JarbasAl/e07e17a2d98a80eb6bf60139acc1b9c7](https://gist.github.com/JarbasAl/e07e17a2d98a80eb6bf60139acc1b9c7)  
40. Generative AI Meets Intent Classification \- A Smarter Approach \- Nish Blog, 访问时间为 十一月 11, 2025， [https://nishbhana.com/Intent-Classification/](https://nishbhana.com/Intent-Classification/)  
41. Extract data from text and parse it as a JSON \- Beginners \- Hugging Face Forums, 访问时间为 十一月 11, 2025， [https://discuss.huggingface.co/t/extract-data-from-text-and-parse-it-as-a-json/64971](https://discuss.huggingface.co/t/extract-data-from-text-and-parse-it-as-a-json/64971)  
42. Practical Techniques to constraint LLM output in JSON format | by Minyang Chen \- Medium, 访问时间为 十一月 11, 2025， [https://mychen76.medium.com/practical-techniques-to-constraint-llm-output-in-json-format-e3e72396c670](https://mychen76.medium.com/practical-techniques-to-constraint-llm-output-in-json-format-e3e72396c670)  
43. Generating Perfectly Validated JSON Using LLMs — All the Time \- Python in Plain English, 访问时间为 十一月 11, 2025， [https://python.plainenglish.io/generating-perfectly-structured-json-using-llms-all-the-time-13b7eb504240](https://python.plainenglish.io/generating-perfectly-structured-json-using-llms-all-the-time-13b7eb504240)  
44. How to play generated audio from Edge TTS directly to speaker without saving it first?, 访问时间为 十一月 11, 2025， [https://stackoverflow.com/questions/78757503/how-to-play-generated-audio-from-edge-tts-directly-to-speaker-without-saving-it](https://stackoverflow.com/questions/78757503/how-to-play-generated-audio-from-edge-tts-directly-to-speaker-without-saving-it)  
45. Streaming real-time text to speech with XTTS V2 \- Baseten, 访问时间为 十一月 11, 2025， [https://www.baseten.co/blog/streaming-real-time-text-to-speech-with-xtts-v2/](https://www.baseten.co/blog/streaming-real-time-text-to-speech-with-xtts-v2/)  
46. coqui-ai/TTS: \- a deep learning toolkit for Text-to-Speech, battle-tested in research and production \- GitHub, 访问时间为 十一月 11, 2025， [https://github.com/coqui-ai/TTS](https://github.com/coqui-ai/TTS)  
47. Voice-Activated AI with Whisper \+ LLaMA: Talk to Your Model | by ..., 访问时间为 十一月 11, 2025， [https://medium.com/@kgiannopoulou4033/voice-activated-ai-with-whisper-llama-talk-to-your-model-85449748fda0](https://medium.com/@kgiannopoulou4033/voice-activated-ai-with-whisper-llama-talk-to-your-model-85449748fda0)  
48. Build your own voice assistant and run it locally: Whisper \+ Ollama \+ ..., 访问时间为 十一月 11, 2025， [https://medium.com/@vndee.huynh/build-your-own-voice-assistant-and-run-it-locally-whisper-ollama-bark-c80e6f815cba](https://medium.com/@vndee.huynh/build-your-own-voice-assistant-and-run-it-locally-whisper-ollama-bark-c80e6f815cba)  
49. 访问时间为 一月 1, 1970， [https://github.com/legion1581/unitree\_webrtc\_connect/tree/2.x.x](https://github.com/legion1581/unitree_webrtc_connect/tree/2.x.x)  
50. 访问时间为 一月 1, 1970， [https://github.com/JackWPP/unitree\_webrtc\_connect/tree/wpp](https://github.com/JackWPP/unitree_webrtc_connect/tree/wpp)  
51. 访问时间为 一月 1, 1970， [https://github.com/legion1581/go2\_webrtc\_connect/blob/main/example/main.js](https://github.com/legion1581/go2_webrtc_connect/blob/main/example/main.js)