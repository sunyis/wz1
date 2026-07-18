import json
import os
import threading
from pathlib import Path
from typing import Any, Dict


class ConfigManager:
    """配置管理器 - 支持热加载和动态修改"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str = "config.json"):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        # 【关键修复】将 Lock 改为 RLock，允许同一线程多次获取锁，防止 load 调用 save 时死锁
        self._file_lock = threading.RLock()
        self.load()

    def load(self) -> Dict[str, Any]:
        """加载配置文件"""
        with self._file_lock:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
            else:
                self._config = self._default_config()
                self.save()
        return self._config

    def save(self) -> None:
        """保存配置到文件"""
        with self._file_lock:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)

    def get(self, *keys, default=None) -> Any:
        """嵌套获取配置值: config.get('auth', 'password')"""
        data = self._config
        for key in keys:
            if isinstance(data, dict) and key in data:
                data = data[key]
            else:
                return default
        return data

    def set(self, *keys_and_value) -> None:
        """嵌套设置配置值: config.set('auth', 'password', 'new_pwd')"""
        if len(keys_and_value) < 2:
            raise ValueError("至少需要一个键和一个值")
        *keys, value = keys_and_value
        data = self._config
        for key in keys[:-1]:
            if key not in data:
                data[key] = {}
            data = data[key]
        data[keys[-1]] = value
        self.save()

    def update(self, section: str, data: Dict) -> None:
        """更新整个配置段"""
        if section not in self._config:
            self._config[section] = {}
        self._config[section].update(data)
        self.save()

    def reload(self) -> None:
        """重新加载配置"""
        self.load()

    def _default_config(self) -> Dict[str, Any]:
        return {
            "server": {
                "host": "0.0.0.0",
                "port": None,  # 【关键修复】将 null 改为 Python 的 None
                "port_range": [30000, 55000],
                "auto_port": True
            },
            "auth": {
                "password": "admin123",
                "session_timeout": 2592000,
                "max_attempts": 5,
                "lock_minutes": 30
            },
            "ssh": {
                "host": "",
                "port": 22,
                "username": "root",
                "auth_type": "password",
                "password": "",
                "key_path": "",
                "key_password": ""
            },
            "security": {
                "allowed_ips": [],
                "https": False
            },
            "logging": {
                "level": "INFO",
                "file": "logs/app.log"
            }
        }
