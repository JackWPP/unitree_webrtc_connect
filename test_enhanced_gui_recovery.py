#!/usr/bin/env python3
"""
测试增强版GUI恢复的功能
验证正方形走路、舞蹈派对、紧急停止等功能是否正常恢复
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading
import time

from dual_dog_controller import DualDogController
from dual_dog_movement import DualDogMovement, MovementPattern


class RecoveryTestGUI:
    """恢复功能测试GUI"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("自动运动功能恢复测试")
        self.root.geometry("800x400")
        
        # 控制器和运动控制
        self.controller = None
        self.movement = None
        self.asyncio_loop = None
        self.asyncio_thread = None
        
        # 状态变量
        self.status_var = tk.StringVar(value="未初始化")
        
        self._setup_gui()
        self._start_asyncio_thread()
    
    def _setup_gui(self):
        """设置GUI"""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 状态显示
        status_frame = ttk.LabelFrame(main_frame, text="系统状态", padding="10")
        status_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(status_frame, text="状态:").pack(side=tk.LEFT)
        ttk.Label(status_frame, textvariable=self.status_var, foreground="blue").pack(side=tk.LEFT, padx=(5, 0))
        
        # 自动运动模式测试
        auto_frame = ttk.LabelFrame(main_frame, text="自动运动模式测试", padding="10")
        auto_frame.pack(fill=tk.X, pady=(0, 10))
        
        # 第一排按钮
        btn_frame1 = ttk.Frame(auto_frame)
        btn_frame1.pack(fill=tk.X, pady=5)
        
        self.square_btn = ttk.Button(btn_frame1, text="🏃 开始正方形走路", 
                                    command=self._test_square_walk)
        self.square_btn.pack(side=tk.LEFT, padx=5)
        
        self.dance_btn = ttk.Button(btn_frame1, text="💃 开始舞蹈派对", 
                                   command=self._test_dance_party)
        self.dance_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame1, text="⏹️ 停止自动运动", 
                                  command=self._test_stop_auto)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.emergency_btn = ttk.Button(btn_frame1, text="🚨 紧急停止", 
                                       command=self._test_emergency_stop,
                                       style="Emergency.TButton")
        self.emergency_btn.pack(side=tk.LEFT, padx=10)
        
        # 第二排按钮 - 扑跃测试
        btn_frame2 = ttk.Frame(auto_frame)
        btn_frame2.pack(fill=tk.X, pady=5)
        
        self.pounce_once_btn = ttk.Button(btn_frame2, text="🦘 扑跃一次", 
                                         command=self._test_pounce_once)
        self.pounce_once_btn.pack(side=tk.LEFT, padx=5)
        
        self.pounce_triple_btn = ttk.Button(btn_frame2, text="🦘🦘🦘 扑跃三次", 
                                           command=self._test_pounce_triple)
        self.pounce_triple_btn.pack(side=tk.LEFT, padx=5)
        
        # 测试日志
        log_frame = ttk.LabelFrame(main_frame, text="测试日志", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = tk.Text(log_frame, height=10, width=80)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 初始禁用按钮
        self._set_buttons_state("disabled")
    
    def _start_asyncio_thread(self):
        """启动asyncio线程"""
        def run_asyncio():
            self.asyncio_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.asyncio_loop)
            
            # 创建控制器和运动控制
            self.controller = DualDogController()
            self.movement = DualDogMovement(self.controller)
            
            # 更新状态
            self.status_var.set("系统已初始化")
            self._set_buttons_state("normal")
            
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
    
    def _set_buttons_state(self, state):
        """设置按钮状态"""
        self.root.after(0, lambda: self._update_buttons_state(state))
    
    def _update_buttons_state(self, state):
        """更新按钮状态"""
        buttons = [
            self.square_btn, self.dance_btn, self.stop_btn, 
            self.emergency_btn, self.pounce_once_btn, self.pounce_triple_btn
        ]
        for btn in buttons:
            btn.config(state=state)
    
    def _log_message(self, message):
        """记录测试日志"""
        timestamp = time.strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        self.root.after(0, lambda: self._add_log(log_msg))
    
    def _add_log(self, message):
        """添加日志到文本框"""
        self.log_text.insert(tk.END, message + '\n')
        self.log_text.see(tk.END)
    
    # 测试方法
    def _test_square_walk(self):
        """测试正方形走路"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.SQUARE_WALK))
            self._log_message("✅ 测试: 开始正方形走路")
            self.status_var.set("正方形走路中")
    
    def _test_dance_party(self):
        """测试舞蹈派对"""
        if self.movement:
            self._run_async(self.movement.start_pattern(MovementPattern.DANCE_PARTY))
            self._log_message("✅ 测试: 开始舞蹈派对")
            self.status_var.set("舞蹈派对中")
    
    def _test_stop_auto(self):
        """测试停止自动运动"""
        if self.movement:
            self._run_async(self.movement.stop_movement())
            self._log_message("✅ 测试: 停止自动运动")
            self.status_var.set("已停止")
    
    def _test_emergency_stop(self):
        """测试紧急停止"""
        if self.movement:
            self._run_async(self.movement.emergency_stop())
            self._log_message("🚨 测试: 紧急停止")
            self.status_var.set("紧急停止")
    
    def _test_pounce_once(self):
        """测试单次扑跃"""
        if self.movement:
            self._run_async(self.movement.front_pounce())
            self._log_message("🦘 测试: 单次扑跃")
    
    def _test_pounce_triple(self):
        """测试三次扑跃"""
        if self.movement:
            self._run_async(self._execute_triple_pounce())
            self._log_message("🦘🦘🦘 测试: 开始三次连续扑跃")
    
    async def _execute_triple_pounce(self):
        """执行三次连续扑跃"""
        try:
            for i in range(3):
                self._log_message(f"🦘 执行第 {i+1} 次扑跃")
                await self.movement.front_pounce()
                
                if i < 2:  # 前两次之间等待
                    await asyncio.sleep(3.0)
            
            self._log_message("✅ 三次连续扑跃完成")
        except Exception as e:
            self._log_message(f"❌ 三次扑跃执行失败: {e}")
    
    def run(self):
        """运行测试程序"""
        self._log_message("🚀 自动运动功能恢复测试程序启动")
        self._log_message("📝 测试项目:")
        self._log_message("  - 正方形走路功能")
        self._log_message("  - 舞蹈派对功能") 
        self._log_message("  - 停止自动运动功能")
        self._log_message("  - 紧急停止功能")
        self._log_message("  - 单次扑跃功能")
        self._log_message("  - 三次连续扑跃功能")
        self._log_message("⏳ 等待系统初始化...")
        
        self.root.mainloop()


def main():
    """主函数"""
    app = RecoveryTestGUI()
    app.run()


if __name__ == "__main__":
    main()