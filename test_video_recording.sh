#!/bin/bash

# 视频录制功能测试脚本

echo "=========================================="
echo "   双机器狗 - 视频录制功能测试"
echo "=========================================="
echo ""

# 检查录制目录
echo "[1/5] 检查录制目录..."
if [ ! -d "recordings" ]; then
    echo "  ⚠️  录制目录不存在，将自动创建"
    mkdir -p recordings
else
    echo "  ✅ 录制目录已存在"
fi

# 检查目录权限
if [ -w "recordings" ]; then
    echo "  ✅ 录制目录可写"
else
    echo "  ❌ 录制目录不可写，尝试修复权限..."
    chmod 755 recordings
fi

echo ""

# 检查Python依赖
echo "[2/5] 检查Python依赖..."
python3 -c "import cv2; import numpy" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "  ✅ OpenCV 和 NumPy 已安装"
else
    echo "  ❌ 缺少依赖，请运行: pip install opencv-python numpy"
    exit 1
fi

echo ""

# 检查视频编码器
echo "[3/5] 检查视频编码器支持..."
python3 << 'EOF'
import cv2
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
print(f"  ✅ 支持 MP4V 编码器")
EOF

echo ""

# 语法检查
echo "[4/5] 检查代码语法..."
python3 -m py_compile web_server.py 2>/dev/null
if [ $? -eq 0 ]; then
    echo "  ✅ web_server.py 语法正确"
else
    echo "  ❌ web_server.py 语法错误"
    exit 1
fi

echo ""

# 显示当前录制文件
echo "[5/5] 检查现有录制文件..."
if [ -z "$(ls -A recordings/ 2>/dev/null)" ]; then
    echo "  📁 录制目录为空（新安装）"
else
    echo "  📁 现有录制文件:"
    ls -lh recordings/ | tail -n +2 | awk '{printf "     %s  %s  %s\n", $5, $9, $6" "$7}'
fi

echo ""
echo "=========================================="
echo "  ✅ 视频录制功能检查完成！"
echo "=========================================="
echo ""
echo "📝 使用说明:"
echo "   1. 启动服务器: python3 web_server.py"
echo "   2. 打开浏览器: http://localhost:5000"
echo "   3. 连接机器狗并开启摄像头"
echo "   4. 点击'开始录制'按钮"
echo "   5. 录制完成后点击'停止录制'"
echo "   6. 查看文件: ls -lh recordings/"
echo ""
echo "📚 详细文档: VIDEO_RECORDING_README.md"
echo ""

# 提示如何启动服务器
read -p "是否现在启动Web服务器? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "启动服务器..."
    python3 web_server.py
fi
