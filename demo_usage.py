#!/usr/bin/env python3
"""
双机器狗控制系统使用演示
展示如何通过编程方式使用双机器狗控制系统
"""

import asyncio
import logging
import time
from dual_dog_controller import DualDogController, DogStatus, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def demo_dual_dog_control():
    """演示双机器狗控制"""
    print("=" * 60)
    print("🤖 双机器狗控制系统演示")
    print("=" * 60)
    
    # 创建控制器
    controller = DualDogController()
    movement = DualDogMovement(controller)
    
    # 添加状态回调
    def status_callback(dog_name: str, status: DogStatus):
        print(f"🔄 {dog_name} 状态变化: {status.value}")
    
    def movement_callback(pattern: MovementPattern, step_info: str):
        print(f"🎯 运动状态: {pattern.value} - {step_info}")
    
    controller.add_status_callback(status_callback)
    movement.add_movement_callback(movement_callback)
    
    try:
        # 启动监控
        await controller.start_monitoring()
        
        print("\n📋 配置机器狗...")
        # 添加机器狗（使用默认配置中的IP）
        controller.add_dog("Dog1", "192.168.31.245", connection_method=WebRTCConnectionMethod.LocalSTA)
        controller.add_dog("Dog2", "192.168.31.246", connection_method=WebRTCConnectionMethod.LocalSTA)
        
        print("\n🔗 尝试连接机器狗...")
        results = await controller.connect_all()
        
        connected_dogs = [name for name, success in results.items() if success]
        if not connected_dogs:
            print("❌ 没有机器狗连接成功，演示无法继续")
            return
        
        print(f"✅ 成功连接 {len(connected_dogs)}/{len(results)} 台机器狗: {connected_dogs}")
        
        # 等待连接稳定
        await asyncio.sleep(2)
        
        print("\n🎭 演示1: 打招呼")
        await controller.send_command_to_all("Hello")
        await asyncio.sleep(3)
        
        print("\n🕺 演示2: 跳舞表演")
        await controller.send_command_to_all("Dance1")
        await asyncio.sleep(6)
        
        print("\n🚶 演示3: 基本移动")
        print("  - 前进")
        await controller.send_command_to_all("Move", {"x": 0.5, "y": 0, "z": 0})
        await asyncio.sleep(2)
        
        print("  - 后退")
        await controller.send_command_to_all("Move", {"x": -0.5, "y": 0, "z": 0})
        await asyncio.sleep(2)
        
        print("  - 左转")
        await controller.send_command_to_all("Move", {"x": 0, "y": 0, "z": 1.0})
        await asyncio.sleep(2)
        
        print("  - 右转")
        await controller.send_command_to_all("Move", {"x": 0, "y": 0, "z": -1.0})
        await asyncio.sleep(2)
        
        print("\n⏹ 演示4: 停止移动")
        await controller.send_command_to_all("StopMove")
        await asyncio.sleep(1)
        
        print("\n🔄 演示5: 正方形走路模式 (10秒)")
        await movement.start_pattern(MovementPattern.SQUARE_WALK)
        
        # 运行正方形走路10秒
        await asyncio.sleep(10)
        
        print("\n💃 演示6: 触发跳舞")
        await movement.trigger_dance()
        await asyncio.sleep(6)
        
        print("\n🛑 停止自动运动")
        await movement.stop_movement()
        
        print("\n🎮 演示7: 手动控制模式")
        await movement.start_pattern(MovementPattern.MANUAL_CONTROL)
        
        # 手动控制演示
        manual_commands = [
            ("前进", movement.move_forward, 1.0),
            ("左移", movement.move_left, 1.0), 
            ("后退", movement.move_backward, 1.0),
            ("右移", movement.move_right, 1.0),
            ("左转", movement.turn_left, 1.0),
            ("坐下", movement.sit_down, 2.0),
            ("站立", movement.stand_up, 2.0),
            ("打招呼", movement.say_hello, 3.0)
        ]
        
        for desc, command, duration in manual_commands:
            print(f"  - {desc}")
            await command()
            await asyncio.sleep(duration)
        
        print("\n🛑 停止手动控制")
        await movement.stop_movement()
        
        print("\n👋 演示完成，执行告别动作")
        await controller.send_command_to_all("Hello")
        await asyncio.sleep(3)
        
    except Exception as e:
        print(f"❌ 演示过程中发生错误: {e}")
        logging.error(f"演示错误: {e}", exc_info=True)
    
    finally:
        print("\n🔌 断开所有连接...")
        await movement.stop_movement()
        await controller.disconnect_all()
        await controller.stop_monitoring()
        
        print("✅ 演示结束")


async def demo_single_dog_robustness():
    """演示单机器狗鲁棒性"""
    print("\n" + "=" * 60)
    print("🤖 单机器狗鲁棒性演示")
    print("=" * 60)
    
    controller = DualDogController()
    movement = DualDogMovement(controller)
    
    try:
        await controller.start_monitoring()
        
        # 只添加一台机器狗
        controller.add_dog("OnlyDog", "192.168.31.245", connection_method=WebRTCConnectionMethod.LocalSTA)
        
        print("🔗 连接单台机器狗...")
        results = await controller.connect_all()
        
        if not any(results.values()):
            print("❌ 机器狗连接失败，跳过单机器狗演示")
            return
        
        print("✅ 单机器狗连接成功")
        
        print("🕺 单机器狗跳舞演示")
        await controller.send_command_to_all("Dance1")
        await asyncio.sleep(6)
        
        print("🔄 单机器狗正方形走路演示 (5秒)")
        await movement.start_pattern(MovementPattern.SQUARE_WALK)
        await asyncio.sleep(5)
        await movement.stop_movement()
        
        print("✅ 单机器狗鲁棒性演示完成")
        
    finally:
        await movement.stop_movement()
        await controller.disconnect_all()
        await controller.stop_monitoring()


def print_usage_tips():
    """打印使用提示"""
    print("\n" + "=" * 60)
    print("📚 使用提示")
    print("=" * 60)
    print("1. 启动GUI界面：")
    print("   python3 dual_dog_main.py")
    print()
    print("2. 使用启动脚本：")
    print("   ./start_dual_dog.sh")
    print()
    print("3. 创建桌面快捷方式：")
    print("   python3 dual_dog_main.py --create-shortcut")
    print()
    print("4. 修改配置：")
    print("   编辑 config.json 文件中的IP地址")
    print()
    print("5. 查看日志：")
    print("   tail -f logs/dual_dog_main_*.log")
    print()
    print("6. 故障排除：")
    print("   - 确保机器狗开机并连接到网络")
    print("   - 检查IP地址是否正确")
    print("   - 确认原始sportsmode.py能正常工作")
    print("   - 查看错误日志：logs/dual_dog_errors_*.log")


async def main():
    """主函数"""
    print("🤖 双机器狗控制系统演示程序")
    print("注意：此演示需要实际的机器狗连接才能完整运行")
    print("如果没有机器狗，可以观察日志输出了解系统功能")
    
    # 询问用户是否继续
    try:
        user_input = input("\n是否继续演示？(y/N): ").strip().lower()
        if user_input != 'y':
            print("演示已取消")
            print_usage_tips()
            return
    except KeyboardInterrupt:
        print("\n演示已取消")
        return
    
    print("\n开始演示...")
    
    try:
        # 双机器狗演示
        await demo_dual_dog_control()
        
        # 等待用户确认
        input("\n按Enter键继续单机器狗鲁棒性演示...")
        
        # 单机器狗鲁棒性演示
        await demo_single_dog_robustness()
        
    except KeyboardInterrupt:
        print("\n用户中断演示")
    except Exception as e:
        print(f"\n演示过程中发生未预期错误: {e}")
        logging.error(f"演示主函数错误: {e}", exc_info=True)
    
    finally:
        print_usage_tips()


if __name__ == "__main__":
    asyncio.run(main())