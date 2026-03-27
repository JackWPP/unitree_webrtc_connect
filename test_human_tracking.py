#!/usr/bin/env python3
"""
人体追踪测试脚本
用于测试YOLOv11人体追踪和PID控制器的功能
"""

import cv2
import numpy as np
from unitree_webrtc_connect.human_tracking import HumanTrackingManager


def test_human_tracking():
    """测试人体追踪功能"""
    print("初始化人体追踪系统...")

    # 初始化人体追踪管理器
    tracking_manager = HumanTrackingManager(
        yolo_model_path="yolov11n.pt",
        device="cuda",
        desired_bbox_area=50000,
        linear_kp=0.1,
        angular_kp=0.005
    )

    # 启动追踪系统
    tracking_manager.start()

    print("人体追踪系统已启动，按'q'键退出测试")

    # 打开本地摄像头（用于测试）
    cap = cv2.VideoCapture(0)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # 创建一个模拟的av.VideoFrame（简化测试）
        # 实际应用中，这将来自WebRTC连接
        class MockVideoFrame:
            def __init__(self, frame):
                self.frame = frame

            def to_ndarray(self, format="bgr24"):
                return self.frame

        mock_frame = MockVideoFrame(frame)

        # 处理视频帧
        tracking_manager.process_video_frame(mock_frame)

        # 显示原始帧
        cv2.imshow("Human Tracking Test", frame)

        # 按'q'键退出
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # 停止追踪系统
    tracking_manager.stop()

    # 释放摄像头和窗口
    cap.release()
    cv2.destroyAllWindows()

    print("人体追踪测试完成")


if __name__ == "__main__":
    test_human_tracking()
