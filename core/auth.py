import time
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

class AuthManager:
    """认证管理器 - 密码验证 + 失败锁定"""

    def __init__(self, config_manager):
        self.config = config_manager
        self._lock = threading.Lock()
        # {ip: {"attempts": int, "locked_until": float, "last_attempt": float}}
        self._failed_attempts: Dict[str, Dict] = {}
        # {token: {"ip": str, "expires": float}}
        self._sessions: Dict[str, Dict] = {}

    def _get_client_ip(self, request) -> str:
        """获取客户端真实 IP"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def is_locked(self, ip: str) -> Tuple[bool, Optional[int]]:
        """检查 IP 是否被锁定"""
        with self._lock:
            info = self._failed_attempts.get(ip)
            if not info:
                return False, None
            locked_until = info.get("locked_until", 0)
            if locked_until and time.time() < locked_until:
                remaining = int((locked_until - time.time()) / 60) + 1
                return True, remaining
            if locked_until and time.time() >= locked_until:
                # 锁定过期，清除记录
                self._failed_attempts.pop(ip, None)
            return False, None

    def verify_password(self, password: str) -> bool:
        """验证密码"""
        stored = self.config.get("auth", "password", default="")
        return secrets.compare_digest(password, stored)

    def _get_session_timeout(self) -> int:
        """安全获取会话超时时间，防止配置为0或过小导致会话过快过期"""
        timeout_val = self.config.get("auth", "session_timeout", default=2592000)
        try:
            timeout_int = int(timeout_val)
            # 如果配置小于等于0，则赋予默认值 2592000秒(30天)
            if timeout_int <= 0:
                return 2592000
            # 【修复】设置最小超时时间为3600秒(1小时)，防止会话几秒后过期
            if timeout_int < 3600:
                return 3600
            return timeout_int
        except (ValueError, TypeError):
            return 2592000

    def login(self, request, password: str) -> Tuple[bool, str, Optional[str]]:
        """
        登录验证
        返回: (success, message, token)
        """
        ip = self._get_client_ip(request)

        # 检查是否被锁定
        locked, remaining = self.is_locked(ip)
        if locked:
            return False, f"访问已被禁止，请 {remaining} 分钟后再试", None

        # 验证密码
        if not self.verify_password(password):
            with self._lock:
                info = self._failed_attempts.setdefault(ip, {
                    "attempts": 0,
                    "locked_until": 0,
                    "last_attempt": time.time()
                })
                info["attempts"] += 1
                info["last_attempt"] = time.time()

                max_attempts = self.config.get("auth", "max_attempts", default=5)
                lock_minutes = self.config.get("auth", "lock_minutes", default=30)

                if info["attempts"] >= max_attempts:
                    info["locked_until"] = time.time() + lock_minutes * 60
                    return False, f"密码错误次数过多，已禁止访问 {lock_minutes} 分钟", None

                remaining_attempts = max_attempts - info["attempts"]
                return False, f"密码错误，剩余尝试次数 {remaining_attempts} 次", None

        # 登录成功，清除失败记录
        with self._lock:
            self._failed_attempts.pop(ip, None)
            # 生成 session token
            token = secrets.token_hex(32)
            timeout = self._get_session_timeout()
            self._sessions[token] = {
                "ip": ip,
                "expires": time.time() + timeout,
                "created": time.time()
            }

        return True, "登录成功", token

    def verify_session(self, token: str, request) -> bool:
        """验证 session"""
        if not token:
            return False
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return False
            if time.time() > session["expires"]:
                self._sessions.pop(token, None)
                return False
            
            # 【关键修复】滑动续期：只要用户还在操作，就自动延长会话过期时间，保持连接不断开
            timeout = self._get_session_timeout()
            session["expires"] = time.time() + timeout
            
            return True

    def logout(self, token: str) -> None:
        """退出登录"""
        with self._lock:
            self._sessions.pop(token, None)

    def change_password(self, old_password: str, new_password: str) -> Tuple[bool, str]:
        """修改密码"""
        if not self.verify_password(old_password):
            return False, "原密码错误"
        if len(new_password) < 6:
            return False, "新密码长度不能少于6位"
        self.config.set("auth", "password", new_password)
        # 清除所有 session，强制重新登录
        with self._lock:
            self._sessions.clear()
        return True, "密码修改成功，请重新登录"

    def cleanup_expired(self) -> None:
        """清理过期的 session 和锁定记录"""
        now = time.time()
        with self._lock:
            expired_tokens = [
                t for t, s in self._sessions.items()
                if now > s["expires"]
            ]
            for t in expired_tokens:
                self._sessions.pop(t, None)
            expired_locks = [
                ip for ip, info in self._failed_attempts.items()
                if info.get("locked_until", 0) and now > info["locked_until"]
            ]
            for ip in expired_locks:
                self._failed_attempts.pop(ip, None)