#!/bin/bash
# 状态同步修复后的完整测试流程

echo "=========================================="
echo "  机器狗状态同步修复 - 完整测试"
echo "=========================================="
echo ""

# 步骤1: 运行自动化测试
echo "步骤1: 运行自动化测试..."
python3 test_status_sync.py

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ 自动化测试失败，请检查代码"
    exit 1
fi

echo ""
echo "=========================================="
echo "  准备启动Web服务器"
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
echo "=========================================="
echo "  手动测试步骤"
echo "=========================================="
echo ""
echo "1. 在浏览器中打开Web界面"
echo "2. 添加机器狗配置:"
echo "   - 名称: Dog1"
echo "   - IP: 192.168.31.245 (替换为你的IP)"
echo "   - 连接方式: LocalSTA"
echo ""
echo "3. 点击'连接所有机器狗'"
echo ""
echo "4. 观察:"
echo "   ✅ 后端日志应显示: '机器狗 Dog1 状态变化: connected'"
echo "   ✅ 前端界面应显示: Dog1 状态变为绿色的'connected'"
echo "   ✅ 摄像头选择器应显示: 'Dog1 (已连接)'"
echo ""
echo "5. 测试摄像头:"
echo "   - 在摄像头选择器中选择Dog1"
echo "   - 点击'开启摄像头'"
echo "   - 应该能看到视频流"
echo ""
echo "=========================================="
echo ""
echo "按Ctrl+C停止服务器..."
echo ""
sleep 2

# 启动Web服务器
python3 web_server.py --host 0.0.0.0 --port 5000
