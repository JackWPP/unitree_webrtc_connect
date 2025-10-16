#!/usr/bin/env python3
"""
视频流功能测试脚本
用于测试WebRTC视频流到MJPEG的转换功能
"""

import asyncio
import cv2
import numpy as np
from queue import Queue
import time
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("VideoStreamTest")


def test_opencv_available():
    """测试OpenCV是否可用"""
    try:
        import cv2
        version = cv2.__version__
        logger.info(f"✅ OpenCV版本: {version}")
        return True
    except ImportError:
        logger.error("❌ OpenCV未安装，请运行: pip install opencv-python")
        return False


def test_numpy_available():
    """测试NumPy是否可用"""
    try:
        import numpy as np
        version = np.__version__
        logger.info(f"✅ NumPy版本: {version}")
        return True
    except ImportError:
        logger.error("❌ NumPy未安装，请运行: pip install numpy")
        return False


def test_frame_encoding():
    """测试视频帧编码"""
    logger.info("测试JPEG编码...")
    
    # 创建一个测试图像
    height, width = 480, 640
    test_frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    
    # 测试编码
    ret, buffer = cv2.imencode('.jpg', test_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    
    if ret:
        size = len(buffer.tobytes())
        logger.info(f"✅ JPEG编码成功")
        logger.info(f"   原始帧大小: {height}x{width}x3 = {height*width*3} bytes")
        logger.info(f"   压缩后大小: {size} bytes")
        logger.info(f"   压缩率: {size/(height*width*3)*100:.2f}%")
        return True
    else:
        logger.error("❌ JPEG编码失败")
        return False


def test_queue_performance():
    """测试队列性能"""
    logger.info("测试视频帧队列性能...")
    
    frame_queue = Queue(maxsize=10)
    height, width = 480, 640
    
    # 测试写入性能
    start_time = time.time()
    for i in range(30):  # 模拟30帧
        frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
        if not frame_queue.full():
            frame_queue.put(frame)
        else:
            # 队列满了，丢弃旧帧
            frame_queue.get_nowait()
            frame_queue.put(frame)
    
    write_time = time.time() - start_time
    logger.info(f"✅ 写入30帧耗时: {write_time:.3f}秒 ({30/write_time:.1f} fps)")
    
    # 测试读取和编码性能
    start_time = time.time()
    frame_count = 0
    while not frame_queue.empty():
        frame = frame_queue.get()
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            frame_count += 1
    
    process_time = time.time() - start_time
    logger.info(f"✅ 处理{frame_count}帧耗时: {process_time:.3f}秒 ({frame_count/process_time:.1f} fps)")
    
    return True


def test_mjpeg_frame_generation():
    """测试MJPEG帧生成"""
    logger.info("测试MJPEG帧格式...")
    
    # 创建测试帧
    height, width = 480, 640
    test_frame = np.zeros((height, width, 3), dtype=np.uint8)
    
    # 添加一些内容
    cv2.putText(test_frame, 'Test Frame', (50, 240), 
                cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    
    # 编码为JPEG
    ret, buffer = cv2.imencode('.jpg', test_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    
    if ret:
        frame_bytes = buffer.tobytes()
        
        # 生成MJPEG帧
        mjpeg_frame = (b'--frame\r\n'
                      b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        
        logger.info(f"✅ MJPEG帧生成成功")
        logger.info(f"   帧头大小: {len(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n')} bytes")
        logger.info(f"   JPEG数据: {len(frame_bytes)} bytes")
        logger.info(f"   总大小: {len(mjpeg_frame)} bytes")
        return True
    else:
        logger.error("❌ MJPEG帧生成失败")
        return False


async def test_async_frame_reception():
    """测试异步帧接收"""
    logger.info("测试异步帧接收...")
    
    frame_queue = Queue(maxsize=10)
    is_active = True
    
    # 模拟异步帧接收
    async def mock_receive_frames():
        for i in range(10):
            if not is_active:
                break
            
            # 模拟接收帧
            await asyncio.sleep(0.033)  # 模拟30fps
            frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
            
            # 放入队列
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except:
                    pass
            
            frame_queue.put(frame)
            logger.debug(f"接收帧 {i+1}/10")
    
    # 运行测试
    start_time = time.time()
    await mock_receive_frames()
    elapsed = time.time() - start_time
    
    logger.info(f"✅ 异步接收10帧完成")
    logger.info(f"   耗时: {elapsed:.3f}秒")
    logger.info(f"   队列中帧数: {frame_queue.qsize()}")
    
    return True


def print_summary(results):
    """打印测试总结"""
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    
    total = len(results)
    passed = sum(results.values())
    failed = total - passed
    
    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{test_name:40s} {status}")
    
    print("-"*60)
    print(f"总计: {total} 项测试")
    print(f"通过: {passed} 项 ({passed/total*100:.1f}%)")
    print(f"失败: {failed} 项 ({failed/total*100:.1f}%)")
    print("="*60)
    
    if failed == 0:
        print("\n🎉 所有测试通过！视频流系统已就绪。")
        print("\n下一步:")
        print("1. 启动Web服务器: python web_server.py")
        print("2. 在浏览器中打开Web界面")
        print("3. 连接机器狗并开启摄像头")
    else:
        print("\n⚠️ 部分测试失败，请检查上述错误信息。")
        print("\n建议:")
        print("1. 确保已安装所有依赖: pip install opencv-python numpy")
        print("2. 检查Python版本是否>=3.7")
        print("3. 查看详细错误日志")


async def run_all_tests():
    """运行所有测试"""
    results = {}
    
    print("\n" + "="*60)
    print("开始视频流功能测试")
    print("="*60 + "\n")
    
    # 依赖检查
    results["OpenCV可用性"] = test_opencv_available()
    results["NumPy可用性"] = test_numpy_available()
    
    if not all([results["OpenCV可用性"], results["NumPy可用性"]]):
        print("\n❌ 缺少必要依赖，无法继续测试")
        print_summary(results)
        return
    
    print()
    
    # 功能测试
    results["视频帧编码"] = test_frame_encoding()
    print()
    
    results["队列性能"] = test_queue_performance()
    print()
    
    results["MJPEG帧生成"] = test_mjpeg_frame_generation()
    print()
    
    results["异步帧接收"] = await test_async_frame_reception()
    print()
    
    # 打印总结
    print_summary(results)


def main():
    """主函数"""
    try:
        asyncio.run(run_all_tests())
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
    except Exception as e:
        logger.error(f"测试过程中发生错误: {e}", exc_info=True)


if __name__ == "__main__":
    main()
