# 🤖 双机器狗Web控制系统 - 完整部署指南

## 📋 目录

- [系统概述](#系统概述)
- [环境要求](#环境要求)
- [安装部署](#安装部署)
- [快速启动](#快速启动)
- [功能使用](#功能使用)
- [高级配置](#高级配置)
- [故障排查](#故障排查)
- [API文档](#api文档)
- [开发指南](#开发指南)

---

## 🎯 系统概述

### 项目简介

双机器狗Web控制系统是一个基于Web的远程控制平台，用于管理和控制多个Unitree Go2机器狗。系统提供了友好的Web界面，支持实时视频流、运动控制、状态监控和视频录制等功能。

### 核心特性

✨ **多机器狗管理**
- 同时连接和控制多个机器狗
- 独立管理每个机器狗的状态
- 支持选择性控制（单个或全部）

📹 **实时视频流**
- WebRTC视频流转MJPEG
- 实时显示机器狗摄像头画面
- 支持多摄像头切换

🎥 **视频录制**
- 一键开始/停止录制
- 自动保存为MP4格式
- 实时显示录制状态和时长

🎮 **运动控制**
- 自动运动模式（正方形走路、舞蹈派对）
- 手动遥控（前后左右、旋转）
- 特殊动作（站立、趴下、扑跃）

📊 **状态监控**
- 实时连接状态显示
- 心跳检测和自动重连
- 详细的系统日志

🌐 **网络扫描**
- 自动扫描局域网中的Unitree设备
- 一键添加发现的设备
- 支持SN和IP显示

### 技术栈

**后端**:
- Python 3.8+
- Flask (Web框架)
- Flask-SocketIO (WebSocket支持)
- aiortc (WebRTC通信)
- OpenCV (视频处理)
- asyncio (异步编程)

**前端**:
- HTML5 + CSS3
- JavaScript (ES6+)
- Bootstrap 5 (UI框架)
- Socket.IO (实时通信)
- Font Awesome (图标)

**通信协议**:
- WebRTC (视频流)
- WebSocket (实时状态)
- HTTP REST API (控制接口)

---

## 💻 环境要求

### 系统要求

| 组件 | 最低要求 | 推荐配置 |
|------|---------|---------|
| 操作系统 | Ubuntu 20.04+ / WSL2 | Ubuntu 22.04 |
| Python | 3.8+ | 3.10+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 10 GB | 50 GB+ (用于视频录制) |
| 网络 | 100 Mbps | 1 Gbps |

### 软件依赖

**必需组件**:
```bash
- Python 3.8+
- pip (Python包管理器)
- git (版本控制)
- ffmpeg (视频处理，可选)
```

**Python包**:
```
Flask>=2.0.0
Flask-SocketIO>=5.0.0
Flask-CORS
python-socketio
opencv-python
numpy
aiortc
aioice
cryptography
```

### 网络要求

- 服务器和机器狗在同一局域网
- 开放端口5000 (Web服务器)
- 支持UDP通信（WebRTC需要）
- 稳定的网络连接（建议有线连接）

---

## 🚀 安装部署

### 步骤 1: 克隆项目

```bash
# 克隆仓库
git clone https://github.com/JackWPP/unitree_webrtc_connect.git
cd unitree_webrtc_connect

# 切换到开发分支（如果需要）
git checkout wpp
```

### 步骤 2: 创建虚拟环境

```bash
# 创建Python虚拟环境（推荐）
python3 -m venv venv

# 激活虚拟环境
source venv/bin/activate  # Linux/Mac
# 或
.\venv\Scripts\activate   # Windows
```

### 步骤 3: 安装依赖

```bash
# 升级pip
pip install --upgrade pip

# 安装项目依赖
pip install -r requirements.txt

# 如果没有requirements.txt，手动安装
pip install Flask Flask-SocketIO Flask-CORS python-socketio
pip install opencv-python numpy
pip install aiortc aioice cryptography
```

### 步骤 4: 安装Web依赖

```bash
# 安装Web相关依赖
pip install -r web_requirements.txt

# 或手动安装
pip install Flask==2.3.0 Flask-SocketIO==5.3.0 Flask-CORS==4.0.0
```

### 步骤 5: 验证安装

```bash
# 检查Python版本
python3 --version

# 检查依赖是否安装成功
python3 -c "import flask, flask_socketio, cv2, aiortc; print('✅ 所有依赖已安装')"

# 检查代码语法
python3 -m py_compile web_server.py
```

### 步骤 6: 创建必要目录

```bash
# 创建录制文件存储目录
mkdir -p recordings

# 创建日志目录
mkdir -p logs

# 设置权限
chmod 755 recordings logs
```

---

## ⚡ 快速启动

### 方式 1: 直接启动

```bash
# 启动Web服务器
python3 web_server.py

# 指定端口和主机
python3 web_server.py --host 0.0.0.0 --port 5000

# 启用调试模式
python3 web_server.py --debug
```

### 方式 2: 使用启动脚本

```bash
# 使用提供的启动脚本
chmod +x start_web_server.sh
./start_web_server.sh
```

### 方式 3: 后台运行

```bash
# 使用nohup后台运行
nohup python3 web_server.py > logs/web_server.log 2>&1 &

# 查看进程
ps aux | grep web_server.py

# 停止服务
pkill -f web_server.py
```

### 方式 4: 使用systemd服务

创建服务文件 `/etc/systemd/system/dual-dog-web.service`:

```ini
[Unit]
Description=Dual Dog Web Control System
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/go2_webrtc_connect
ExecStart=/usr/bin/python3 web_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务:
```bash
sudo systemctl daemon-reload
sudo systemctl start dual-dog-web
sudo systemctl enable dual-dog-web
sudo systemctl status dual-dog-web
```

### 访问Web界面

启动成功后，在浏览器访问：

```
http://localhost:5000           # 本地访问
http://192.168.1.100:5000      # 局域网访问（替换为实际IP）
http://0.0.0.0:5000            # 监听所有接口
```

---

## 📖 功能使用

### 1️⃣ 连接机器狗

#### 方法A: 手动添加

1. 在左侧"机器狗管理"面板
2. 输入机器狗名称（如：`1212`）
3. 输入IP地址（如：`192.168.123.161`）
4. 选择连接方式：
   - `LocalSTA` - 本地STA模式（推荐）
   - `LocalAP` - 本地AP模式
5. 点击"添加机器狗"

#### 方法B: 网络扫描

1. 点击"扫描网络中Unitree设备"
2. 等待扫描完成（约5-10秒）
3. 在扫描结果中勾选要添加的设备
4. 点击"添加选中设备"

#### 连接所有机器狗

点击"连接所有机器狗"按钮，系统会：
- 自动连接所有已添加的机器狗
- 建立WebRTC连接
- 开始心跳检测
- 更新连接状态

### 2️⃣ 查看实时视频

#### 开启摄像头

1. 确保机器狗已连接（状态显示为"已连接"或"空闲"）
2. 在右侧"实时摄像头"面板
3. 从下拉菜单选择机器狗
4. 点击"开启摄像头"按钮
5. 等待视频流加载（2-5秒）

#### 视频流说明

- **格式**: MJPEG流
- **帧率**: 约20-30 fps
- **延迟**: 约300-500毫秒
- **分辨率**: 根据机器狗摄像头自动调整

#### 切换摄像头

- 在摄像头选择下拉菜单中选择其他机器狗
- 系统会自动关闭当前摄像头并开启新的

#### 关闭摄像头

点击"关闭摄像头"按钮，视频流会停止。

### 3️⃣ 录制视频

#### 开始录制

1. 确保摄像头已开启
2. 点击"开始录制"按钮（🔴 红色圆点）
3. 录制指示器显示在视频左上角
4. 实时显示录制时长

#### 录制中

- 左上角显示：`🔴 录制中 00:25`
- 下方显示文件名：`正在录制: 1212_20251016_143025.mp4`
- 红点闪烁动画

#### 停止录制

1. 点击"停止录制"按钮（⏹️）
2. 视频自动保存到 `recordings/` 目录
3. 显示录制信息：文件名、时长、帧数

#### 查看录制文件

```bash
# 列出所有录制
ls -lh recordings/

# 播放视频
vlc recordings/1212_20251016_143025.mp4
ffplay recordings/1212_20251016_143025.mp4
```

### 4️⃣ 运动控制

#### 控制模式

**全部控制模式**（默认）:
- 所有命令发送给所有机器狗
- 适合同步动作

**单个控制模式**:
- 选择特定机器狗
- 只控制选中的机器狗

切换方式：在控制模式区域选择单选按钮

#### 自动运动模式

**正方形走路**:
1. 点击"开始正方形走路"
2. 机器狗会自动走出正方形路径
3. 每边长度约1米

**舞蹈派对**:
1. 点击"开始舞蹈派对"
2. 机器狗执行一系列舞蹈动作
3. 包含转圈、摇摆等动作

**停止运动**:
- 点击"停止自动运动"正常停止
- 点击"紧急停止"立即停止所有动作

#### 扑跃动作

**扑跃一次**:
- 机器狗执行一次向前扑跃动作

**扑跃三次**:
- 连续执行三次扑跃
- 每次间隔3秒

#### 手动遥控

1. 点击"启用手动控制"
2. 使用方向按钮控制：
   - ⬆️ 前进
   - ⬇️ 后退
   - ⬅️ 左移
   - ➡️ 右移
   - 🔄 左转
   - 🔄 右转
   - ⏹️ 停止

3. 按住按钮持续移动，松开停止

#### 快捷动作

侧边栏提供快捷动作按钮：
- 🐕 站立
- 😴 趴下
- 🤝 握手
- 💃 跳舞
- 🎉 欢呼
- 🌊 摇摆

### 5️⃣ 系统日志

#### 查看日志

- 右侧"系统日志"面板实时显示
- 包含所有操作记录和错误信息
- 按时间倒序排列

#### 日志级别

可通过下拉菜单筛选：
- `所有日志` - 显示全部
- `错误` - 只显示错误
- `警告` - 显示警告和错误
- `信息` - 显示信息、警告、错误
- `调试` - 显示所有级别

#### 日志操作

- 点击"刷新"手动刷新日志
- 点击"清空"清除所有日志
- 日志自动滚动到最新

#### 日志文件

系统日志也会保存到文件：
```bash
logs/dual_dog_main_20251016.log
logs/dual_dog_errors_20251016.log
```

---

## ⚙️ 高级配置

### 配置文件

创建 `config.json` 配置文件：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 5000,
    "debug": false
  },
  "dogs": [
    {
      "name": "1212",
      "ip": "192.168.123.161",
      "connection_method": "LocalSTA"
    },
    {
      "name": "Dog_B",
      "ip": "192.168.123.162",
      "connection_method": "LocalSTA"
    }
  ],
  "video": {
    "fps": 30,
    "quality": 85,
    "resolution": "auto"
  },
  "recording": {
    "directory": "recordings",
    "format": "mp4",
    "codec": "mp4v"
  }
}
```

### 环境变量

```bash
# 设置服务器端口
export FLASK_PORT=5000

# 设置调试模式
export FLASK_DEBUG=true

# 设置日志级别
export LOG_LEVEL=INFO
```

### 性能优化

#### 视频流优化

编辑 `web_server.py`:

```python
# 调整JPEG质量（行~580）
cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])  # 降低质量减少带宽

# 调整队列大小（行~620）
Queue(maxsize=5)  # 减少缓冲降低延迟

# 限制帧率
await asyncio.sleep(0.033)  # 约30fps
```

#### 并发连接优化

```python
# Flask-SocketIO配置
self.socketio = SocketIO(
    self.app, 
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25
)
```

### 安全配置

#### 启用HTTPS

1. 生成SSL证书：
```bash
openssl req -x509 -newkey rsa:4096 -nodes -out cert.pem -keyout key.pem -days 365
```

2. 修改启动代码：
```python
self.socketio.run(
    self.app,
    host=self.host,
    port=self.port,
    ssl_context=('cert.pem', 'key.pem')
)
```

#### 添加身份验证

创建 `auth.py`:
```python
from functools import wraps
from flask import request, jsonify

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization')
        if not auth or not check_auth(auth):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated
```

---

## 🔧 故障排查

### 常见问题

#### 1. 无法启动服务器

**症状**: `python3 web_server.py` 报错

**可能原因**:
- 缺少依赖包
- 端口被占用
- Python版本不兼容

**解决方法**:
```bash
# 检查依赖
pip install -r requirements.txt

# 检查端口占用
lsof -i :5000
# 或
netstat -tulpn | grep 5000

# 杀死占用进程
kill -9 <PID>

# 换个端口
python3 web_server.py --port 8080
```

#### 2. 无法连接机器狗

**症状**: 点击连接后状态一直显示"正在连接"

**检查清单**:
- [ ] 机器狗是否开机
- [ ] 服务器和机器狗在同一网络
- [ ] IP地址是否正确
- [ ] 防火墙是否阻止连接
- [ ] 机器狗WebRTC服务是否运行

**解决方法**:
```bash
# 测试网络连通性
ping 192.168.123.161

# 检查UDP端口
nc -u -v 192.168.123.161 8081

# 查看详细日志
tail -f logs/dual_dog_main_*.log
```

#### 3. 视频流无法显示

**症状**: 点击"开启摄像头"后没有画面

**可能原因**:
- WebRTC连接失败
- 视频通道未开启
- 网络带宽不足
- OpenCV编码问题

**解决方法**:
```bash
# 检查OpenCV安装
python3 -c "import cv2; print(cv2.__version__)"

# 查看视频相关日志
grep "视频" logs/dual_dog_main_*.log

# 测试视频编码器
python3 -c "import cv2; print(cv2.VideoWriter_fourcc(*'mp4v'))"

# 降低视频质量
# 编辑web_server.py，将JPEG质量从85降到60
```

#### 4. 录制失败

**症状**: 点击"开始录制"后提示错误

**检查清单**:
- [ ] 摄像头是否已开启
- [ ] recordings目录是否存在
- [ ] 磁盘空间是否充足
- [ ] 目录权限是否正确

**解决方法**:
```bash
# 创建目录
mkdir -p recordings

# 设置权限
chmod 755 recordings

# 检查磁盘空间
df -h

# 查看录制日志
grep "录制" logs/dual_dog_main_*.log
```

#### 5. 运动控制无响应

**症状**: 发送控制命令后机器狗不动

**可能原因**:
- 机器狗未处于运动模式
- 控制模式选择错误
- 机器狗电量不足
- 紧急停止被触发

**解决方法**:
```bash
# 检查机器狗状态
# 在Web界面查看连接状态

# 查看控制日志
grep "控制" logs/dual_dog_main_*.log

# 尝试重新连接
# 点击"断开所有连接"，再点击"连接所有机器狗"
```

### 日志分析

#### 查看实时日志

```bash
# 查看主日志
tail -f logs/dual_dog_main_*.log

# 查看错误日志
tail -f logs/dual_dog_errors_*.log

# 搜索特定关键词
grep "ERROR" logs/dual_dog_main_*.log
grep "视频" logs/dual_dog_main_*.log
```

#### 常见错误信息

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `Connection refused` | 机器狗未开启或IP错误 | 检查机器狗状态和IP |
| `No event loop` | 线程同步问题 | 已在v2.2.3修复 |
| `VideoWriter failed` | OpenCV问题 | 重新安装opencv-python |
| `Queue is full` | 视频帧积压 | 降低帧率或增加队列大小 |
| `Timeout` | 网络延迟太高 | 检查网络连接 |

### 性能问题

#### CPU使用率过高

```bash
# 查看进程CPU使用
top -p $(pgrep -f web_server.py)

# 降低视频质量
# 编辑web_server.py，调整JPEG质量和帧率

# 限制并发连接
# 只连接必要的机器狗
```

#### 内存占用过高

```bash
# 查看内存使用
ps aux | grep web_server.py

# 清理视频帧队列
# 减小队列大小（maxsize参数）

# 重启服务
pkill -f web_server.py
python3 web_server.py
```

#### 网络延迟高

```bash
# 测试网络延迟
ping -c 100 192.168.123.161

# 使用有线连接替代WiFi
# 减少同时连接的机器狗数量
# 降低视频质量和帧率
```

---

## 📡 API文档

### REST API接口

#### 连接管理

**添加机器狗**
```http
POST /api/dogs/add
Content-Type: application/json

{
  "name": "1212",
  "ip": "192.168.123.161",
  "connection_method": "LocalSTA"
}

Response: {"success": true, "dog_name": "1212"}
```

**连接所有机器狗**
```http
POST /api/dogs/connect-all

Response: {"success": true, "message": "开始连接所有机器狗"}
```

**断开连接**
```http
POST /api/dogs/disconnect-all

Response: {"success": true, "message": "已断开所有连接"}
```

#### 视频流

**开启视频**
```http
POST /api/video/start
Content-Type: application/json

{
  "dog_name": "1212"
}

Response: {
  "success": true,
  "stream_url": "/api/video/stream/1212",
  "dog_name": "1212"
}
```

**停止视频**
```http
POST /api/video/stop
Content-Type: application/json

{
  "dog_name": "1212"
}

Response: {"success": true, "dog_name": "1212"}
```

**视频流**
```http
GET /api/video/stream/<dog_name>

Response: multipart/x-mixed-replace MJPEG stream
```

#### 视频录制

**开始录制**
```http
POST /api/video/record/start
Content-Type: application/json

{
  "dog_name": "1212"
}

Response: {
  "success": true,
  "filename": "1212_20251016_143025.mp4",
  "start_time": 1697459425.123
}
```

**停止录制**
```http
POST /api/video/record/stop
Content-Type: application/json

{
  "dog_name": "1212"
}

Response: {
  "success": true,
  "filename": "1212_20251016_143025.mp4",
  "duration": 45.6,
  "frame_count": 1368
}
```

**查询录制状态**
```http
GET /api/video/record/status?dog_name=1212

Response: {
  "success": true,
  "recording": true,
  "filename": "1212_20251016_143025.mp4",
  "duration": 25.3,
  "frame_count": 759
}
```

#### 运动控制

**自动运动**
```http
POST /api/movement/start
Content-Type: application/json

{
  "pattern": "square_walk"  // 或 "dance_party"
}

Response: {"success": true, "pattern": "square_walk"}
```

**停止运动**
```http
POST /api/movement/stop

Response: {"success": true}
```

**执行扑跃**
```http
POST /api/movement/pounce
Content-Type: application/json

{
  "count": 3  // 1或3
}

Response: {"success": true, "count": 3}
```

**手动控制**
```http
POST /api/control/manual
Content-Type: application/json

{
  "action": "forward",  // forward, backward, left, right, turn_left, turn_right, stop
  "duration": 1.0       // 持续时间（秒）
}

Response: {"success": true}
```

**执行动作**
```http
POST /api/control/action
Content-Type: application/json

{
  "action": "stand"  // stand, lie, handshake, dance, cheer, sway
}

Response: {"success": true, "action": "stand"}
```

#### 系统状态

**获取状态**
```http
GET /api/status

Response: {
  "success": true,
  "dogs": {
    "1212": {
      "status": "connected",
      "ip": "192.168.123.161",
      "connected": true,
      "last_heartbeat": "2025-10-16T14:30:25"
    }
  },
  "movement": {
    "pattern": "stop",
    "running": false
  }
}
```

**获取日志**
```http
GET /api/logs?level=INFO&limit=100

Response: {
  "success": true,
  "logs": [...],
  "total": 250
}
```

#### 网络扫描

**扫描设备**
```http
POST /api/network/scan

Response: {
  "success": true,
  "devices": [
    {
      "ip": "192.168.123.161",
      "sn": "B42D2000XXXXXXXX",
      "type": "go2"
    }
  ]
}
```

### WebSocket事件

#### 客户端监听

```javascript
const socket = io();

// 连接状态
socket.on('connect', () => {
  console.log('已连接到服务器');
});

socket.on('disconnect', () => {
  console.log('已断开连接');
});

// 状态更新
socket.on('status_update', (data) => {
  console.log('状态更新:', data);
  // data.dogs - 机器狗状态
  // data.movement - 运动状态
});

// 运动更新
socket.on('movement_update', (data) => {
  console.log('运动更新:', data);
  // data.pattern - 当前模式
  // data.running - 是否运行中
});
```

#### 服务端发送

系统会自动通过WebSocket推送：
- 机器狗连接状态变化
- 运动模式变化
- 心跳更新
- 错误事件

---

## 👨‍💻 开发指南

### 项目结构

```
go2_webrtc_connect/
├── web_server.py              # Web服务器主程序
├── dual_dog_controller.py     # 机器狗控制器
├── dual_dog_movement.py       # 运动控制模块
├── network_scanner.py         # 网络扫描工具
├── keyboard_controller.py     # 键盘控制（独立使用）
│
├── templates/                 # HTML模板
│   └── index.html            # 主界面
│
├── static/                    # 静态资源
│   ├── css/
│   │   └── style.css         # 样式文件
│   └── js/
│       └── app.js            # 前端JavaScript
│
├── go2_webrtc_driver/        # WebRTC驱动
│   ├── webrtc_driver.py      # 主驱动
│   ├── webrtc_video.py       # 视频处理
│   ├── webrtc_datachannel.py # 数据通道
│   └── ...
│
├── recordings/               # 视频录制文件
├── logs/                     # 日志文件
│
├── requirements.txt          # Python依赖
├── web_requirements.txt      # Web依赖
├── setup.py                  # 安装脚本
│
└── docs/                     # 文档
    ├── VIDEO_RECORDING_README.md
    ├── VIDEO_STREAM_USAGE.md
    └── ...
```

### 添加新功能

#### 1. 添加新的API接口

在 `web_server.py` 的 `_register_routes()` 方法中：

```python
@self.app.route('/api/your/endpoint', methods=['POST'])
def your_function():
    try:
        data = request.get_json()
        # 处理逻辑
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
```

#### 2. 添加前端功能

在 `static/js/app.js` 中：

```javascript
// 添加方法到DualDogController类
async yourFunction() {
    try {
        const result = await this.apiCall('/api/your/endpoint', 'POST', {
            param: value
        });
        
        if (result.success) {
            this.showMessage('操作成功', 'success');
        }
    } catch (error) {
        this.showMessage('操作失败: ' + error.message, 'error');
    }
}
```

在 `templates/index.html` 中添加按钮：

```html
<button class="btn btn-primary" id="yourBtn">
    <i class="fas fa-icon"></i>你的功能
</button>
```

绑定事件：

```javascript
document.getElementById('yourBtn').addEventListener('click', () => {
    this.yourFunction();
});
```

#### 3. 添加新的运动模式

在 `dual_dog_movement.py` 中：

```python
async def your_movement_pattern(self):
    """你的运动模式"""
    try:
        # 实现运动逻辑
        await self.controller.send_command('forward', duration=1.0)
        await asyncio.sleep(1.0)
        # ...
        return True
    except Exception as e:
        self.logger.error(f"运动模式失败: {e}")
        return False
```

### 调试技巧

#### 启用调试模式

```bash
# 方式1: 命令行参数
python3 web_server.py --debug

# 方式2: 环境变量
export FLASK_DEBUG=1
python3 web_server.py

# 方式3: 代码中设置
server.run(debug=True)
```

#### 查看详细日志

在代码中添加调试日志：

```python
self.logger.debug(f"调试信息: {variable}")
self.logger.info(f"信息: {message}")
self.logger.warning(f"警告: {warning}")
self.logger.error(f"错误: {error}")
```

#### 浏览器调试

1. 打开浏览器开发者工具（F12）
2. Console标签查看JavaScript日志
3. Network标签查看API请求
4. WebSocket标签查看实时通信

### 测试

#### 单元测试

创建 `test_web_server.py`:

```python
import unittest
from web_server import DualDogWebServer

class TestWebServer(unittest.TestCase):
    def setUp(self):
        self.server = DualDogWebServer()
    
    def test_api_status(self):
        with self.server.app.test_client() as client:
            response = client.get('/api/status')
            self.assertEqual(response.status_code, 200)

if __name__ == '__main__':
    unittest.main()
```

#### 集成测试

使用提供的测试脚本：

```bash
# 测试视频录制功能
./test_video_recording.sh

# 测试完整功能
./test_complete_fix.sh
```

### 贡献代码

1. Fork项目
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -am 'Add some feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 创建Pull Request

---

## 📚 相关文档

### 功能文档

- [视频录制使用说明](VIDEO_RECORDING_README.md)
- [视频录制快速开始](VIDEO_RECORDING_QUICKSTART.md)
- [视频录制实现细节](VIDEO_RECORDING_IMPLEMENTATION.md)
- [视频流使用说明](VIDEO_STREAM_USAGE.md)
- [Web界面功能](WEB_INTERFACE_README.md)

### 技术文档

- [完整修复总结](COMPLETE_FIX_SUMMARY.md)
- [事件循环修复](EVENTLOOP_FIX.md)
- [状态同步修复](STATUS_SYNC_FIX_SUMMARY.md)
- [摄像头修复](CAMERA_FIX_SUMMARY.md)

### 测试脚本

- `test_video_recording.sh` - 视频录制测试
- `test_complete_fix.sh` - 完整功能测试
- `test_status_sync.py` - 状态同步测试
- `test_video_stream.py` - 视频流测试

---

## 🙋 常见问题 (FAQ)

### Q1: 支持哪些机器狗型号？
**A**: 目前支持Unitree Go2系列机器狗。理论上也兼容其他使用WebRTC通信的Unitree机器狗。

### Q2: 可以同时控制多少个机器狗？
**A**: 理论上没有限制，但实际受网络带宽和服务器性能影响。建议同时控制2-4个机器狗。

### Q3: 视频延迟有多大？
**A**: 通常在300-500毫秒之间，取决于网络质量和带宽。

### Q4: 录制的视频文件有多大？
**A**: 约50-100 MB/分钟，取决于视频分辨率和画面复杂度。

### Q5: 可以远程访问吗？
**A**: 可以，但需要：
1. 服务器有公网IP或使用内网穿透
2. 机器狗和服务器在同一网络
3. 配置防火墙规则

### Q6: 支持手机访问吗？
**A**: 支持！界面采用响应式设计，可以在手机浏览器中使用。

### Q7: 如何备份录制的视频？
**A**: 录制文件保存在 `recordings/` 目录，可以：
- 手动复制到其他位置
- 使用rsync定期同步
- 配置自动上传到云存储

### Q8: 可以自定义运动模式吗？
**A**: 可以！在 `dual_dog_movement.py` 中添加新的运动模式函数。

### Q9: 系统需要GPU吗？
**A**: 不需要。视频编码使用CPU，性能足够。

### Q10: 如何更新系统？
**A**:
```bash
git pull origin wpp
pip install -r requirements.txt --upgrade
```

---

## 🎉 开始使用

现在你已经掌握了所有必要的知识，开始体验双机器狗Web控制系统吧！

### 快速启动检查清单

- [ ] Python 3.8+ 已安装
- [ ] 依赖包已安装
- [ ] 服务器已启动
- [ ] 浏览器可以访问Web界面
- [ ] 机器狗已开机并在同一网络
- [ ] 成功连接至少一个机器狗
- [ ] 视频流正常显示
- [ ] 能够控制机器狗运动

### 获取帮助

如果遇到问题：

1. 查看本文档的[故障排查](#故障排查)部分
2. 查看 `logs/` 目录中的日志文件
3. 查阅相关功能文档
4. 在GitHub提Issue: https://github.com/JackWPP/unitree_webrtc_connect/issues

### 联系方式

- **GitHub**: https://github.com/JackWPP/unitree_webrtc_connect
- **分支**: wpp
- **文档**: 项目根目录下的各个Markdown文件

---

## 📄 许可证

本项目采用MIT许可证。详见LICENSE文件。

---

## 🌟 致谢

感谢所有为本项目做出贡献的开发者和用户！

特别感谢：
- Unitree Robotics 提供的机器狗平台
- aiortc 项目提供的WebRTC支持
- Flask 和 Socket.IO 社区

---

**版本**: v2.3.0  
**最后更新**: 2025-10-16  
**维护者**: GitHub Copilot  
**状态**: ✅ 生产就绪

祝使用愉快！🐕🤖✨
