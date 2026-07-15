#!/bin/bash
# WzFileManager Docker 启动脚本

APP_DIR="/opt/wzfilemanager"
DATA_DIR="$APP_DIR/data"
PID_FILE="$DATA_DIR/wzfilemanager.pid"
LOG_FILE="$DATA_DIR/wzfilemanager.log"
CONFIG_FILE="$DATA_DIR/config.json"
BINARY="$APP_DIR/wzfilemanager"

# 确保数据目录存在
mkdir -p "$DATA_DIR"

# 生成默认配置 (如果不存在)
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
    "file": "$DATA_DIR/app.log"
  }
}
EOF
    echo "Generated default config file: $CONFIG_FILE"
fi

# 启动程序并阻塞终端
echo "Starting WzFileManager..."
exec "$BINARY"
