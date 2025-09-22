#!/usr/bin/env python3
"""
双机器狗控制系统核心类
支持同时控制两台Unitree Go2机器狗，包含完整的连接管理、运动控制和状态监控功能
"""

import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass
from enum import Enum

from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod
from go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD


class DogStatus(Enum):
    """机器狗状态枚举"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    MOVING = "moving"
    DANCING = "dancing"
    IDLE = "idle"


@dataclass
class DogInfo:
    """机器狗信息数据类"""
    name: str
    ip: str
    serial_number: Optional[str] = None
    connection_method: WebRTCConnectionMethod = WebRTCConnectionMethod.LocalSTA
    status: DogStatus = DogStatus.DISCONNECTED
    connection: Optional[Go2WebRTCConnection] = None
    last_heartbeat: float = 0
    error_message: str = ""


class DualDogController:
    """双机器狗控制器核心类"""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or self._setup_logger()
        self.dogs: Dict[str, DogInfo] = {}
        self.status_callbacks: List[Callable] = []
        self.is_running = False
        self._monitor_task = None
        
        # 选择性控制设置
        self.control_mode = "all"  # "all", "single", "selected"
        self.selected_dogs: List[str] = []  # 选中的机器狗列表
        
    def _setup_logger(self) -> logging.Logger:
        """设置日志系统"""
        logger = logging.getLogger("DualDogController")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        return logger
    
    def add_dog(self, name: str, ip: str, serial_number: Optional[str] = None,
                connection_method: WebRTCConnectionMethod = WebRTCConnectionMethod.LocalSTA) -> bool:
        """添加机器狗到控制列表"""
        if name in self.dogs:
            self.logger.warning(f"机器狗 {name} 已存在，将覆盖配置")
        
        dog_info = DogInfo(
            name=name,
            ip=ip,
            serial_number=serial_number,
            connection_method=connection_method
        )
        
        self.dogs[name] = dog_info
        self.logger.info(f"添加机器狗: {name} (IP: {ip})")
        return True
    
    def remove_dog(self, name: str) -> bool:
        """移除机器狗"""
        if name not in self.dogs:
            self.logger.warning(f"机器狗 {name} 不存在")
            return False
        
        # 如果连接着，先断开
        if self.dogs[name].connection:
            asyncio.create_task(self._disconnect_dog(name))
        
        del self.dogs[name]
        self.logger.info(f"移除机器狗: {name}")
        return True
    
    async def connect_dog(self, name: str) -> bool:
        """连接单个机器狗"""
        if name not in self.dogs:
            self.logger.error(f"机器狗 {name} 不存在")
            return False
        
        dog = self.dogs[name]
        dog.status = DogStatus.CONNECTING
        self._notify_status_change(name, dog.status)
        
        try:
            # 创建连接
            if dog.connection_method == WebRTCConnectionMethod.LocalSTA:
                conn = Go2WebRTCConnection(dog.connection_method, ip=dog.ip)
            elif dog.connection_method == WebRTCConnectionMethod.LocalAP:
                conn = Go2WebRTCConnection(dog.connection_method)
            else:  # Remote
                conn = Go2WebRTCConnection(
                    dog.connection_method, 
                    serialNumber=dog.serial_number
                )
            
            # 建立连接
            await conn.connect()
            
            # 设置状态回调
            self._setup_dog_callbacks(name, conn)
            
            dog.connection = conn
            dog.status = DogStatus.CONNECTED
            dog.last_heartbeat = time.time()
            dog.error_message = ""
            
            self.logger.info(f"机器狗 {name} 连接成功")
            self._notify_status_change(name, dog.status)
            
            # 初始化为normal模式
            await self._initialize_dog_mode(name)
            
            return True
            
        except Exception as e:
            dog.status = DogStatus.ERROR
            dog.error_message = str(e)
            self.logger.error(f"机器狗 {name} 连接失败: {e}")
            self._notify_status_change(name, dog.status)
            return False
    
    async def _disconnect_dog(self, name: str) -> bool:
        """断开单个机器狗连接"""
        if name not in self.dogs:
            return False
        
        dog = self.dogs[name]
        if dog.connection:
            try:
                await dog.connection.disconnect()
                self.logger.info(f"机器狗 {name} 断开连接")
            except Exception as e:
                self.logger.error(f"断开机器狗 {name} 连接时出错: {e}")
        
        dog.connection = None
        dog.status = DogStatus.DISCONNECTED
        dog.error_message = ""
        self._notify_status_change(name, dog.status)
        return True
    
    async def connect_all(self) -> Dict[str, bool]:
        """连接所有机器狗"""
        results = {}
        connection_tasks = []
        
        for name in self.dogs.keys():
            task = asyncio.create_task(self.connect_dog(name))
            connection_tasks.append((name, task))
        
        # 并行连接所有机器狗
        for name, task in connection_tasks:
            try:
                results[name] = await task
            except Exception as e:
                results[name] = False
                self.logger.error(f"连接机器狗 {name} 时发生异常: {e}")
        
        connected_count = sum(results.values())
        self.logger.info(f"连接完成: {connected_count}/{len(self.dogs)} 台机器狗连接成功")
        
        return results
    
    async def disconnect_all(self) -> bool:
        """断开所有机器狗连接"""
        disconnection_tasks = []
        
        for name in self.dogs.keys():
            task = asyncio.create_task(self._disconnect_dog(name))
            disconnection_tasks.append(task)
        
        await asyncio.gather(*disconnection_tasks, return_exceptions=True)
        self.logger.info("所有机器狗已断开连接")
        return True
    
    def _setup_dog_callbacks(self, name: str, conn: Go2WebRTCConnection):
        """设置机器狗状态回调"""
        def lowstate_callback(message):
            """低级状态回调"""
            dog = self.dogs[name]
            dog.last_heartbeat = time.time()
            if dog.status == DogStatus.CONNECTED:
                dog.status = DogStatus.IDLE
                self._notify_status_change(name, dog.status)
        
        # 订阅低级状态数据
        conn.datachannel.pub_sub.subscribe(RTC_TOPIC['LOW_STATE'], lowstate_callback)
    
    async def _initialize_dog_mode(self, name: str) -> bool:
        """初始化机器狗为normal模式"""
        dog = self.dogs[name]
        if not dog.connection:
            return False
        
        try:
            # 获取当前运动模式
            response = await dog.connection.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"],
                {"api_id": 1001}
            )
            
            if response['data']['header']['status']['code'] == 0:
                data = json.loads(response['data']['data'])
                current_mode = data['name']
                
                # 如果不是normal模式，切换到normal模式
                if current_mode != "normal":
                    self.logger.info(f"机器狗 {name} 切换模式: {current_mode} -> normal")
                    await dog.connection.datachannel.pub_sub.publish_request_new(
                        RTC_TOPIC["MOTION_SWITCHER"],
                        {
                            "api_id": 1002,
                            "parameter": {"name": "normal"}
                        }
                    )
                    await asyncio.sleep(2)  # 等待模式切换完成
            
            return True
            
        except Exception as e:
            self.logger.error(f"初始化机器狗 {name} 模式失败: {e}")
            return False
    
    async def send_command_to_dog(self, name: str, command: str, parameters: Optional[Dict] = None) -> bool:
        """向指定机器狗发送运动指令"""
        if name not in self.dogs:
            self.logger.error(f"机器狗 {name} 不存在")
            return False
        
        dog = self.dogs[name]
        if not dog.connection or dog.status == DogStatus.ERROR:
            self.logger.error(f"机器狗 {name} 未连接或处于错误状态")
            return False
        
        try:
            # 更新状态
            if command in ["Move"]:
                dog.status = DogStatus.MOVING
            elif command in ["Dance1", "Dance2"]:
                dog.status = DogStatus.DANCING
            
            self._notify_status_change(name, dog.status)
            
            # 构建指令
            cmd_data = {"api_id": SPORT_CMD[command]}
            if parameters:
                cmd_data["parameter"] = parameters
            
            # 发送指令
            await dog.connection.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                cmd_data
            )
            
            self.logger.info(f"向机器狗 {name} 发送指令: {command} {parameters or ''}")
            return True
            
        except Exception as e:
            self.logger.error(f"向机器狗 {name} 发送指令失败: {e}")
            dog.status = DogStatus.ERROR
            dog.error_message = str(e)
            self._notify_status_change(name, dog.status)
            return False
    
    async def send_command_to_all(self, command: str, parameters: Optional[Dict] = None) -> Dict[str, bool]:
        """向所有连接的机器狗发送相同指令"""
        return await self.send_command_to_targets(command, parameters, self._get_all_connected_dogs())
    
    async def send_command_to_selected(self, command: str, parameters: Optional[Dict] = None) -> Dict[str, bool]:
        """向选中的机器狗发送指令"""
        target_dogs = self._get_target_dogs_by_mode()
        return await self.send_command_to_targets(command, parameters, target_dogs)
    
    async def send_command_to_targets(self, command: str, parameters: Optional[Dict] = None, target_dogs: List[str] = None) -> Dict[str, bool]:
        """向指定的机器狗发送指令"""
        results = {}
        command_tasks = []
        
        if target_dogs is None:
            target_dogs = self._get_all_connected_dogs()
        
        for name in target_dogs:
            if name in self.dogs:
                dog = self.dogs[name]
                if dog.connection and dog.status not in [DogStatus.DISCONNECTED, DogStatus.ERROR]:
                    task = asyncio.create_task(self.send_command_to_dog(name, command, parameters))
                    command_tasks.append((name, task))
                else:
                    results[name] = False
            else:
                results[name] = False
        
        # 并行发送指令
        for name, task in command_tasks:
            try:
                results[name] = await task
            except Exception as e:
                results[name] = False
                self.logger.error(f"向机器狗 {name} 发送指令时发生异常: {e}")
        
        return results
    
    def get_dog_status(self, name: str) -> Optional[DogStatus]:
        """获取机器狗状态"""
        if name not in self.dogs:
            return None
        return self.dogs[name].status
    
    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有机器狗状态"""
        status = {}
        for name, dog in self.dogs.items():
            status[name] = {
                "status": dog.status.value,
                "ip": dog.ip,
                "last_heartbeat": dog.last_heartbeat,
                "error_message": dog.error_message,
                "connected": dog.connection is not None
            }
        return status
    
    def add_status_callback(self, callback: Callable):
        """添加状态变化回调函数"""
        self.status_callbacks.append(callback)
    
    def set_control_mode(self, mode: str, selected_dogs: List[str] = None):
        """设置控制模式
        
        Args:
            mode: "all" (所有), "single" (单个), "selected" (选中的)
            selected_dogs: 当mode为"single"或"selected"时，指定要控制的机器狗列表
        """
        if mode not in ["all", "single", "selected"]:
            self.logger.error(f"不支持的控制模式: {mode}")
            return False
        
        self.control_mode = mode
        
        if mode == "all":
            self.selected_dogs = list(self.dogs.keys())
        elif mode in ["single", "selected"] and selected_dogs:
            # 验证选中的机器狗是否存在
            valid_dogs = []
            for dog_name in selected_dogs:
                if dog_name in self.dogs:
                    valid_dogs.append(dog_name)
                else:
                    self.logger.warning(f"机器狗 {dog_name} 不存在")
            self.selected_dogs = valid_dogs
        else:
            self.selected_dogs = []
        
        self.logger.info(f"控制模式已设置为: {mode}, 选中的机器狗: {self.selected_dogs}")
        return True
    
    def get_control_mode(self) -> tuple:
        """获取当前控制模式"""
        return self.control_mode, self.selected_dogs.copy()
    
    def _get_target_dogs_by_mode(self) -> List[str]:
        """根据控制模式获取目标机器狗列表"""
        if self.control_mode == "all":
            return self._get_all_connected_dogs()
        elif self.control_mode in ["single", "selected"]:
            # 只返回在线的选中机器狗
            connected_dogs = self._get_all_connected_dogs()
            return [dog for dog in self.selected_dogs if dog in connected_dogs]
        else:
            return []
    
    def _get_all_connected_dogs(self) -> List[str]:
        """获取所有已连接的机器狗列表"""
        connected_dogs = []
        for name, dog in self.dogs.items():
            if dog.connection and dog.status not in [DogStatus.DISCONNECTED, DogStatus.ERROR]:
                connected_dogs.append(name)
        return connected_dogs
    
    def _notify_status_change(self, dog_name: str, status: DogStatus):
        """通知状态变化"""
        for callback in self.status_callbacks:
            try:
                callback(dog_name, status)
            except Exception as e:
                self.logger.error(f"状态回调执行失败: {e}")
    
    async def start_monitoring(self):
        """开始状态监控"""
        if self.is_running:
            return
        
        self.is_running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.logger.info("状态监控已启动")
    
    async def stop_monitoring(self):
        """停止状态监控"""
        if not self.is_running:
            return
        
        self.is_running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        
        self.logger.info("状态监控已停止")
    
    async def _monitor_loop(self):
        """监控循环"""
        while self.is_running:
            try:
                current_time = time.time()
                
                for name, dog in self.dogs.items():
                    if dog.connection and dog.status != DogStatus.DISCONNECTED:
                        # 检查心跳超时 (30秒无心跳视为连接异常)
                        if current_time - dog.last_heartbeat > 30:
                            self.logger.warning(f"机器狗 {name} 心跳超时")
                            dog.status = DogStatus.ERROR
                            dog.error_message = "心跳超时"
                            self._notify_status_change(name, dog.status)
                
                await asyncio.sleep(5)  # 每5秒检查一次
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"监控循环出错: {e}")
                await asyncio.sleep(5)