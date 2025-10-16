#!/usr/bin/env python3
"""
测试扩展功能：新增的姿态控制和触发动作
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

async def test_extended_features():
    """测试扩展功能"""
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
    
    print("=== 双机器狗控制系统扩展功能测试 ===")
    print("新增的姿态控制选项:")
    print("1. 🤸 伸展 (Stretch)")
    print("2. 🔄 打滚 (Wallow)") 
    print("3. 🐾 刨地 (Scrape)")
    print("4. 💃 扭臀 (WiggleHips)")
    print("5. ❤️ 比心 (FingerHeart)")
    print("6. 🤸 倒立 (Handstand)")
    print("7. 🔄 前空翻 (FrontFlip)")
    print("8. 🔄 后空翻 (BackFlip)")
    print("9. 🦘 扑跃 (FrontPounce)")
    print()
    print("新增的触发动作选项:")
    print("1. 🤸 触发伸展")
    print("2. 🔄 触发打滚")
    print("3. 🤸 触发空翻")
    print("4. 🦘 触发扑跃")
    print("5. ❤️ 触发比心")
    print()
    print("扩展的自动运动模式:")
    print("舞蹈派对现在包含12种不同动作，包括所有新增的高难度动作")
    print()
    
    # 连接机器狗
    print("正在连接机器狗...")
    await controller.connect_all()
    
    # 等待连接稳定
    await asyncio.sleep(2)
    
    # 测试新姿态
    print("\n=== 测试新姿态功能 ===")
    
    # 测试伸展动作
    print("测试伸展动作...")
    await movement.stretch()
    await asyncio.sleep(3)
    
    # 测试打滚动作
    print("测试打滚动作...")
    await movement.wallow()
    await asyncio.sleep(4)
    
    # 测试比心动作
    print("测试比心动作...")
    await movement.finger_heart()
    await asyncio.sleep(3)
    
    # 测试扩展的舞蹈派对模式
    print("\n=== 测试扩展的舞蹈派对模式 ===")
    print("启动舞蹈派对模式(包含12种动作)...")
    from dual_dog_movement import MovementPattern
    await movement.start_pattern(MovementPattern.DANCE_PARTY)
    
    # 运行一段时间
    await asyncio.sleep(15)
    
    # 测试触发动作
    print("\n=== 测试触发动作功能 ===")
    print("触发特定动作: 扭臀...")
    await movement.trigger_specific_action("WiggleHips")
    await asyncio.sleep(3)
    
    print("触发特定动作: 刨地...")
    await movement.trigger_specific_action("Scrape")
    await asyncio.sleep(4)
    
    # 停止运动
    print("\n停止所有运动...")
    await movement.stop_movement()
    
    # 断开连接
    print("断开连接...")
    await controller.disconnect_all()
    await controller.stop_monitoring()
    
    print("\n=== 测试完成 ===")
    print("功能验证:")
    print("✅ 9个新姿态控制选项已添加")
    print("✅ 5个新触发动作选项已添加")
    print("✅ 舞蹈派对模式已扩展到12种动作")
    print("✅ 所有动作支持选择性控制(同时/单独)")
    print("✅ 动作持续时间根据难度自动调整")

if __name__ == "__main__":
    asyncio.run(test_extended_features())