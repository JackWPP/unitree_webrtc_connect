# 双机器狗控制系统 v1.0

## 🎯 项目简介

本项目是基于Unitree Go2 WebRTC驱动的双机器狗同步控制系统，支持同时控制两台机器狗执行各种运动任务。系统具备完整的GUI界面、日志记录和鲁棒性保障，即使只有一台机器狗连接也能正常使用。

### ✨ 核心功能

- **双设备连接管理**：支持同时连接和控制两台机器狗
- **完整运动控制**：迁移了原[sportsmode.py](sportsmode.py)的所有功能
- **友好用户界面**：基于tkinter的直观GUI界面
- **智能运动模式**：
  - 正方形走路循环（带跳舞触发）
  - 舞蹈派对模式
  - 手动遥控模式
- **鲁棒性设计**：单机器狗也能正常使用
- **完整日志系统**：多级日志记录和自动清理
- **状态监控**：实时监控机器狗连接和运动状态

## 🏗️ 系统架构

```
双机器狗控制系统
├── dual_dog_main.py          # 主程序入口
├── dual_dog_controller.py    # 核心控制器类
├── dual_dog_movement.py      # 运动控制逻辑
├── dual_dog_gui.py          # GUI界面
├── config.json              # 配置文件
└── logs/                    # 日志目录
    ├── dual_dog_main_YYYYMMDD.log
    ├── dual_dog_errors_YYYYMMDD.log
    └── dual_dog_movement_YYYYMMDD.log
```

### 核心模块

1. **[DualDogController](dual_dog_controller.py)**：双机器狗连接和状态管理
2. **[DualDogMovement](dual_dog_movement.py)**：运动模式和指令控制
3. **[DualDogGUI](dual_dog_gui.py)**：用户界面和交互逻辑
4. **[DualDogLogger](dual_dog_main.py)**：日志管理系统

## 🚀 快速开始

### 环境要求

- **操作系统**：Linux (Ubuntu 22.04推荐)
- **Python版本**：Python 3.7+
- **依赖项**：
  ```bash
  sudo apt update
  sudo apt install python3-pip portaudio19-dev
  pip install -e .  # 安装go2_webrtc_connect包
  ```

### 安装步骤

1. **确保项目已正确安装**：
   ```bash
   cd /home/wppjkw/go2_webrtc_connect
   pip install -e .
   ```

2. **配置机器狗信息**：
   编辑[config.json](config.json)文件，设置机器狗IP地址：
   ```json
   {
     "dogs": {
       "dog1": {
         "name": "Dog1",
         "ip": "192.168.31.245",  # 修改为实际IP
         "enabled": true
       },
       "dog2": {
         "name": "Dog2",
         "ip": "192.168.31.246",  # 修改为实际IP
         "enabled": true
       }
     }
   }
   ```

3. **启动系统**：
   ```bash
   python3 dual_dog_main.py
   ```

## 📖 使用指南

### 连接机器狗

1. 确保两台机器狗都处于开机状态
2. 在GUI界面中输入正确的IP地址
3. 点击"连接所有"按钮
4. 等待连接状态变为绿色"connected"

### 运动控制模式

#### 1. 自动运动模式

- **正方形走路**：机器狗会自动执行正方形路径行走
  - 按"触发跳舞"按钮可随时插入跳舞动作
  - 跳舞完成后继续正方形走路
- **舞蹈派对**：机器狗会循环执行各种舞蹈动作

#### 2. 手动遥控模式

启用手动控制后，可使用以下按钮：
- **方向控制**：↑前进、↓后退、←左移、→右移
- **旋转控制**：⟲左转、⟳右转
- **姿态控制**：坐下、站立、打招呼
- **紧急停止**：⏹停止按钮

### 日志监控

系统提供实时日志显示，支持不同日志级别：
- **DEBUG**：详细调试信息
- **INFO**：一般运行信息
- **WARNING**：警告信息
- **ERROR**：错误信息

日志文件自动保存到`logs/`目录，支持按日期分类和自动清理。

## ⚙️ 配置说明

### 系统配置

```json
{
  "system": {
    "log_level": "INFO",           # 日志级别
    "monitor_interval": 5,         # 状态监控间隔(秒)
    "heartbeat_timeout": 30        # 心跳超时时间(秒)
  }
}
```

### 运动参数

```json
{
  "movement": {
    "default_speed": 0.5,          # 默认移动速度
    "default_turn_speed": 1.0,     # 默认转弯速度
    "square_walk_speed": 1.0,      # 正方形走路速度
    "dance_duration": 5.0          # 跳舞动作持续时间
  }
}
```

## 🛠️ 命令行选项

```bash
python3 dual_dog_main.py [OPTIONS]

选项：
  --log-level {DEBUG,INFO,WARNING,ERROR}  # 设置日志级别
  --log-dir PATH                          # 日志目录路径
  --create-shortcut                       # 创建桌面快捷方式
  --cleanup-logs DAYS                     # 清理N天前的日志
  --no-gui                               # 不启动GUI（测试模式）

示例：
  python3 dual_dog_main.py --log-level DEBUG
  python3 dual_dog_main.py --cleanup-logs 3
  python3 dual_dog_main.py --create-shortcut
```

## 🔧 高级功能

### 自定义运动序列

可以通过编程方式添加自定义运动模式：

```python
from dual_dog_movement import DualDogMovement, MovementPattern

# 创建自定义运动序列
async def custom_dance_sequence(movement: DualDogMovement):
    await movement.send_manual_command("Hello")
    await asyncio.sleep(2)
    await movement.send_manual_command("Dance1")
    await asyncio.sleep(5)
    await movement.send_manual_command("WiggleHips")
```

### 状态回调

系统支持自定义状态变化回调：

```python
def my_status_callback(dog_name: str, status: DogStatus):
    print(f"机器狗 {dog_name} 状态变为: {status}")

controller.add_status_callback(my_status_callback)
```

## 🚨 故障排除

### 连接问题

1. **连接失败**：
   - 检查机器狗是否开机
   - 确认IP地址正确
   - 确保网络连通性
   - 检查是否有其他客户端占用连接

2. **心跳超时**：
   - 检查网络稳定性
   - 尝试重新连接
   - 查看错误日志获取详细信息

### 运动控制问题

1. **指令不响应**：
   - 确认机器狗处于connected状态
   - 检查运动模式是否正确切换
   - 查看运动日志了解执行情况

2. **只有一台机器狗工作**：
   - 系统设计支持单机器狗运行
   - 检查另一台机器狗的连接状态
   - 查看日志了解具体错误信息

## 📝 开发说明

### 扩展运动模式

要添加新的运动模式，需要：

1. 在[MovementPattern](dual_dog_movement.py)枚举中添加新模式
2. 在[DualDogMovement](dual_dog_movement.py)类中实现对应的循环方法
3. 在GUI中添加相应的控制按钮

### 添加新的运动指令

所有可用的运动指令定义在[constants.py](go2_webrtc_driver/constants.py)的`SPORT_CMD`字典中：

```python
SPORT_CMD = {
    "Hello": 1016,
    "Dance1": 1022,
    "Dance2": 1023,
    "Move": 1008,
    # ... 更多指令
}
```

## 🔄 版本更新

### v1.0 特性

- ✅ 双机器狗同步控制
- ✅ 完整GUI界面
- ✅ 鲁棒性设计
- ✅ 完整日志系统
- ✅ 配置文件支持
- ✅ 手动遥控功能
- ✅ 自动运动模式

### 计划功能

- 🔄 远程连接支持
- 🔄 编程模式
- 🔄 录制回放功能
- 🔄 语音控制集成

## 📞 技术支持

如有问题或建议，请：

1. 查看日志文件：`logs/dual_dog_errors_*.log`
2. 检查系统状态：运行 `python3 dual_dog_main.py --no-gui`
3. 确认原始[sportsmode.py](sportsmode.py)能否正常工作

## 📄 许可证

本项目基于原始go2_webrtc_connect项目开发，遵循相同的许可证协议。

---

**🤖 双机器狗控制系统 - 让机器狗协同工作更简单！**