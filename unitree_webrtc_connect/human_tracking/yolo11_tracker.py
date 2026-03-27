"""
YOLOv11人体追踪模块
封装YOLOv11的人体检测与追踪功能
"""

import logging
from ultralytics import YOLO


class YOLO11HumanTracker:
    """YOLOv11人体追踪器"""

    def __init__(self, model_path="yolov11n.pt", device="cuda"):
        """
        初始化YOLOv11人体追踪器

        参数:
            model_path: str - YOLO模型文件路径
            device: str - 运行设备 ("cuda" 或 "cpu")
        """
        self.logger = logging.getLogger("YOLO11HumanTracker")
        self.model = YOLO(model_path)
        self.model.to(device)

        # 设置追踪参数
        self.track_params = {
            "persist": True,  # 持久化追踪
            "track_buffer": 30,  # 追踪缓冲区大小
            "classes": 0,  # 仅检测人类 (COCO类别ID: 0)
            "conf": 0.5,  # 置信度阈值
            "iou": 0.5  # IOU阈值
        }

        self.logger.info(f"YOLOv11人体追踪器初始化完成，使用设备: {device}")

    def track(self, frame):
        """
        对输入帧进行人体检测与追踪

        参数:
            frame: numpy.ndarray - BGR格式的视频帧

        返回:
            results: ultralytics.engine.results.Results - 检测与追踪结果
        """
        results = self.model.track(
            source=frame,
            **self.track_params
        )
        return results

    def get_human_bboxes(self, results):
        """
        从YOLO追踪结果中提取人体bbox和ID

        参数:
            results: ultralytics.engine.results.Results - 检测与追踪结果

        返回:
            human_bboxes: list of tuples - 人体bbox列表，每个元素为 (track_id, bbox)
            其中 bbox 格式为 (xmin, ymin, xmax, ymax)
        """
        human_bboxes = []

        if results[0].boxes.id is not None:
            boxes = results[0].boxes.xyxy.cpu().numpy()
            track_ids = results[0].boxes.id.cpu().numpy()
            confidences = results[0].boxes.conf.cpu().numpy()
            classes = results[0].boxes.cls.cpu().numpy()

            # 筛选出人类检测结果
            for box, track_id, conf, cls in zip(boxes, track_ids, confidences, classes):
                if cls == 0:  # 仅保留人类
                    xmin, ymin, xmax, ymax = box
                    human_bboxes.append((int(track_id), (float(xmin), float(ymin), float(xmax), float(ymax)), float(conf)))

        return human_bboxes

    def draw_bboxes(self, frame, human_bboxes):
        """
        在帧上绘制人体bbox和ID

        参数:
            frame: numpy.ndarray - BGR格式的视频帧
            human_bboxes: list of tuples - 人体bbox列表

        返回:
            frame: numpy.ndarray - 绘制了bbox的视频帧
        """
        import cv2

        for track_id, bbox, conf in human_bboxes:
            xmin, ymin, xmax, ymax = bbox
            xmin, ymin, xmax, ymax = int(xmin), int(ymin), int(xmax), int(ymax)

            # 绘制bbox
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 255, 0), 2)

            # 绘制ID和置信度
            label = f"Person {track_id} {conf:.2f}"
            cv2.putText(frame, label, (xmin, ymin - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        return frame
