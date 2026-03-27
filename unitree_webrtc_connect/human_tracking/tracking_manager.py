"""
人体追踪管理器模块
整合YOLOv11追踪器、PID控制器和视频处理器
"""

import asyncio
import logging
from .yolo11_tracker import YOLO11HumanTracker
from .pid_controller import HumanTrackingPID
from .video_processor import VideoProcessor


class HumanTrackingManager:
    """人体追踪管理器"""

    def __init__(self,
                 yolo_model_path="yolov11n.pt",
                 device="cuda",
                 desired_bbox_area=50000,
                 linear_kp=0.1,
                 linear_ki=0.0,
                 linear_kd=0.0,
                 angular_kp=0.005,
                 angular_ki=0.0,
                 angular_kd=0.0):
        """
        初始化人体追踪管理器

        参数:
            yolo_model_path: str - YOLOv11模型路径
            device: str - 运行设备 ("cuda" 或 "cpu")
            desired_bbox_area: int - 目标bbox面积（像素）
            linear_kp: float - 线速度PID比例系数
            linear_ki: float - 线速度PID积分系数
            linear_kd: float - 线速度PID微分系数
            angular_kp: float - 角速度PID比例系数
            angular_ki: float - 角速度PID积分系数
            angular_kd: float - 角速度PID微分系数
        """
        self.logger = logging.getLogger("HumanTrackingManager")

        # 初始化YOLOv11人体追踪器
        self.yolo_tracker = YOLO11HumanTracker(model_path=yolo_model_path, device=device)

        # 初始化PID控制器
        self.pid_controller = HumanTrackingPID(
            linear_kp=linear_kp,
            linear_ki=linear_ki,
            linear_kd=linear_kd,
            angular_kp=angular_kp,
            angular_ki=angular_ki,
            angular_kd=angular_kd,
            desired_bbox_area=desired_bbox_area
        )

        # 创建命令队列
        self.command_queue = asyncio.Queue(maxsize=10)

        # 初始化视频处理器
        self.video_processor = VideoProcessor(
            yolo_tracker=self.yolo_tracker,
            pid_controller=self.pid_controller,
            command_queue=self.command_queue
        )

        self.logger.info("人体追踪管理器初始化完成")

    def start(self):
        """启动人体追踪系统"""
        self.video_processor.start()
        self.logger.info("人体追踪系统已启动")

    def stop(self):
        """停止人体追踪系统"""
        self.video_processor.stop()
        self.logger.info("人体追踪系统已停止")

    def process_video_frame(self, frame):
        """
        处理视频帧

        参数:
            frame: av.VideoFrame - 视频帧
        """
        self.video_processor.put_frame(frame)

    async def get_command(self):
        """
        从命令队列获取运动指令

        返回:
            command: dict - 运动指令
        """
        try:
            return await asyncio.wait_for(self.command_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    def get_yolo_tracker(self):
        """获取YOLOv11追踪器实例"""
        return self.yolo_tracker

    def get_pid_controller(self):
        """获取PID控制器实例"""
        return self.pid_controller

    def get_video_processor(self):
        """获取视频处理器实例"""
        return self.video_processor
