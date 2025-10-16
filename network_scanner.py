#!/usr/bin/env python3
"""
网络扫描器模块 - 用于自动发现Unitree机器狗
集成multicast_scanner的功能，提供GUI友好的接口
"""

import asyncio
import logging
import socket
import struct
import json
import threading
from typing import Dict, List, Callable, Optional
from dataclasses import dataclass
from datetime import datetime

# 导入现有的发现功能
from unitree_webrtc_connect.multicast_scanner import discover_ip_sn


@dataclass
class DiscoveredDevice:
    """发现的设备信息"""
    ip: str
    serial_number: str
    device_name: str = "Unitree Device"
    discovered_at: Optional[datetime] = None
    
    def __post_init__(self):
        if self.discovered_at is None:
            self.discovered_at = datetime.now()


class NetworkScanner:
    """网络扫描器类"""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("NetworkScanner")
        self.discovered_devices: Dict[str, DiscoveredDevice] = {}
        self.scan_callbacks: List[Callable] = []
        self.is_scanning = False
        self.scan_task = None
        
    def add_scan_callback(self, callback: Callable):
        """添加扫描结果回调"""
        self.scan_callbacks.append(callback)
    
    def _notify_scan_result(self, devices: List[DiscoveredDevice]):
        """通知扫描结果"""
        for callback in self.scan_callbacks:
            try:
                callback(devices)
            except Exception as e:
                self.logger.error(f"扫描结果回调执行失败: {e}")
    
    async def scan_once(self, timeout: float = 3.0) -> List[DiscoveredDevice]:
        """执行一次网络扫描"""
        self.logger.info("开始扫描网络中的Unitree设备...")
        
        try:
            # 使用现有的discover_ip_sn函数
            def scan_in_thread():
                return discover_ip_sn(timeout=int(timeout))
            
            # 在线程池中执行扫描，避免阻塞
            loop = asyncio.get_event_loop()
            serial_to_ip = await loop.run_in_executor(None, scan_in_thread)
            
            # 转换为DiscoveredDevice对象
            devices = []
            for serial_number, ip in serial_to_ip.items():
                device = DiscoveredDevice(
                    ip=ip,
                    serial_number=serial_number,
                    device_name=f"Unitree-{serial_number[-4:]}"  # 使用序列号后4位作为简短名称
                )
                devices.append(device)
                self.discovered_devices[serial_number] = device
            
            self.logger.info(f"扫描完成，发现 {len(devices)} 台Unitree设备")
            
            # 通知回调
            self._notify_scan_result(devices)
            
            return devices
            
        except Exception as e:
            self.logger.error(f"网络扫描失败: {e}")
            return []
    
    async def start_continuous_scan(self, interval: float = 10.0, timeout: float = 3.0):
        """开始连续扫描"""
        if self.is_scanning:
            self.logger.warning("扫描已在进行中")
            return
        
        self.is_scanning = True
        self.logger.info(f"开始连续扫描，间隔 {interval} 秒")
        
        try:
            while self.is_scanning:
                await self.scan_once(timeout)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            self.logger.info("连续扫描被取消")
        except Exception as e:
            self.logger.error(f"连续扫描出错: {e}")
        finally:
            self.is_scanning = False
    
    def stop_continuous_scan(self):
        """停止连续扫描"""
        if self.is_scanning:
            self.is_scanning = False
            if self.scan_task:
                self.scan_task.cancel()
            self.logger.info("已停止连续扫描")
    
    def get_discovered_devices(self) -> List[DiscoveredDevice]:
        """获取已发现的设备列表"""
        return list(self.discovered_devices.values())
    
    def get_device_by_ip(self, ip: str) -> Optional[DiscoveredDevice]:
        """根据IP获取设备"""
        for device in self.discovered_devices.values():
            if device.ip == ip:
                return device
        return None
    
    def get_device_by_serial(self, serial_number: str) -> Optional[DiscoveredDevice]:
        """根据序列号获取设备"""
        return self.discovered_devices.get(serial_number)
    
    def clear_discovered_devices(self):
        """清空已发现的设备"""
        self.discovered_devices.clear()
        self.logger.info("已清空发现的设备列表")


class NetworkScannerGUI:
    """网络扫描器GUI组件"""
    
    def __init__(self, parent_frame, scanner: NetworkScanner):
        self.parent_frame = parent_frame
        self.scanner = scanner
        self.devices_listbox = None
        self.scan_button = None
        self.auto_scan_var = None
        
        # 添加扫描结果回调
        self.scanner.add_scan_callback(self._on_devices_discovered)
        
        self._create_gui()
    
    def _create_gui(self):
        """创建GUI组件"""
        import tkinter as tk
        from tkinter import ttk
        
        # 扫描控制框架
        scan_control_frame = ttk.Frame(self.parent_frame)
        scan_control_frame.pack(fill=tk.X, pady=5)
        
        # 扫描按钮
        self.scan_button = ttk.Button(scan_control_frame, text="🔍 扫描网络", command=self._on_scan_clicked)
        self.scan_button.pack(side=tk.LEFT, padx=2)
        
        # 自动扫描选项
        self.auto_scan_var = tk.BooleanVar()
        auto_scan_check = ttk.Checkbutton(scan_control_frame, text="自动扫描", 
                                         variable=self.auto_scan_var, command=self._on_auto_scan_toggled)
        auto_scan_check.pack(side=tk.LEFT, padx=5)
        
        # 清空按钮
        clear_button = ttk.Button(scan_control_frame, text="清空", command=self._on_clear_clicked)
        clear_button.pack(side=tk.RIGHT, padx=2)
        
        # 设备列表
        devices_frame = ttk.LabelFrame(self.parent_frame, text="发现的设备", padding="5")
        devices_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # 创建Treeview来显示设备
        columns = ('name', 'ip', 'serial', 'time')
        self.devices_tree = ttk.Treeview(devices_frame, columns=columns, show='headings', height=6)
        
        # 定义列标题
        self.devices_tree.heading('name', text='设备名称')
        self.devices_tree.heading('ip', text='IP地址')
        self.devices_tree.heading('serial', text='序列号')
        self.devices_tree.heading('time', text='发现时间')
        
        # 定义列宽
        self.devices_tree.column('name', width=100)
        self.devices_tree.column('ip', width=120)
        self.devices_tree.column('serial', width=120)
        self.devices_tree.column('time', width=120)
        
        self.devices_tree.pack(fill=tk.BOTH, expand=True)
        
        # 双击选择设备
        self.devices_tree.bind('<Double-1>', self._on_device_selected)
    
    def _on_scan_clicked(self):
        """扫描按钮点击事件"""
        # 检查是否有运行中的事件循环
        try:
            loop = asyncio.get_running_loop()
            # 在现有循环中创建任务
            asyncio.run_coroutine_threadsafe(self.scanner.scan_once(), loop)
        except RuntimeError:
            # 没有运行中的事件循环，在线程池中执行
            import threading
            def run_scan():
                asyncio.run(self.scanner.scan_once())
            threading.Thread(target=run_scan, daemon=True).start()
    
    def _on_auto_scan_toggled(self):
        """自动扫描切换事件"""
        if self.auto_scan_var and self.auto_scan_var.get():
            try:
                loop = asyncio.get_running_loop()
                self.scanner.scan_task = asyncio.run_coroutine_threadsafe(
                    self.scanner.start_continuous_scan(), loop
                )
            except RuntimeError:
                pass  # 没有运行中的事件循环
        else:
            self.scanner.stop_continuous_scan()
    
    def _on_clear_clicked(self):
        """清空按钮点击事件"""
        self.scanner.clear_discovered_devices()
        # 清空GUI显示
        for item in self.devices_tree.get_children():
            self.devices_tree.delete(item)
    
    def _on_device_selected(self, event):
        """设备选择事件"""
        selection = self.devices_tree.selection()
        if selection:
            item = self.devices_tree.item(selection[0])
            values = item['values']
            if values:
                device_name, ip, serial, _ = values
                # 触发设备选择回调
                if hasattr(self, 'device_selection_callback'):
                    self.device_selection_callback(ip, serial, device_name)
    
    def _on_devices_discovered(self, devices: List[DiscoveredDevice]):
        """设备发现回调"""
        # 更新GUI显示 - 需要在主线程中执行
        def update_gui():
            # 清空现有项目
            for item in self.devices_tree.get_children():
                self.devices_tree.delete(item)
            
            # 添加新发现的设备
            for device in devices:
                time_str = device.discovered_at.strftime("%H:%M:%S")
                self.devices_tree.insert('', 'end', values=(
                    device.device_name,
                    device.ip,
                    device.serial_number,
                    time_str
                ))
        
        # 使用after方法在主线程中执行GUI更新
        if hasattr(self.parent_frame, 'after'):
            self.parent_frame.after(0, update_gui)
    
    def set_device_selection_callback(self, callback: Callable):
        """设置设备选择回调"""
        self.device_selection_callback = callback