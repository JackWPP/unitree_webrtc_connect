#!/bin/bash
# 双机器狗Web控制服务器启动脚本

echo "🚀 启动双机器狗Web控制服务器..."

# 检查端口是否被占用
PORT=5000
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null ; then
    echo "⚠️  端口 $PORT 已被占用，正在停止现有进程..."
    lsof -ti:$PORT | xargs kill -9
    sleep 2
fi

# 激活虚拟环境（如果有）
if [ -d "venv" ]; then
    echo "📦 激活虚拟环境..."
    source venv/bin/activate
fi

# 设置环境变量
export FLASK_APP=web_server.py
export FLASK_ENV=production

# 启动Web服务器
echo "🌐 启动Web服务器 (http://0.0.0.0:$PORT)"
echo "💡 可以在Windows浏览器中访问 http://WSL_IP:$PORT"
echo "   (将WSL_IP替换为你的WSL IP地址)"
echo ""
echo "🔧 服务器启动中..."

python web_server.py --host 0.0.0.0 --port $PORT

echo "✅ Web服务器已关闭"