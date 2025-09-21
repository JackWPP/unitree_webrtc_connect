import asyncio
import logging
import json
import sys
import threading
import select
from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod
from go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD

# Enable logging for debugging
logging.basicConfig(level=logging.FATAL)


# 全局变量用于键盘输入检测
dance_trigger = False

def keyboard_listener():
    """键盘监听线程函数"""
    global dance_trigger
    print("按任意键触发跳舞动作...")
    while True:
        try:
            # 检查是否有键盘输入
            if select.select([sys.stdin], [], [], 0.1)[0]:
                sys.stdin.read(1)  # 读取一个字符
                dance_trigger = True
                print("\n检测到按键，准备跳舞！")
        except:
            pass

async def perform_dance(conn):
    """执行跳舞动作"""
    print("开始跳舞动作...")
    try:
        # 执行Dance1动作
        await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["Dance1"]}
        )
        # 等待跳舞动作完成
        await asyncio.sleep(5)  # Dance1大约需要5秒
        print("跳舞动作完成，继续走路循环...")
    except Exception as e:
        print(f"跳舞动作执行失败: {e}")

async def main():
    global dance_trigger
    
    try:
        conn = Go2WebRTCConnection(
            WebRTCConnectionMethod.LocalSTA, ip="192.168.31.245")

        # Connect to the WebRTC service.
        await conn.connect()

        ####### NORMAL MODE ########
        print("Checking current motion mode...")

        # Get the current motion_switcher status
        response = await conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1001}
        )

        if response['data']['header']['status']['code'] == 0:
            data = json.loads(response['data']['data'])
            current_motion_switcher_mode = data['name']
            print(f"Current motion mode: {current_motion_switcher_mode}")

        # Switch to "normal" mode if not already
        if current_motion_switcher_mode != "normal":
            print(
                f"Switching motion mode from {current_motion_switcher_mode} to 'normal'...")
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"],
                {
                    "api_id": 1002,
                    "parameter": {"name": "normal"}
                }
            )
            await asyncio.sleep(1)  # Wait while it stands up

        await asyncio.sleep(1)
        
        # 启动键盘监听线程
        keyboard_thread = threading.Thread(target=keyboard_listener, daemon=True)
        keyboard_thread.start()

        # 无限循环正方形移动，检查跳舞触发
        step = 0
        while True:
            # 检查是否触发跳舞
            if dance_trigger:
                dance_trigger = False  # 重置触发标志
                await perform_dance(conn)
            
            step += 1
            print(f"Step {step}: Moving forward...")
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                {
                    "api_id": SPORT_CMD["Move"],
                    "parameter": {"x": 1, "y": 0, "z": 0}
                }
            )
            await asyncio.sleep(2)
            
            # 再次检查跳舞触发
            if dance_trigger:
                dance_trigger = False
                await perform_dance(conn)

            print(f"Step {step}: Rotating right 90 degrees...")
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                {
                    "api_id": SPORT_CMD["Move"],
                    "parameter": {"x": 0, "y": 0, "z": -1.57}
                }
            )
            await asyncio.sleep(2)

    except ValueError as e:
        # Log any value errors that occur during the process.
        logging.error(f"An error occurred: {e}")

if __name__ == "__main__":
    print("=== Go2机器狗运动控制程序 ===")
    print("程序功能：")
    print("1. 机器狗会自动执行正方形走路循环")
    print("2. 按任意键可触发跳舞动作")
    print("3. 跳舞完成后会继续走路循环")
    print("4. 按Ctrl+C退出程序并执行告别动作")
    print("================================\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Handle Ctrl+C to exit gracefully and挥手
        print("\n程序被用户中断，执行告别动作...")
        import asyncio as _asyncio
        async def goodbye_action():
            from go2_webrtc_driver.webrtc_driver import Go2WebRTCConnection, WebRTCConnectionMethod
            from go2_webrtc_driver.constants import RTC_TOPIC, SPORT_CMD
            # 重新建立连接
            conn = Go2WebRTCConnection(WebRTCConnectionMethod.LocalSTA, ip="192.168.31.245")
            await conn.connect()
            print("执行告别动作...")
            await conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["SPORT_MOD"],
                {"api_id": SPORT_CMD["Hello"]}
            )
            await _asyncio.sleep(3)
            print("告别动作完成，程序退出。")
        try:
            _asyncio.run(goodbye_action())
        except Exception as e:
            print(f"执行告别动作失败: {e}")
        sys.exit(0)
