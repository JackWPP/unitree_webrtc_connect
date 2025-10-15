#!/bin/bash
# 视频流功能快速启动脚本

echo "=========================================="
echo "  双机器狗Web控制 - 视频流功能测试"
echo "=========================================="
echo ""

# 检查依赖
echo "1. 检查依赖..."
if python3 -c "import cv2, numpy" 2>/dev/null; then
    echo "✅ 依赖检查通过"
else
    echo "❌ 缺少依赖，正在安装..."
    pip install opencv-python numpy
fi

echo ""

# 运行功能测试
echo "2. 运行功能测试..."
python3 test_video_stream.py

echo ""
echo "=========================================="
echo "  启动Web服务器"
echo "=========================================="
echo ""

# 获取WSL IP
if command -v hostname &> /dev/null; then
    WSL_IP=$(hostname -I | awk '{print $1}')
    echo "📍 WSL IP地址: $WSL_IP"
    echo "🌐 Windows访问地址: http://$WSL_IP:5000"
else
    echo "🌐 本地访问地址: http://localhost:5000"
fi

echo ""
echo "按Ctrl+C停止服务器..."
echo ""

# 启动Web服务器
python3 web_server.py --host 0.0.0.0 --port 5000
