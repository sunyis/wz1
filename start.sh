#!/bin/bash
# WzFileManager 服务管理脚本

PID_FILE="/opt/data/wzfilemanager.pid"
LOG_FILE="/opt/data/wzfilemanager.log"
CONFIG_FILE="/opt/data/config.json"
BINARY="/opt/wzfilemanager"

# 检查服务是否在运行
is_running() {
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        else
            rm -f "$PID_FILE"
        fi
    fi
    return 1
}

# 生成默认配置
generate_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        cat > "$CONFIG_FILE" << EOF
{
  "server": {
    "host": "0.0.0.0",
    "port": 36688,
    "port_range": [30000, 55000]
  },
  "auth": {
    "password": "admin123",
    "session_timeout": 2592000,
    "max_attempts": 5,
    "lock_minutes": 30
  },
  "ssh": {
    "port": 22,
    "username": "root",
    "auth_type": "password",
    "password": "",
    "key_path": "",
    "key_password": ""
  },
  "security": {
    "allowed_ips": [],
    "https": false
  },
  "logging": {
    "level": "INFO",
    "file": "/opt/data/app.log"
  }
}
EOF
        echo "Generated default config file: $CONFIG_FILE"
    fi
}

# 启动服务
start() {
    if is_running; then
        echo "WzFileManager is already running (PID: $(cat $PID_FILE))"
        return 1
    fi
    
    echo "Starting WzFileManager..."
    generate_config
    
    nohup $BINARY > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    
    sleep 2
    if is_running; then
        echo "WzFileManager started successfully (PID: $(cat $PID_FILE))"
    else
        echo "Failed to start WzFileManager"
        rm -f "$PID_FILE"
        return 1
    fi
}

# 停止服务
stop() {
    if ! is_running; then
        echo "WzFileManager is not running"
        return 1
    fi
    
    echo "Stopping WzFileManager..."
    pid=$(cat "$PID_FILE")
    kill "$pid"
    
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo "WzFileManager stopped successfully"
            return 0
        fi
        sleep 1
    done
    
    kill -9 "$pid" 2>/dev/null
    rm -f "$PID_FILE"
    echo "WzFileManager force stopped"
}

# 重启服务
restart() {
    stop
    sleep 2
    start
}

# 查看服务状态
status() {
    if is_running; then
        echo "WzFileManager is running (PID: $(cat $PID_FILE))"
        return 0
    else
        echo "WzFileManager is not running"
        return 1
    fi
}

# 查看日志
logs() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "No log file found: $LOG_FILE"
        return 1
    fi
    echo "=== Showing WzFileManager logs (Ctrl+C to exit) ==="
    tail -f "$LOG_FILE"
}

# 主程序
case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        # 如果没有传入参数，默认执行 start 并阻塞终端防止 Docker 退出
        start
        tail -f /dev/null
        ;;
esac
