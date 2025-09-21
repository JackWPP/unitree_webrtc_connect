#!/bin/bash
# 双机器狗控制系统启动脚本

echo "========================================"
echo "🤖 双机器狗控制系统启动器 v1.0"
echo "========================================"

# 检查Python版本
python_version=$(python3 --version 2>&1)
echo "Python版本: $python_version"

# 检查项目目录
project_dir="/home/wppjkw/go2_webrtc_connect"
if [ ! -d "$project_dir" ]; then
    echo "❌ 错误: 项目目录不存在: $project_dir"
    exit 1
fi

cd "$project_dir"
echo "📁 项目目录: $(pwd)"

# 检查核心文件
required_files=(
    "dual_dog_main.py"
    "dual_dog_controller.py" 
    "dual_dog_movement.py"
    "dual_dog_gui.py"
    "config.json"
)

missing_files=()
for file in "${required_files[@]}"; do
    if [ ! -f "$file" ]; then
        missing_files+=("$file")
    fi
done

if [ ${#missing_files[@]} -ne 0 ]; then
    echo "❌ 错误: 缺少必要文件:"
    printf "   %s\n" "${missing_files[@]}"
    exit 1
fi

echo "✅ 所有核心文件检查通过"

# 检查日志目录
if [ ! -d "logs" ]; then
    mkdir -p logs
    echo "📝 创建日志目录: logs/"
fi

# 检查依赖
echo "🔍 检查依赖项..."
python3 -c "
import sys
missing = []
required = ['tkinter', 'asyncio', 'threading', 'logging', 'json', 'pathlib']
for module in required:
    try:
        __import__(module)
    except ImportError:
        missing.append(module)

try:
    from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection
    from go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD
    print('✅ Go2 WebRTC驱动模块正常')
except ImportError as e:
    missing.append('go2_webrtc_driver')
    print(f'❌ Go2 WebRTC驱动模块导入失败: {e}')

if missing:
    print(f'❌ 缺少依赖: {missing}')
    sys.exit(1)
else:
    print('✅ 所有依赖项检查通过')
"

if [ $? -ne 0 ]; then
    echo "请先安装依赖:"
    echo "  pip install -e ."
    exit 1
fi

# 显示配置信息
echo ""
echo "📋 当前配置:"
if [ -f "config.json" ]; then
    python3 -c "
import json
try:
    with open('config.json', 'r') as f:
        config = json.load(f)
    print(f'  机器狗1: {config[\"dogs\"][\"dog1\"][\"name\"]} ({config[\"dogs\"][\"dog1\"][\"ip\"]})')
    print(f'  机器狗2: {config[\"dogs\"][\"dog2\"][\"name\"]} ({config[\"dogs\"][\"dog2\"][\"ip\"]})')
    print(f'  日志级别: {config[\"system\"][\"log_level\"]}')
except Exception as e:
    print(f'  配置文件读取失败: {e}')
"
fi

echo ""
echo "🚀 启动双机器狗控制系统..."
echo "   (按Ctrl+C退出)"
echo ""

# 启动程序
python3 dual_dog_main.py "$@"

echo ""
echo "👋 双机器狗控制系统已退出"