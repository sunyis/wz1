#!/bin/bash
# WzFileManager Docker 启动脚本

APP_DIR="/opt/wzfilemanager"
DATA_DIR="$APP_DIR/data"
BINARY="$APP_DIR/wzfilemanager"

# 1. 确保数据和日志目录存在
mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/logs"

# 2. 生成默认配置到 data 目录中 (如果不存在)
if [ ! -f "$DATA_DIR/config.json" ]; then
    cat > "$DATA_DIR/config.json" << EOF
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
    "file": "logs/app.log"
  }
}
EOF
    echo "Generated default config file in data directory."
fi

# 3. 创建软链接，让程序在主目录运行时能读到 data 目录中的配置和日志
# -f 强制覆盖，-s 创建软链接，-n 如果是目录链接不追踪
ln -sfn "$DATA_DIR/config.json" "$APP_DIR/config.json"
ln -sfn "$DATA_DIR/logs" "$APP_DIR/logs"

# 4. 启动程序并阻塞终端
echo "Starting WzFileManager..."
exec "$BINARY"
