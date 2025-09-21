#!/usr/bin/env python3
"""
单机器狗控制系统启动器
专门为单台机器狗优化的启动程序
"""

import sys
import asyncio
import logging
from pathlib import Path

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from dual_dog_controller import DualDogController, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

class SingleDogGUI:
    """单机器狗简化GUI"""
    
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, messagebox, scrolledtext
        
        self.root = tk.Tk()
        self.root.title("单机器狗控制系统 v1.0")
        self.root.geometry("800x600")
        
        # 控制器
        self.controller = None
        self.movement = None
        self.asyncio_loop = None
        
        # 变量
        self.dog_ip_var = tk.StringVar(value="192.168.31.245")
        self.dog_status_var = tk.StringVar(value="未连接")
        self.movement_status_var = tk.StringVar(value="停止")
        
        self._setup_gui()
        self._start_asyncio()
        
    def _setup_gui(self):
        import tkinter as tk
        from tkinter import ttk, scrolledtext
        
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill="both", expand=True)
        
        # 连接区域
        conn_frame = ttk.LabelFrame(main_frame, text="机器狗连接", padding="10")
        conn_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(conn_frame, text="IP地址:").pack(side="left")
        ttk.Entry(conn_frame, textvariable=self.dog_ip_var, width=20).pack(side="left", padx=5)
        
        self.connect_btn = ttk.Button(conn_frame, text="连接", command=self._on_connect)
        self.connect_btn.pack(side="left", padx=5)
        
        self.disconnect_btn = ttk.Button(conn_frame, text="断开", command=self._on_disconnect, state="disabled")
        self.disconnect_btn.pack(side="left", padx=2)
        
        # 状态显示
        status_frame = ttk.Frame(conn_frame)
        status_frame.pack(side="right")
        ttk.Label(status_frame, text="状态:").pack(side="left")
        self.status_label = ttk.Label(status_frame, textvariable=self.dog_status_var, foreground="red")
        self.status_label.pack(side="left", padx=5)
        
        # 控制区域
        control_frame = ttk.LabelFrame(main_frame, text="运动控制", padding="10")
        control_frame.pack(fill="x", pady=(0, 10))
        
        # 自动模式
        auto_frame = ttk.Frame(control_frame)
        auto_frame.pack(fill="x", pady=(0, 5))
        
        self.square_btn = ttk.Button(auto_frame, text="🔄 正方形走路", command=self._on_square_walk, state="disabled")
        self.square_btn.pack(side="left", padx=2)
        
        self.dance_btn = ttk.Button(auto_frame, text="💃 跳舞", command=self._on_dance, state="disabled")
        self.dance_btn.pack(side="left", padx=2)
        
        self.stop_btn = ttk.Button(auto_frame, text="⏹ 停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=2)
        
        # 手动控制
        manual_frame = ttk.Frame(control_frame)
        manual_frame.pack(pady=(5, 0))
        
        self.manual_btn = ttk.Button(manual_frame, text="🎮 手动控制", command=self._on_manual_mode, state="disabled")
        self.manual_btn.pack(side="left", padx=2)
        
        # 方向按钮
        direction_frame = ttk.Frame(manual_frame)
        direction_frame.pack(side="left", padx=10)
        
        # 上
        self.forward_btn = ttk.Button(direction_frame, text="↑", command=self._on_forward, state="disabled", width=3)
        self.forward_btn.grid(row=0, column=1)
        
        # 中
        self.left_btn = ttk.Button(direction_frame, text="←", command=self._on_left, state="disabled", width=3)
        self.left_btn.grid(row=1, column=0)
        
        self.stop_move_btn = ttk.Button(direction_frame, text="⏹", command=self._on_stop_move, state="disabled", width=3)
        self.stop_move_btn.grid(row=1, column=1)
        
        self.right_btn = ttk.Button(direction_frame, text="→", command=self._on_right, state="disabled", width=3)
        self.right_btn.grid(row=1, column=2)
        
        # 下
        self.backward_btn = ttk.Button(direction_frame, text="↓", command=self._on_backward, state="disabled", width=3)
        self.backward_btn.grid(row=2, column=1)
        
        # 状态显示
        status_info_frame = ttk.Frame(control_frame)
        status_info_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(status_info_frame, text="运动状态:").pack(side="left")
        ttk.Label(status_info_frame, textvariable=self.movement_status_var, foreground="blue").pack(side="left", padx=5)
        
        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="5")
        log_frame.pack(fill="both", expand=True)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15)
        self.log_text.pack(fill="both", expand=True)
        
        # 设置日志处理器
        self._setup_logging()
        
        self.control_buttons = [
            self.square_btn, self.dance_btn, self.stop_btn, self.manual_btn,
            self.forward_btn, self.left_btn, self.right_btn, self.backward_btn, self.stop_move_btn
        ]
        
    def _setup_logging(self):
        """设置日志到GUI"""
        class GUIHandler(logging.Handler):
            def __init__(self, text_widget):
                super().__init__()
                self.text_widget = text_widget
                
            def emit(self, record):
                msg = self.format(record)
                def append():
                    self.text_widget.insert("end", msg + "\n")
                    self.text_widget.see("end")
                self.text_widget.after(0, append)
        
        handler = GUIHandler(self.log_text)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        
    def _start_asyncio(self):
        """启动异步事件循环"""
        import threading
        
        def run_loop():
            self.asyncio_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.asyncio_loop)
            self.asyncio_loop.run_forever()
            
        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        
        # 等待循环启动
        import time
        time.sleep(0.1)
        
    def _run_async(self, coro):
        """运行异步任务"""
        if self.asyncio_loop:
            future = asyncio.run_coroutine_threadsafe(coro, self.asyncio_loop)
            return future
        
    async def _connect_dog(self):
        """连接机器狗"""
        try:
            ip = self.dog_ip_var.get()
            logging.info(f"正在连接机器狗: {ip}")
            
            self.controller = DualDogController()
            self.movement = DualDogMovement(self.controller)
            
            # 添加状态回调
            def status_callback(dog_name, status):
                def update():
                    status_text = status.value
                    color = "green" if status.value == "connected" else "red"
                    self.dog_status_var.set(status_text)
                    self.status_label.config(foreground=color)
                    if status.value == "connected":
                        for btn in self.control_buttons:
                            btn.config(state="normal")
                self.root.after(0, update)
                
            def movement_callback(pattern, info):
                def update():
                    self.movement_status_var.set(f"{pattern.value} - {info}")
                self.root.after(0, update)
            
            self.controller.add_status_callback(status_callback)
            self.movement.add_movement_callback(movement_callback)
            
            # 启动监控
            await self.controller.start_monitoring()
            
            # 添加并连接机器狗
            self.controller.add_dog("MyDog", ip, connection_method=WebRTCConnectionMethod.LocalSTA)
            results = await self.controller.connect_all()
            
            if any(results.values()):
                logging.info("✅ 机器狗连接成功！")
                def update_ui():
                    self.connect_btn.config(state="disabled")
                    self.disconnect_btn.config(state="normal")
                self.root.after(0, update_ui)
            else:
                logging.error("❌ 机器狗连接失败")
                
        except Exception as e:
            logging.error(f"连接失败: {e}")
    
    def _on_connect(self):
        self._run_async(self._connect_dog())
        
    def _on_disconnect(self):
        async def disconnect():
            if self.controller:
                await self.controller.disconnect_all()
                await self.controller.stop_monitoring()
            logging.info("机器狗已断开连接")
            def update_ui():
                self.connect_btn.config(state="normal")
                self.disconnect_btn.config(state="disabled")
                for btn in self.control_buttons:
                    btn.config(state="disabled")
                self.dog_status_var.set("未连接")
                self.status_label.config(foreground="red")
            self.root.after(0, update_ui)
        self._run_async(disconnect())
        
    def _on_square_walk(self):
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.SQUARE_WALK))
            
    def _on_dance(self):
        if self.movement:
            self._run_async(self.movement.trigger_dance())
            
    def _on_stop(self):
        if self.movement:
            self._run_async(self.movement.stop_movement())
            
    def _on_manual_mode(self):
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.MANUAL_CONTROL))
            
    def _on_forward(self):
        if self.movement:
            self._run_async(self.movement.move_forward())
            
    def _on_backward(self):
        if self.movement:
            self._run_async(self.movement.move_backward())
            
    def _on_left(self):
        if self.movement:
            self._run_async(self.movement.move_left())
            
    def _on_right(self):
        if self.movement:
            self._run_async(self.movement.move_right())
            
    def _on_stop_move(self):
        if self.movement:
            self._run_async(self.movement.stop_move())
            
    def run(self):
        try:
            self.root.mainloop()
        finally:
            if self.asyncio_loop:
                self.asyncio_loop.call_soon_threadsafe(self.asyncio_loop.stop)


def main():
    print("🤖 单机器狗控制系统启动")
    print(f"目标IP: 192.168.31.245")
    print("=" * 50)
    
    app = SingleDogGUI()
    app.run()


if __name__ == "__main__":
    main()