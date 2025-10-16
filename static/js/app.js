/**
 * 双机器狗控制中心 JavaScript 应用
 */

class DualDogController {
    constructor() {
        this.socket = null;
        this.isConnected = false;
        this.currentControlMode = 'all';
        this.selectedDog = null;
        this.movementStatus = { pattern: 'stop', running: false };
        this.scannedDevices = [];  // 扫描到的设备
        this.selectedDevices = []; // 选中的设备
        this.logs = [];           // 日志数据
        this.videoStream = null;  // 摄像头流
        this.currentVideoSource = null; // 当前摄像头源
        this.isRecording = false; // 录制状态
        this.recordingStartTime = null; // 录制开始时间
        this.recordingTimer = null; // 录制计时器
        
        this.init();
    }
    
    init() {
        this.initializeSocket();
        this.bindEvents();
        this.loadInitialData();
        this.setupKeyboardShortcuts();
    }
    
    // Socket.IO 连接管理
    initializeSocket() {
        this.socket = io();
        
        this.socket.on('connect', () => {
            console.log('Connected to server');
            this.isConnected = true;
            this.updateConnectionStatus(true);
        });
        
        this.socket.on('disconnect', () => {
            console.log('Disconnected from server');
            this.isConnected = false;
            this.updateConnectionStatus(false);
        });
        
        this.socket.on('status_update', (data) => {
            this.handleStatusUpdate(data);
        });
        
        this.socket.on('movement_update', (data) => {
            this.handleMovementUpdate(data);
        });
        
        this.socket.on('log_message', (data) => {
            this.handleLogMessage(data);
        });
    }
    
    // 更新连接状态显示
    updateConnectionStatus(connected) {
        const statusElement = document.getElementById('connectionStatus');
        if (connected) {
            statusElement.innerHTML = '<i class="fas fa-circle text-success"></i> 已连接';
        } else {
            statusElement.innerHTML = '<i class="fas fa-circle text-danger"></i> 连接断开';
        }
    }
    
    // 绑定事件监听器
    bindEvents() {
        // 扫描控制
        document.getElementById('scanDevicesBtn').addEventListener('click', () => this.scanDevices());
        document.getElementById('addSelectedDevicesBtn').addEventListener('click', () => this.addSelectedDevices());
        
        // 日志控制
        document.getElementById('clearLogsBtn').addEventListener('click', () => this.clearLogs());
        document.getElementById('refreshLogsBtn').addEventListener('click', () => this.refreshLogs());
        document.getElementById('logLevelFilter').addEventListener('change', () => this.filterLogs());
        
        // 摄像头控制
        document.getElementById('startVideoBtn').addEventListener('click', () => this.startVideo());
        document.getElementById('stopVideoBtn').addEventListener('click', () => this.stopVideo());
        document.getElementById('cameraSelector').addEventListener('change', (e) => this.selectCamera(e.target.value));
        
        // 录制控制
        document.getElementById('startRecordBtn').addEventListener('click', () => this.startRecording());
        document.getElementById('stopRecordBtn').addEventListener('click', () => this.stopRecording());
        
        // 连接控制
        document.getElementById('connectAllBtn').addEventListener('click', () => this.connectAllDogs());
        document.getElementById('disconnectAllBtn').addEventListener('click', () => this.disconnectAllDogs());
        
        // 添加机器狗
        document.getElementById('addDogForm').addEventListener('submit', (e) => this.addDog(e));
        
        // 控制模式切换
        document.querySelectorAll('input[name="controlMode"]').forEach(radio => {
            radio.addEventListener('change', (e) => this.setControlMode(e.target.value));
        });
        
        // 机器狗选择
        document.getElementById('selectedDog').addEventListener('change', (e) => {
            this.selectedDog = e.target.value;
        });
        
        // 自动运动控制
        document.getElementById('startSquareWalkBtn').addEventListener('click', () => this.startMovement('square_walk'));
        document.getElementById('startDancePartyBtn').addEventListener('click', () => this.startMovement('dance_party'));
        document.getElementById('stopMovementBtn').addEventListener('click', () => this.stopMovement());
        document.getElementById('emergencyStopBtn').addEventListener('click', () => this.emergencyStop());
        
        // 扑跃控制
        document.getElementById('pounceOnceBtn').addEventListener('click', () => this.executePounce(1));
        document.getElementById('pounceTripleBtn').addEventListener('click', () => this.executePounce(3));
        
        // 手动控制
        document.getElementById('enableManualBtn').addEventListener('click', () => this.enableManualControl());
        
        // 方向控制按钮
        document.querySelectorAll('.control-btn').forEach(btn => {
            btn.addEventListener('mousedown', (e) => this.startManualControl(e.target.dataset.direction));
            btn.addEventListener('mouseup', () => this.stopManualControl());
            btn.addEventListener('mouseleave', () => this.stopManualControl());
        });
        
        // 动作按钮
        document.querySelectorAll('.action-btn').forEach(btn => {
            btn.addEventListener('click', (e) => this.executeAction(e.target.dataset.action));
        });
    }
    
    // 加载初始数据
    async loadInitialData() {
        try {
            const response = await fetch('/api/status');
            const data = await response.json();
            
            if (data.success) {
                this.updateDogStatus(data.dogs);
                this.updateMovementStatus(data.movement);
                this.updateControlMode(data.control_mode);
            }
            
            // 加载日志
            await this.refreshLogs();
            
        } catch (error) {
            console.error('Failed to load initial data:', error);
            this.showMessage('加载初始数据失败', 'error');
        }
    }
    
    // 键盘快捷键
    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // 只在没有输入框焦点时响应
            if (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'SELECT') {
                return;
            }
            
            switch(e.key.toLowerCase()) {
                case 'w':
                    this.startManualControl('forward');
                    break;
                case 's':
                    this.startManualControl('backward');
                    break;
                case 'a':
                    this.startManualControl('left');
                    break;
                case 'd':
                    this.startManualControl('right');
                    break;
                case 'q':
                    this.startManualControl('turn_left');
                    break;
                case 'e':
                    this.startManualControl('turn_right');
                    break;
                case ' ':
                    e.preventDefault();
                    this.stopManualControl();
                    break;
                case 'escape':
                    this.emergencyStop();
                    break;
            }
        });
        
        document.addEventListener('keyup', (e) => {
            if (['w', 's', 'a', 'd', 'q', 'e'].includes(e.key.toLowerCase())) {
                this.stopManualControl();
            }
        });
    }
    
    // 扫描设备管理
    async scanDevices() {
        const btn = document.getElementById('scanDevicesBtn');
        btn.classList.add('btn-scanning');
        btn.disabled = true;
        
        try {
            const result = await this.apiCall('/api/scan/devices', 'GET');
            this.scannedDevices = result.devices || [];
            this.displayScanResults();
            
            this.showMessage(`扫描完成，发现 ${this.scannedDevices.length} 台设备`, 'success');
            
        } catch (error) {
            this.showMessage('扫描设备失败', 'error');
        } finally {
            btn.classList.remove('btn-scanning');
            btn.disabled = false;
        }
    }
    
    displayScanResults() {
        const resultsDiv = document.getElementById('scanResults');
        const listDiv = document.getElementById('scanResultsList');
        
        if (this.scannedDevices.length === 0) {
            resultsDiv.style.display = 'none';
            return;
        }
        
        resultsDiv.style.display = 'block';
        listDiv.innerHTML = '';
        
        this.scannedDevices.forEach((device, index) => {
            const deviceItem = document.createElement('div');
            deviceItem.className = 'device-scan-item';
            deviceItem.dataset.index = index;
            
            deviceItem.innerHTML = `
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <div class="device-name">${device.name}</div>
                        <div class="device-info">
                            IP: ${device.ip} | SN: ${device.serial}
                        </div>
                    </div>
                    <div>
                        <span class="device-type">${device.device_type}</span>
                    </div>
                </div>
            `;
            
            deviceItem.addEventListener('click', () => this.toggleDeviceSelection(index));
            listDiv.appendChild(deviceItem);
        });
        
        this.updateAddButton();
    }
    
    toggleDeviceSelection(index) {
        const deviceItem = document.querySelector(`[data-index="${index}"]`);
        const isSelected = deviceItem.classList.contains('selected');
        
        if (isSelected) {
            deviceItem.classList.remove('selected');
            this.selectedDevices = this.selectedDevices.filter(i => i !== index);
        } else {
            deviceItem.classList.add('selected');
            this.selectedDevices.push(index);
        }
        
        this.updateAddButton();
    }
    
    updateAddButton() {
        const addBtn = document.getElementById('addSelectedDevicesBtn');
        addBtn.disabled = this.selectedDevices.length === 0;
        addBtn.textContent = this.selectedDevices.length > 0 ? 
            `添加选中设备 (${this.selectedDevices.length})` : 
            '添加选中设备';
    }
    
    async addSelectedDevices() {
        if (this.selectedDevices.length === 0) return;
        
        let successCount = 0;
        
        for (const index of this.selectedDevices) {
            const device = this.scannedDevices[index];
            try {
                await this.apiCall('/api/dogs', 'POST', {
                    name: device.name,
                    ip: device.ip,
                    serial: device.serial,
                    method: 'LocalSTA'
                });
                successCount++;
            } catch (error) {
                console.error(`添加设备 ${device.name} 失败:`, error);
            }
        }
        
        this.showMessage(`成功添加 ${successCount}/${this.selectedDevices.length} 台设备`, 'success');
        this.selectedDevices = [];
        this.displayScanResults();
        this.loadInitialData(); // 重新加载数据
    }
    
    // 日志管理
    async refreshLogs() {
        try {
            const result = await this.apiCall('/api/logs?limit=100', 'GET');
            this.logs = result.logs || [];
            this.displayLogs();
        } catch (error) {
            console.error('刷新日志失败:', error);
        }
    }
    
    async clearLogs() {
        try {
            await this.apiCall('/api/logs/clear', 'POST');
            this.logs = [];
            this.displayLogs();
            this.showMessage('日志已清空', 'info');
        } catch (error) {
            this.showMessage('清空日志失败', 'error');
        }
    }
    
    filterLogs() {
        this.displayLogs();
    }
    
    displayLogs() {
        const logDisplay = document.getElementById('logDisplay');
        const levelFilter = document.getElementById('logLevelFilter').value;
        
        let filteredLogs = this.logs;
        if (levelFilter) {
            filteredLogs = this.logs.filter(log => log.level.toUpperCase() === levelFilter);
        }
        
        if (filteredLogs.length === 0) {
            logDisplay.innerHTML = '<div class="p-2 text-muted text-center">暂无日志数据</div>';
            return;
        }
        
        logDisplay.innerHTML = '';
        
        filteredLogs.forEach(log => {
            const logEntry = document.createElement('div');
            logEntry.className = `log-entry log-${log.level.toLowerCase()}`;
            
            const timestamp = new Date(log.timestamp).toLocaleTimeString();
            
            logEntry.innerHTML = `
                <div class="d-flex">
                    <span class="log-timestamp">${timestamp}</span>
                    <span class="log-source">[${log.source}]</span>
                    <span class="log-message">${log.message}</span>
                </div>
            `;
            
            logDisplay.appendChild(logEntry);
        });
        
        // 滚动到底部
        logDisplay.scrollTop = logDisplay.scrollHeight;
    }
    
    handleLogMessage(logData) {
        this.logs.push(logData);
        
        // 限制日志数量
        if (this.logs.length > 500) {
            this.logs = this.logs.slice(-500);
        }
        
        this.displayLogs();
    }
    async apiCall(url, method = 'GET', data = null) {
        try {
            const options = {
                method: method,
                headers: {
                    'Content-Type': 'application/json',
                }
            };
            
            if (data) {
                options.body = JSON.stringify(data);
            }
            
            const response = await fetch(url, options);
            const result = await response.json();
            
            if (!result.success) {
                throw new Error(result.error || '操作失败');
            }
            
            return result;
            
        } catch (error) {
            console.error('API call failed:', error);
            this.showMessage(error.message, 'error');
            throw error;
        }
    }
    
    // 机器狗管理
    async addDog(event) {
        event.preventDefault();
        
        const name = document.getElementById('dogName').value;
        const ip = document.getElementById('dogIP').value;
        const method = document.getElementById('connectionMethod').value;
        
        if (!name || !ip) {
            this.showMessage('请填写机器狗名称和IP地址', 'warning');
            return;
        }
        
        try {
            await this.apiCall('/api/dogs', 'POST', {
                name: name,
                ip: ip,
                method: method
            });
            
            this.showMessage(`机器狗 ${name} 添加成功`, 'success');
            document.getElementById('addDogForm').reset();
            this.loadInitialData(); // 重新加载数据
            
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    async removeDog(dogName) {
        try {
            await this.apiCall(`/api/dogs/${dogName}`, 'DELETE');
            this.showMessage(`机器狗 ${dogName} 移除成功`, 'success');
            this.loadInitialData();
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    async connectAllDogs() {
        const btn = document.getElementById('connectAllBtn');
        btn.classList.add('btn-loading');
        btn.disabled = true;
        
        try {
            const result = await this.apiCall('/api/dogs/connect', 'POST', {});
            this.showMessage('连接请求已发送', 'info');
        } catch (error) {
            // 错误已在apiCall中处理
        } finally {
            btn.classList.remove('btn-loading');
            btn.disabled = false;
        }
    }
    
    async disconnectAllDogs() {
        try {
            await this.apiCall('/api/dogs/disconnect', 'POST');
            this.showMessage('所有机器狗已断开连接', 'info');
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    // 控制模式管理
    async setControlMode(mode) {
        this.currentControlMode = mode;
        
        const dogSelectionDiv = document.getElementById('dogSelectionDiv');
        if (mode === 'single') {
            dogSelectionDiv.style.display = 'block';
        } else {
            dogSelectionDiv.style.display = 'none';
        }
        
        try {
            await this.apiCall('/api/control/mode', 'POST', {
                mode: mode,
                selected_dogs: mode === 'single' && this.selectedDog ? [this.selectedDog] : []
            });
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    // 运动控制
    async startMovement(pattern) {
        const buttons = {
            'square_walk': 'startSquareWalkBtn',
            'dance_party': 'startDancePartyBtn'
        };
        
        const btn = document.getElementById(buttons[pattern]);
        if (btn) {
            btn.classList.add('btn-loading');
            btn.disabled = true;
        }
        
        try {
            await this.apiCall('/api/movement/start', 'POST', { pattern: pattern });
            this.showMessage(`${pattern === 'square_walk' ? '正方形走路' : '舞蹈派对'}模式已启动`, 'success');
        } catch (error) {
            // 错误已在apiCall中处理
        } finally {
            if (btn) {
                btn.classList.remove('btn-loading');
                btn.disabled = false;
            }
        }
    }
    
    async stopMovement() {
        try {
            await this.apiCall('/api/movement/stop', 'POST');
            this.showMessage('运动已停止', 'info');
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    async emergencyStop() {
        const btn = document.getElementById('emergencyStopBtn');
        btn.classList.add('btn-emergency');
        
        try {
            await this.apiCall('/api/movement/emergency_stop', 'POST');
            this.showMessage('紧急停止已执行', 'warning');
        } catch (error) {
            // 错误已在apiCall中处理
        } finally {
            setTimeout(() => {
                btn.classList.remove('btn-emergency');
            }, 2000);
        }
    }
    
    async executePounce(count) {
        const btnId = count === 1 ? 'pounceOnceBtn' : 'pounceTripleBtn';
        const btn = document.getElementById(btnId);
        btn.classList.add('btn-loading');
        btn.disabled = true;
        
        try {
            await this.apiCall('/api/movement/pounce', 'POST', {
                count: count,
                dog_name: this.currentControlMode === 'single' ? this.selectedDog : null
            });
            
            this.showMessage(`扑跃${count === 1 ? '一次' : '三次'}动作已执行`, 'success');
        } catch (error) {
            // 错误已在apiCall中处理
        } finally {
            btn.classList.remove('btn-loading');
            btn.disabled = false;
        }
    }
    
    // 手动控制
    async enableManualControl() {
        try {
            await this.apiCall('/api/movement/start', 'POST', { pattern: 'manual_control' });
            this.showMessage('手动控制模式已启用', 'success');
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    async startManualControl(direction) {
        if (this.movementStatus.pattern !== 'manual_control') {
            await this.enableManualControl();
        }
        
        try {
            await this.apiCall('/api/movement/manual', 'POST', {
                direction: direction,
                dog_name: this.currentControlMode === 'single' ? this.selectedDog : null,
                speed: 0.5,
                duration: 0.5
            });
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    async stopManualControl() {
        try {
            await this.apiCall('/api/movement/manual', 'POST', {
                direction: 'stop',
                dog_name: this.currentControlMode === 'single' ? this.selectedDog : null
            });
        } catch (error) {
            // 错误已在apiCall中处理
        }
    }
    
    // 动作执行
    async executeAction(action) {
        const btn = event.target.closest('.action-btn');
        btn.classList.add('btn-loading');
        btn.disabled = true;
        
        try {
            await this.apiCall('/api/movement/action', 'POST', {
                action: action,
                dog_name: this.currentControlMode === 'single' ? this.selectedDog : null
            });
            
            this.showMessage(`${action}动作已执行`, 'success');
        } catch (error) {
            // 错误已在apiCall中处理
        } finally {
            btn.classList.remove('btn-loading');
            btn.disabled = false;
        }
    }
    
    // 状态更新处理
    handleStatusUpdate(data) {
        if (data.dogs) {
            this.updateDogStatus(data.dogs);
        }
        
        if (data.dog_name) {
            // 单个机器狗状态更新 - 使用完整的dog_info或status
            const statusInfo = data.dog_info || data.status;
            this.updateSingleDogStatus(data.dog_name, statusInfo);
        }
        
        if (data.control_mode) {
            this.updateControlMode(data.control_mode);
        }
    }
    
    handleMovementUpdate(data) {
        this.movementStatus = {
            pattern: data.pattern,
            running: data.running,
            info: data.info
        };
        this.updateMovementStatus(data);
    }
    
    // UI 更新方法
    updateDogStatus(dogs) {
        const container = document.getElementById('dogStatusList');
        container.innerHTML = '';
        
        // 更新机器狗选择下拉框
        const selectElement = document.getElementById('selectedDog');
        selectElement.innerHTML = '<option value="">选择机器狗</option>';
        
        Object.entries(dogs).forEach(([name, info]) => {
            // 创建状态显示项
            const item = document.createElement('div');
            item.className = 'dog-status-item fade-in';
            
            // 使用整个 info 对象来获取正确的状态类
            const statusClass = this.getStatusClass(info);
            
            item.innerHTML = `
                <div class="d-flex justify-content-between align-items-center">
                    <div>
                        <div class="dog-name">${name}</div>
                        <div class="dog-ip">${info.ip}</div>
                    </div>
                    <div class="text-end">
                        <div class="dog-status dog-status-${statusClass}">${info.status}</div>
                        <button class="btn btn-sm btn-outline-danger mt-1" onclick="dogController.removeDog('${name}')">
                            <i class="fas fa-trash"></i>
                        </button>
                    </div>
                </div>
            `;
            
            container.appendChild(item);
            
            // 添加到选择下拉框（无论是否连接都显示，方便选择）
            const option = document.createElement('option');
            option.value = name;
            option.textContent = info.connected ? name : `${name} (未连接)`;
            if (!info.connected) {
                option.style.color = '#6c757d';
            }
            selectElement.appendChild(option);
        });
        
        // 更新摄像头选择器
        this.updateCameraSelector(dogs);
    }
    
    updateSingleDogStatus(dogName, statusData) {
        console.log(`Dog ${dogName} status updated:`, statusData);
        
        // 如果有完整的状态信息，更新对应的UI元素
        if (statusData && typeof statusData === 'object') {
            // 找到对应的状态显示元素
            const container = document.getElementById('dogStatusList');
            const dogItems = container.querySelectorAll('.dog-status-item');
            
            dogItems.forEach(item => {
                const nameElement = item.querySelector('.dog-name');
                if (nameElement && nameElement.textContent === dogName) {
                    // 更新状态显示
                    const statusElement = item.querySelector('.dog-status');
                    if (statusElement) {
                        const statusClass = this.getStatusClass(statusData);
                        statusElement.className = `dog-status dog-status-${statusClass}`;
                        statusElement.textContent = statusData.status || statusData;
                    }
                }
            });
            
            // 更新摄像头选择器
            this.updateCameraSelectorSingle(dogName, statusData);
        } else {
            // 如果只有状态字符串，刷新所有状态
            this.loadInitialData();
        }
    }
    
    updateMovementStatus(movement) {
        const statusElement = document.getElementById('movementStatus');
        const statusText = movement.running ? 
            `运行中: ${movement.pattern} ${movement.info || ''}` : 
            '系统就绪';
        
        statusElement.textContent = statusText;
        
        // 更新状态样式
        const alertElement = statusElement.closest('.alert');
        alertElement.className = movement.running ? 
            'alert alert-success' : 
            'alert alert-info';
    }
    
    updateControlMode(controlMode) {
        const [mode, selectedDogs] = controlMode;
        
        // 更新单选按钮
        document.getElementById(`controlMode${mode.charAt(0).toUpperCase() + mode.slice(1)}`).checked = true;
        
        // 更新控制模式
        this.setControlMode(mode);
        
        if (selectedDogs.length > 0) {
            this.selectedDog = selectedDogs[0];
            document.getElementById('selectedDog').value = selectedDogs[0];
        }
    }
    
    getStatusClass(info) {
        // 优先使用后端返回的 connected 字段来判断连接状态
        if (typeof info === 'object' && info.hasOwnProperty('connected')) {
            return info.connected ? 'connected' : 'disconnected';
        }
        
        // 兼容旧的字符串状态格式
        const status = typeof info === 'string' ? info : (info.status || 'disconnected');
        const statusMap = {
            // 连接状态
            'connected': 'connected',
            'disconnected': 'disconnected',
            'connecting': 'connecting',
            'error': 'error',
            // 工作状态  
            'idle': 'connected',      // idle状态显示为已连接
            'moving': 'connected',    // moving状态显示为已连接
            'dancing': 'connected'    // dancing状态显示为已连接
        };
        
        return statusMap[status.toLowerCase()] || 'disconnected';
    }
    
    // 摄像头管理
    async startVideo() {
        const cameraSelector = document.getElementById('cameraSelector');
        const selectedDog = cameraSelector.value;
        
        if (!selectedDog) {
            this.showMessage('请选择要查看摄像头的机器狗', 'warning');
            return;
        }
        
        const startBtn = document.getElementById('startVideoBtn');
        const stopBtn = document.getElementById('stopVideoBtn');
        const videoElement = document.getElementById('videoElement');
        const videoPlaceholder = document.getElementById('videoPlaceholder');
        const videoContainer = document.getElementById('videoContainer');
        const videoStatus = document.getElementById('videoStatus');
        
        try {
            startBtn.disabled = true;
            startBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>连接中...';
            
            // 调用后端 API 开启视频通道
            const result = await this.apiCall('/api/video/start', 'POST', {
                dog_name: selectedDog
            });
            
            if (result.success && result.stream_url) {
                // 使用MJPEG流显示视频
                videoPlaceholder.style.display = 'none';
                videoElement.style.display = 'block';
                
                // 设置视频源为MJPEG流
                videoElement.src = result.stream_url;
                
                // 更新UI状态
                videoContainer.classList.add('active');
                videoContainer.classList.remove('error');
                startBtn.style.display = 'none';
                stopBtn.style.display = 'inline-block';
                videoStatus.textContent = `正在播放 ${selectedDog} 的实时画面`;
                videoStatus.className = 'text-success';
                
                // 显示录制按钮
                document.getElementById('startRecordBtn').style.display = 'inline-block';
                
                this.currentVideoSource = selectedDog;
                this.showMessage('视频流已开启', 'success');
                
                // 添加错误处理
                videoElement.onerror = () => {
                    this.handleVideoError('视频流加载失败，请检查连接');
                };
                
            } else {
                this.handleVideoError(result.error || '启动视频通道失败');
            }
            
        } catch (error) {
            this.handleVideoError('启动摄像头失败: ' + error.message);
        } finally {
            startBtn.disabled = false;
            startBtn.innerHTML = '<i class="fas fa-play me-1"></i>开启摄像头';
        }
    }
    
    async stopVideo() {
        const startBtn = document.getElementById('startVideoBtn');
        const stopBtn = document.getElementById('stopVideoBtn');
        const videoElement = document.getElementById('videoElement');
        const videoPlaceholder = document.getElementById('videoPlaceholder');
        const videoContainer = document.getElementById('videoContainer');
        const videoStatus = document.getElementById('videoStatus');
        
        try {
            if (this.currentVideoSource) {
                // 停止视频元素播放
                videoElement.src = '';
                
                // 调用后端 API 停止视频流
                await this.apiCall('/api/video/stop', 'POST', {
                    dog_name: this.currentVideoSource
                });
            }
            
            // 重置显示状态
            videoElement.style.display = 'none';
            videoPlaceholder.style.display = 'flex';
            videoPlaceholder.innerHTML = `
                <div class="text-light">
                    <i class="fas fa-video-slash fa-3x mb-2"></i>
                    <div>请选择机器狗并启用摄像头</div>
                </div>
            `;
            
            // 重置UI状态
            videoContainer.classList.remove('active', 'error');
            startBtn.style.display = 'inline-block';
            stopBtn.style.display = 'none';
            videoStatus.textContent = '摄像头已关闭';
            videoStatus.className = 'text-muted';
            
            // 隐藏录制按钮
            document.getElementById('startRecordBtn').style.display = 'none';
            document.getElementById('stopRecordBtn').style.display = 'none';
            document.getElementById('recordingIndicator').style.display = 'none';
            document.getElementById('recordingStatus').textContent = '';
            
            this.currentVideoSource = null;
            this.showMessage('视频流已关闭', 'info');
            
            // 如果正在录制，先停止录制
            if (this.isRecording) {
                await this.stopRecording();
            }
            
        } catch (error) {
            this.showMessage('关闭视频通道失败: ' + error.message, 'error');
        }
    }
    
    // 开始录制
    async startRecording() {
        if (!this.currentVideoSource) {
            this.showMessage('请先开启摄像头', 'warning');
            return;
        }
        
        if (this.isRecording) {
            this.showMessage('已经在录制中', 'warning');
            return;
        }
        
        const startRecordBtn = document.getElementById('startRecordBtn');
        const stopRecordBtn = document.getElementById('stopRecordBtn');
        const recordingIndicator = document.getElementById('recordingIndicator');
        const recordingStatus = document.getElementById('recordingStatus');
        
        try {
            startRecordBtn.disabled = true;
            startRecordBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>启动中...';
            
            // 调用后端 API 开始录制
            const result = await this.apiCall('/api/video/record/start', 'POST', {
                dog_name: this.currentVideoSource
            });
            
            if (result.success) {
                this.isRecording = true;
                this.recordingStartTime = Date.now();
                
                // 更新UI
                startRecordBtn.style.display = 'none';
                stopRecordBtn.style.display = 'inline-block';
                recordingIndicator.style.display = 'block';
                recordingStatus.textContent = `正在录制: ${result.filename}`;
                recordingStatus.className = 'text-danger recording';
                
                // 启动计时器
                this.startRecordingTimer();
                
                this.showMessage('开始录制视频', 'success');
            } else {
                this.showMessage(result.error || '开始录制失败', 'error');
            }
            
        } catch (error) {
            this.showMessage('开始录制失败: ' + error.message, 'error');
        } finally {
            startRecordBtn.disabled = false;
            startRecordBtn.innerHTML = '<i class="fas fa-circle me-1"></i>开始录制';
        }
    }
    
    // 停止录制
    async stopRecording() {
        if (!this.isRecording) {
            return;
        }
        
        const startRecordBtn = document.getElementById('startRecordBtn');
        const stopRecordBtn = document.getElementById('stopRecordBtn');
        const recordingIndicator = document.getElementById('recordingIndicator');
        const recordingStatus = document.getElementById('recordingStatus');
        
        try {
            stopRecordBtn.disabled = true;
            stopRecordBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>停止中...';
            
            // 调用后端 API 停止录制
            const result = await this.apiCall('/api/video/record/stop', 'POST', {
                dog_name: this.currentVideoSource
            });
            
            if (result.success) {
                this.isRecording = false;
                this.recordingStartTime = null;
                
                // 停止计时器
                this.stopRecordingTimer();
                
                // 更新UI
                startRecordBtn.style.display = 'inline-block';
                stopRecordBtn.style.display = 'none';
                recordingIndicator.style.display = 'none';
                recordingStatus.textContent = `录制完成: ${result.filename} (${Math.floor(result.duration)}秒, ${result.frame_count}帧)`;
                recordingStatus.className = 'text-success';
                
                this.showMessage(`视频已保存: ${result.filename}`, 'success');
                
                // 5秒后清空录制状态提示
                setTimeout(() => {
                    recordingStatus.textContent = '';
                }, 5000);
            } else {
                this.showMessage(result.error || '停止录制失败', 'error');
            }
            
        } catch (error) {
            this.showMessage('停止录制失败: ' + error.message, 'error');
        } finally {
            stopRecordBtn.disabled = false;
            stopRecordBtn.innerHTML = '<i class="fas fa-stop-circle me-1"></i>停止录制';
        }
    }
    
    // 启动录制计时器
    startRecordingTimer() {
        const recordingTime = document.getElementById('recordingTime');
        
        this.recordingTimer = setInterval(() => {
            if (this.recordingStartTime) {
                const elapsed = Math.floor((Date.now() - this.recordingStartTime) / 1000);
                const minutes = Math.floor(elapsed / 60).toString().padStart(2, '0');
                const seconds = (elapsed % 60).toString().padStart(2, '0');
                recordingTime.textContent = `${minutes}:${seconds}`;
            }
        }, 1000);
    }
    
    // 停止录制计时器
    stopRecordingTimer() {
        if (this.recordingTimer) {
            clearInterval(this.recordingTimer);
            this.recordingTimer = null;
        }
        
        const recordingTime = document.getElementById('recordingTime');
        recordingTime.textContent = '00:00';
    }
    
    selectCamera(dogName) {
        // 如果当前正在播放视频，则自动切换
        if (this.currentVideoSource && dogName && dogName !== this.currentVideoSource) {
            this.stopVideo().then(() => {
                if (dogName) {
                    // 延迟一下再开启新的摄像头
                    setTimeout(() => {
                        this.startVideo();
                    }, 500);
                }
            });
        }
    }
    
    handleVideoError(errorMessage) {
        const videoContainer = document.getElementById('videoContainer');
        const videoStatus = document.getElementById('videoStatus');
        const videoElement = document.getElementById('videoElement');
        const videoPlaceholder = document.getElementById('videoPlaceholder');
        const startBtn = document.getElementById('startVideoBtn');
        const stopBtn = document.getElementById('stopVideoBtn');
        
        // 显示错误状态
        videoContainer.classList.remove('active');
        videoContainer.classList.add('error');
        videoElement.style.display = 'none';
        videoPlaceholder.style.display = 'flex';
        videoPlaceholder.innerHTML = `
            <div class="text-danger">
                <i class="fas fa-exclamation-triangle fa-3x mb-2"></i>
                <div>${errorMessage}</div>
            </div>
        `;
        
        videoStatus.textContent = errorMessage;
        videoStatus.className = 'video-status error';
        
        // 重置按钮状态
        startBtn.style.display = 'inline-block';
        stopBtn.style.display = 'none';
        
        this.currentVideoSource = null;
        this.showMessage(errorMessage, 'error');
        
        // 5秒后重置显示
        setTimeout(() => {
            videoContainer.classList.remove('error');
            videoPlaceholder.innerHTML = `
                <div class="text-light">
                    <i class="fas fa-video-slash fa-3x mb-2"></i>
                    <div>请选择机器狗并启用摄像头</div>
                </div>
            `;
        }, 5000);
    }
    
    updateCameraSelector(dogs) {
        const cameraSelector = document.getElementById('cameraSelector');
        cameraSelector.innerHTML = '<option value="">选择摄像头</option>';
        
        Object.entries(dogs).forEach(([name, info]) => {
            const option = document.createElement('option');
            option.value = name;
            
            // 直接使用后端返回的 connected 字段，它已经包含了正确的连接状态判断
            const isConnected = info.connected === true;
                               
            option.textContent = isConnected ? `${name} (已连接)` : `${name} (未连接)`;
            
            if (!isConnected) {
                option.disabled = true;
                option.style.color = '#6c757d';
            }
            cameraSelector.appendChild(option);
        });
    }
    
    updateCameraSelectorSingle(dogName, statusInfo) {
        const cameraSelector = document.getElementById('cameraSelector');
        const options = cameraSelector.querySelectorAll('option');
        
        options.forEach(option => {
            if (option.value === dogName) {
                const isConnected = statusInfo.connected === true;
                option.textContent = isConnected ? `${dogName} (已连接)` : `${dogName} (未连接)`;
                option.disabled = !isConnected;
                option.style.color = isConnected ? '' : '#6c757d';
            }
        });
    }

    // 消息显示
    showMessage(message, type = 'info') {
        const modal = document.getElementById('messageModal');
        const title = document.getElementById('messageModalTitle');
        const body = document.getElementById('messageModalBody');
        
        const titles = {
            'success': '成功',
            'error': '错误',
            'warning': '警告',
            'info': '信息'
        };
        
        title.textContent = titles[type] || '提示';
        body.textContent = message;
        
        const modalInstance = new bootstrap.Modal(modal);
        modalInstance.show();
        
        // 自动关闭成功和信息消息
        if (type === 'success' || type === 'info') {
            setTimeout(() => {
                modalInstance.hide();
            }, 2000);
        }
    }
}

// 初始化应用
let dogController;
document.addEventListener('DOMContentLoaded', () => {
    dogController = new DualDogController();
});