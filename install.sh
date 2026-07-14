#!/bin/bash
# 文件管理器安装脚本

set -e

if [ "$EUID" -ne 0 ]; then
  echo "请使用 root 用户或 sudo 权限执行此脚本"
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
MAIN_PY="$APP_DIR/main.py"

echo "================================"
echo "  WzFileManager 安装脚本"
echo "================================"

# 1. 判断系统并安装环境
install_system_deps() {
    if [ -f /etc/os-release ]; then . /etc/os-release; OS=$ID; else OS="unknown"; fi
    echo "检测到系统环境: $OS"

    if [[ "$OS" == "ubuntu" ]] || [[ "$OS" == "debian" ]]; then
        apt-get update -y >/dev/null 2>&1
        # 新增 zip, tar, rar, p7zip-full, p7zip-rar, unrar 的安装
        apt-get install -y python3 python3-pip python3-venv curl iproute2 zip tar rar p7zip-full p7zip-rar unrar openssh-server >/dev/null 2>&1
    elif [[ "$OS" == "centos" ]] || [[ "$OS" == "rhel" ]] || [[ "$OS" == "rocky" ]] || [[ "$OS" == "almalinux" ]] || [[ "$OS" == "fedora" ]]; then
        if command -v dnf &> /dev/null; then
            dnf install -y python3 python3-pip curl iproute zip tar rar p7zip p7zip-plugins unrar openssh-server >/dev/null 2>&1
        else
            yum install -y python3 python3-pip curl iproute zip tar rar p7zip p7zip-plugins unrar openssh-server >/dev/null 2>&1
        fi
    elif [[ "$OS" == "openwrt" ]]; then
        echo "检测到 OpenWrt 系统，使用 opkg 安装依赖..."
        opkg update >/dev/null 2>&1
        opkg install python3 python3-pip python3-venv unzip zip tar curl >/dev/null 2>&1
        opkg install p7zip >/dev/null 2>&1 || true
    else
        echo "未识别的系统，尝试使用 apt 安装..."
        apt-get update -y >/dev/null 2>&1
        apt-get install -y python3 python3-pip python3-venv curl zip tar rar p7zip-full p7zip-rar unrar openssh-server >/dev/null 2>&1 || true
    fi
}

# 2. 检查并安装 SFTP 服务
check_sftp_support() {
    echo "正在检查 SFTP 服务支持..."
    # 检查 sshd_config 中是否配置了 sftp 子系统
    if grep -q "^Subsystem sftp" /etc/ssh/sshd_config 2>/dev/null; then
        echo "✅ SFTP 服务已配置"
        return 0
    fi

    echo "⚠️ 未检测到 SFTP 服务，尝试自动安装并配置..."
    
    if [[ "$OS" == "ubuntu" ]] || [[ "$OS" == "debian" ]]; then
        apt-get install -y openssh-server >/dev/null 2>&1
        echo "Subsystem sftp /usr/lib/openssh/sftp-server" >> /etc/ssh/sshd_config
        systemctl restart sshd >/dev/null 2>&1 || service ssh restart >/dev/null 2>&1
    elif [[ "$OS" == "centos" ]] || [[ "$OS" == "rhel" ]] || [[ "$OS" == "rocky" ]] || [[ "$OS" == "almalinux" ]] || [[ "$OS" == "fedora" ]]; then
        dnf install -y openssh-server >/dev/null 2>&1 || yum install -y openssh-server >/dev/null 2>&1
        echo "Subsystem sftp /usr/libexec/openssh/sftp-server" >> /etc/ssh/sshd_config
        systemctl restart sshd >/dev/null 2>&1
    elif [[ "$OS" == "openwrt" ]]; then
        opkg install openssh-sftp-server >/dev/null 2>&1
        /etc/init.d/sshd restart >/dev/null 2>&1 || /etc/init.d/dropbear restart >/dev/null 2>&1
    else
        echo "❌ 无法自动安装 SFTP，请手动确保已安装 openssh-server 并在 sshd_config 中启用 Subsystem sftp"
    fi
}

check_python() {
    if ! command -v python3 &> /dev/null; then echo "错误: 未找到 python3"; exit 1; fi
}

create_venv() {
    if [ ! -d "venv" ]; then
        python3 -m venv venv || { echo "创建虚拟环境失败，请尝试 apt/yum/opkg install python3-venv"; exit 1; }
    fi
}

install_python_deps() {
    source venv/bin/activate
    pip install --upgrade pip >/dev/null 2>&1
    pip install -r requirements.txt
}

setup_config_and_port() {
    mkdir -p logs web/static/css web/static/js web/templates
    echo "正在检测公网IP并分配随机端口..."
    RESULT=$(python3 << 'EOF'
import json, urllib.request, socket, random, os, subprocess

def get_public_ip():
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp: return json.loads(resp.read().decode()).get("ip", "")
    except: pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except: return ""

def get_available_port(port_range):
    min_port, max_port = port_range; used_ports = set()
    try:
        result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 4 and ":" in parts[3]: used_ports.add(int(parts[3].rsplit(":", 1)[1]))
    except: pass
    for _ in range(200):
        port = random.randint(min_port, max_port)
        if port not in used_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try: s.bind(("0.0.0.0", port)); return port
                except OSError: continue
    return None

config = {}
if os.path.exists("config.json"):
    try:
        with open("config.json", 'r') as f: config = json.load(f)
    except: pass

config.setdefault("server", {})
config["server"].setdefault("host", "0.0.0.0")
config["server"].setdefault("port_range", [30000, 55000])
config.setdefault("auth", {})
config["auth"].setdefault("password", "admin123")
config["auth"].setdefault("session_timeout", 2592000) # 30天
config["auth"].setdefault("max_attempts", 5)
config["auth"].setdefault("lock_minutes", 30)
config.setdefault("ssh", {})
config["ssh"].setdefault("port", 22)
config["ssh"].setdefault("username", "root")
config["ssh"].setdefault("auth_type", "password")
config["ssh"].setdefault("password", "")
config["ssh"].setdefault("key_path", "")
config["ssh"].setdefault("key_password", "")
config.setdefault("security", {"allowed_ips": [], "https": False})
config.setdefault("logging", {"level": "INFO", "file": "logs/app.log"})

public_ip = get_public_ip()
if public_ip: config["ssh"]["host"] = public_ip

if not config["server"].get("port"):
    config["server"]["port"] = get_available_port(config["server"]["port_range"])

with open("config.json", 'w') as f: json.dump(config, f, indent=2, ensure_ascii=False)
print(f"{public_ip},{config['server']['port']}")
EOF
)

    PUBLIC_IP=$(echo "$RESULT" | cut -d',' -f1)
    PORT=$(echo "$RESULT" | cut -d',' -f2)

    if [ -n "$PUBLIC_IP" ]; then echo "✅ 检测到公网IP: $PUBLIC_IP"; else PUBLIC_IP="服务器IP"; fi
    if [ -n "$PORT" ]; then
        echo "✅ 分配端口: $PORT"
        if command -v firewall-cmd &> /dev/null; then
            firewall-cmd --zone=public --add-port=$PORT/tcp --permanent >/dev/null 2>&1
            firewall-cmd --reload >/dev/null 2>&1
        elif command -v ufw &> /dev/null; then
            ufw allow $PORT/tcp >/dev/null 2>&1
        else
            iptables -I INPUT -p tcp --dport $PORT -j ACCEPT >/dev/null 2>&1
        fi
    else
        PORT="未分配"
    fi
}

generate_help_file() {
    cat > 使用帮助.txt << EOF
================================
  WzFileManager 使用帮助
================================

访问地址: http://$PUBLIC_IP:$PORT
默认密码: admin123

登录后点击右上角设置图标→添加SSH配置→保存连接并获取数据.

【管理命令】
启动: cd $APP_DIR && venv/bin/python main.py
后台启动: cd $APP_DIR && nohup venv/bin/python main.py > logs/output.log 2>&1 &
停止: pkill -f "$APP_DIR/main.py"
重启: pkill -f "$APP_DIR/main.py"; cd $APP_DIR && nohup venv/bin/python main.py > logs/output.log 2>&1 &

【注意事项】
如果修改了配置文件 config.json 中的端口，请记得在系统防火墙中放行新端口。
EOF
}

# 执行安装
install_system_deps
check_sftp_support
check_python
create_venv
install_python_deps
setup_config_and_port
generate_help_file

echo ""
echo "================================"
echo "  安装完成! 正在启动服务..."
echo "================================"
echo "访问地址: http://$PUBLIC_IP:$PORT"
echo "默认密码: admin123"
echo "登录后点击右上角设置图标→添加SSH配置→保存连接并获取数据."
echo ""

# 自动后台启动 (使用绝对路径 pkill 防止误杀)
pkill -f "$APP_DIR/main.py" >/dev/null 2>&1 || true
nohup $APP_DIR/venv/bin/python $APP_DIR/main.py > $APP_DIR/logs/output.log 2>&1 &
sleep 2
echo "服务已启动，详细帮助请查看当前目录下的 使用帮助.txt"