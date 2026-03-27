"""
PID控制器模块
用于将YOLOv11的像素空间误差转换为机器狗的运动速度指令
"""

from simple_pid import PID


class HumanTrackingPID:
    """人体跟踪专用PID控制器"""

    def __init__(self,
                 linear_kp=0.1, linear_ki=0.0, linear_kd=0.0,
                 angular_kp=0.005, angular_ki=0.0, angular_kd=0.0,
                 desired_bbox_area=50000,  # 目标bbox面积（像素）
                 max_linear_velocity=0.5,  # 最大线速度 (m/s)
                 max_angular_velocity=0.8):  # 最大角速度 (rad/s)

        # 线速度PID控制器（控制前进/后退，基于bbox面积）
        self.linear_pid = PID(
            Kp=linear_kp,
            Ki=linear_ki,
            Kd=linear_kd,
            setpoint=desired_bbox_area,
            output_limits=(-max_linear_velocity, max_linear_velocity)
        )

        # 角速度PID控制器（控制转向，基于bbox中心x坐标）
        self.angular_pid = PID(
            Kp=angular_kp,
            Ki=angular_ki,
            Kd=angular_kd,
            output_limits=(-max_angular_velocity, max_angular_velocity)
        )

        self.desired_bbox_area = desired_bbox_area
        self.max_linear_velocity = max_linear_velocity
        self.max_angular_velocity = max_angular_velocity

    def calculate_velocity(self, bbox, frame_width):
        """
        根据检测到的人体bbox计算运动速度指令

        参数:
            bbox: tuple (xmin, ymin, xmax, ymax) - 人体 bounding box
            frame_width: int - 视频帧宽度

        返回:
            linear_velocity: float - 线速度 (x方向，前进为正)
            angular_velocity: float - 角速度 (z方向，左转为正)
        """
        xmin, ymin, xmax, ymax = bbox

        # 计算bbox中心和面积
        bbox_center_x = (xmin + xmax) / 2
        bbox_area = (xmax - xmin) * (ymax - ymin)

        # 计算角速度（转向控制）
        angular_error = frame_width / 2 - bbox_center_x
        angular_velocity = self.angular_pid(angular_error)

        # 计算线速度（前进/后退控制）
        linear_error = self.desired_bbox_area - bbox_area
        linear_velocity = self.linear_pid(linear_error)

        return linear_velocity, angular_velocity

    def reset(self):
        """重置PID控制器"""
        self.linear_pid.reset()
        self.angular_pid.reset()

    def set_desired_bbox_area(self, area):
        """设置目标bbox面积"""
        self.desired_bbox_area = area
        self.linear_pid.setpoint = area

    def set_linear_limits(self, min_velocity, max_velocity):
        """设置线速度限制"""
        self.linear_pid.output_limits = (min_velocity, max_velocity)

    def set_angular_limits(self, min_velocity, max_velocity):
        """设置角速度限制"""
        self.angular_pid.output_limits = (min_velocity, max_velocity)
