# 前端状态显示问题修复报告

## 问题描述

用户报告即使机器狗状态为 "idle" 或 "connected"，前端仍然显示 "disconnected"，并且无法调用摄像头功能。

## 根本原因分析

### 1. 状态获取逻辑问题
前端的 `getStatusClass()` 方法只接受字符串状态参数，但实际从后端获取的是包含多个字段的对象：
```json
{
  "status": "moving",
  "connected": true,
  "ip": "192.168.10.157",
  "last_heartbeat": 1758606451.4526227,
  "error_message": ""
}
```

### 2. 摄像头选择器逻辑错误
`updateCameraSelector()` 方法使用了复杂的状态判断逻辑，而不是直接使用后端已经计算好的 `connected` 字段。

### 3. 状态显示更新问题
`updateDogStatus()` 方法在调用 `getStatusClass()` 时只传递了 `info.status` 字符串，没有传递完整的 info 对象。

## 修复方案

### 1. 修复 getStatusClass() 方法
```javascript
getStatusClass(info) {
    // 优先使用后端返回的 connected 字段来判断连接状态
    if (typeof info === 'object' && info.hasOwnProperty('connected')) {
        return info.connected ? 'connected' : 'disconnected';
    }
    
    // 兼容旧的字符串状态格式
    const status = typeof info === 'string' ? info : (info.status || 'disconnected');
    const statusMap = {
        'connected': 'connected',
        'disconnected': 'disconnected',
        'connecting': 'connecting',
        'error': 'error',
        'idle': 'connected',
        'moving': 'connected',
        'dancing': 'connected'
    };
    
    return statusMap[status.toLowerCase()] || 'disconnected';
}
```

### 2. 修复 updateCameraSelector() 方法
```javascript
updateCameraSelector(dogs) {
    const cameraSelector = document.getElementById('cameraSelector');
    cameraSelector.innerHTML = '<option value="">选择摄像头</option>';
    
    Object.entries(dogs).forEach(([name, info]) => {
        const option = document.createElement('option');
        option.value = name;
        
        // 直接使用后端返回的 connected 字段
        const isConnected = info.connected === true;
                           
        option.textContent = isConnected ? `${name} (已连接)` : `${name} (未连接)`;
        
        if (!isConnected) {
            option.disabled = true;
            option.style.color = '#6c757d';
        }
        cameraSelector.appendChild(option);
    });
}
```

### 3. 修复 updateDogStatus() 方法
```javascript
// 传递完整的 info 对象而不是仅仅 info.status
const statusClass = this.getStatusClass(info);
```

## 测试验证

修复后的效果：
1. ✅ 机器狗状态为 "moving" 时，前端正确显示为已连接状态
2. ✅ 摄像头选择框正确显示已连接的机器狗
3. ✅ 状态显示与后端数据保持一致

## 技术要点

1. **状态判断统一化**：使用后端计算的 `connected` 字段作为权威状态，避免前端重复判断逻辑
2. **对象vs字符串处理**：兼容处理对象和字符串两种状态格式
3. **数据流简化**：减少前端状态转换逻辑，直接使用后端状态

## 修复文件

- `/home/wppjkw/go2_webrtc_connect/static/js/app.js`
  - 修复 `getStatusClass()` 方法
  - 修复 `updateCameraSelector()` 方法
  - 修复 `updateDogStatus()` 方法

修复已完成，Web服务器已重启并正常运行。