#!/usr/bin/env python3
"""
双机器狗控制系统GUI界面
使用tkinter实现的用户友好界面，支持所有控制功能
新增功能：网络扫描和键盘WASD控制
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
from network_scanner import NetworkScanner, NetworkScannerGUI
from keyboard_controller import KeyboardController, KeyboardControlGUI


class DualDogGUI:
    """双机器狗控制GUI主类"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("双机器狗控制系统 v2.0 - 网络扫描 + 键盘控制")
        self.root.geometry("1400x900")
        self.root.resizable(True, True)
        
        # 设置样式
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # 控制器和运动控制
        self.controller = None
        self.movement = None
        self.asyncio_loop = None
        self.asyncio_thread = None
        
        # 新增：网络扫描器和键盘控制器
        self.network_scanner = None
        self.network_scanner_gui = None
        self.keyboard_controller = None
        self.keyboard_control_gui = None
        
        # GUI变量
        self.dog1_status_var = tk.StringVar(value="未连接")
        self.dog2_status_var = tk.StringVar(value="未连接") 
        self.movement_status_var = tk.StringVar(value="停止")
        self.log_level_var = tk.StringVar(value="INFO")
        
        # 控制模式变量
        self.control_mode_var = tk.StringVar(value="all")  # "all", "single", "dog1", "dog2"
        self.selected_dog_var = tk.StringVar(value="Dog1")  # 当前选中的机器狗
        
        # 机器狗配置
        self.dog1_name_var = tk.StringVar(value="Dog1")
        self.dog1_ip_var = tk.StringVar(value="192.168.31.245")
        self.dog2_name_var = tk.StringVar(value="Dog2")
        self.dog2_ip_var = tk.StringVar(value="192.168.31.246")
        
        # 初始化GUI
        self._setup_gui()
        self._setup_logging()
        self._start_asyncio_thread()
        
        # 在 asyncio 线程启动后设置其他组件
        self._setup_network_scanner()
        self._setup_keyboard_controller()
        self._setup_additional_gui_components()
        
        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        
        # 定期更新状态显示
        self._update_status_display()
    
    def _setup_additional_gui_components(self):
        """设置额外的GUI组件"""
        # 网络扫描器GUI组件
        if self.network_scanner_gui:
            self.network_scanner_gui.set_device_selection_callback(self._on_device_from_scanner_selected)
        
        # 键盘控制GUI组件
        if self.keyboard_control_gui:
            self.keyboard_control_gui.set_control_mode_vars(self.control_mode_var, self.selected_dog_var)
    
    def _setup_network_scanner(self):
        """设置网络扫描器"""
        self.network_scanner = NetworkScanner()
        
    def _setup_keyboard_controller(self):
        """设置键盘控制器"""
        self.keyboard_controller = KeyboardController()
    
    def _on_device_from_scanner_selected(self, ip: str, serial: str, device_name: str):
        """从网络扫描器选择设备的回调"""
        # 自动填充到第一个或第二个机器狗配置
        if not self.dog1_ip_var.get() or self.dog1_ip_var.get() == "192.168.31.245":
            self.dog1_name_var.set(device_name)
            self.dog1_ip_var.set(ip)
        elif not self.dog2_ip_var.get() or self.dog2_ip_var.get() == "192.168.31.246":
            self.dog2_name_var.set(device_name)
            self.dog2_ip_var.set(ip)
        
        self.logger.info(f"已选择设备: {device_name} ({ip})")
    
    def _setup_gui(self):
        """设置GUI界面"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=tk.W+tk.E+tk.N+tk.S)
        
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
        conn_frame.grid(row=0, column=0, sticky=tk.W+tk.E+tk.N, padx=(0, 5))
        
        # 网络扫描器
        scanner_frame = ttk.LabelFrame(conn_frame, text="网络扫描", padding="5")
        scanner_frame.grid(row=0, column=0, columnspan=2, sticky=tk.W+tk.E, pady=(0, 10))
        
        # 创建网络扫描器GUI
        if self.network_scanner:
            self.network_scanner_gui = NetworkScannerGUI(scanner_frame, self.network_scanner)
            self.network_scanner_gui.set_device_selection_callback(self._on_device_from_scanner_selected)
        
        # 机器狗1配置
        ttk.Label(conn_frame, text="机器狗1:").grid(row=0, column=0, sticky=tk.W, pady=2)
        ttk.Label(conn_frame, text="名称:").grid(row=1, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog1_name_var, width=15).grid(row=1, column=1, sticky=tk.W+tk.E, padx=5)
        ttk.Label(conn_frame, text="IP:").grid(row=2, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog1_ip_var, width=15).grid(row=2, column=1, sticky=tk.W+tk.E, padx=5)
        
        # 状态显示
        ttk.Label(conn_frame, text="状态:").grid(row=3, column=0, sticky=tk.W, padx=(20, 0))
        status1_label = ttk.Label(conn_frame, textvariable=self.dog1_status_var, foreground="red")
        status1_label.grid(row=3, column=1, sticky=tk.W, padx=5)
        
        # 机器狗2配置
        ttk.Separator(conn_frame, orient='horizontal').grid(row=4, column=0, columnspan=2, sticky=tk.W+tk.E, pady=10)
        ttk.Label(conn_frame, text="机器狗2:").grid(row=5, column=0, sticky=tk.W, pady=2)
        ttk.Label(conn_frame, text="名称:").grid(row=6, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog2_name_var, width=15).grid(row=6, column=1, sticky=tk.W+tk.E, padx=5)
        ttk.Label(conn_frame, text="IP:").grid(row=7, column=0, sticky=tk.W, padx=(20, 0))
        ttk.Entry(conn_frame, textvariable=self.dog2_ip_var, width=15).grid(row=7, column=1, sticky=tk.W+tk.E, padx=5)
        
        # 状态显示
        ttk.Label(conn_frame, text="状态:").grid(row=8, column=0, sticky=tk.W, padx=(20, 0))
        status2_label = ttk.Label(conn_frame, textvariable=self.dog2_status_var, foreground="red")
        status2_label.grid(row=8, column=1, sticky=tk.W, padx=5)
        
        # 连接按钮
        ttk.Separator(conn_frame, orient='horizontal').grid(row=9, column=0, columnspan=2, sticky=tk.W+tk.E, pady=10)
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
        control_frame.grid(row=0, column=1, sticky=tk.W+tk.E+tk.N, padx=(5, 0))
        
        # 控制模式选择
        mode_selection_frame = ttk.LabelFrame(control_frame, text="控制模式选择", padding="5")
        mode_selection_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 控制模式单选按钮
        mode_frame = ttk.Frame(mode_selection_frame)
        mode_frame.pack(fill=tk.X, pady=2)
        
        self.mode_all_radio = ttk.Radiobutton(mode_frame, text="同时控制两台", 
                                             variable=self.control_mode_var, value="all",
                                             command=self._on_control_mode_change)
        self.mode_all_radio.pack(side=tk.LEFT, padx=5)
        
        self.mode_single_radio = ttk.Radiobutton(mode_frame, text="单独控制：", 
                                                variable=self.control_mode_var, value="single",
                                                command=self._on_control_mode_change)
        self.mode_single_radio.pack(side=tk.LEFT, padx=5)
        
        # 机器狗选择下拉框
        self.dog_selection_combo = ttk.Combobox(mode_frame, textvariable=self.selected_dog_var,
                                               values=["Dog1", "Dog2"], state="readonly", width=8)
        self.dog_selection_combo.pack(side=tk.LEFT, padx=5)
        self.dog_selection_combo.bind("<<ComboboxSelected>>", self._on_dog_selection_change)
        
        # 初始状态设置
        self.dog_selection_combo.config(state="disabled")
        
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
        trigger_frame = ttk.Frame(auto_frame)
        trigger_frame.pack(pady=5)
        
        # 第一排触发按钮
        trigger_frame1 = ttk.Frame(trigger_frame)
        trigger_frame1.pack(pady=2)
        
        self.trigger_dance_btn = ttk.Button(trigger_frame1, text="🎭 触发跳舞", command=self._on_trigger_dance)
        self.trigger_dance_btn.pack(side=tk.LEFT, padx=2)
        
        self.trigger_stretch_btn = ttk.Button(trigger_frame1, text="🤸 触发伸展", command=self._on_trigger_stretch)
        self.trigger_stretch_btn.pack(side=tk.LEFT, padx=2)
        
        self.trigger_wallow_btn = ttk.Button(trigger_frame1, text="🔄 触发打滚", command=self._on_trigger_wallow)
        self.trigger_wallow_btn.pack(side=tk.LEFT, padx=2)
        
        # 第二排触发按钮
        trigger_frame2 = ttk.Frame(trigger_frame)
        trigger_frame2.pack(pady=2)
        
        self.trigger_flip_btn = ttk.Button(trigger_frame2, text="🤸 触发空翻", command=self._on_trigger_flip)
        self.trigger_flip_btn.pack(side=tk.LEFT, padx=2)
        
        self.trigger_pounce_btn = ttk.Button(trigger_frame2, text="🦘 触发扑跃", command=self._on_trigger_pounce)
        self.trigger_pounce_btn.pack(side=tk.LEFT, padx=2)
        
        self.trigger_heart_btn = ttk.Button(trigger_frame2, text="❤️ 触发比心", command=self._on_trigger_heart)
        self.trigger_heart_btn.pack(side=tk.LEFT, padx=2)
        
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
        
        # 基础姿态按钮（第一排）
        posture_btn_frame1 = ttk.Frame(posture_frame)
        posture_btn_frame1.pack(pady=2)
        
        self.sit_btn = ttk.Button(posture_btn_frame1, text="坐下", command=self._on_sit)
        self.sit_btn.pack(side=tk.LEFT, padx=2)
        
        self.stand_btn = ttk.Button(posture_btn_frame1, text="站立", command=self._on_stand)
        self.stand_btn.pack(side=tk.LEFT, padx=2)
        
        self.hello_btn = ttk.Button(posture_btn_frame1, text="👋 打招呼", command=self._on_hello)
        self.hello_btn.pack(side=tk.LEFT, padx=2)
        
        self.stretch_btn = ttk.Button(posture_btn_frame1, text="🤸 伸展", command=self._on_stretch)
        self.stretch_btn.pack(side=tk.LEFT, padx=2)
        
        # 特技动作按钮（第二排）
        posture_btn_frame2 = ttk.Frame(posture_frame)
        posture_btn_frame2.pack(pady=2)
        
        self.wallow_btn = ttk.Button(posture_btn_frame2, text="🔄 打滚", command=self._on_wallow)
        self.wallow_btn.pack(side=tk.LEFT, padx=2)
        
        self.scrape_btn = ttk.Button(posture_btn_frame2, text="🐾 刨地", command=self._on_scrape)
        self.scrape_btn.pack(side=tk.LEFT, padx=2)
        
        self.wiggle_hips_btn = ttk.Button(posture_btn_frame2, text="💃 扭臀", command=self._on_wiggle_hips)
        self.wiggle_hips_btn.pack(side=tk.LEFT, padx=2)
        
        self.finger_heart_btn = ttk.Button(posture_btn_frame2, text="❤️ 比心", command=self._on_finger_heart)
        self.finger_heart_btn.pack(side=tk.LEFT, padx=2)
        
        # 高难度动作按钮（第三排）
        posture_btn_frame3 = ttk.Frame(posture_frame)
        posture_btn_frame3.pack(pady=2)
        
        self.handstand_btn = ttk.Button(posture_btn_frame3, text="🤸 倒立", command=self._on_handstand)
        self.handstand_btn.pack(side=tk.LEFT, padx=2)
        
        self.front_flip_btn = ttk.Button(posture_btn_frame3, text="🔄 前空翻", command=self._on_front_flip)
        self.front_flip_btn.pack(side=tk.LEFT, padx=2)
        
        self.back_flip_btn = ttk.Button(posture_btn_frame3, text="🔄 后空翻", command=self._on_back_flip)
        self.back_flip_btn.pack(side=tk.LEFT, padx=2)
        
        self.pounce_btn = ttk.Button(posture_btn_frame3, text="🦘 扑跃", command=self._on_front_pounce)
        self.pounce_btn.pack(side=tk.LEFT, padx=2)
        
        # 初始状态设置为禁用
        self._set_control_buttons_state("disabled")
    
    def _create_log_panel(self, parent):
        """创建日志面板"""
        # 日志框架
        log_frame = ttk.LabelFrame(parent, text="系统日志", padding="5")
        log_frame.grid(row=2, column=0, columnspan=2, sticky=tk.W+tk.E+tk.N+tk.S, pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        
        # 日志控制栏
        log_control_frame = ttk.Frame(log_frame)
        log_control_frame.grid(row=0, column=0, sticky=tk.W+tk.E, pady=(0, 5))
        
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
        self.log_text.grid(row=1, column=0, sticky=tk.W+tk.E+tk.N+tk.S)
    
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
        connected_dogs = []
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
                connected_dogs.append(dog1_name)
            
            dog2_name = self.dog2_name_var.get()
            dog2_ip = self.dog2_ip_var.get()
            # 只有当用户输入了有效的第二台机器狗信息时才添加
            if dog2_name and dog2_ip and dog2_ip != "192.168.31.246":
                self.controller.add_dog(
                    dog2_name,
                    dog2_ip,
                    connection_method=WebRTCConnectionMethod.LocalSTA
                )
                connected_dogs.append(dog2_name)
        
        # 更新机器狗选择列表
        if connected_dogs:
            self.dog_selection_combo.config(values=connected_dogs)
            if connected_dogs[0] not in ["Dog1", "Dog2"]:
                self.selected_dog_var.set(connected_dogs[0])
        
        # 异步连接
        if self.controller:
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
    
    def _on_trigger_stretch(self):
        """触发伸展动作"""
        if self.movement:
            self._run_async(self.movement.trigger_specific_action("Stretch"))
    
    def _on_trigger_wallow(self):
        """触发打滚动作"""
        if self.movement:
            self._run_async(self.movement.trigger_specific_action("Wallow"))
    
    def _on_trigger_flip(self):
        """触发空翻动作"""
        if self.movement:
            self._run_async(self.movement.trigger_specific_action("FrontFlip"))
    
    def _on_trigger_pounce(self):
        """触发扑跃动作"""
        if self.movement:
            self._run_async(self.movement.trigger_specific_action("FrontPounce"))
    
    def _on_trigger_heart(self):
        """触发比心动作"""
        if self.movement:
            self._run_async(self.movement.trigger_specific_action("FingerHeart"))
    
    def _on_manual_mode(self):
        """启用手动控制模式"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.MANUAL_CONTROL))
    
    def _on_move_forward(self):
        """前进"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.move_forward_single(dog_name))
            else:
                self._run_async(self.movement.move_forward())
    
    def _on_move_backward(self):
        """后退"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.move_backward_single(dog_name))
            else:
                self._run_async(self.movement.move_backward())
    
    def _on_move_left(self):
        """左移"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.move_left_single(dog_name))
            else:
                self._run_async(self.movement.move_left())
    
    def _on_move_right(self):
        """右移"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.move_right_single(dog_name))
            else:
                self._run_async(self.movement.move_right())
    
    def _on_turn_left(self):
        """左转"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.turn_left_single(dog_name))
            else:
                self._run_async(self.movement.turn_left())
    
    def _on_turn_right(self):
        """右转"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.turn_right_single(dog_name))
            else:
                self._run_async(self.movement.turn_right())
    
    def _on_stop_move(self):
        """停止移动"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.stop_move_single(dog_name))
            else:
                self._run_async(self.movement.stop_move())
    
    def _on_sit(self):
        """坐下"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.sit_down_single(dog_name))
            else:
                self._run_async(self.movement.sit_down())
    
    def _on_stand(self):
        """站立"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.stand_up_single(dog_name))
            else:
                self._run_async(self.movement.stand_up())
    
    def _on_hello(self):
        """打招呼"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.say_hello_single(dog_name))
            else:
                self._run_async(self.movement.say_hello())
    
    def _on_stretch(self):
        """伸展"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.stretch_single(dog_name))
            else:
                self._run_async(self.movement.stretch())
    
    def _on_wallow(self):
        """打滚"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.wallow_single(dog_name))
            else:
                self._run_async(self.movement.wallow())
    
    def _on_scrape(self):
        """刨地"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.scrape_single(dog_name))
            else:
                self._run_async(self.movement.scrape())
    
    def _on_wiggle_hips(self):
        """扭臀"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.wiggle_hips_single(dog_name))
            else:
                self._run_async(self.movement.wiggle_hips())
    
    def _on_finger_heart(self):
        """比心"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.finger_heart_single(dog_name))
            else:
                self._run_async(self.movement.finger_heart())
    
    def _on_handstand(self):
        """倒立"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.handstand_single(dog_name))
            else:
                self._run_async(self.movement.handstand())
    
    def _on_front_flip(self):
        """前空翻"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.front_flip_single(dog_name))
            else:
                self._run_async(self.movement.front_flip())
    
    def _on_back_flip(self):
        """后空翻"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.back_flip_single(dog_name))
            else:
                self._run_async(self.movement.back_flip())
    
    def _on_front_pounce(self):
        """前扑跃"""
        if self.movement:
            if self.control_mode_var.get() == "single":
                dog_name = self.selected_dog_var.get()
                self._run_async(self.movement.front_pounce_single(dog_name))
            else:
                self._run_async(self.movement.front_pounce())
    
    def _on_control_mode_change(self):
        """控制模式变化回调"""
        mode = self.control_mode_var.get()
        
        if mode == "single":
            self.dog_selection_combo.config(state="readonly")
        else:
            self.dog_selection_combo.config(state="disabled")
        
        # 更新控制器模式
        if self.controller:
            if mode == "all":
                self.controller.set_control_mode("all")
            else:  # single
                selected_dog = self.selected_dog_var.get()
                self.controller.set_control_mode("single", [selected_dog])
    
    def _on_dog_selection_change(self, event):
        """机器狗选择变化回调"""
        if self.control_mode_var.get() == "single" and self.controller:
            selected_dog = self.selected_dog_var.get()
            self.controller.set_control_mode("single", [selected_dog])
    
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
            self.trigger_dance_btn, self.trigger_stretch_btn, self.trigger_wallow_btn,
            self.trigger_flip_btn, self.trigger_pounce_btn, self.trigger_heart_btn,
            self.manual_mode_btn, self.forward_btn, self.backward_btn, self.left_btn, 
            self.right_btn, self.turn_left_btn, self.turn_right_btn, self.stop_btn, 
            self.sit_btn, self.stand_btn, self.hello_btn, self.stretch_btn, 
            self.wallow_btn, self.scrape_btn, self.wiggle_hips_btn, self.finger_heart_btn, 
            self.handstand_btn, self.front_flip_btn, self.back_flip_btn, self.pounce_btn
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