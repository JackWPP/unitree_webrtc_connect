#!/usr/bin/env python3
"""
选择性控制功能测试程序
测试同时控制和单独控制两种模式
"""

import asyncio
import logging
import time
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dual_dog_controller import DualDogController, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern


class SelectiveControlTester:
    """选择性控制测试类"""
    
    def __init__(self):
        # 设置日志
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger("SelectiveControlTester")
        
        # 创建控制器
        self.controller = DualDogController(self.logger)
        self.movement = DualDogMovement(self.controller, self.logger)
        
        # 机器狗配置
        self.dogs_config = [
            {"name": "Dog1", "ip": "192.168.31.245"},
            # {"name": "Dog2", "ip": "192.168.31.246"},  # 如果有第二台机器狗，取消注释
        ]
    
    async def setup_dogs(self):
        """设置机器狗连接"""
        self.logger.info("🔧 开始设置机器狗连接...")
        
        # 添加机器狗
        for dog_config in self.dogs_config:
            self.controller.add_dog(
                dog_config["name"],
                dog_config["ip"],
                connection_method=WebRTCConnectionMethod.LocalSTA
            )
        
        # 启动监控
        await self.controller.start_monitoring()
        
        # 连接所有机器狗
        results = await self.controller.connect_all()
        
        connected_count = sum(results.values())
        if connected_count == 0:
            self.logger.error("❌ 没有机器狗连接成功")
            return False
        
        self.logger.info(f"✅ {connected_count}/{len(self.dogs_config)} 台机器狗连接成功")
        
        # 等待连接稳定
        await asyncio.sleep(2)
        return True
    
    async def test_all_mode(self):
        """测试同时控制所有机器狗模式"""
        self.logger.info("\n🎯 === 测试同时控制模式 ===")
        
        # 设置为全部控制模式
        self.controller.set_control_mode("all")
        
        # 启动手动控制模式
        await self.movement.start_pattern(MovementPattern.MANUAL_CONTROL)
        await asyncio.sleep(1)
        
        # 测试各种动作
        self.logger.info("📍 测试同时控制 - 前进")
        await self.movement.move_forward(speed=0.3, duration=1.5)
        await asyncio.sleep(1)
        
        self.logger.info("📍 测试同时控制 - 左转")
        await self.movement.turn_left(speed=0.8, duration=1.0)
        await asyncio.sleep(1)
        
        self.logger.info("📍 测试同时控制 - 坐下")
        await self.movement.sit_down()
        await asyncio.sleep(2)
        
        self.logger.info("📍 测试同时控制 - 站立")
        await self.movement.stand_up()
        await asyncio.sleep(1)
        
        self.logger.info("📍 测试同时控制 - 打招呼")
        await self.movement.say_hello()
        await asyncio.sleep(3)
        
        await self.movement.stop_movement()
        self.logger.info("✅ 同时控制模式测试完成")
    
    async def test_single_mode(self):
        """测试单独控制模式"""
        self.logger.info("\n🎯 === 测试单独控制模式 ===")
        
        # 获取可用的机器狗列表
        connected_dogs = self.controller._get_all_connected_dogs()
        if not connected_dogs:
            self.logger.error("❌ 没有连接的机器狗可测试")
            return
        
        for dog_name in connected_dogs:
            self.logger.info(f"\n🐕 测试单独控制机器狗: {dog_name}")
            
            # 设置为单独控制模式
            self.controller.set_control_mode("single", [dog_name])
            
            # 启动手动控制模式
            await self.movement.start_pattern(MovementPattern.MANUAL_CONTROL)
            await asyncio.sleep(1)
            
            # 测试单独控制动作
            self.logger.info(f"📍 {dog_name} - 前进")
            await self.movement.move_forward_single(dog_name, speed=0.3, duration=1.0)
            await asyncio.sleep(1)
            
            self.logger.info(f"📍 {dog_name} - 右转")
            await self.movement.turn_right_single(dog_name, speed=0.8, duration=1.0)
            await asyncio.sleep(1)
            
            self.logger.info(f"📍 {dog_name} - 坐下")
            await self.movement.sit_down_single(dog_name)
            await asyncio.sleep(2)
            
            self.logger.info(f"📍 {dog_name} - 站立")
            await self.movement.stand_up_single(dog_name)
            await asyncio.sleep(1)
            
            self.logger.info(f"📍 {dog_name} - 打招呼")
            await self.movement.say_hello_single(dog_name)
            await asyncio.sleep(3)
            
            await self.movement.stop_movement()
            self.logger.info(f"✅ {dog_name} 单独控制测试完成")
    
    async def test_mixed_control(self):
        """测试混合控制模式"""
        self.logger.info("\n🎯 === 测试混合控制模式 ===")
        
        connected_dogs = self.controller._get_all_connected_dogs()
        if len(connected_dogs) < 2:
            self.logger.info("📝 只有一台机器狗，跳过混合控制测试")
            return
        
        # 先让所有机器狗站立
        self.controller.set_control_mode("all")
        await self.movement.start_pattern(MovementPattern.MANUAL_CONTROL)
        await self.movement.stand_up()
        await asyncio.sleep(2)
        
        # 让第一台机器狗坐下
        self.logger.info(f"📍 让 {connected_dogs[0]} 坐下")
        await self.movement.sit_down_single(connected_dogs[0])
        await asyncio.sleep(2)
        
        # 让第二台机器狗打招呼
        self.logger.info(f"📍 让 {connected_dogs[1]} 打招呼")
        await self.movement.say_hello_single(connected_dogs[1])
        await asyncio.sleep(3)
        
        # 所有机器狗一起站立
        self.logger.info("📍 所有机器狗一起站立")
        self.controller.set_control_mode("all")
        await self.movement.stand_up()
        await asyncio.sleep(2)
        
        await self.movement.stop_movement()
        self.logger.info("✅ 混合控制模式测试完成")
    
    async def test_square_walk_with_selective_control(self):
        """测试选择性控制下的正方形走路"""
        self.logger.info("\n🎯 === 测试选择性正方形走路 ===")
        
        connected_dogs = self.controller._get_all_connected_dogs()
        
        # 如果有多台机器狗，让第一台做正方形走路
        if len(connected_dogs) >= 2:
            self.logger.info(f"📍 让 {connected_dogs[0]} 执行正方形走路")
            self.controller.set_control_mode("single", [connected_dogs[0]])
        else:
            self.logger.info("📍 单台机器狗执行正方形走路")
            self.controller.set_control_mode("all")
        
        # 启动正方形走路（缩短版本）
        await self.movement.start_pattern(MovementPattern.SQUARE_WALK)
        
        # 运行10秒
        self.logger.info("📍 正方形走路运行中... (10秒)")
        await asyncio.sleep(10)
        
        # 停止运动
        await self.movement.stop_movement()
        self.logger.info("✅ 选择性正方形走路测试完成")
    
    async def cleanup(self):
        """清理资源"""
        self.logger.info("\n🧹 开始清理资源...")
        
        # 停止所有运动
        await self.movement.stop_movement()
        
        # 断开所有连接
        await self.controller.disconnect_all()
        
        # 停止监控
        await self.controller.stop_monitoring()
        
        self.logger.info("✅ 清理完成")
    
    async def run_all_tests(self):
        """运行所有测试"""
        try:
            self.logger.info("🚀 开始选择性控制功能测试")
            
            # 设置机器狗连接
            if not await self.setup_dogs():
                return
            
            # 运行各种测试
            await self.test_all_mode()
            await asyncio.sleep(2)
            
            await self.test_single_mode()
            await asyncio.sleep(2)
            
            await self.test_mixed_control()
            await asyncio.sleep(2)
            
            await self.test_square_walk_with_selective_control()
            
            self.logger.info("\n🎉 所有测试完成！选择性控制功能工作正常")
            
        except Exception as e:
            self.logger.error(f"❌ 测试过程中出错: {e}")
        finally:
            await self.cleanup()


async def main():
    """主函数"""
    tester = SelectiveControlTester()
    await tester.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())