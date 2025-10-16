#!/usr/bin/env python3
"""
键盘控制模块 - 支持WASD控制机器狗移动
提供实时键盘输入处理和机器狗控制映射
"""

import tkinter as tk
import asyncio
import logging
import time
from typing import Dict, Set, Optional, Callable
from dataclasses import dataclass
from enum import Enum


class KeyAction(Enum):
    """键盘动作枚举"""
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    MOVE_LEFT = "move_left"
    MOVE_RIGHT = "move_right"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    STOP = "stop"
    SIT = "sit"
    STAND = "stand"
    HELLO = "hello"


@dataclass
class KeyMapping:
    """键盘映射配置"""
    key: str
    action: KeyAction
    description: str
    continuous: bool = True  # 是否支持连续按键


class KeyboardController:
    """键盘控制器类"""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("KeyboardController")
        self.is_enabled = False
        self.pressed_keys: Set[str] = set()
        self.movement_callbacks: Dict[KeyAction, Callable] = {}
        self.update_task = None
        self.last_action_time = 0
        self.action_interval = 0.1  # 动作间隔，防止过于频繁的指令
        
        # 默认键盘映射
        self.key_mappings = {
            'w': KeyMapping('w', KeyAction.MOVE_FORWARD, '前进', True),
            's': KeyMapping('s', KeyAction.MOVE_BACKWARD, '后退', True),
            'a': KeyMapping('a', KeyAction.MOVE_LEFT, '左移', True),
            'd': KeyMapping('d', KeyAction.MOVE_RIGHT, '右移', True),
            'q': KeyMapping('q', KeyAction.TURN_LEFT, '左转', True),
            'e': KeyMapping('e', KeyAction.TURN_RIGHT, '右转', True),
            'space': KeyMapping('space', KeyAction.STOP, '停止', False),
            'x': KeyMapping('x', KeyAction.SIT, '坐下', False),
            'z': KeyMapping('z', KeyAction.STAND, '站立', False),
            'h': KeyMapping('h', KeyAction.HELLO, '打招呼', False),
        }
        
    def set_movement_callback(self, action: KeyAction, callback: Callable):
        """设置运动回调函数"""
        self.movement_callbacks[action] = callback
        
    def enable(self):
        """启用键盘控制"""
        if not self.is_enabled:
            self.is_enabled = True
            self.logger.info("键盘控制已启用")
            # 启动更新循环
            if not self.update_task:
                self.update_task = asyncio.create_task(self._update_loop())
    
    def disable(self):
        """禁用键盘控制"""
        if self.is_enabled:
            self.is_enabled = False
            self.pressed_keys.clear()
            self.logger.info("键盘控制已禁用")
            # 停止更新循环
            if self.update_task:
                self.update_task.cancel()
                self.update_task = None
    
    def on_key_press(self, key: str):
        """按键按下事件"""
        if not self.is_enabled:
            return
        
        key = key.lower()
        if key in self.key_mappings:
            mapping = self.key_mappings[key]
            self.pressed_keys.add(key)
            
            # 如果是非连续动作，立即执行
            if not mapping.continuous:
                self._execute_action(mapping.action)
            
            self.logger.debug(f"按键按下: {key} -> {mapping.description}")
    
    def on_key_release(self, key: str):
        """按键释放事件"""
        if not self.is_enabled:
            return
        
        key = key.lower()
        if key in self.pressed_keys:
            self.pressed_keys.discard(key)
            self.logger.debug(f"按键释放: {key}")
    
    async def _update_loop(self):
        """更新循环，处理连续按键"""
        try:
            while self.is_enabled:
                current_time = time.time()
                
                # 检查是否需要执行动作
                if current_time - self.last_action_time >= self.action_interval:
                    await self._process_continuous_keys()
                    self.last_action_time = current_time
                
                await asyncio.sleep(0.05)  # 20Hz更新频率
        except asyncio.CancelledError:
            self.logger.info("键盘控制更新循环已取消")
        except Exception as e:
            self.logger.error(f"键盘控制更新循环出错: {e}")
    
    async def _process_continuous_keys(self):
        """处理连续按键"""
        if not self.pressed_keys:
            return
        
        # 按优先级处理按键（移动优先于转向）
        movement_keys = {'w', 's', 'a', 'd'}
        turn_keys = {'q', 'e'}
        
        # 处理移动按键
        for key in self.pressed_keys:
            if key in movement_keys and self.key_mappings[key].continuous:
                action = self.key_mappings[key].action
                await self._execute_action_async(action)
                break  # 一次只执行一个移动动作
        
        # 处理转向按键
        for key in self.pressed_keys:
            if key in turn_keys and self.key_mappings[key].continuous:
                action = self.key_mappings[key].action
                await self._execute_action_async(action)
                break  # 一次只执行一个转向动作
    
    def _execute_action(self, action: KeyAction):
        """执行动作（同步版本）"""
        if action in self.movement_callbacks:
            try:
                callback = self.movement_callbacks[action]
                # 如果回调是协程，需要在事件循环中执行
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback())
                else:
                    callback()
            except Exception as e:
                self.logger.error(f"执行动作 {action.value} 失败: {e}")
    
    async def _execute_action_async(self, action: KeyAction):
        """执行动作（异步版本）"""
        if action in self.movement_callbacks:
            try:
                callback = self.movement_callbacks[action]
                if asyncio.iscoroutinefunction(callback):
                    await callback()
                else:
                    callback()
            except Exception as e:
                self.logger.error(f"执行动作 {action.value} 失败: {e}")
    
    def get_key_mappings_description(self) -> str:
        """获取键盘映射说明"""
        descriptions = []
        for mapping in self.key_mappings.values():
            key_display = mapping.key.upper() if len(mapping.key) == 1 else mapping.key.title()
            descriptions.append(f"{key_display}: {mapping.description}")
        return "\n".join(descriptions)


class KeyboardControlGUI:
    """键盘控制GUI组件"""
    
    def __init__(self, parent_frame, controller: KeyboardController, movement_module=None):
        self.parent_frame = parent_frame
        self.controller = controller
        self.movement_module = movement_module
        self.is_focused = False
        self.selected_dog_var = None
        
        self._create_gui()
        self._setup_key_bindings()
        self._setup_movement_callbacks()
    
    def _create_gui(self):
        """创建GUI组件"""
        import tkinter as tk
        from tkinter import ttk
        
        # 键盘控制框架
        keyboard_frame = ttk.LabelFrame(self.parent_frame, text="键盘控制 (WASD)", padding="5")
        keyboard_frame.pack(fill=tk.X, pady=5)
        
        # 控制按钮框架
        control_frame = ttk.Frame(keyboard_frame)
        control_frame.pack(fill=tk.X, pady=2)
        
        # 启用/禁用按钮
        self.enable_btn = ttk.Button(control_frame, text="启用键盘控制", command=self._on_enable_clicked)
        self.enable_btn.pack(side=tk.LEFT, padx=2)
        
        self.disable_btn = ttk.Button(control_frame, text="禁用键盘控制", command=self._on_disable_clicked, state="disabled")
        self.disable_btn.pack(side=tk.LEFT, padx=2)
        
        # 焦点指示器
        self.focus_label = ttk.Label(control_frame, text="●", foreground="red")
        self.focus_label.pack(side=tk.RIGHT, padx=5)
        
        focus_text_label = ttk.Label(control_frame, text="焦点状态:")
        focus_text_label.pack(side=tk.RIGHT)
        
        # 键盘映射说明
        mappings_frame = ttk.Frame(keyboard_frame)
        mappings_frame.pack(fill=tk.X, pady=5)
        
        # 左侧：移动控制
        move_frame = ttk.LabelFrame(mappings_frame, text="移动控制", padding="3")
        move_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        
        move_text = "W: 前进\nS: 后退\nA: 左移\nD: 右移\nQ: 左转\nE: 右转"
        ttk.Label(move_frame, text=move_text, font=("Consolas", 9)).pack()
        
        # 右侧：动作控制
        action_frame = ttk.LabelFrame(mappings_frame, text="动作控制", padding="3")
        action_frame.pack(side=tk.LEFT, fill=tk.Y)
        
        action_text = "Space: 停止\nZ: 站立\nX: 坐下\nH: 打招呼"
        ttk.Label(action_frame, text=action_text, font=("Consolas", 9)).pack()
        
        # 焦点捕获区域（不可见但可以接收键盘事件）
        self.focus_frame = tk.Frame(keyboard_frame, height=1)
        self.focus_frame.pack(fill=tk.X)
        self.focus_frame.focus_set()  # 设置焦点
    
    def _setup_key_bindings(self):
        """设置键盘绑定"""
        # 绑定到父窗口，这样可以全局捕获按键
        root = self.parent_frame.winfo_toplevel()
        
        # 绑定按键事件
        root.bind('<KeyPress>', self._on_key_press)
        root.bind('<KeyRelease>', self._on_key_release)
        
        # 绑定焦点事件
        root.bind('<FocusIn>', self._on_focus_in)
        root.bind('<FocusOut>', self._on_focus_out)
        
        # 确保窗口可以接收键盘事件
        root.focus_set()
    
    def _setup_movement_callbacks(self):
        """设置运动回调"""
        if not self.movement_module:
            return
        
        # 设置各种动作的回调
        self.controller.set_movement_callback(KeyAction.MOVE_FORWARD, 
                                            lambda: self._execute_movement('move_forward'))
        self.controller.set_movement_callback(KeyAction.MOVE_BACKWARD, 
                                            lambda: self._execute_movement('move_backward'))
        self.controller.set_movement_callback(KeyAction.MOVE_LEFT, 
                                            lambda: self._execute_movement('move_left'))
        self.controller.set_movement_callback(KeyAction.MOVE_RIGHT, 
                                            lambda: self._execute_movement('move_right'))
        self.controller.set_movement_callback(KeyAction.TURN_LEFT, 
                                            lambda: self._execute_movement('turn_left'))
        self.controller.set_movement_callback(KeyAction.TURN_RIGHT, 
                                            lambda: self._execute_movement('turn_right'))
        self.controller.set_movement_callback(KeyAction.STOP, 
                                            lambda: self._execute_movement('stop_move'))
        self.controller.set_movement_callback(KeyAction.SIT, 
                                            lambda: self._execute_movement('sit_down'))
        self.controller.set_movement_callback(KeyAction.STAND, 
                                            lambda: self._execute_movement('stand_up'))
        self.controller.set_movement_callback(KeyAction.HELLO, 
                                            lambda: self._execute_movement('say_hello'))
    
    async def _execute_movement(self, action_name: str):
        """执行运动指令"""
        if not self.movement_module:
            return
        
        try:
            # 检查是否有选中的机器狗变量
            if (hasattr(self, 'control_mode_var') and hasattr(self, 'selected_dog_var') and 
                self.control_mode_var is not None and self.selected_dog_var is not None):
                if self.control_mode_var.get() == "single":
                    # 单独控制模式
                    dog_name = self.selected_dog_var.get()
                    method = getattr(self.movement_module, f"{action_name}_single", None)
                    if method:
                        await method(dog_name)
                    return
            
            # 默认为全部控制模式
            method = getattr(self.movement_module, action_name, None)
            if method:
                await method()
                
        except Exception as e:
            self.controller.logger.error(f"执行运动指令 {action_name} 失败: {e}")
    
    def set_control_mode_vars(self, control_mode_var, selected_dog_var):
        """设置控制模式变量"""
        self.control_mode_var = control_mode_var
        self.selected_dog_var = selected_dog_var
    
    def _on_enable_clicked(self):
        """启用按钮点击事件"""
        self.controller.enable()
        self.enable_btn.config(state="disabled")
        self.disable_btn.config(state="normal")
        self._update_focus_indicator()
    
    def _on_disable_clicked(self):
        """禁用按钮点击事件"""
        self.controller.disable()
        self.enable_btn.config(state="normal")
        self.disable_btn.config(state="disabled")
        self._update_focus_indicator()
    
    def _on_key_press(self, event):
        """按键按下事件"""
        key = event.keysym.lower()
        if key == 'space':
            key = 'space'
        self.controller.on_key_press(key)
    
    def _on_key_release(self, event):
        """按键释放事件"""
        key = event.keysym.lower()
        if key == 'space':
            key = 'space'
        self.controller.on_key_release(key)
    
    def _on_focus_in(self, event):
        """焦点获得事件"""
        self.is_focused = True
        self._update_focus_indicator()
    
    def _on_focus_out(self, event):
        """焦点失去事件"""
        self.is_focused = False
        self._update_focus_indicator()
    
    def _update_focus_indicator(self):
        """更新焦点指示器"""
        if self.controller.is_enabled and self.is_focused:
            self.focus_label.config(foreground="green", text="●")
        elif self.controller.is_enabled:
            self.focus_label.config(foreground="orange", text="●")
        else:
            self.focus_label.config(foreground="red", text="●")