import urllib.request
import json
import socket
from typing import Optional

class IPDetector:
    """公网 IP 检测器"""

    # 使用 2 个稳定的 API，防止一个失效自动尝试第二个
    DETECT_URLS = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/all.json",
        "http://checkip.spdyn.de",
        "http://checkip.feste-ip.net",
        "https://api.ip.sb/geoip",
        "http://ip-api.com/json",
        "http://ipv4.wzzw.eu.cc/ip.php?json=1",
        "http://ip.wzzw.eu.cc/myip.php?json=1",
        "https://api.mir6.com/api/ip_json",
        "https://api-ipv6.ip.sb/geoip",
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
                    # 不同 API 返回字段不同 (ipify 返回 ip, ip-api 返回 query)
                    for key in ("ip", "query", "ip_addr", "Current IP Address", "origin"):
                        if key in data:
                            return data[key]
            except Exception:
                continue

        # 备用：如果公网 API 都失败，尝试获取局域网 IP
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