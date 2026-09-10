"""
国际化（i18n）核心模块，提供翻译资源加载和动态切换功能。
支持回退链：精确匹配 -> 中文变体回退到 zh_CN -> 最终 en_US。
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

from PySide6.QtCore import QObject, Signal

from phonemic.utils.paths import get_res_path
from phonemic.utils.settings_manager import SettingsManager

logger = logging.getLogger(__name__)


class I18n(QObject):
    """
    国际化单例类，负责加载 JSON 翻译文件，提供翻译方法，
    并监听 SettingsManager 的语言变更信号，自动重载并发射信号。
    """

    language_changed = Signal(str)  # 参数为新语言代码

    _instance: Optional["I18n"] = None

    def __new__(cls) -> "I18n":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化，仅执行一次"""
        if hasattr(self, "_initialized"):
            return
        super().__init__()
        self._initialized = True

        self._strings: Dict[str, Any] = {}
        self._current_lang: str = "en_US"

        # 监听配置变更
        self.sm = SettingsManager.instance()
        self.sm.setting_changed.connect(self._on_setting_changed)

        # 加载初始语言
        initial_lang = self.sm.get("language", "en_US")
        self._load_language(initial_lang)

    def _try_load(self, lang_code: str) -> bool:
        """
        尝试加载指定语言的 JSON 文件。
        成功则更新 self._strings 和 self._current_lang，返回 True；
        失败返回 False。
        """
        # 文件名使用下划线格式，例如 zh_CN.json
        filename = lang_code + ".json"
        file_path = get_res_path(f"locales/{filename}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                self._strings = json.load(f)
            self._current_lang = lang_code
            logger.info(f"Loaded language: {lang_code} from {file_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to load language {lang_code}: {e}")
            return False

    def _load_language(self, lang_code: str) -> None:
        """
        加载指定语言的翻译，支持回退链：
        1. 精确匹配 lang_code
        2. 如果是中文变体（zh_XX），尝试加载 zh_CN
        3. 最终回退到 en_US
        """
        # 1. 精确匹配
        if self._try_load(lang_code):
            return

        # 2. 中文变体回退：如果是 zh_XX 且不是 zh_CN，尝试 zh_CN
        if lang_code.startswith("zh_") and lang_code != "zh_CN":
            if self._try_load("zh_CN"):
                return

        # 3. 最终回退到 en_US
        if lang_code != "en_US":
            logger.warning(f"Fallback to en_US for language: {lang_code}")
            self._try_load("en_US")
        else:
            # en_US 也加载失败的情况（极罕见），保证 _strings 不为 None
            if not self._strings:
                logger.critical("Failed to load en_US translation, using empty dict")
                self._strings = {}
                self._current_lang = "en_US"

    def _on_setting_changed(self, key: str, value: Any) -> None:
        """响应 SettingsManager 的配置变更"""
        if key == "language":
            new_lang = str(value)
            if new_lang != self._current_lang:
                self._load_language(new_lang)
                self.language_changed.emit(new_lang)

    def tr(self, key: str, **kwargs) -> str:
        """
        翻译指定 key 的文本，支持参数格式化。

        Args:
            key: 点分命名空间的键，如 "dashboard.title"
            **kwargs: 用于格式化字符串的参数

        Returns:
            翻译后的字符串，若 key 不存在则返回 key 本身。
        """
        # 逐级查找
        parts = key.split(".")
        current = self._strings
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                logger.warning(f"Translation key not found: {key}")
                return key

        if not isinstance(current, str):
            logger.warning(f"Translation value for {key} is not a string: {current}")
            return key

        if len(kwargs) == 0: return current
        # 格式化
        try:
            return current.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Missing format argument {e} for key: {key}")
            return current
        except Exception as e:
            logger.warning(f"Failed to format translation for {key}: {e}")
            return current

    def get_language(self) -> str:
        """返回当前语言代码（如 'zh_CN', 'en_US'）"""
        return self._current_lang

    def reload(self) -> None:
        """从磁盘重读当前语言文件。

        开发期修改 locale JSON 后无需重启 PC 端：/api/lang.json 每次请求前调用，
        手机端刷新即可拿到最新翻译。加载失败时保留内存中的现有翻译（_try_load
        只在成功时才覆盖 _strings），不会因坏文件导致语言包丢失。
        """
        self._try_load(self._current_lang)

    def get_section(self, key: str) -> Dict[str, Any]:
        """
        获取指定路径下的整个 JSON 部分（字典），用于批量导出（如手机端翻译）。

        Args:
            key: 点分命名空间的键，如 "mobile"

        Returns:
            该路径下的字典，若路径不存在或不是字典则返回空字典。
        """
        parts = key.split(".")
        current = self._strings
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                logger.warning(f"Section key not found: {key}")
                return {}
        if isinstance(current, dict):
            return current
        else:
            logger.warning(f"Section value for {key} is not a dict: {type(current)}")
            return {}

    @staticmethod
    def instance() -> "I18n":
        """获取单例实例"""
        return I18n()

def get_available_languages() -> List[Tuple[str, str]]:
    """
    扫描 locales 目录，返回所有可用语言的列表。

    每个元素为 (language_code, display_name) 元组，
    其中 display_name 格式为 "{name} ({code})"，
    例如 "简体中文 (zh_CN)"。

    如果某个 JSON 文件缺少 "name" 字段，则跳过该语言并记录警告。
    如果目录不存在或没有有效文件，返回空列表。

    Returns:
        语言列表，按 language_code 排序。
    """
    locales_dir = Path(get_res_path("locales"))
    if not locales_dir.exists() or not locales_dir.is_dir():
        logger.warning(f"Locales directory not found: {locales_dir}")
        return []

    languages = []
    for file_path in locales_dir.glob("*.json"):
        code = file_path.stem  # 例如 "zh_CN"
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load language file {file_path.name}: {e}")
            continue

        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            logger.warning(f"Language file {file_path.name} missing valid 'name' field, skipping")
            continue

        display_name = f"{name} ({code})"
        languages.append((code, display_name))

    # 按语言代码排序，确保顺序稳定（例如 en_US 在前，zh_CN 在后）
    languages.sort(key=lambda x: x[0])
    return languages