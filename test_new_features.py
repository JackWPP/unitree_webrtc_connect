#!/usr/bin/env python3
"""
测试新增功能：网络扫描和键盘控制
"""

import asyncio
import logging
from network_scanner import NetworkScanner
from keyboard_controller import KeyboardController, KeyAction

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_network_scanner():
    """测试网络扫描功能"""
    print("=== 测试网络扫描功能 ===")
    
    scanner = NetworkScanner()
    
    # 测试单次扫描
    print("执行网络扫描...")
    devices = await scanner.scan_once(timeout=3.0)
    
    if devices:
        print(f"发现 {len(devices)} 台Unitree设备:")
        for device in devices:
            print(f"  - {device.device_name}: {device.ip} (序列号: {device.serial_number})")
    else:
        print("未发现任何Unitree设备")
    
    print("网络扫描测试完成\n")

def test_keyboard_controller():
    """测试键盘控制功能"""
    print("=== 测试键盘控制功能 ===")
    
    controller = KeyboardController()
    
    # 设置回调函数
    def mock_movement_callback(action_name):
        print(f"执行动作: {action_name}")
    
    # 设置各种动作回调
    controller.set_movement_callback(KeyAction.MOVE_FORWARD, 
                                   lambda: mock_movement_callback("前进"))
    controller.set_movement_callback(KeyAction.MOVE_BACKWARD, 
                                   lambda: mock_movement_callback("后退"))
    controller.set_movement_callback(KeyAction.MOVE_LEFT, 
                                   lambda: mock_movement_callback("左移"))
    controller.set_movement_callback(KeyAction.MOVE_RIGHT, 
                                   lambda: mock_movement_callback("右移"))
    controller.set_movement_callback(KeyAction.TURN_LEFT, 
                                   lambda: mock_movement_callback("左转"))
    controller.set_movement_callback(KeyAction.TURN_RIGHT, 
                                   lambda: mock_movement_callback("右转"))
    controller.set_movement_callback(KeyAction.STOP, 
                                   lambda: mock_movement_callback("停止"))
    controller.set_movement_callback(KeyAction.SIT, 
                                   lambda: mock_movement_callback("坐下"))
    controller.set_movement_callback(KeyAction.STAND, 
                                   lambda: mock_movement_callback("站立"))
    controller.set_movement_callback(KeyAction.HELLO, 
                                   lambda: mock_movement_callback("打招呼"))
    
    # 启用键盘控制
    controller.enable()
    
    # 模拟按键
    print("模拟按键操作:")
    
    # 测试移动按键
    print("测试前进 (W键)")
    controller.on_key_press('w')
    controller.on_key_release('w')
    
    print("测试后退 (S键)")
    controller.on_key_press('s')
    controller.on_key_release('s')
    
    print("测试左移 (A键)")
    controller.on_key_press('a')
    controller.on_key_release('a')
    
    print("测试右移 (D键)")
    controller.on_key_press('d')
    controller.on_key_release('d')
    
    # 测试动作按键
    print("测试坐下 (X键)")
    controller.on_key_press('x')
    
    print("测试站立 (Z键)")
    controller.on_key_press('z')
    
    print("测试打招呼 (H键)")
    controller.on_key_press('h')
    
    # 显示键盘映射
    print("\n键盘映射说明:")
    print(controller.get_key_mappings_description())
    
    # 禁用键盘控制
    controller.disable()
    print("\n键盘控制测试完成")

async def main():
    """主测试函数"""
    print("双机器狗控制系统新功能测试")
    print("=" * 50)
    
    # 测试网络扫描
    await test_network_scanner()
    
    # 测试键盘控制
    test_keyboard_controller()
    
    print("\n=== 新功能说明 ===")
    print("1. 网络扫描功能:")
    print("   - 自动发现网络中的Unitree机器狗")
    print("   - 支持单次扫描和连续扫描")
    print("   - 显示设备名称、IP地址和序列号")
    print("   - 双击设备可自动填充连接配置")
    
    print("\n2. 键盘控制功能:")
    print("   - 支持WASD控制机器狗移动")
    print("   - W/S: 前进/后退")
    print("   - A/D: 左移/右移")
    print("   - Q/E: 左转/右转")
    print("   - Space: 停止")
    print("   - Z/X: 站立/坐下")
    print("   - H: 打招呼")
    print("   - 支持同时控制和单独控制模式")
    print("   - 实时响应，20Hz更新频率")
    
    print("\n=== 使用方法 ===")
    print("1. 启动增强版GUI:")
    print("   python dual_dog_gui_enhanced.py")
    print()
    print("2. 使用网络扫描:")
    print("   - 点击'🔍 扫描网络'按钮")
    print("   - 勾选'自动扫描'进行连续扫描")
    print("   - 双击发现的设备自动配置连接")
    print()
    print("3. 使用键盘控制:")
    print("   - 点击'启用键盘控制'按钮")
    print("   - 确保窗口获得焦点(绿色指示器)")
    print("   - 使用WASD键控制机器狗移动")
    print("   - 配合控制模式选择单独或同时控制")

if __name__ == "__main__":
    asyncio.run(main())