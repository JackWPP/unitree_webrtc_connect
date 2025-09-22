#!/usr/bin/env python3
"""
双机器狗运动控制逻辑模块
包含从sportsmode.py迁移的所有运动逻辑，支持双机器狗同步执行
"""

import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional, Callable, Tuple
from dataclasses import dataclass
from enum import Enum

from dual_dog_controller import DualDogController, DogStatus


class MovementPattern(Enum):
    """运动模式枚举"""
    STOP = "stop"
    SQUARE_WALK = "square_walk"
    MANUAL_CONTROL = "manual_control"
    DANCE_PARTY = "dance_party"
    FOLLOW_LEADER = "follow_leader"


@dataclass
class MovementStep:
    """运动步骤数据类"""
    command: str
    parameters: Optional[Dict] = None
    duration: float = 2.0
    description: str = ""


class DualDogMovement:
    """双机器狗运动控制类"""
    
    def __init__(self, controller: DualDogController, logger: Optional[logging.Logger] = None):
        self.controller = controller
        self.logger = logger or logging.getLogger("DualDogMovement")
        
        self.current_pattern = MovementPattern.STOP
        self.is_running = False
        self.movement_task = None
        
        # 运动状态
        self.square_walk_step = 0
        self.dance_triggered = False
        self.manual_command_queue = asyncio.Queue()
        
        # 回调函数
        self.movement_callbacks: List[Callable] = []
        
    def add_movement_callback(self, callback: Callable):
        """添加运动状态回调"""
        self.movement_callbacks.append(callback)
    
    def _notify_movement_change(self, pattern: MovementPattern, step_info: str = ""):
        """通知运动状态变化"""
        for callback in self.movement_callbacks:
            try:
                callback(pattern, step_info)
            except Exception as e:
                self.logger.error(f"运动状态回调执行失败: {e}")
    
    async def start_pattern(self, pattern: MovementPattern) -> bool:
        """启动指定运动模式"""
        if self.is_running and self.current_pattern == pattern:
            self.logger.info(f"运动模式 {pattern.value} 已在运行")
            return True
        
        # 停止当前运动
        await self.stop_movement()
        
        self.current_pattern = pattern
        self.is_running = True
        
        if pattern == MovementPattern.SQUARE_WALK:
            self.movement_task = asyncio.create_task(self._square_walk_loop())
        elif pattern == MovementPattern.MANUAL_CONTROL:
            self.movement_task = asyncio.create_task(self._manual_control_loop())
        elif pattern == MovementPattern.DANCE_PARTY:
            self.movement_task = asyncio.create_task(self._dance_party_loop())
        else:
            self.is_running = False
            return False
        
        self.logger.info(f"启动运动模式: {pattern.value}")
        self._notify_movement_change(pattern, "已启动")
        
        return True
    
    async def stop_movement(self) -> bool:
        """停止当前运动"""
        if not self.is_running:
            return True
        
        self.is_running = False
        
        if self.movement_task:
            self.movement_task.cancel()
            try:
                await self.movement_task
            except asyncio.CancelledError:
                pass
        
        # 发送停止指令
        await self.controller.send_command_to_selected("StopMove")
        
        self.current_pattern = MovementPattern.STOP
        self.logger.info("运动已停止")
        self._notify_movement_change(MovementPattern.STOP, "已停止")
        
        return True
    
    async def trigger_dance(self) -> bool:
        """触发跳舞动作（可在任何运动模式中调用）"""
        self.dance_triggered = True
        self.logger.info("跳舞动作已触发")
        return True
    
    async def trigger_specific_action(self, action: str) -> bool:
        """触发特定动作（可在任何运动模式中调用）"""
        try:
            self.logger.info(f"触发特定动作: {action}")
            
            # 发送动作指令
            results = await self.controller.send_command_to_selected(action)
            
            # 等待动作完成
            action_duration = self._get_action_duration(action)
            await asyncio.sleep(action_duration)
            
            success_count = sum(results.values())
            self.logger.info(f"特定动作 {action} 完成: {success_count}/{len(results)} 台机器狗成功执行")
            
            return success_count > 0
            
        except Exception as e:
            self.logger.error(f"执行特定动作 {action} 失败: {e}")
            return False
    
    def _get_action_duration(self, action: str) -> float:
        """获取动作持续时间"""
        # 根据动作类型返回不同的持续时间
        high_difficulty_actions = ["FrontFlip", "BackFlip", "Handstand"]
        medium_actions = ["Wallow", "Scrape", "FrontPounce"]
        
        if action in high_difficulty_actions:
            return 6.0  # 高难度动作时间更长
        elif action in medium_actions:
            return 4.0  # 中等难度动作
        else:
            return 3.0  # 常规动作
    
    async def _perform_dance(self, dance_type: str = "Dance1") -> bool:
        """执行跳舞动作"""
        try:
            self.logger.info(f"开始执行跳舞动作: {dance_type}")
            self._notify_movement_change(self.current_pattern, f"正在跳舞 ({dance_type})")
            
            # 发送跳舞指令
            results = await self.controller.send_command_to_selected(dance_type)
            
            # 等待跳舞动作完成
            dance_duration = 5.0 if dance_type == "Dance1" else 8.0
            await asyncio.sleep(dance_duration)
            
            success_count = sum(results.values())
            self.logger.info(f"跳舞动作完成: {success_count}/{len(results)} 台机器狗成功执行")
            
            return success_count > 0
            
        except Exception as e:
            self.logger.error(f"执行跳舞动作失败: {e}")
            return False
    
    async def _square_walk_loop(self):
        """正方形走路循环 (从sportsmode.py迁移)"""
        self.square_walk_step = 0
        
        # 定义正方形走路的步骤
        square_steps = [
            MovementStep("Move", {"x": 1, "y": 0, "z": 0}, 2.0, "向前走"),
            MovementStep("Move", {"x": 0, "y": 0, "z": -1.57}, 2.0, "右转90度"),  # -1.57 = -π/2
            MovementStep("Move", {"x": 1, "y": 0, "z": 0}, 2.0, "向前走"),
            MovementStep("Move", {"x": 0, "y": 0, "z": -1.57}, 2.0, "右转90度"),
            MovementStep("Move", {"x": 1, "y": 0, "z": 0}, 2.0, "向前走"),
            MovementStep("Move", {"x": 0, "y": 0, "z": -1.57}, 2.0, "右转90度"),
            MovementStep("Move", {"x": 1, "y": 0, "z": 0}, 2.0, "向前走"),
            MovementStep("Move", {"x": 0, "y": 0, "z": -1.57}, 2.0, "右转90度")
        ]
        
        try:
            while self.is_running:
                # 检查是否触发跳舞
                if self.dance_triggered:
                    self.dance_triggered = False
                    await self._perform_dance("Dance1")
                    continue
                
                # 执行当前步骤
                current_step = square_steps[self.square_walk_step % len(square_steps)]
                
                self.logger.info(f"正方形走路 - 步骤 {self.square_walk_step + 1}: {current_step.description}")
                self._notify_movement_change(
                    MovementPattern.SQUARE_WALK, 
                    f"步骤 {self.square_walk_step + 1}: {current_step.description}"
                )
                
                    # 发送运动指令
                results = await self.controller.send_command_to_selected(
                    current_step.command, 
                    current_step.parameters
                )
                
                success_count = sum(results.values())
                if success_count == 0:
                    self.logger.warning("所有机器狗都未能执行指令，暂停运动")
                    await asyncio.sleep(1)
                    continue
                
                # 等待步骤完成
                await asyncio.sleep(current_step.duration)
                
                # 再次检查跳舞触发
                if self.dance_triggered:
                    self.dance_triggered = False
                    await self._perform_dance("Dance1")
                
                self.square_walk_step += 1
                
        except asyncio.CancelledError:
            self.logger.info("正方形走路循环被取消")
            raise
        except Exception as e:
            self.logger.error(f"正方形走路循环出错: {e}")
    
    async def _manual_control_loop(self):
        """手动控制循环"""
        try:
            while self.is_running:
                try:
                    # 等待手动指令
                    command_data = await asyncio.wait_for(
                        self.manual_command_queue.get(), 
                        timeout=0.1
                    )
                    
                    command, parameters, duration = command_data
                    
                    self.logger.info(f"执行手动指令: {command} {parameters}")
                    self._notify_movement_change(
                        MovementPattern.MANUAL_CONTROL, 
                        f"执行: {command}"
                    )
                    
                    # 发送指令
                    await self.controller.send_command_to_selected(command, parameters)
                    
                    # 如果有持续时间，等待完成
                    if duration > 0:
                        await asyncio.sleep(duration)
                    
                except asyncio.TimeoutError:
                    # 没有新指令，继续等待
                    continue
                
        except asyncio.CancelledError:
            self.logger.info("手动控制循环被取消")
            raise
        except Exception as e:
            self.logger.error(f"手动控制循环出错: {e}")
    
    async def _dance_party_loop(self):
        """舞蹈派对循环 - 包含更多动作选项"""
        # 扩展舞蹈序列，包含更多姿态和动作
        dance_sequence = [
            "Dance1", "Dance2", "Hello", "WiggleHips", "FingerHeart", 
            "Stretch", "Wallow", "Scrape", "FrontFlip", "BackFlip", 
            "FrontPounce", "Handstand"
        ]
        dance_index = 0
        
        try:
            while self.is_running:
                current_dance = dance_sequence[dance_index % len(dance_sequence)]
                
                self.logger.info(f"舞蹈派对 - 执行: {current_dance}")
                self._notify_movement_change(
                    MovementPattern.DANCE_PARTY, 
                    f"正在执行: {current_dance}"
                )
                
                await self._perform_dance(current_dance)
                
                # 休息时间根据动作难度调整
                if current_dance in ["FrontFlip", "BackFlip", "Handstand"]:
                    await asyncio.sleep(3)  # 高难度动作休息更久
                else:
                    await asyncio.sleep(2)  # 常规动作休息时间
                
                dance_index += 1
                
        except asyncio.CancelledError:
            self.logger.info("舞蹈派对循环被取消")
            raise
        except Exception as e:
            self.logger.error(f"舞蹈派对循环出错: {e}")
    
    async def send_manual_command(self, command: str, parameters: Optional[Dict] = None, duration: float = 0) -> bool:
        """发送手动控制指令"""
        if self.current_pattern != MovementPattern.MANUAL_CONTROL:
            self.logger.warning("当前不在手动控制模式")
            return False
        
        try:
            await self.manual_command_queue.put((command, parameters, duration))
            return True
        except Exception as e:
            self.logger.error(f"发送手动指令失败: {e}")
            return False
    
    async def send_manual_command_to_specific_dog(self, dog_name: str, command: str, parameters: Optional[Dict] = None) -> bool:
        """向指定机器狗发送手动控制指令"""
        try:
            result = await self.controller.send_command_to_dog(dog_name, command, parameters)
            self.logger.info(f"向机器狗 {dog_name} 发送手动指令: {command} {parameters or ''}")
            return result
        except Exception as e:
            self.logger.error(f"向机器狗 {dog_name} 发送手动指令失败: {e}")
            return False
    
    # 便捷的手动控制方法
    async def move_forward(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        """向前移动"""
        return await self.send_manual_command("Move", {"x": speed, "y": 0, "z": 0}, duration)
    
    async def move_backward(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        """向后移动"""
        return await self.send_manual_command("Move", {"x": -speed, "y": 0, "z": 0}, duration)
    
    async def move_left(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        """向左移动"""
        return await self.send_manual_command("Move", {"x": 0, "y": speed, "z": 0}, duration)
    
    async def move_right(self, speed: float = 0.5, duration: float = 1.0) -> bool:
        """向右移动"""
        return await self.send_manual_command("Move", {"x": 0, "y": -speed, "z": 0}, duration)
    
    async def turn_left(self, speed: float = 1.0, duration: float = 1.0) -> bool:
        """左转"""
        return await self.send_manual_command("Move", {"x": 0, "y": 0, "z": speed}, duration)
    
    async def turn_right(self, speed: float = 1.0, duration: float = 1.0) -> bool:
        """右转"""
        return await self.send_manual_command("Move", {"x": 0, "y": 0, "z": -speed}, duration)
    
    async def sit_down(self) -> bool:
        """坐下"""
        return await self.send_manual_command("Sit")
    
    async def stand_up(self) -> bool:
        """站立"""
        return await self.send_manual_command("StandUp")
    
    async def say_hello(self) -> bool:
        """打招呼"""
        return await self.send_manual_command("Hello")
    
    async def stretch(self) -> bool:
        """伸展"""
        return await self.send_manual_command("Stretch")
    
    async def wallow(self) -> bool:
        """打滚"""
        return await self.send_manual_command("Wallow")
    
    async def scrape(self) -> bool:
        """刨地"""
        return await self.send_manual_command("Scrape")
    
    async def wiggle_hips(self) -> bool:
        """扭臀"""
        return await self.send_manual_command("WiggleHips")
    
    async def finger_heart(self) -> bool:
        """比心"""
        return await self.send_manual_command("FingerHeart")
    
    async def handstand(self) -> bool:
        """倒立"""
        return await self.send_manual_command("Handstand")
    
    async def front_flip(self) -> bool:
        """前空翻"""
        return await self.send_manual_command("FrontFlip")
    
    async def back_flip(self) -> bool:
        """后空翻"""
        return await self.send_manual_command("BackFlip")
    
    async def front_pounce(self) -> bool:
        """前扑跃"""
        return await self.send_manual_command("FrontPounce")
    
    # 单个机器狗控制方法
    async def move_forward_single(self, dog_name: str, speed: float = 0.5, duration: float = 1.0) -> bool:
        """单个机器狗向前移动"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": speed, "y": 0, "z": 0})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def move_backward_single(self, dog_name: str, speed: float = 0.5, duration: float = 1.0) -> bool:
        """单个机器狗向后移动"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": -speed, "y": 0, "z": 0})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def move_left_single(self, dog_name: str, speed: float = 0.5, duration: float = 1.0) -> bool:
        """单个机器狗向左移动"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": 0, "y": speed, "z": 0})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def move_right_single(self, dog_name: str, speed: float = 0.5, duration: float = 1.0) -> bool:
        """单个机器狗向右移动"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": 0, "y": -speed, "z": 0})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def turn_left_single(self, dog_name: str, speed: float = 1.0, duration: float = 1.0) -> bool:
        """单个机器狗左转"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": 0, "y": 0, "z": speed})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def turn_right_single(self, dog_name: str, speed: float = 1.0, duration: float = 1.0) -> bool:
        """单个机器狗右转"""
        result = await self.send_manual_command_to_specific_dog(dog_name, "Move", {"x": 0, "y": 0, "z": -speed})
        if result and duration > 0:
            await asyncio.sleep(duration)
            await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
        return result
    
    async def sit_down_single(self, dog_name: str) -> bool:
        """单个机器狗坐下"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Sit")
    
    async def stand_up_single(self, dog_name: str) -> bool:
        """单个机器狗站立"""
        return await self.send_manual_command_to_specific_dog(dog_name, "StandUp")
    
    async def say_hello_single(self, dog_name: str) -> bool:
        """单个机器狗打招呼"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Hello")
    
    async def stretch_single(self, dog_name: str) -> bool:
        """单个机器狗伸展"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Stretch")
    
    async def wallow_single(self, dog_name: str) -> bool:
        """单个机器狗打滚"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Wallow")
    
    async def scrape_single(self, dog_name: str) -> bool:
        """单个机器狗刨地"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Scrape")
    
    async def wiggle_hips_single(self, dog_name: str) -> bool:
        """单个机器狗扭臀"""
        return await self.send_manual_command_to_specific_dog(dog_name, "WiggleHips")
    
    async def finger_heart_single(self, dog_name: str) -> bool:
        """单个机器狗比心"""
        return await self.send_manual_command_to_specific_dog(dog_name, "FingerHeart")
    
    async def handstand_single(self, dog_name: str) -> bool:
        """单个机器狗倒立"""
        return await self.send_manual_command_to_specific_dog(dog_name, "Handstand")
    
    async def front_flip_single(self, dog_name: str) -> bool:
        """单个机器狗前空翻"""
        return await self.send_manual_command_to_specific_dog(dog_name, "FrontFlip")
    
    async def back_flip_single(self, dog_name: str) -> bool:
        """单个机器狗后空翻"""
        return await self.send_manual_command_to_specific_dog(dog_name, "BackFlip")
    
    async def front_pounce_single(self, dog_name: str) -> bool:
        """单个机器狗前扑跃"""
        return await self.send_manual_command_to_specific_dog(dog_name, "FrontPounce")
    
    async def stop_move_single(self, dog_name: str) -> bool:
        """单个机器狗停止移动"""
        return await self.send_manual_command_to_specific_dog(dog_name, "StopMove")
    
    async def stop_move(self) -> bool:
        """停止移动（紧急停止）"""
        try:
            await self.controller.send_command_to_selected("StopMove")
            return True
        except Exception as e:
            self.logger.error(f"紧急停止失败: {e}")
            return False
    
    def get_current_pattern(self) -> MovementPattern:
        """获取当前运动模式"""
        return self.current_pattern
    
    def is_pattern_running(self) -> bool:
        """检查运动模式是否在运行"""
        return self.is_running
    
    def get_square_walk_step(self) -> int:
        """获取正方形走路当前步骤"""
        return self.square_walk_step