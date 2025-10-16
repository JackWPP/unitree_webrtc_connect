#!/usr/bin/env python3
"""
双机器狗Web控制服务器
提供RESTful API和Web界面控制
"""

import asyncio
import threading
import json
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from flask_socketio import SocketIO, emit

from dual_dog_controller import DualDogController, DogStatus, WebRTCConnectionMethod
from dual_dog_movement import DualDogMovement, MovementPattern
from network_scanner import NetworkScanner
from unitree_webrtc_connect.multicast_scanner import discover_ip_sn
import cv2
import numpy as np
from queue import Queue
import base64


class DualDogWebServer:
    """双机器狗Web控制服务器"""
    
    def __init__(self, host='0.0.0.0', port=5000):
        self.host = host
        self.port = port
        
        # Flask应用
        self.app = Flask(__name__, template_folder='templates', static_folder='static')
        self.app.config['SECRET_KEY'] = 'dual_dog_secret_key'
        
        # CORS支持
        CORS(self.app)
        
        # SocketIO支持（用于实时状态更新）
        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')
        
        # 控制器和运动控制
        self.controller = None
        self.movement = None
        self.asyncio_loop = None
        self.asyncio_thread = None
        
        # 网络扫描器
        self.network_scanner = NetworkScanner()
        
        # 状态缓存
        self.status_cache = {}
        self.movement_status = {"pattern": "stop", "info": "", "running": False}
        self.log_messages = []  # 新增：日志消息缓存
        self.max_log_messages = 500  # 最大日志条数
        
        # 视频帧队列管理
        self.video_frame_queues = {}  # dog_name -> Queue
        self.video_active_streams = {}  # dog_name -> bool
        
        # 视频录制管理
        self.video_recording = {}  # dog_name -> {'writer': VideoWriter, 'start_time': float, 'filename': str}
        self.recordings_dir = 'recordings'  # 录制文件存储目录
        
        # 确保录制目录存在
        import os
        if not os.path.exists(self.recordings_dir):
            os.makedirs(self.recordings_dir)
        
        # 日志
        self.logger = logging.getLogger("DualDogWebServer")
        self._setup_logging()
        
        # 注册路由和事件
        self._register_routes()
        self._register_socketio_events()
        
        # 启动异步控制线程
        self._start_asyncio_thread()
    
    def _setup_logging(self):
        """设置日志系统"""
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    def _start_asyncio_thread(self):
        """启动asyncio控制线程"""
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
        time.sleep(1)
        self.logger.info("Asyncio控制线程已启动")
    
    def _run_async(self, coro):
        """在asyncio线程中运行协程"""
        if self.asyncio_loop:
            future = asyncio.run_coroutine_threadsafe(coro, self.asyncio_loop)
            return future
        return None
    
    def _add_log_message(self, level: str, message: str, source: str = "系统"):
        """添加日志消息"""
        timestamp = datetime.now().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'level': level,
            'message': message,
            'source': source
        }
        
        self.log_messages.append(log_entry)
        
        # 限制日志数量
        if len(self.log_messages) > self.max_log_messages:
            self.log_messages = self.log_messages[-self.max_log_messages:]
        
        # 通过WebSocket推送日志
        self.socketio.emit('log_message', log_entry)
        
        # 同时记录到Python日志
        log_level = getattr(logging, level.upper(), logging.INFO)
        self.logger.log(log_level, f"[{source}] {message}")
        """机器狗状态变化回调"""
    def _on_dog_status_change(self, dog_name: str, status: DogStatus):
        """机器狗状态变化回调"""
        self.status_cache[dog_name] = {
            "status": status.value,
            "timestamp": time.time()
        }
        
        # 获取完整的机器狗状态信息
        if self.controller and dog_name in self.controller.dogs:
            all_status = self.controller.get_all_status()
            dog_info = all_status.get(dog_name, {})
            
            # 通过WebSocket推送完整状态更新
            self.socketio.emit('status_update', {
                'dog_name': dog_name,
                'status': status.value,
                'dog_info': dog_info,  # 包含完整信息（含connected字段）
                'timestamp': datetime.now().isoformat()
            })
        else:
            # 如果无法获取完整信息，至少发送基本状态
            self.socketio.emit('status_update', {
                'dog_name': dog_name,
                'status': status.value,
                'timestamp': datetime.now().isoformat()
            })
        
        self.logger.info(f"机器狗 {dog_name} 状态变化: {status.value}")
        self._add_log_message("INFO", f"机器狗 {dog_name} 状态变化: {status.value}", "连接管理")
    
    def _on_movement_status_change(self, pattern: MovementPattern, step_info: str = ""):
        """运动状态变化回调"""
        self.movement_status = {
            "pattern": pattern.value,
            "info": step_info,
            "running": pattern != MovementPattern.STOP,
            "timestamp": time.time()
        }
        
        # 通过WebSocket推送运动状态更新
        self.socketio.emit('movement_update', {
            'pattern': pattern.value,
            'info': step_info,
            'running': pattern != MovementPattern.STOP,
            'timestamp': datetime.now().isoformat()
        })
        
        self.logger.info(f"运动状态变化: {pattern.value} - {step_info}")
    
    def _register_routes(self):
        """注册Web路由"""
        
        @self.app.route('/')
        def index():
            """主页"""
            return render_template('index.html')
        
        @self.app.route('/api/dogs', methods=['GET'])
        def get_dogs():
            """获取所有机器狗状态"""
            if self.controller:
                status = self.controller.get_all_status()
                return jsonify({
                    'success': True,
                    'data': status,
                    'control_mode': self.controller.get_control_mode()
                })
            return jsonify({'success': False, 'error': '控制器未初始化'})
        
        @self.app.route('/api/dogs', methods=['POST'])
        def add_dog():
            """添加机器狗"""
            try:
                data = request.get_json()
                name = data.get('name')
                ip = data.get('ip')
                serial = data.get('serial', None)
                method = data.get('method', 'LocalSTA')
                
                if not name or not ip:
                    return jsonify({'success': False, 'error': '缺少必要参数'})
                
                # 转换连接方法
                connection_method = WebRTCConnectionMethod.LocalSTA
                if method == 'LocalAP':
                    connection_method = WebRTCConnectionMethod.LocalAP
                elif method == 'Remote':
                    connection_method = WebRTCConnectionMethod.Remote
                
                if self.controller:
                    result = self.controller.add_dog(name, ip, serial, connection_method)
                    return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/dogs/<dog_name>', methods=['DELETE'])
        def remove_dog(dog_name):
            """移除机器狗"""
            if self.controller:
                result = self.controller.remove_dog(dog_name)
                return jsonify({'success': result})
            return jsonify({'success': False, 'error': '控制器未初始化'})
        
        @self.app.route('/api/dogs/connect', methods=['POST'])
        def connect_dogs():
            """连接机器狗"""
            try:
                data = request.get_json()
                dog_names = data.get('dogs', [])
                
                if not dog_names:
                    # 连接所有机器狗
                    future = self._run_async(self.controller.connect_all())
                    if future:
                        results = future.result(timeout=30)
                        return jsonify({'success': True, 'results': results})
                else:
                    # 连接指定机器狗
                    results = {}
                    for dog_name in dog_names:
                        future = self._run_async(self.controller.connect_dog(dog_name))
                        if future:
                            results[dog_name] = future.result(timeout=15)
                    return jsonify({'success': True, 'results': results})
                
                return jsonify({'success': False, 'error': '控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/dogs/disconnect', methods=['POST'])
        def disconnect_dogs():
            """断开机器狗连接"""
            try:
                if self.controller:
                    future = self._run_async(self.controller.disconnect_all())
                    if future:
                        result = future.result(timeout=15)
                        return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/control/mode', methods=['POST'])
        def set_control_mode():
            """设置控制模式"""
            try:
                data = request.get_json()
                mode = data.get('mode')
                selected_dogs = data.get('selected_dogs', [])
                
                if self.controller:
                    result = self.controller.set_control_mode(mode, selected_dogs)
                    return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/start', methods=['POST'])
        def start_movement():
            """启动运动模式"""
            try:
                data = request.get_json()
                pattern = data.get('pattern')
                
                if not pattern:
                    return jsonify({'success': False, 'error': '缺少运动模式参数'})
                
                # 转换运动模式
                movement_pattern = None
                if pattern == 'square_walk':
                    movement_pattern = MovementPattern.SQUARE_WALK
                elif pattern == 'dance_party':
                    movement_pattern = MovementPattern.DANCE_PARTY
                elif pattern == 'manual_control':
                    movement_pattern = MovementPattern.MANUAL_CONTROL
                else:
                    return jsonify({'success': False, 'error': '不支持的运动模式'})
                
                if self.movement:
                    future = self._run_async(self.movement.start_pattern(movement_pattern))
                    if future:
                        result = future.result(timeout=10)
                        return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/stop', methods=['POST'])
        def stop_movement():
            """停止运动"""
            try:
                if self.movement:
                    future = self._run_async(self.movement.stop_movement())
                    if future:
                        result = future.result(timeout=10)
                        return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/emergency_stop', methods=['POST'])
        def emergency_stop():
            """紧急停止"""
            try:
                if self.movement:
                    future = self._run_async(self.movement.stop_move())
                    if future:
                        result = future.result(timeout=5)
                        return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/action', methods=['POST'])
        def execute_action():
            """执行特定动作"""
            try:
                data = request.get_json()
                action = data.get('action')
                dog_name = data.get('dog_name', None)
                parameters = data.get('parameters', {})
                
                if not action:
                    return jsonify({'success': False, 'error': '缺少动作参数'})
                
                if self.movement:
                    if dog_name:
                        # 单个机器狗动作
                        method_name = f"{action.lower()}_single"
                        if hasattr(self.movement, method_name):
                            method = getattr(self.movement, method_name)
                            future = self._run_async(method(dog_name))
                            if future:
                                result = future.result(timeout=10)
                                return jsonify({'success': result})
                    else:
                        # 所有机器狗动作
                        method_name = action.lower()
                        if hasattr(self.movement, method_name):
                            method = getattr(self.movement, method_name)
                            future = self._run_async(method())
                            if future:
                                result = future.result(timeout=10)
                                return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '动作不存在或运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/pounce', methods=['POST'])
        def execute_pounce():
            """执行扑跃动作"""
            try:
                data = request.get_json()
                count = data.get('count', 1)
                dog_name = data.get('dog_name', None)
                
                if self.movement:
                    if count == 1:
                        # 单次扑跃
                        if dog_name:
                            future = self._run_async(self.movement.front_pounce_single(dog_name))
                        else:
                            future = self._run_async(self.movement.front_pounce())
                    elif count == 3:
                        # 三次扑跃
                        future = self._run_async(self._execute_triple_pounce(dog_name))
                    else:
                        return jsonify({'success': False, 'error': '不支持的扑跃次数'})
                    
                    if future:
                        result = future.result(timeout=30)
                        return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/movement/manual', methods=['POST'])
        def manual_control():
            """手动控制"""
            try:
                data = request.get_json()
                direction = data.get('direction')
                dog_name = data.get('dog_name', None)
                speed = data.get('speed', 0.5)
                duration = data.get('duration', 1.0)
                
                if not direction:
                    return jsonify({'success': False, 'error': '缺少方向参数'})
                
                if self.movement:
                    # 方向映射到方法
                    direction_methods = {
                        'forward': 'move_forward',
                        'backward': 'move_backward', 
                        'left': 'move_left',
                        'right': 'move_right',
                        'turn_left': 'turn_left',
                        'turn_right': 'turn_right',
                        'stop': 'stop_move'
                    }
                    
                    method_name = direction_methods.get(direction)
                    if not method_name:
                        return jsonify({'success': False, 'error': '不支持的方向'})
                    
                    if dog_name:
                        # 单个机器狗控制
                        method_name += '_single'
                        if hasattr(self.movement, method_name):
                            method = getattr(self.movement, method_name)
                            if direction == 'stop':
                                future = self._run_async(method(dog_name))
                            else:
                                future = self._run_async(method(dog_name, speed, duration))
                            if future:
                                result = future.result(timeout=10)
                                return jsonify({'success': result})
                    else:
                        # 所有机器狗控制
                        if hasattr(self.movement, method_name):
                            method = getattr(self.movement, method_name)
                            if direction == 'stop':
                                future = self._run_async(method())
                            else:
                                future = self._run_async(method(speed, duration))
                            if future:
                                result = future.result(timeout=10)
                                return jsonify({'success': result})
                
                return jsonify({'success': False, 'error': '方法不存在或运动控制器未初始化'})
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/scan/devices', methods=['GET'])
        def scan_devices():
            """扫描网络中的Unitree设备"""
            try:
                self._add_log_message("INFO", "开始扫描网络中Unitree设备...", "网络扫描")
                
                # 使用内置的multicast_scanner
                def scan_in_thread():
                    return discover_ip_sn()
                
                # 在单独线程中执行扫描
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(scan_in_thread)
                    devices = future.result(timeout=10)  # 10秒超时
                
                device_list = []
                for serial, ip in devices.items():
                    device_name = f"Unitree-{serial[-6:]}"
                    device_list.append({
                        'name': device_name,
                        'ip': ip,
                        'serial': serial,
                        'device_type': 'Unitree'
                    })
                
                self._add_log_message("INFO", f"扫描完成，发现 {len(device_list)} 台设备", "网络扫描")
                
                return jsonify({
                    'success': True,
                    'devices': device_list,
                    'count': len(device_list)
                })
                
            except Exception as e:
                error_msg = f"扫描设备失败: {str(e)}"
                self._add_log_message("ERROR", error_msg, "网络扫描")
                return jsonify({'success': False, 'error': error_msg})
        
        @self.app.route('/api/logs', methods=['GET'])
        def get_logs():
            """获取系统日志"""
            try:
                limit = request.args.get('limit', 100, type=int)
                level = request.args.get('level', None)
                
                logs = self.log_messages[-limit:] if limit > 0 else self.log_messages
                
                # 按日志级别过滤
                if level:
                    logs = [log for log in logs if log['level'].upper() == level.upper()]
                
                return jsonify({
                    'success': True,
                    'logs': logs,
                    'total': len(self.log_messages)
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/logs/clear', methods=['POST'])
        def clear_logs():
            """清空日志"""
            try:
                self.log_messages.clear()
                self._add_log_message("INFO", "日志已清空", "系统")
                return jsonify({'success': True})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
        
        @self.app.route('/api/video/stream/<dog_name>')
        def video_stream(dog_name):
            """视频流接口（MJPEG格式）"""
            def generate_frames():
                try:
                    while self.video_active_streams.get(dog_name, False):
                        if dog_name not in self.video_frame_queues:
                            break
                        
                        try:
                            # 从队列获取帧
                            frame = self.video_frame_queues[dog_name].get(timeout=1.0)
                            
                            # 编码为JPEG
                            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                            if ret:
                                frame_bytes = buffer.tobytes()
                                yield (b'--frame\r\n'
                                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                        except:
                            # 超时或队列为空，继续等待
                            continue
                except Exception as e:
                    self._add_log_message("ERROR", f"视频流生成错误: {str(e)}", "视频管理")
            
            return Response(generate_frames(),
                          mimetype='multipart/x-mixed-replace; boundary=frame')
        
        @self.app.route('/api/status', methods=['GET'])
        def get_status():
            """获取系统状态"""
            return jsonify({
                'success': True,
                'dogs': self.controller.get_all_status() if self.controller else {},
                'movement': self.movement_status,
                'control_mode': self.controller.get_control_mode() if self.controller else ('all', []),
                'logs_count': len(self.log_messages)
            })
        
        @self.app.route('/api/video/start', methods=['POST'])
        def start_video():
            """启动视频流"""
            try:
                data = request.get_json()
                dog_name = data.get('dog_name')
                
                if not dog_name:
                    return jsonify({'success': False, 'error': '缺少机器狗名称参数'})
                
                if not self.controller:
                    return jsonify({'success': False, 'error': '控制器未初始化'})
                
                # 检查机器狗是否存在且已连接
                if dog_name not in self.controller.dogs:
                    return jsonify({'success': False, 'error': f'机器狗 {dog_name} 不存在'})
                
                dog_info = self.controller.dogs[dog_name]
                if dog_info.status.value != 'connected' and dog_info.status.value != 'idle':
                    return jsonify({'success': False, 'error': f'机器狗 {dog_name} 未连接'})
                
                if not dog_info.connection:
                    return jsonify({'success': False, 'error': f'无法获取 {dog_name} 的连接实例'})
                
                # 开启视频通道
                try:
                    # 初始化视频帧队列
                    if dog_name not in self.video_frame_queues:
                        self.video_frame_queues[dog_name] = Queue(maxsize=10)
                    
                    # 创建视频帧接收回调
                    async def recv_video_frames(track):
                        self._add_log_message("INFO", f"开始接收 {dog_name} 的视频帧", "视频管理")
                        try:
                            while self.video_active_streams.get(dog_name, False):
                                frame = await track.recv()
                                # 转换为numpy数组
                                img = frame.to_ndarray(format="bgr24")
                                
                                # 将帧放入队列（如果队列满了，丢弃旧帧）
                                if self.video_frame_queues[dog_name].full():
                                    try:
                                        self.video_frame_queues[dog_name].get_nowait()
                                    except:
                                        pass
                                
                                self.video_frame_queues[dog_name].put(img)
                                
                                # 如果正在录制，写入视频文件
                                if dog_name in self.video_recording:
                                    rec_info = self.video_recording[dog_name]
                                    writer = rec_info['writer']
                                    
                                    # 如果是第一帧，重新创建writer以匹配实际分辨率
                                    if rec_info['frame_size'] is None:
                                        height, width = img.shape[:2]
                                        rec_info['frame_size'] = (width, height)
                                        
                                        # 重新创建writer
                                        writer.release()
                                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                                        fps = 30.0
                                        new_writer = cv2.VideoWriter(rec_info['filepath'], fourcc, fps, (width, height))
                                        rec_info['writer'] = new_writer
                                        writer = new_writer
                                    
                                    # 写入帧
                                    writer.write(img)
                                    rec_info['frame_count'] += 1
                        except Exception as e:
                            self._add_log_message("ERROR", f"接收视频帧错误: {str(e)}", "视频管理")
                    
                    # 标记视频流为活跃
                    self.video_active_streams[dog_name] = True
                    
                    # 添加视频帧接收回调
                    dog_info.connection.video.add_track_callback(recv_video_frames)
                    
                    # 创建异步任务来开启视频通道
                    async def enable_video_async():
                        try:
                            # 在正确的事件循环中执行
                            dog_info.connection.video.switchVideoChannel(True)
                            self._add_log_message("INFO", f"已开启 {dog_name} 的视频通道", "视频管理")
                            return True
                        except Exception as e:
                            self._add_log_message("ERROR", f"switchVideoChannel错误: {str(e)}", "视频管理")
                            return False
                    
                    # 在asyncio事件循环中执行并等待结果
                    if self.asyncio_loop:
                        future = asyncio.run_coroutine_threadsafe(
                            enable_video_async(), 
                            self.asyncio_loop
                        )
                        result = future.result(timeout=5.0)  # 等待最多5秒
                        
                        if not result:
                            return jsonify({
                                'success': False,
                                'error': '开启视频通道失败，请查看日志'
                            })
                    else:
                        return jsonify({
                            'success': False,
                            'error': 'asyncio事件循环未运行'
                        })
                    
                    # 返回视频流URL
                    return jsonify({
                        'success': True,
                        'stream_url': f'/api/video/stream/{dog_name}',
                        'dog_name': dog_name,
                        'message': '视频流已开启'
                    })
                    
                except Exception as video_error:
                    error_msg = f"开启视频通道失败: {str(video_error)}"
                    self._add_log_message("ERROR", error_msg, "视频管理")
                    return jsonify({'success': False, 'error': error_msg})
                
            except Exception as e:
                error_msg = f"启动视频流失败: {str(e)}"
                self._add_log_message("ERROR", error_msg, "视频管理")
                return jsonify({'success': False, 'error': error_msg})
        
        @self.app.route('/api/video/stop', methods=['POST'])
        def stop_video():
            """停止视频流"""
            try:
                data = request.get_json()
                dog_name = data.get('dog_name')
                
                if not dog_name:
                    return jsonify({'success': False, 'error': '缺少机器狗名称参数'})
                
                if not self.controller or dog_name not in self.controller.dogs:
                    return jsonify({'success': False, 'error': f'机器狗 {dog_name} 不存在'})
                
                # 停止视频流
                self.video_active_streams[dog_name] = False
                
                dog_info = self.controller.dogs[dog_name]
                if dog_info.connection:
                    try:
                        # 创建异步任务来关闭视频通道
                        async def disable_video_async():
                            try:
                                dog_info.connection.video.switchVideoChannel(False)
                                self._add_log_message("INFO", f"已关闭 {dog_name} 的视频通道", "视频管理")
                                return True
                            except Exception as e:
                                self._add_log_message("WARNING", f"switchVideoChannel(False)错误: {str(e)}", "视频管理")
                                return False
                        
                        # 在asyncio事件循环中执行
                        if self.asyncio_loop:
                            future = asyncio.run_coroutine_threadsafe(
                                disable_video_async(),
                                self.asyncio_loop
                            )
                            future.result(timeout=3.0)  # 等待最多3秒
                        
                    except Exception as video_error:
                        self._add_log_message("WARNING", f"关闭视频通道时出现错误: {str(video_error)}", "视频管理")
                
                # 停止录制（如果正在录制）
                if dog_name in self.video_recording:
                    self._stop_recording_internal(dog_name)
                
                # 清空视频帧队列
                if dog_name in self.video_frame_queues:
                    while not self.video_frame_queues[dog_name].empty():
                        try:
                            self.video_frame_queues[dog_name].get_nowait()
                        except:
                            break
                
                return jsonify({
                    'success': True,
                    'dog_name': dog_name
                })
                
            except Exception as e:
                error_msg = f"停止视频流失败: {str(e)}"
                self._add_log_message("ERROR", error_msg, "视频管理")
                return jsonify({'success': False, 'error': error_msg})
        
        @self.app.route('/api/video/record/start', methods=['POST'])
        def start_recording():
            """开始录制视频"""
            try:
                data = request.get_json()
                dog_name = data.get('dog_name')
                
                if not dog_name:
                    return jsonify({'success': False, 'error': '缺少机器狗名称参数'})
                
                # 检查视频流是否已开启
                if not self.video_active_streams.get(dog_name, False):
                    return jsonify({'success': False, 'error': '请先开启摄像头'})
                
                # 检查是否已在录制
                if dog_name in self.video_recording:
                    return jsonify({'success': False, 'error': '该机器狗已在录制中'})
                
                # 生成录制文件名
                import os
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f"{dog_name}_{timestamp}.mp4"
                filepath = os.path.join(self.recordings_dir, filename)
                
                # 创建VideoWriter
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                fps = 30.0
                frame_size = (640, 480)  # 默认分辨率，会在第一帧时更新
                
                writer = cv2.VideoWriter(filepath, fourcc, fps, frame_size)
                
                if not writer.isOpened():
                    return jsonify({'success': False, 'error': '无法创建视频文件'})
                
                # 保存录制信息
                self.video_recording[dog_name] = {
                    'writer': writer,
                    'start_time': time.time(),
                    'filename': filename,
                    'filepath': filepath,
                    'frame_count': 0,
                    'frame_size': None  # 将在第一帧时设置
                }
                
                self._add_log_message("INFO", f"开始录制 {dog_name} 的视频: {filename}", "视频录制")
                
                return jsonify({
                    'success': True,
                    'dog_name': dog_name,
                    'filename': filename,
                    'start_time': time.time(),
                    'message': '录制已开始'
                })
                
            except Exception as e:
                error_msg = f"开始录制失败: {str(e)}"
                self._add_log_message("ERROR", error_msg, "视频录制")
                return jsonify({'success': False, 'error': error_msg})
        
        @self.app.route('/api/video/record/stop', methods=['POST'])
        def stop_recording():
            """停止录制视频"""
            try:
                data = request.get_json()
                dog_name = data.get('dog_name')
                
                if not dog_name:
                    return jsonify({'success': False, 'error': '缺少机器狗名称参数'})
                
                if dog_name not in self.video_recording:
                    return jsonify({'success': False, 'error': '该机器狗未在录制中'})
                
                # 停止录制
                result = self._stop_recording_internal(dog_name)
                
                return jsonify({
                    'success': True,
                    'dog_name': dog_name,
                    'filename': result['filename'],
                    'duration': result['duration'],
                    'frame_count': result['frame_count'],
                    'message': '录制已停止'
                })
                
            except Exception as e:
                error_msg = f"停止录制失败: {str(e)}"
                self._add_log_message("ERROR", error_msg, "视频录制")
                return jsonify({'success': False, 'error': error_msg})
        
        @self.app.route('/api/video/record/status', methods=['GET'])
        def get_recording_status():
            """获取录制状态"""
            try:
                dog_name = request.args.get('dog_name')
                
                if not dog_name:
                    return jsonify({'success': False, 'error': '缺少机器狗名称参数'})
                
                if dog_name not in self.video_recording:
                    return jsonify({
                        'success': True,
                        'recording': False,
                        'dog_name': dog_name
                    })
                
                rec_info = self.video_recording[dog_name]
                duration = time.time() - rec_info['start_time']
                
                return jsonify({
                    'success': True,
                    'recording': True,
                    'dog_name': dog_name,
                    'filename': rec_info['filename'],
                    'duration': duration,
                    'frame_count': rec_info['frame_count'],
                    'start_time': rec_info['start_time']
                })
                
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
    
    def _stop_recording_internal(self, dog_name: str) -> Dict[str, Any]:
        """内部方法：停止录制"""
        if dog_name not in self.video_recording:
            return None
        
        rec_info = self.video_recording[dog_name]
        writer = rec_info['writer']
        
        # 释放VideoWriter
        writer.release()
        
        # 计算录制时长
        duration = time.time() - rec_info['start_time']
        
        result = {
            'filename': rec_info['filename'],
            'filepath': rec_info['filepath'],
            'duration': duration,
            'frame_count': rec_info['frame_count']
        }
        
        # 从录制列表中移除
        del self.video_recording[dog_name]
        
        self._add_log_message("INFO", 
            f"停止录制 {dog_name} 的视频: {rec_info['filename']}, "
            f"时长: {duration:.1f}秒, 帧数: {rec_info['frame_count']}", 
            "视频录制")
        
        return result
    
    async def _execute_triple_pounce(self, dog_name: Optional[str] = None):
        """执行三次连续扑跃"""
        try:
            for i in range(3):
                if dog_name:
                    await self.movement.front_pounce_single(dog_name)
                else:
                    await self.movement.front_pounce()
                
                if i < 2:  # 前两次之间等待
                    await asyncio.sleep(3.0)
            
            return True
        except Exception as e:
            self.logger.error(f"执行三次扑跃失败: {e}")
            return False
    
    def _register_socketio_events(self):
        """注册SocketIO事件"""
        
        @self.socketio.on('connect')
        def handle_connect():
            """客户端连接"""
            self.logger.info('Web客户端已连接')
            # 发送当前状态
            emit('status_update', {
                'dogs': self.controller.get_all_status() if self.controller else {},
                'movement': self.movement_status,
                'control_mode': self.controller.get_control_mode() if self.controller else ('all', [])
            })
        
        @self.socketio.on('disconnect')
        def handle_disconnect():
            """客户端断开连接"""
            self.logger.info('Web客户端已断开连接')
    
    def run(self, debug=False):
        """启动Web服务器"""
        self.logger.info(f"启动双机器狗Web控制服务器: http://{self.host}:{self.port}")
        self.socketio.run(
            self.app, 
            host=self.host, 
            port=self.port, 
            debug=debug,
            use_reloader=False  # 避免与asyncio线程冲突
        )


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='双机器狗Web控制服务器')
    parser.add_argument('--host', default='0.0.0.0', help='服务器主机地址')
    parser.add_argument('--port', type=int, default=5000, help='服务器端口')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    
    args = parser.parse_args()
    
    # 创建并启动Web服务器
    server = DualDogWebServer(host=args.host, port=args.port)
    server.run(debug=args.debug)


if __name__ == "__main__":
    main()