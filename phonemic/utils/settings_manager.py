"""
配置管理模块 - 单例模式，支持持久化存储和精准变更通知
"""

import json
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, QStandardPaths
from phonemic.utils import system_lang
from phonemic.utils.paths import get_config_dir

class SettingsManager(QObject):
    """配置管理单例类 - 精准变更通知"""
    
    # 精准信号：参数为 (key, new_value)，仅在值真正变化时发射
    setting_changed = Signal(str, object)
    
    _instance: Optional["SettingsManager"] = None
    
    def __new__(cls) -> "SettingsManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        """初始化（仅执行一次）"""
        if hasattr(self, "_initialized"):
            return
        super().__init__()
        self._initialized = True
        self._settings: dict = {}
        self._config_path: Optional[Path] = None
        self.load()
    
    def _get_config_path(self) -> Path:
        """获取配置文件路径（自动创建目录）"""
        if self._config_path is None:
            app_dir = get_config_dir()
            app_dir.mkdir(parents=True, exist_ok=True)
            self._config_path = app_dir / "settings.json"
        return self._config_path
    
    def load(self) -> None:
        """从磁盘加载配置，若文件不存在或损坏则写入默认配置"""
        default = {
            "hud_timeout_sec": 5,
            "hud_font_size": 14,
            "hud_escape_enabled": True,
            "mobile_max_records": 10,
            "language": system_lang.detect_system_language(),
            "close_action": None  # None=未设置, "quit"=退出, "tray"=最小化到托盘
        }
        print(f"default lan is {default['language']}")
        
        path = self._get_config_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # 合并默认值（确保新增字段存在）
                for k, v in default.items():
                    if k not in loaded:
                        loaded[k] = v
                # 类型校验：hud_font_size 可以是 int 或 "system"
                if "hud_font_size" in loaded:
                    val = loaded["hud_font_size"]
                    if not (isinstance(val, int) or val == "system"):
                        loaded["hud_font_size"] = default["hud_font_size"]
                self._settings = loaded
            except Exception:
                # 文件损坏，重置为默认配置
                self._settings = default.copy()
                self.save()
        else:
            self._settings = default.copy()
            self.save()
    
    def save(self) -> None:
        """将当前配置写入磁盘"""
        try:
            path = self._get_config_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[SettingsManager] Failed to save config: {e}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取指定配置项的值"""
        return self._settings.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """
        设置配置项，仅在值真正变化时保存并发射 setting_changed 信号。
        参数：key - 配置键名，value - 新值
        """
        old_value = self._settings.get(key)
        if key not in self._settings or old_value != value:
            self._settings[key] = value
            self.save()
            self.setting_changed.emit(key, value)  # 精准告知变更
    
    def get_all(self) -> dict:
        """返回完整配置的副本"""
        return self._settings.copy()
    
    def connect_changed(self, key: str, callback: Callable[[Any], None]) -> None:
        """
        便捷方法：为特定的 key 连接回调函数。
        回调函数只会收到该 key 的新值，无需自行判断。
        例如：sm.connect_changed("hud_timeout_sec", lambda v: print(v))
        """
        def handler(k: str, v: Any) -> None:
            if k == key:
                callback(v)
        self.setting_changed.connect(handler)
    
    @classmethod
    def instance(cls) -> "SettingsManager":
        """获取单例实例（等价于直接调用构造函数）"""
        return cls()