# 🤖 Unitree Go2 WebRTC 控制系统

一个功能完整的Unitree Go2机器狗WebRTC控制系统，提供Python驱动和Web控制界面。无需越狱或固件修改，开箱即用！

> 支持 Go2 AIR/PRO/EDU 所有型号

![系统界面](./images/screenshot_1.png)

## ⚡ 快速开始

### 🌟 Web控制系统（推荐）

```bash
# 1. 安装依赖
pip install -r requirements.txt
pip install -r web_requirements.txt

# 2. 启动Web服务器
python3 web_server.py

# 3. 打开浏览器
# http://localhost:5000
```

**功能特性**:
- 🎮 多机器狗管理和控制
- 📹 实时视频流显示（WebRTC → MJPEG）
- 🎥 视频录制功能（MP4格式）
- 🕹️ 自动/手动运动控制
- 📊 实时状态监控和日志
- 🌐 网络设备自动扫描
- 📱 响应式设计，支持手机访问

**详细文档**: [📖 Web控制系统完整部署指南](WEB_CONTROL_SYSTEM_GUIDE.md)

### 🐍 Python驱动（编程接口）

这是原始的Python WebRTC驱动库，提供底层API接口。

## Supported Versions

The currently supported firmware packages are:
- 1.1.1 - 1.1.7 (latest available)
- 1.0.19 - 1.0.25

## Audio and Video Support

There are video (recvonly) and audio (sendrecv) channels in WebRTC that you can connect to. Check out the examples in the `/example` folder.

## Lidar support

There is a lidar decoder built in, so you can handle decoded PoinClouds directly. Check out the examples in the `/example` folder.

## Connection Methods

The driver supports three types of connection methods:

1. **AP Mode**: Go2 is in AP mode, and the WebRTC client is connected directly to it:

    ```python
    Go2WebRTCConnection(WebRTCConnectionMethod.LocalAP)
    ```

2. **STA-L Mode**: Go2 and the WebRTC client are on the same local network. An IP or Serial number is required:

    ```python
    Go2WebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip="192.168.8.181")
    ```


    If the IP is unknown, you can specify only the serial number, and the driver will try to find the IP using the special Multicast discovery feature available on Go2:

    ```python
    Go2WebRTCConnection(WebRTCConnectionMethod.LocalSTA, serialNumber="B42D2000XXXXXXXX")
    ```

3. **STA-T mode**: Remote connection through remote Unitrees TURN server. Could control your Go2 even being on the diffrent network. Requires username and pass from Unitree account

    ```python
    Go2WebRTCConnection(WebRTCConnectionMethod.Remote, serialNumber="B42D2000XXXXXXXX", username="email@gmail.com", password="pass")
    ```

## Multicast scanner
The driver has a built-in Multicast scanner to find the Unitree Go2 on the local network and connect using only the serial number.


## 📦 安装部署

### Web控制系统安装

```bash
# 克隆仓库
git clone --recurse-submodules https://github.com/JackWPP/unitree_webrtc_connect.git
cd unitree_webrtc_connect
git checkout wpp  # 切换到Web控制分支

# 系统依赖
sudo apt update
sudo apt install python3-pip portaudio19-dev

# Python依赖
pip install -r requirements.txt
pip install -r web_requirements.txt

# 启动服务器
python3 web_server.py
```

**完整部署指南**: [WEB_CONTROL_SYSTEM_GUIDE.md](WEB_CONTROL_SYSTEM_GUIDE.md)

### Python驱动安装

```bash
cd ~
sudo apt update
sudo apt install python3-pip portaudio19-dev
git clone --recurse-submodules https://github.com/legion1581/go2_webrtc_connect.git
cd go2_webrtc_connect
pip install -e .
```

## 📚 文档导航

### 🚀 快速开始
- [Web控制系统完整部署指南](WEB_CONTROL_SYSTEM_GUIDE.md) - 从零开始部署Web系统
- [视频录制快速开始](VIDEO_RECORDING_QUICKSTART.md) - 30秒上手视频录制

### 📖 功能文档
- [视频录制使用说明](VIDEO_RECORDING_README.md) - 详细的录制功能说明
- [视频流使用说明](VIDEO_STREAM_USAGE.md) - 视频流功能介绍
- [Web界面功能](WEB_INTERFACE_README.md) - Web界面完整功能

### 🔧 技术文档
- [视频录制实现细节](VIDEO_RECORDING_IMPLEMENTATION.md) - 技术实现和架构
- [完整修复总结](COMPLETE_FIX_SUMMARY.md) - 所有bug修复记录
- [事件循环修复](EVENTLOOP_FIX.md) - 异步线程问题解决

### 🧪 测试脚本
- `test_video_recording.sh` - 视频录制功能测试
- `test_complete_fix.sh` - 完整系统测试
- `test_video_stream.py` - 视频流测试

## 💡 使用示例

### Web控制系统
```bash
# 启动服务器
python3 web_server.py

# 浏览器访问
# http://localhost:5000
# 或 http://你的IP:5000

# 使用流程:
# 1. 添加机器狗（或使用网络扫描）
# 2. 连接机器狗
# 3. 开启摄像头查看视频
# 4. 开始录制或控制运动
```

### Python编程接口
示例程序位于 `/examples` 目录。

## 🌟 主要特性

### Web控制系统（v2.3.0）

- ✅ **多机器狗管理**: 同时控制多个Go2机器狗
- ✅ **实时视频流**: WebRTC转MJPEG，低延迟显示
- ✅ **视频录制**: 一键录制，自动保存MP4文件
- ✅ **运动控制**: 自动模式（正方形、舞蹈）+ 手动遥控
- ✅ **状态监控**: 实时连接状态、心跳检测、系统日志
- ✅ **网络扫描**: 自动发现局域网中的Unitree设备
- ✅ **响应式UI**: 支持桌面和移动设备

### Python驱动

- ✅ **WebRTC通信**: 与Go2建立WebRTC连接
- ✅ **视频/音频**: 接收视频流，发送音频
- ✅ **LiDAR支持**: 解码点云数据
- ✅ **多种连接方式**: AP模式、STA-L模式、STA-T远程模式
- ✅ **Multicast扫描**: 自动发现设备

## 🎯 项目结构

```
go2_webrtc_connect/
├── web_server.py              # Web服务器（新功能）
├── dual_dog_controller.py     # 双机器狗控制器
├── dual_dog_movement.py       # 运动控制模块
├── templates/index.html       # Web界面
├── static/                    # 静态资源（CSS/JS）
├── go2_webrtc_driver/        # WebRTC驱动核心
├── examples/                  # 示例程序
├── recordings/               # 视频录制文件
├── logs/                     # 系统日志
└── docs/                     # 文档（各种README）
```

## 📊 版本历史

### v2.3.0 (2025-10-16) - 最新版本
- ✨ 新增视频录制功能
- ✨ 录制状态实时显示
- ✨ 自动文件管理
- 📝 完整文档和测试脚本

### v2.2.0 (2025-10-15)
- ✨ Web控制系统上线
- ✨ 实时视频流显示
- 🐛 修复状态同步问题
- 🐛 修复事件循环线程问题

### v1.0.0
- 🎉 初始版本：Python WebRTC驱动

## ❓ 常见问题

**Q: 如何快速开始？**  
A: 查看 [WEB_CONTROL_SYSTEM_GUIDE.md](WEB_CONTROL_SYSTEM_GUIDE.md)，从零开始的完整部署指南。

**Q: 视频录制文件在哪里？**  
A: 保存在 `recordings/` 目录，格式为 `{机器狗名}_{时间戳}.mp4`

**Q: 支持远程访问吗？**  
A: 支持，但需要公网IP或内网穿透。详见部署指南。

**Q: 可以同时控制多个机器狗吗？**  
A: 可以！系统支持多机器狗独立管理和控制。

**更多问题**: 查看 [WEB_CONTROL_SYSTEM_GUIDE.md](WEB_CONTROL_SYSTEM_GUIDE.md) 的FAQ部分

## 🔗 相关链接

- **主分支**: [legion1581/go2_webrtc_connect](https://github.com/legion1581/go2_webrtc_connect)
- **Web控制分支**: [JackWPP/unitree_webrtc_connect (wpp)](https://github.com/JackWPP/unitree_webrtc_connect/tree/wpp)
- **文档中心**: 项目根目录下的各个Markdown文件

## 🙏 致谢

感谢所有为本项目做出贡献的人！

- **TheRoboVerse** 社区: [TheRoboVerse](https://theroboverse.com)
- **tfoldi** 的 [WebRTC项目](https://github.com/tfoldi/go2-webrtc)
- **abizovnuralem** 添加LiDAR支持
- **MrRobotow** 提供LiDAR绘图示例

### 支持项目

如果你喜欢这个项目，可以考虑：

<a href="https://www.buymeacoffee.com/legion1581" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

---

**License**: MIT  
**Version**: v2.3.0  
**Status**: ✅ Production Ready  
**Last Update**: 2025-10-16
