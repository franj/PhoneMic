# settings_manager.py (完整文件)

"""
配置管理模块 - 单例模式，支持持久化存储和精准变更通知
"""

import json
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, QStandardPaths
from phonemic.utils import system_lang
from phonemic.utils.paths import get_config_dir

# ---------- 手机端配置：取值范围与规范化 ----------
# 这几个键由手机端设置页写入（上行 config 帧），因此**校验必须在这里**：
# settings.json 是唯一真相源，脏值一旦落盘，之后每次握手都会被下发出去。
MOBILE_MAX_RECORDS_MIN = 5
MOBILE_MAX_RECORDS_MAX = 50
QUICK_ACTION_MAX = 6          # 输入框下方一行最多放 6 个，多了按钮就挤成一条线
QUICK_LABEL_MAX_CHARS = 8     # 按钮文字上限（6 个按钮时每个约 50px，再多就显示不全）

# 手机端可写回的配置键 —— 白名单，不在表里的一律丢弃（见 api.py 的 config 上行分支）
MOBILE_UPLINK_KEYS = (
    "mobile_max_records",
    "mobile_send_mode",
    "mobile_show_clear_btn",
    "mobile_show_quick_bar",
    "mobile_quick_actions",
)


def normalize_quick_actions(value: Any) -> Optional[list]:
    """校验并规范化快捷按钮列表，非法返回 None。

    `label` 可省略：省略即「用内置池里的默认名」，由手机端按 `keys` 反查并翻译。
    这样 settings.json 里不存任何语言相关的字面文案，换语言包后默认按钮名跟着变；
    用户改过名的才落下 `label`，此后不再随语言包变化（改过的名字是他的东西）。

    返回新列表（不原地改入参），保证调用方拿到的就是能直接落盘的值。
    """
    if not isinstance(value, list) or len(value) > QUICK_ACTION_MAX:
        return None
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            return None
        keys = item.get("keys")
        if not isinstance(keys, str):
            return None
        keys = keys.strip()
        # 键名格式只做粗校验（pyautogui 键名 + `+` 组合 + `,` 分隔），
        # 真正的执行校验在 gui/keyboard.py:validate_key_sequence，这里只挡垃圾
        if not keys or len(keys) > 40:
            return None
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789+ ," for c in keys.lower()):
            return None
        entry = {"keys": keys}
        label = item.get("label")
        if label is not None:
            if not isinstance(label, str):
                return None
            label = label.strip()
            if not label or len(label) > QUICK_LABEL_MAX_CHARS:
                return None
            entry["label"] = label
        normalized.append(entry)
    return normalized


def normalize_uplink_config(key: str, value: Any) -> Optional[Any]:
    """手机端上行 config 帧的规范化入口。非法返回 None（调用方据此丢弃并回真实值）。

    只认真布尔、只认范围内的整数 —— 手机端不校验也能走到这里，
    脏数据绝不能穿过这一层进入 settings.json。
    """
    if key == "mobile_max_records":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return max(MOBILE_MAX_RECORDS_MIN, min(MOBILE_MAX_RECORDS_MAX, value))
    if key == "mobile_send_mode":
        # "" 只作为「从未配置过」的初始态保留，手机端写回必须落到具体模式
        return value if value in ("auto", "manual") else None
    if key in ("mobile_show_clear_btn", "mobile_show_quick_bar"):
        return value if isinstance(value, bool) else None
    if key == "mobile_quick_actions":
        return normalize_quick_actions(value)
    return None


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
            "mobile_send_mode": "",               # 手机端发送方式: ""(默认，等同自动), "auto"(自动发送), "manual"(手动发送)
            # ---- 手机端界面偏好：由手机端设置页写入，PC 不提供控件（见 api.py config 上行分支）----
            "mobile_show_clear_btn": True,        # 输入框左侧的「清空草稿」（垃圾桶）按钮是否显示
            "mobile_show_quick_bar": False,       # 输入框下方的快捷按钮行是否显示（默认关闭，保持与旧版界面一致）
            "mobile_quick_actions": [             # 快捷按钮行内容，顺序即显示顺序，最多 6 个
                {"keys": "ctrl+a, delete"},       # 不写 label = 用内置默认名（清空）
                {"keys": "ctrl+z"},               # 撤销
                {"keys": "shift+enter"},          # 换行
                {"keys": "enter"},                # 确定
            ],
            "language": system_lang.detect_system_language(),
            "close_action": None,                # None, "quit", "tray"
            "network_selection_mode": "ask",      # "auto", "last", "ask"
            "last_network_mac": None,             # 上次使用的网卡MAC
            "auto_start_silent": False,           # 开机自启时是否静默（不显示主窗口）
            "auth_method": "tofu",                # 认证方式: "tofu"(手动审批), "url_fragment"(扫码认证)
            "text_input_mode": "paste",           # 上屏方式: "paste"(剪贴板+Ctrl+V), "type"(模拟键盘逐字符输入), "direct"(模拟键盘+识别过程中直接输入)
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
                # 校验 network_selection_mode
                if "network_selection_mode" in loaded:
                    if loaded["network_selection_mode"] not in ("auto", "last", "ask"):
                        loaded["network_selection_mode"] = default["network_selection_mode"]
                # 校验 auto_start_silent 必须为 bool
                if "auto_start_silent" in loaded:
                    if not isinstance(loaded["auto_start_silent"], bool):
                        loaded["auto_start_silent"] = default["auto_start_silent"]
                # 迁移 e2ee_algorithm → auth_method（一次性迁移，迁移后删除旧键）
                if "e2ee_algorithm" in loaded:
                    old_algo = loaded.pop("e2ee_algorithm")
                    if old_algo in ("xsalsa20", "xchacha20", "auto"):
                        loaded["auth_method"] = "url_fragment"
                    else:
                        loaded["auth_method"] = "tofu"
                # 校验 auth_method
                if "auth_method" in loaded:
                    if loaded["auth_method"] not in ("tofu", "url_fragment"):
                        loaded["auth_method"] = default["auth_method"]
                # 校验 text_input_mode
                if loaded.get("text_input_mode") not in ("paste", "type", "direct"):
                    loaded["text_input_mode"] = default["text_input_mode"]
                # 校验 mobile_send_mode（"" 表示不干预，手机端用自己的默认值）
                if loaded.get("mobile_send_mode") not in ("", "auto", "manual"):
                    loaded["mobile_send_mode"] = default["mobile_send_mode"]
                # 校验手机端界面偏好：布尔键必须是真布尔，按钮列表必须过规范化
                # （手机端可写回这几个键，脏值落盘后会长期影响每一台连上来的设备）
                for bool_key in ("mobile_show_clear_btn", "mobile_show_quick_bar"):
                    if not isinstance(loaded.get(bool_key), bool):
                        loaded[bool_key] = default[bool_key]
                quick = normalize_quick_actions(loaded.get("mobile_quick_actions"))
                loaded["mobile_quick_actions"] = (
                    quick if quick is not None else default["mobile_quick_actions"]
                )
                # 聊天记录上限：夹到滑杆范围内，而不是重置成默认值（用户的范围感不要丢）
                if isinstance(loaded.get("mobile_max_records"), int) and not isinstance(
                    loaded.get("mobile_max_records"), bool
                ):
                    loaded["mobile_max_records"] = max(
                        MOBILE_MAX_RECORDS_MIN,
                        min(MOBILE_MAX_RECORDS_MAX, loaded["mobile_max_records"]),
                    )
                else:
                    loaded["mobile_max_records"] = default["mobile_max_records"]
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