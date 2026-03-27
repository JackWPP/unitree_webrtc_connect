"""
WebRTC人体追踪扩展
将人体追踪功能集成到UnitreeWebRTCConnection中
"""

import asyncio
import logging
from unitree_webrtc_connect.human_tracking import HumanTrackingManager
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD


class WebRTCHumanTrackingExtension:
    """WebRTC人体追踪扩展"""

    def __init__(self, webrtc_conn,
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
        初始化人体追踪扩展

        参数:
            webrtc_conn: UnitreeWebRTCConnection - WebRTC连接实例
            yolo_model_path: str - YOLO模型路径
            device: str - 运行设备 ("cuda" 或 "cpu")
            desired_bbox_area: int - 目标bbox面积（像素）
            linear_kp: float - 线速度PID比例系数
            linear_ki: float - 线速度PID积分系数
            linear_kd: float - 线速度PID微分系数
            angular_kp: float - 角速度PID比例系数
            angular_ki: float - 角速度PID积分系数
            angular_kd: float - 角速度PID微分系数
        """
        self.logger = logging.getLogger("WebRTCHumanTrackingExtension")
        self.webrtc_conn = webrtc_conn

        # 初始化人体追踪管理器
        self.tracking_manager = HumanTrackingManager(
            yolo_model_path=yolo_model_path,
            device=device,
            desired_bbox_area=desired_bbox_area,
            linear_kp=linear_kp,
            linear_ki=linear_ki,
            linear_kd=linear_kd,
            angular_kp=angular_kp,
            angular_ki=angular_ki,
            angular_kd=angular_kd
        )

        # 注册视频回调
        self.webrtc_conn.video.add_track_callback(self._video_track_callback)

        # 运动命令发送任务
        self.command_task = None
        self.is_running = False

        self.logger.info("WebRTC人体追踪扩展初始化完成")

    def start(self):
        """启动人体追踪扩展"""
        self.logger.info("启动人体追踪扩展...")
        self.is_running = True
        self.tracking_manager.start()
        self.command_task = asyncio.create_task(self._command_loop())
        self.logger.info("人体追踪扩展已启动")

    def stop(self):
        """停止人体追踪扩展"""
        self.logger.info("停止人体追踪扩展...")
        self.is_running = False

        if self.command_task:
            self.command_task.cancel()
            self.command_task = None

        self.tracking_manager.stop()
        self.logger.info("人体追踪扩展已停止")

    async def _video_track_callback(self, track):
        """视频帧回调函数"""
        self.logger.info("开始处理WebRTC视频流...")

        while self.is_running:
            try:
                # 接收视频帧
                frame = await track.recv()

                # 处理视频帧
                self.tracking_manager.process_video_frame(frame)

            except Exception as e:
                self.logger.error(f"视频流处理错误: {e}", exc_info=True)
                break

        self.logger.info("WebRTC视频流处理结束")

    async def _command_loop(self):
        """运动命令发送循环"""
        self.logger.info("启动运动命令发送循环...")

        while self.is_running:
            try:
                # 获取运动命令
                command = await self.tracking_manager.get_command()

                if command and hasattr(self.webrtc_conn, 'datachannel'):
                    # 发送运动命令到机器狗
                    await self._send_move_command(command)

                await asyncio.sleep(0.1)  # 控制发送频率

            except Exception as e:
                self.logger.error(f"运动命令处理错误: {e}", exc_info=True)

        self.logger.info("运动命令发送循环结束")

    async def _send_move_command(self, command):
        """发送运动命令到机器狗"""
        linear_velocity = command["linear_velocity"]
        angular_velocity = command["angular_velocity"]

        try:
            await self.webrtc_conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                {
                    "api_id": SPORT_CMD["Move"],
                    "parameter": {
                        "x": linear_velocity,
                        "y": 0,
                        "z": angular_velocity
                    }
                }
            )
            self.logger.debug(f"发送运动命令: x={linear_velocity}, z={angular_velocity}")

        except Exception as e:
            self.logger.error(f"发送运动命令失败: {e}", exc_info=True)

    def get_tracking_manager(self):
        """获取追踪管理器实例"""
        return self.tracking_manager

    def get_pid_controller(self):
        """获取PID控制器实例"""
        return self.tracking_manager.get_pid_controller()

    def get_yolo_tracker(self):
        """获取YOLO追踪器实例"""
        return self.tracking_manager.get_yolo_tracker()

    def set_desired_bbox_area(self, area):
        """设置目标bbox面积"""
        self.tracking_manager.get_pid_controller().set_desired_bbox_area(area)
        self.logger.info(f"目标bbox面积已设置为: {area}")

    def set_linear_pid(self, kp, ki, kd):
        """设置线速度PID参数"""
        pid = self.tracking_manager.get_pid_controller()
        pid.linear_pid.Kp = kp
        pid.linear_pid.Ki = ki
        pid.linear_pid.Kd = kd
        self.logger.info(f"线速度PID参数已设置为: Kp={kp}, Ki={ki}, Kd={kd}")

    def set_angular_pid(self, kp, ki, kd):
        """设置角速度PID参数"""
        pid = self.tracking_manager.get_pid_controller()
        pid.angular_pid.Kp = kp
        pid.angular_pid.Ki = ki
        pid.angular_pid.Kd = kd
        self.logger.info(f"角速度PID参数已设置为: Kp={kp}, Ki={ki}, Kd={kd}")
