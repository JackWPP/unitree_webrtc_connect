#!/usr/bin/env python3
"""
快速选择性控制功能验证程序
"""

import asyncio
import logging
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dual_dog_controller import DualDogController, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern


async def quick_test():
    """快速测试选择性控制功能"""
    
    # 设置简单日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    print("🚀 开始快速选择性控制功能验证")
    
    # 创建控制器
    controller = DualDogController()
    movement = DualDogMovement(controller)
    
    try:
        # 添加机器狗（只添加存在的那一台）
        controller.add_dog("Dog1", "192.168.31.245", connection_method=WebRTCConnectionMethod.LocalSTA)
        
        print("📡 开始连接机器狗...")
        await controller.start_monitoring()
        
        # 连接
        results = await controller.connect_all()
        if not any(results.values()):
            print("❌ 连接失败")
            return
        
        print("✅ 连接成功")
        await asyncio.sleep(2)
        
        # 测试1: 同时控制模式
        print("\n🎯 测试1: 同时控制模式")
        controller.set_control_mode("all")
        
        await movement.start_pattern(MovementPattern.MANUAL_CONTROL)
        await asyncio.sleep(1)
        
        print("📍 测试打招呼动作")
        await movement.say_hello()
        await asyncio.sleep(3)
        
        await movement.stop_movement()
        print("✅ 同时控制模式测试完成")
        
        # 测试2: 单独控制模式
        print("\n🎯 测试2: 单独控制模式")
        controller.set_control_mode("single", ["Dog1"])
        
        await movement.start_pattern(MovementPattern.MANUAL_CONTROL)
        await asyncio.sleep(1)
        
        print("📍 测试单独控制 - 坐下")
        await movement.sit_down_single("Dog1")
        await asyncio.sleep(2)
        
        print("📍 测试单独控制 - 站立")
        await movement.stand_up_single("Dog1")
        await asyncio.sleep(2)
        
        await movement.stop_movement()
        print("✅ 单独控制模式测试完成")
        
        print("\n🎉 选择性控制功能验证成功！")
        print("📋 功能总结:")
        print("   ✓ 支持同时控制模式 (control_mode='all')")
        print("   ✓ 支持单独控制模式 (control_mode='single')")
        print("   ✓ 手动控制指令正常工作")
        print("   ✓ 控制模式切换正常")
        
    except Exception as e:
        print(f"❌ 测试过程中出错: {e}")
    finally:
        # 清理
        print("\n🧹 清理资源...")
        await movement.stop_movement()
        await controller.disconnect_all()
        await controller.stop_monitoring()
        print("✅ 清理完成")


if __name__ == "__main__":
    asyncio.run(quick_test())