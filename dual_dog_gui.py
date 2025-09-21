#!/usr/bin/env python3
"""
双机器狗控制系统GUI界面
使用tkinter实现的用户友好界面，支持所有控制功能
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import asyncio
import threading
import logging
import time
from typing import Dict, Any, Optional
from datetime import datetime

from dual_dog_controller import DualDogController, DogStatus, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern


class DualDogGUI:
    """双机器狗控制GUI主类"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("双机器狗控制系统 v1.0")
        self.root.geometry("1200x800")
        self.root.resizable(True, True)
        
        # 设置样式
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # 控制器和运动控制
        self.controller = None
        self.movement = None
        self.asyncio_loop = None
        self.asyncio_thread = None
        
        # GUI变量
        self.dog1_status_var = tk.StringVar(value="未连接")
        self.dog2_status_var = tk.StringVar(value="未连接") 
        self.movement_status_var = tk.StringVar(value="停止")
        self.log_level_var = tk.StringVar(value="INFO")
        
        # 机器狗配置
        self.dog1_name_var = tk.StringVar(value="Dog1")
        self.dog1_ip_var = tk.StringVar(value="192.168.31.245")
        self.dog2_name_var = tk.StringVar(value="Dog2")
        self.dog2_ip_var = tk.StringVar(value="192.168.31.246")
        
        # 初始化GUI
        self._setup_gui()
        self._setup_logging()
        self._start_asyncio_thread()
        
        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # 定期更新状态显示
        self._update_status_display()
    
    def _setup_gui(self):
        """设置GUI界面"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 配置网格权重
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(2, weight=1)
        
        # 左侧面板 - 连接配置
        self._create_connection_panel(main_frame)
        
        # 右侧面板 - 控制面板
        self._create_control_panel(main_frame)
        
        # 底部面板 - 日志显示
        self._create_log_panel(main_frame)
    
    def _create_connection_panel(self, parent):
        """创建连接配置面板"""
        # 连接配置框架
        conn_frame = ttk.LabelFrame(parent, text="机器狗连接配置", padding="10")
        conn_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N), padx=(0, 5))
        
        # 机器狗1配置
        ttk.Label(conn_frame, text="机器狗1:").grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Label(conn_frame, text="名称:").grid(row=1, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog1_name_var, width=15).grid(row=1, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Label(conn_frame, text="IP:").grid(row=2, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog1_ip_var, width=15).grid(row=2, column=1, sticky=(tk.W, tk.E), padx=5)
        
        # 状态显示
        ttk.Label(conn_frame, text="状态:").grid(row=3, column=0, sticky=tk.W, padx=(20, 0))
        status1_label = ttk.Label(conn_frame, textvariable=self.dog1_status_var, foreground="red")
        status1_label.grid(row=3, column=1, sticky=tk.W, padx=5)
        
        # 机器狗2配置
        ttk.Separator(conn_frame, orient='horizontal').grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=10)
        ttk.Label(conn_frame, text="机器狗2:").grid(row=5, column=0, sticky=tk.W, pady=2)
        ttk.Label(conn_frame, text="名称:").grid(row=6, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog2_name_var, width=15).grid(row=6, column=1, sticky=(tk.W, tk.E), padx=5)
        ttk.Label(conn_frame, text="IP:").grid(row=7, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog2_ip_var, width=15).grid(row=7, column=1, sticky=(tk.W, tk.E), padx=5)
        
        # 状态显示
        ttk.Label(conn_frame, text="状态:").grid(row=8, column=0, sticky=tk.W, padx=(20, 0))
        status2_label = ttk.Label(conn_frame, textvariable=self.dog2_status_var, foreground="red")
        status2_label.grid(row=8, column=1, sticky=tk.W, padx=5)
        
        # 连接按钮
        ttk.Separator(conn_frame, orient='horizontal').grid(row=9, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=10)
        btn_frame = ttk.Frame(conn_frame)
        btn_frame.grid(row=10, column=0, columnspan=2, pady=5)
        
        self.connect_btn = ttk.Button(btn_frame, text="连接所有", command=self._on_connect_all)
        self.connect_btn.pack(side=tk.LEFT, padx=2)
        
        self.disconnect_btn = ttk.Button(btn_frame, text="断开所有", command=self._on_disconnect_all, state="disabled")
        self.disconnect_btn.pack(side=tk.LEFT, padx=2)
        
        # 存储状态标签引用
        self.status1_label = status1_label
        self.status2_label = status2_label
    
    def _create_control_panel(self, parent):
        """创建控制面板"""
        # 控制面板框架
        control_frame = ttk.LabelFrame(parent, text="运动控制", padding="10")
        control_frame.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N), padx=(5, 0))
        
        # 自动运动模式
        auto_frame = ttk.LabelFrame(control_frame, text="自动运动模式", padding="5")
        auto_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 运动模式按钮
        mode_frame = ttk.Frame(auto_frame)
        mode_frame.pack(fill=tk.X)
        
        self.square_walk_btn = ttk.Button(mode_frame, text="正方形走路", command=self._on_square_walk)
        self.square_walk_btn.pack(side=tk.LEFT, padx=2)
        
        self.dance_party_btn = ttk.Button(mode_frame, text="舞蹈派对", command=self._on_dance_party)
        self.dance_party_btn.pack(side=tk.LEFT, padx=2)
        
        self.stop_auto_btn = ttk.Button(mode_frame, text="停止自动", command=self._on_stop_auto)
        self.stop_auto_btn.pack(side=tk.LEFT, padx=2)
        
        # 触发跳舞按钮
        self.trigger_dance_btn = ttk.Button(auto_frame, text="🎭 触发跳舞", command=self._on_trigger_dance)
        self.trigger_dance_btn.pack(pady=5)
        
        # 运动状态显示
        status_frame = ttk.Frame(auto_frame)
        status_frame.pack(fill=tk.X, pady=5)
        ttk.Label(status_frame, text="运动状态:").pack(side=tk.LEFT)
        ttk.Label(status_frame, textvariable=self.movement_status_var, foreground="blue").pack(side=tk.LEFT, padx=(5, 0))
        
        # 手动控制
        manual_frame = ttk.LabelFrame(control_frame, text="手动遥控", padding="5")
        manual_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 启用手动控制
        self.manual_mode_btn = ttk.Button(manual_frame, text="启用手动控制", command=self._on_manual_mode)
        self.manual_mode_btn.pack(pady=5)
        
        # 方向控制按钮
        direction_frame = ttk.Frame(manual_frame)
        direction_frame.pack(pady=5)
        
        # 上排（前进）
        self.forward_btn = ttk.Button(direction_frame, text="↑ 前进", command=self._on_move_forward)
        self.forward_btn.grid(row=0, column=1, padx=2, pady=2)
        
        # 中排（左，停止，右）
        self.left_btn = ttk.Button(direction_frame, text="← 左移", command=self._on_move_left)
        self.left_btn.grid(row=1, column=0, padx=2, pady=2)
        
        self.stop_btn = ttk.Button(direction_frame, text="⏹ 停止", command=self._on_stop_move)
        self.stop_btn.grid(row=1, column=1, padx=2, pady=2)
        
        self.right_btn = ttk.Button(direction_frame, text="→ 右移", command=self._on_move_right)
        self.right_btn.grid(row=1, column=2, padx=2, pady=2)
        
        # 下排（后退）
        self.backward_btn = ttk.Button(direction_frame, text="↓ 后退", command=self._on_move_backward)
        self.backward_btn.grid(row=2, column=1, padx=2, pady=2)
        
        # 旋转控制
        rotate_frame = ttk.Frame(manual_frame)
        rotate_frame.pack(pady=5)
        
        self.turn_left_btn = ttk.Button(rotate_frame, text="⟲ 左转", command=self._on_turn_left)
        self.turn_left_btn.pack(side=tk.LEFT, padx=2)
        
        self.turn_right_btn = ttk.Button(rotate_frame, text="⟳ 右转", command=self._on_turn_right)
        self.turn_right_btn.pack(side=tk.LEFT, padx=2)
        
        # 姿态控制
        posture_frame = ttk.LabelFrame(control_frame, text="姿态控制", padding="5")
        posture_frame.pack(fill=tk.X, pady=(0, 10))
        
        posture_btn_frame = ttk.Frame(posture_frame)
        posture_btn_frame.pack()
        
        self.sit_btn = ttk.Button(posture_btn_frame, text="坐下", command=self._on_sit)
        self.sit_btn.pack(side=tk.LEFT, padx=2)
        
        self.stand_btn = ttk.Button(posture_btn_frame, text="站立", command=self._on_stand)
        self.stand_btn.pack(side=tk.LEFT, padx=2)
        
        self.hello_btn = ttk.Button(posture_btn_frame, text="👋 打招呼", command=self._on_hello)
        self.hello_btn.pack(side=tk.LEFT, padx=2)
        
        # 初始状态设置为禁用
        self._set_control_buttons_state("disabled")
    
    def _create_log_panel(self, parent):
        """创建日志面板"""
        # 日志框架
        log_frame = ttk.LabelFrame(parent, text="系统日志", padding="5")
        log_frame.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        
        # 日志控制栏
        log_control_frame = ttk.Frame(log_frame)
        log_control_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 5))
        
        ttk.Label(log_control_frame, text="日志级别:").pack(side=tk.LEFT)
        log_level_combo = ttk.Combobox(log_control_frame, textvariable=self.log_level_var, 
                                      values=["DEBUG", "INFO", "WARNING", "ERROR"], 
                                      state="readonly", width=10)
        log_level_combo.pack(side=tk.LEFT, padx=5)
        log_level_combo.bind("<<ComboboxSelected>>", self._on_log_level_change)
        
        self.clear_log_btn = ttk.Button(log_control_frame, text="清空日志", command=self._clear_log)
        self.clear_log_btn.pack(side=tk.RIGHT)
        
        # 日志文本框
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=80)
        self.log_text.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
    
    def _setup_logging(self):
        """设置日志系统"""
        # 创建自定义日志处理器
        class GUILogHandler(logging.Handler):
            def __init__(self, gui):
                super().__init__()
                self.gui = gui
                
            def emit(self, record):
                msg = self.format(record)
                self.gui._add_log_message(msg)
        
        # 设置日志格式
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        # 创建并添加GUI日志处理器
        self.gui_handler = GUILogHandler(self)
        self.gui_handler.setFormatter(formatter)
        self.gui_handler.setLevel(getattr(logging, self.log_level_var.get()))
        
        # 获取根日志记录器并添加处理器
        root_logger = logging.getLogger()
        root_logger.addHandler(self.gui_handler)
        root_logger.setLevel(logging.DEBUG)
    
    def _add_log_message(self, message):
        """添加日志消息到GUI"""
        def add_to_gui():
            self.log_text.insert(tk.END, message + '\n')
            self.log_text.see(tk.END)
            
        # 确保在主线程中更新GUI
        self.root.after(0, add_to_gui)
    
    def _start_asyncio_thread(self):
        """启动asyncio线程"""
        def run_asyncio():
            self.asyncio_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.asyncio_loop)
            
            # 创建控制器和运动控制
            self.controller = DualDogController()
            self.movement = DualDogMovement(self.controller)
            
            # 添加状态回调
            self.controller.add_status_callback(self._on_dog_status_change)
            self.movement.add_movement_callback(self._on_movement_status_change)
            
            # 启动监控
            self.asyncio_loop.run_until_complete(self.controller.start_monitoring())
            
            # 运行事件循环
            self.asyncio_loop.run_forever()
        
        self.asyncio_thread = threading.Thread(target=run_asyncio, daemon=True)
        self.asyncio_thread.start()
        
        # 等待asyncio循环启动
        time.sleep(0.5)
    
    def _run_async(self, coro):
        """在asyncio线程中运行协程"""
        if self.asyncio_loop:
            future = asyncio.run_coroutine_threadsafe(coro, self.asyncio_loop)
            return future
        return None
    
    # 事件处理方法
    def _on_connect_all(self):
        """连接所有机器狗"""
        # 添加机器狗配置
        if self.controller:
            # 只连接启用的机器狗
            dog1_name = self.dog1_name_var.get()
            dog1_ip = self.dog1_ip_var.get()
            if dog1_name and dog1_ip:
                self.controller.add_dog(
                    dog1_name,
                    dog1_ip,
                    connection_method=WebRTCConnectionMethod.LocalSTA
                )
            
            dog2_name = self.dog2_name_var.get()
            dog2_ip = self.dog2_ip_var.get()
            # 只有当用户输入了有效的第二台机器狗信息时才添加
            if dog2_name and dog2_ip and dog2_ip != "192.168.31.246":
                self.controller.add_dog(
                    dog2_name,
                    dog2_ip,
                    connection_method=WebRTCConnectionMethod.LocalSTA
                )
        
        # 异步连接
        self._run_async(self.controller.connect_all())
        
        # 更新按钮状态
        self.connect_btn.config(state="disabled")
        self.disconnect_btn.config(state="normal")
    
    def _on_disconnect_all(self):
        """断开所有机器狗连接"""
        if self.controller:
            self._run_async(self.controller.disconnect_all())
        
        # 更新按钮状态
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self._set_control_buttons_state("disabled")
    
    def _on_square_walk(self):
        """启动正方形走路"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.SQUARE_WALK))
    
    def _on_dance_party(self):
        """启动舞蹈派对"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.DANCE_PARTY))
    
    def _on_stop_auto(self):
        """停止自动运动"""
        if self.movement:
            self._run_async(self.movement.stop_movement())
    
    def _on_trigger_dance(self):
        """触发跳舞"""
        if self.movement:
            self._run_async(self.movement.trigger_dance())
    
    def _on_manual_mode(self):
        """启用手动控制模式"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.MANUAL_CONTROL))
    
    def _on_move_forward(self):
        """前进"""
        if self.movement:
            self._run_async(self.movement.move_forward())
    
    def _on_move_backward(self):
        """后退"""
        if self.movement:
            self._run_async(self.movement.move_backward())
    
    def _on_move_left(self):
        """左移"""
        if self.movement:
            self._run_async(self.movement.move_left())
    
    def _on_move_right(self):
        """右移"""
        if self.movement:
            self._run_async(self.movement.move_right())
    
    def _on_turn_left(self):
        """左转"""
        if self.movement:
            self._run_async(self.movement.turn_left())
    
    def _on_turn_right(self):
        """右转"""
        if self.movement:
            self._run_async(self.movement.turn_right())
    
    def _on_stop_move(self):
        """停止移动"""
        if self.movement:
            self._run_async(self.movement.stop_move())
    
    def _on_sit(self):
        """坐下"""
        if self.movement:
            self._run_async(self.movement.sit_down())
    
    def _on_stand(self):
        """站立"""
        if self.movement:
            self._run_async(self.movement.stand_up())
    
    def _on_hello(self):
        """打招呼"""
        if self.movement:
            self._run_async(self.movement.say_hello())
    
    def _on_log_level_change(self, event):
        """日志级别变化"""
        new_level = getattr(logging, self.log_level_var.get())
        self.gui_handler.setLevel(new_level)
    
    def _clear_log(self):
        """清空日志"""
        self.log_text.delete(1.0, tk.END)
    
    def _on_dog_status_change(self, dog_name: str, status: DogStatus):
        """机器狗状态变化回调"""
        def update_status():
            status_text = status.value
            color = "red"
            
            if status == DogStatus.CONNECTED:
                color = "green"
                self._set_control_buttons_state("normal")
            elif status == DogStatus.CONNECTING:
                color = "orange"
            elif status == DogStatus.MOVING:
                color = "blue"
            elif status == DogStatus.DANCING:
                color = "purple"
            
            if dog_name == self.dog1_name_var.get():
                self.dog1_status_var.set(status_text)
                self.status1_label.config(foreground=color)
            elif dog_name == self.dog2_name_var.get():
                self.dog2_status_var.set(status_text)
                self.status2_label.config(foreground=color)
        
        self.root.after(0, update_status)
    
    def _on_movement_status_change(self, pattern: MovementPattern, step_info: str):
        """运动状态变化回调"""
        def update_status():
            status_text = f"{pattern.value}"
            if step_info:
                status_text += f" - {step_info}"
            self.movement_status_var.set(status_text)
        
        self.root.after(0, update_status)
    
    def _set_control_buttons_state(self, state):
        """设置控制按钮状态"""
        buttons = [
            self.square_walk_btn, self.dance_party_btn, self.stop_auto_btn,
            self.trigger_dance_btn, self.manual_mode_btn, self.forward_btn,
            self.backward_btn, self.left_btn, self.right_btn, self.turn_left_btn,
            self.turn_right_btn, self.stop_btn, self.sit_btn, self.stand_btn,
            self.hello_btn
        ]
        
        for btn in buttons:
            btn.config(state=state)
    
    def _update_status_display(self):
        """定期更新状态显示"""
        # 这里可以添加定期状态更新逻辑
        self.root.after(1000, self._update_status_display)
    
    def _on_closing(self):
        """程序关闭处理"""
        if messagebox.askokcancel("退出", "确定要退出双机器狗控制系统吗？"):
            # 停止所有运动
            if self.movement:
                self._run_async(self.movement.stop_movement())
            
            # 断开所有连接
            if self.controller:
                self._run_async(self.controller.disconnect_all())
                self._run_async(self.controller.stop_monitoring())
            
            # 停止asyncio循环
            if self.asyncio_loop:
                self.asyncio_loop.call_soon_threadsafe(self.asyncio_loop.stop)
            
            time.sleep(0.5)  # 等待清理完成
            self.root.destroy()
    
    def run(self):
        """运行GUI"""
        self.root.mainloop()


def main():
    """主函数"""
    app = DualDogGUI()
    app.run()


if __name__ == "__main__":
    main()