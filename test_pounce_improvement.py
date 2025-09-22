#!/usr/bin/env python3
"""
测试扑跃动作改进功能
- 单次扑跃
- 连续三次扑跃
"""

import asyncio
import logging
from dual_dog_controller import DualDogController, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def test_pounce_improvements():
    """测试扑跃动作改进功能"""
    print("=== 扑跃动作改进功能测试 ===")
    
    # 创建控制器和运动模块
    controller = DualDogController()
    movement = DualDogMovement(controller)
    
    # 添加机器狗（使用用户的配置）
    controller.add_dog(
        "Dog1",
        "192.168.31.245", 
        connection_method=WebRTCConnectionMethod.LocalSTA
    )
    
    # 启动监控
    await controller.start_monitoring()
    
    print("自动运动模式改进:")
    print("✅ 简化为只保留扑跃动作")
    print("✅ 新增单次扑跃按钮")
    print("✅ 新增连续三次扑跃按钮")
    print()
    
    # 连接机器狗
    print("正在连接机器狗...")
    await controller.connect_all()
    
    # 等待连接稳定
    await asyncio.sleep(2)
    
    # 测试单次扑跃
    print("\n=== 测试单次扑跃功能 ===")
    print("执行单次扑跃动作...")
    await movement.front_pounce()
    await asyncio.sleep(4)  # 等待动作完成
    
    # 测试连续三次扑跃
    print("\n=== 测试连续三次扑跃功能 ===")
    print("执行连续三次扑跃动作...")
    
    for i in range(3):
        print(f"执行第 {i+1} 次扑跃动作")
        await movement.front_pounce()
        
        # 在两次动作之间稍停片刻，等待动作完成
        if i < 2:  # 最后一次不需要等待
            print(f"等待第 {i+1} 次动作完成...")
            await asyncio.sleep(3.0)  # 等待3秒让动作完成
    
    print("连续三次扑跃动作完成")
    
    # 断开连接
    print("\n断开连接...")
    await controller.disconnect_all()
    await controller.stop_monitoring()
    
    print("\n=== 改进功能说明 ===")
    print("界面改进:")
    print("• 自动运动模式标题: '自动运动模式 - 扑跃动作'")
    print("• 🦘 扑跃一次: 执行单次扑跃动作")
    print("• 🦘🦘🦘 扑跃三次: 连续执行三次扑跃动作")
    print("• ⏹ 停止: 停止自动运动")
    print()
    print("功能特点:")
    print("• 保持原有的选择性控制支持")
    print("• 单次扑跃保留现有逻辑")
    print("• 连续三次扑跃自动管理间隔时间")
    print("• 符合鲁棒性设计，支持单台和多台设备")
    print("• 完整的日志记录和状态反馈")

if __name__ == "__main__":
    asyncio.run(test_pounce_improvements())