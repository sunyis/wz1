import urllib.request
import json
import socket
from typing import Optional


class IPDetector:
    """公网 IP 检测器"""

    DETECT_URLS = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/all.json",
        "https://api.myip.com",
        "http://ip-api.com/json",
    ]

    @staticmethod
    def get_public_ip() -> Optional[str]:
        """获取当前服务器公网 IP"""
        for url in IPDetector.DETECT_URLS:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0"
                })
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    # 不同 API 返回字段不同
                    for key in ("ip", "query", "origin"):
                        if key in data:
                            return data[key]
            except Exception:
                continue

        # 备用：直接通过 socket 连接外部获取
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return None

    @staticmethod
    def get_local_ip() -> Optional[str]:
        """获取本机局域网 IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"