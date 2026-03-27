"""
视频处理器模块
实现视频帧的格式转换和多线程处理
"""

import asyncio
import threading
import queue
import logging
import av
import cv2
import numpy as np


class VideoProcessor:
    """视频处理器"""

    def __init__(self, yolo_tracker, pid_controller, command_queue, frame_queue_size=10):
        """
        初始化视频处理器

        参数:
            yolo_tracker: YOLO11HumanTracker - YOLOv11人体追踪器实例
            pid_controller: HumanTrackingPID - PID控制器实例
            command_queue: asyncio.Queue - 命令队列，用于将运动指令发送到主循环
            frame_queue_size: int - 视频帧队列大小
        """
        self.logger = logging.getLogger("VideoProcessor")
        self.yolo_tracker = yolo_tracker
        self.pid_controller = pid_controller
        self.command_queue = command_queue

        # 创建线程安全的视频帧队列
        self.frame_queue = queue.Queue(maxsize=frame_queue_size)

        # 工作线程
        self.worker_thread = None
        self.is_running = False

        self.logger.info("视频处理器初始化完成")

    def start(self):
        """启动视频处理器工作线程"""
        if not self.is_running:
            self.is_running = True
            self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self.worker_thread.start()
            self.logger.info("视频处理器工作线程已启动")

    def stop(self):
        """停止视频处理器工作线程"""
        if self.is_running:
            self.is_running = False
            self.worker_thread.join(timeout=5)
            self.logger.info("视频处理器工作线程已停止")

    def put_frame(self, frame):
        """
        将视频帧放入队列

        参数:
            frame: av.VideoFrame - 视频帧
        """
        try:
            self.frame_queue.put(frame, block=False)
        except queue.Full:
            # 如果队列满了，丢弃最早的帧
            try:
                self.frame_queue.get(block=False)
                self.frame_queue.put(frame, block=False)
            except queue.Empty:
                pass

    def _worker_loop(self):
        """工作线程循环"""
        while self.is_running:
            try:
                # 从队列中获取视频帧
                frame = self.frame_queue.get(block=True, timeout=1)
                self._process_frame(frame)
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"视频帧处理错误: {e}", exc_info=True)

    def _process_frame(self, frame):
        """处理单个视频帧"""
        # 转换为BGR格式的numpy数组
        bgr_frame = frame.to_ndarray(format="bgr24")
        frame_width = bgr_frame.shape[1]

        # 使用YOLOv11进行人体追踪
        results = self.yolo_tracker.track(bgr_frame)
        human_bboxes = self.yolo_tracker.get_human_bboxes(results)

        # 如果检测到人体
        if human_bboxes:
            # 选择最近/最大的人体目标（这里选择第一个检测到的）
            # TODO: 实现更智能的目标选择策略（如最近、最大、锁定ID等）
            track_id, bbox, conf = human_bboxes[0]

            # 使用PID控制器计算运动指令
            linear_velocity, angular_velocity = self.pid_controller.calculate_velocity(bbox, frame_width)

            # 构建运动命令
            command = {
                "type": "move",
                "track_id": track_id,
                "linear_velocity": linear_velocity,
                "angular_velocity": angular_velocity,
                "bbox": bbox,
                "confidence": conf
            }

            # 将命令放入异步队列，发送到主循环
            try:
                asyncio.run_coroutine_threadsafe(self.command_queue.put(command), asyncio.get_event_loop())
            except RuntimeError:
                # 如果没有事件循环（例如在调试模式下），记录错误
                self.logger.error("无法获取异步事件循环")

    def avframe_to_bgr(self, frame):
        """
        将av.VideoFrame转换为BGR格式的numpy数组

        参数:
            frame: av.VideoFrame - 视频帧

        返回:
            bgr_frame: numpy.ndarray - BGR格式的视频帧
        """
        return frame.to_ndarray(format="bgr24")

    def bgr_to_avframe(self, bgr_frame):
        """
        将BGR格式的numpy数组转换为av.VideoFrame

        参数:
            bgr_frame: numpy.ndarray - BGR格式的视频帧

        返回:
            frame: av.VideoFrame - 视频帧
        """
        # 将BGR转换为RGB
        rgb_frame = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        return av.VideoFrame.from_ndarray(rgb_frame, format="rgb24")
