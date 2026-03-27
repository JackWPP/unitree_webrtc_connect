#!/usr/bin/env python3
"""
WebRTC人体追踪集成示例
"""

import asyncio
import logging
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.human_tracking import HumanTrackingManager

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WebRTCTrackingExample")


async def video_track_callback(track, tracking_manager):
    """视频帧回调处理函数"""
    logger.info("开始处理视频流")

    # 进入视频帧处理循环
    while True:
        try:
            # 接收视频帧
            frame = await track.recv()

            # 使用人体追踪模块处理视频帧
            tracking_manager.process_video_frame(frame)

            # 获取运动命令
            command = await tracking_manager.get_command()

            if command:
                logger.info(f"生成运动命令: linear_velocity={command['linear_velocity']}, angular_velocity={command['angular_velocity']}")

                # 这里可以将命令发送给机器狗
                # 示例命令发送代码（需要根据实际情况调整）
                # await conn.datachannel.pub_sub.publish_request_new(
                #     RTC_TOPIC["SPORT_MOD"],
                #     {
                #         "api_id": SPORT_CMD["Move"],
                #         "parameter": {
                #             "x": command["linear_velocity"],
                #             "y": 0,
                #             "z": command["angular_velocity"]
                #         }
                #     }
                # )

        except Exception as e:
            logger.error(f"视频帧处理错误: {e}")
            break

    logger.info("视频流处理结束")


async def main():
    """主函数"""
    logger.info("初始化人体追踪系统...")

    # 初始化追踪管理器
    tracking_manager = HumanTrackingManager(
        yolo_model_path="yolov11n.pt",
        device="cuda",
        desired_bbox_area=50000,  # 目标bbox面积（像素）
        linear_kp=0.1,
        angular_kp=0.005
    )

    logger.info("启动人体追踪系统...")
    tracking_manager.start()

    logger.info("初始化WebRTC连接...")
    # 创建WebRTC连接（根据实际情况选择连接方式）
    conn = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalSTA,
        ip="192.168.8.181"  # 替换为你的机器狗IP
        # serialNumber="B42D2000XXXXXXXX"  # 或使用序列号连接
    )

    logger.info("连接到机器狗...")
    await conn.connect()

    logger.info("注册视频帧回调...")
    # 注册视频回调
    conn.video.add_track_callback(lambda track: video_track_callback(track, tracking_manager))

    logger.info("等待用户输入 Ctrl+C 退出...")
    try:
        # 保持程序运行
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("用户中断程序...")

    logger.info("断开WebRTC连接...")
    await conn.disconnect()

    logger.info("停止人体追踪系统...")
    tracking_manager.stop()

    logger.info("程序结束")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"程序错误: {e}", exc_info=True)