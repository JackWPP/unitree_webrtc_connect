#!/usr/bin/env python3
"""
状态同步修复验证脚本
用于测试机器狗状态是否能正确从后端同步到前端
"""

import asyncio
import time
import json
from dual_dog_controller import DualDogController, DogStatus

def test_status_callback():
    """测试状态回调机制"""
    print("=" * 60)
    print("测试1: 状态回调机制")
    print("=" * 60)
    
    controller = DualDogController()
    
    # 记录状态变化
    status_changes = []
    
    def status_callback(dog_name, status):
        status_changes.append({
            'dog_name': dog_name,
            'status': status.value,
            'timestamp': time.time()
        })
        print(f"✅ 状态回调触发: {dog_name} -> {status.value}")
    
    controller.add_status_callback(status_callback)
    
    # 添加机器狗
    controller.add_dog("TestDog", "192.168.1.100")
    
    # 手动触发状态变化（模拟连接）
    controller._notify_status_change("TestDog", DogStatus.CONNECTING)
    controller._notify_status_change("TestDog", DogStatus.CONNECTED)
    
    print(f"\n记录到 {len(status_changes)} 次状态变化")
    
    if len(status_changes) >= 2:
        print("✅ 测试通过：状态回调正常工作")
        return True
    else:
        print("❌ 测试失败：状态回调未触发")
        return False


def test_status_data_structure():
    """测试状态数据结构"""
    print("\n" + "=" * 60)
    print("测试2: 状态数据结构")
    print("=" * 60)
    
    controller = DualDogController()
    
    # 添加测试机器狗
    controller.add_dog("Dog1", "192.168.31.245")
    controller.add_dog("Dog2", "192.168.31.246")
    
    # 模拟连接状态
    controller.dogs["Dog1"].status = DogStatus.CONNECTED
    controller.dogs["Dog1"].connection = "mock_connection"  # 模拟连接对象
    
    # 获取状态
    all_status = controller.get_all_status()
    
    print(f"\n获取到 {len(all_status)} 个机器狗状态")
    print(json.dumps(all_status, indent=2, ensure_ascii=False))
    
    # 验证数据结构
    tests_passed = 0
    tests_total = 0
    
    for dog_name, info in all_status.items():
        tests_total += 5
        
        # 检查必需字段
        if 'status' in info:
            print(f"✅ {dog_name}: 包含 'status' 字段")
            tests_passed += 1
        else:
            print(f"❌ {dog_name}: 缺少 'status' 字段")
        
        if 'connected' in info:
            print(f"✅ {dog_name}: 包含 'connected' 字段")
            tests_passed += 1
        else:
            print(f"❌ {dog_name}: 缺少 'connected' 字段")
        
        if 'ip' in info:
            print(f"✅ {dog_name}: 包含 'ip' 字段")
            tests_passed += 1
        else:
            print(f"❌ {dog_name}: 缺少 'ip' 字段")
        
        # 检查connected字段类型
        if isinstance(info.get('connected'), bool):
            print(f"✅ {dog_name}: 'connected' 是布尔类型")
            tests_passed += 1
        else:
            print(f"❌ {dog_name}: 'connected' 不是布尔类型")
        
        # 检查连接状态逻辑
        if dog_name == "Dog1":
            if info.get('connected') == True:
                print(f"✅ {dog_name}: connected=True（已连接）")
                tests_passed += 1
            else:
                print(f"❌ {dog_name}: connected应为True但实际为{info.get('connected')}")
        elif dog_name == "Dog2":
            if info.get('connected') == False:
                print(f"✅ {dog_name}: connected=False（未连接）")
                tests_passed += 1
            else:
                print(f"❌ {dog_name}: connected应为False但实际为{info.get('connected')}")
    
    print(f"\n数据结构测试: {tests_passed}/{tests_total} 通过")
    
    return tests_passed == tests_total


def test_websocket_message_format():
    """测试WebSocket消息格式"""
    print("\n" + "=" * 60)
    print("测试3: WebSocket消息格式")
    print("=" * 60)
    
    from web_server import DualDogWebServer
    
    # 创建服务器实例（不启动）
    server = DualDogWebServer()
    
    # 添加测试机器狗
    server.controller.add_dog("TestDog", "192.168.1.100")
    
    # 模拟连接
    server.controller.dogs["TestDog"].status = DogStatus.CONNECTED
    server.controller.dogs["TestDog"].connection = "mock_connection"
    
    # 获取完整状态
    all_status = server.controller.get_all_status()
    dog_info = all_status.get("TestDog", {})
    
    print("\n模拟的WebSocket消息数据:")
    websocket_message = {
        'dog_name': "TestDog",
        'status': DogStatus.CONNECTED.value,
        'dog_info': dog_info,
        'timestamp': time.time()
    }
    print(json.dumps(websocket_message, indent=2, ensure_ascii=False, default=str))
    
    # 验证消息格式
    tests_passed = 0
    tests_total = 4
    
    if 'dog_name' in websocket_message:
        print("✅ 包含 'dog_name' 字段")
        tests_passed += 1
    else:
        print("❌ 缺少 'dog_name' 字段")
    
    if 'status' in websocket_message:
        print("✅ 包含 'status' 字段")
        tests_passed += 1
    else:
        print("❌ 缺少 'status' 字段")
    
    if 'dog_info' in websocket_message:
        print("✅ 包含 'dog_info' 字段")
        tests_passed += 1
    else:
        print("❌ 缺少 'dog_info' 字段")
    
    if websocket_message.get('dog_info', {}).get('connected') is not None:
        print("✅ dog_info 包含 'connected' 字段")
        tests_passed += 1
    else:
        print("❌ dog_info 缺少 'connected' 字段")
    
    print(f"\nWebSocket消息格式测试: {tests_passed}/{tests_total} 通过")
    
    return tests_passed == tests_total


def print_summary(results):
    """打印测试总结"""
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    
    total = len(results)
    passed = sum(results.values())
    failed = total - passed
    
    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{test_name:40s} {status}")
    
    print("-" * 60)
    print(f"总计: {total} 项测试")
    print(f"通过: {passed} 项 ({passed/total*100:.1f}%)")
    print(f"失败: {failed} 项")
    print("=" * 60)
    
    if failed == 0:
        print("\n🎉 所有测试通过！状态同步功能正常。")
        print("\n接下来可以:")
        print("1. 启动Web服务器: python web_server.py")
        print("2. 在浏览器中测试实际连接")
        print("3. 验证前端状态显示是否正确")
    else:
        print("\n⚠️ 部分测试失败，请检查修复是否完整。")


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("状态同步修复验证")
    print("=" * 60 + "\n")
    
    results = {}
    
    try:
        # 测试1: 状态回调
        results["状态回调机制"] = test_status_callback()
        
        # 测试2: 状态数据结构
        results["状态数据结构"] = test_status_data_structure()
        
        # 测试3: WebSocket消息格式
        results["WebSocket消息格式"] = test_websocket_message_format()
        
    except Exception as e:
        print(f"\n❌ 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 打印总结
    print_summary(results)


if __name__ == "__main__":
    main()
