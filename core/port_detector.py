import socket
import random
from typing import Optional, Tuple
import subprocess


class PortDetector:
    """端口检测与分配"""

    @staticmethod
    def is_port_in_use(port: int, host: str = "0.0.0.0") -> bool:
        """检测端口是否被占用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return False
        except OSError:
            return True

    @staticmethod
    def check_port_by_connect(port: int, host: str = "127.0.0.1") -> bool:
        """通过连接检测端口"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result == 0
        except Exception:
            return False

    @staticmethod
    def get_used_ports() -> set:
        """获取系统中已使用的端口"""
        used = set()
        try:
            # Linux/Mac
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    addr = parts[3]
                    if ":" in addr:
                        port = int(addr.rsplit(":", 1)[1])
                        used.add(port)
        except Exception:
            try:
                # 备用 netstat
                result = subprocess.run(
                    ["netstat", "-tlnp"],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.split("\n"):
                    if ":" in line:
                        parts = line.split()
                        for part in parts:
                            if ":" in part:
                                try:
                                    port = int(part.rsplit(":", 1)[1])
                                    used.add(port)
                                except ValueError:
                                    continue
            except Exception:
                pass
        return used

    @staticmethod
    def find_available_port(
        port_range: Tuple[int, int] = (30000, 55000),
        max_attempts: int = 200
    ) -> Optional[int]:
        """在指定范围内找到可用端口"""
        min_port, max_port = port_range
        used_ports = PortDetector.get_used_ports()

        # 随机尝试
        for _ in range(max_attempts):
            port = random.randint(min_port, max_port)
            if port in used_ports:
                continue
            if not PortDetector.is_port_in_use(port):
                return port

        # 顺序扫描
        for port in range(min_port, max_port + 1):
            if port in used_ports:
                continue
            if not PortDetector.is_port_in_use(port):
                return port

        return None