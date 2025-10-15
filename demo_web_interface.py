#!/usr/bin/env python3
"""
Web界面快速演示脚本
"""

import webbrowser
import time
import subprocess
import sys

def get_wsl_ip():
    """获取WSL IP地址"""
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        ip = result.stdout.strip().split()[0]
        return ip
    except:
        return "127.0.0.1"

def show_demo_info():
    """显示演示信息"""
    wsl_ip = get_wsl_ip()
    
    print("🎉 双机器狗Web控制界面已启动！")
    print("=" * 50)
    print(f"🌐 WSL内访问地址: http://localhost:5000")
    print(f"🌐 Windows访问地址: http://{wsl_ip}:5000")
    print(f"🌐 局域网访问地址: http://{wsl_ip}:5000")
    print()
    print("✨ 主要功能:")
    print("  📱 现代化Web界面 - 告别tkinter远古界面")
    print("  🎮 实时控制 - 支持同时控制和单独控制")
    print("  🚀 自动运动 - 正方形走路、舞蹈派对、扑跃控制")
    print("  🎯 手动遥控 - 方向控制、键盘快捷键")
    print("  🤸 姿态控制 - 基础姿态、特技动作、高难度动作")
    print("  📊 实时状态 - WebSocket实时推送状态更新")
    print()
    print("⌨️  键盘快捷键:")
    print("  W/A/S/D - 方向控制")
    print("  Q/E - 旋转控制")
    print("  空格 - 停止移动")
    print("  ESC - 紧急停止")
    print()
    print("🔧 使用步骤:")
    print("  1. 在Windows浏览器中访问上述地址")
    print("  2. 在左侧添加机器狗配置")
    print("  3. 点击'连接所有机器狗'")
    print("  4. 选择控制模式开始控制")
    print()
    print("💡 提示: 这个Web界面可以在任何设备的浏览器中访问!")
    print("=" * 50)

if __name__ == "__main__":
    show_demo_info()
    
    # 如果在Windows子系统中，尝试打开浏览器
    wsl_ip = get_wsl_ip()
    try:
        print(f"🔄 尝试自动打开浏览器...")
        webbrowser.open(f"http://{wsl_ip}:5000")
        print("✅ 浏览器已打开")
    except:
        print("❌ 无法自动打开浏览器，请手动访问上述地址")