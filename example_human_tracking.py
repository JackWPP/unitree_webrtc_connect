#!/usr/bin/env python3
"""
WebRTC人体追踪扩展使用示例
"""

import asyncio
import logging
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.human_tracking_extension import WebRTCHumanTrackingExtension


async def main():
    """主函数"""
    # 配置日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger("HumanTrackingExample")

    logger.info("=== 开始WebRTC人体追踪示例 ===")

    # 初始化WebRTC连接
    logger.info("初始化WebRTC连接...")
    conn = UnitreeWebRTCConnection(
        WebRTCConnectionMethod.LocalSTA,
        ip="192.168.8.181"  # 替换为你的机器狗IP
    )

    try:
        # 连接到机器狗
        logger.info("连接到机器狗...")
        await conn.connect()
        logger.info("成功连接到机器狗!")

        # 初始化人体追踪扩展
        logger.info("初始化人体追踪扩展...")
        tracking_extension = WebRTCHumanTrackingExtension(
            conn,
            yolo_model_path="yolov11n.pt",
            device="cuda",
            desired_bbox_area=50000,  # 目标bbox面积（像素）
            linear_kp=0.1,  # 线速度PID比例系数
            angular_kp=0.005  # 角速度PID比例系数
        )

        # 启动追踪扩展
        logger.info("启动人体追踪扩展...")
        tracking_extension.start()

        logger.info("=== 人体追踪系统已启动 ===")
        logger.info("现在机器狗将开始追踪人体!")
        logger.info("按 Ctrl+C 停止示例...")

        # 保持程序运行
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("用户中断程序...")

    except Exception as e:
        logger.error(f"程序错误: {e}", exc_info=True)

    finally:
        # 清理资源
        if 'tracking_extension' in locals():
            logger.info("停止人体追踪扩展...")
            tracking_extension.stop()

        logger.info("断开WebRTC连接...")
        await conn.disconnect()

        logger.info("=== 示例结束 ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已退出")
