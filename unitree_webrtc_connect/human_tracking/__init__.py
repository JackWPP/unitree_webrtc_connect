"""
人体追踪模块
整合YOLOv11追踪器、PID控制器和视频处理器，实现实时人体追踪功能
"""

from .yolo11_tracker import YOLO11HumanTracker
from .pid_controller import HumanTrackingPID
from .video_processor import VideoProcessor
from .tracking_manager import HumanTrackingManager

__all__ = [
    "YOLO11HumanTracker",
    "HumanTrackingPID",
    "VideoProcessor",
    "HumanTrackingManager"
]

__version__ = "1.0.0"
