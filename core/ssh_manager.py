import os
import paramiko
import threading
from typing import Optional, Dict, List, Tuple
from io import BytesIO


class SSHManager:
    """SSH 连接管理器 - 支持密码和密钥认证"""

    def __init__(self, config_manager):
        self.config = config_manager
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._lock = threading.Lock()
        self._connected = False

    def connect(self) -> Tuple[bool, str]:
        """建立 SSH 连接"""
        with self._lock:
            # 如果已连接，检查是否还活着
            if self._connected and self._client:
                try:
                    transport = self._client.get_transport()
                    if transport and transport.is_active():
                        return True, "已连接"
                except Exception:
                    self._connected = False

            host = self.config.get("ssh", "host", default="")
            port = self.config.get("ssh", "port", default=22)
            username = self.config.get("ssh", "username", default="root")
            auth_type = self.config.get("ssh", "auth_type", default="password")
            password = self.config.get("ssh", "password", default="")
            key_path = self.config.get("ssh", "key_path", default="")
            key_password = self.config.get("ssh", "key_password", default="")

            if not host:
                return False, "SSH 主机未配置"

            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                if auth_type == "key":
                    if not key_path or not os.path.exists(key_path):
                        return False, f"密钥文件不存在: {key_path}"
                    private_key = paramiko.RSAKey.from_private_key_file(
                        key_path, password=key_password if key_password else None
                    )
                    client.connect(
                        hostname=host, port=port, username=username,
                        pkey=private_key, timeout=10
                    )
                else:
                    client.connect(
                        hostname=host, port=port, username=username,
                        password=password, timeout=10
                    )

                self._client = client
                
                # 【终极修复】适配 OpenWrt/精简系统：自动查找并强行启动 sftp-server
                sftp_connected = False
                try:
                    # 1. 首先尝试标准 SFTP 子系统连接
                    self._sftp = client.open_sftp()
                    sftp_connected = True
                except Exception:
                    pass
                
                if not sftp_connected:
                    try:
                        transport = client.get_transport()
                        sftp_path = ""
                        
                        # 2. 自动通过 SSH 命令查找 sftp-server 的绝对路径
                        try:
                            stdin, stdout, stderr = client.exec_command("find /usr -name sftp-server -type f 2>/dev/null | head -n 1")
                            sftp_path = stdout.read().decode().strip()
                        except:
                            pass
                        
                        # 3. 如果找到了路径，直接强行执行该程序建立 SFTP 通道
                        if sftp_path:
                            try:
                                chan = transport.open_session()
                                chan.exec_command(sftp_path)
                                self._sftp = paramiko.SFTPClient(chan)
                                sftp_connected = True
                            except:
                                pass
                    except Exception:
                        pass
                
                if not sftp_connected:
                    return False, "SSH已连接，但无法建立SFTP通道。请在系统上执行: opkg install openssh-sftp-server"
                
                self._connected = True
                return True, "SSH 连接成功"
            except paramiko.AuthenticationException:
                return False, "认证失败，密码或密钥不正确"
            except paramiko.SSHException as e:
                err_str = str(e)
                if "Unable to connect to port" in err_str or "Errno None" in err_str or "timed out" in err_str or "Connection refused" in err_str:
                    return False, "请检查IP,端口,密码是否正确及网络是否通畅"
                return False, f"SSH 连接错误: {err_str}"
            except Exception as e:
                err_str = str(e)
                if "Unable to connect to port" in err_str or "Errno None" in err_str or "timed out" in err_str or "Connection refused" in err_str:
                    return False, "请检查IP,端口,密码是否正确及网络是否通畅"
                return False, f"连接异常: {err_str}"

    def disconnect(self) -> None:
        """断开连接"""
        with self._lock:
            if self._sftp:
                try:
                    self._sftp.close()
                except Exception:
                    pass
                self._sftp = None
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
            self._connected = False

    def ensure_connected(self) -> Tuple[bool, str]:
        """确保连接活跃"""
        if not self._connected:
            return self.connect()
        try:
            transport = self._client.get_transport()
            if not transport or not transport.is_active():
                self._connected = False
                return self.connect()
            return True, "连接活跃"
        except Exception:
            self._connected = False
            return self.connect()

    def execute(self, command: str, timeout: int = 30) -> Tuple[bool, str, str]:
        """执行命令"""
        ok, msg = self.ensure_connected()
        if not ok:
            return False, "", msg

        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            exit_code = stdout.channel.recv_exit_status()
            return exit_code == 0, out, err
        except Exception as e:
            self._connected = False
            self.disconnect()
            ok_reconnect, msg_reconnect = self.connect()
            if ok_reconnect:
                try:
                    stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
                    out = stdout.read().decode('utf-8', errors='replace')
                    err = stderr.read().decode('utf-8', errors='replace')
                    exit_code = stdout.channel.recv_exit_status()
                    return exit_code == 0, out, err
                except Exception as e2:
                    return False, "", str(e2)
            return False, "", str(e)

    @property
    def sftp(self) -> Optional[paramiko.SFTPClient]:
        """获取 SFTP 客户端"""
        return self._sftp