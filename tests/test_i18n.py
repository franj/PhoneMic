# tests/test_i18n.py
"""
国际化模块单元测试（适配回退链逻辑）
"""

import json
import locale
from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    """确保 QApplication 存在（整个模块共享）"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # 注意：不要 quit，因为其他测试可能需要


@pytest.fixture(autouse=True)
def isolate_singletons(tmp_path, monkeypatch):
    """
    单例隔离。

    本模块大量直接改写 I18n / SettingsManager 单例，并用 mock_open 伪造翻译文件。
    这两个单例是**全进程共享**的，测试结束后不重置的话，mock 出来的翻译
    （往往只有 dashboard.title 一个键）会泄漏给同一 pytest 会话中后续的测试文件，
    表现为「单独跑通过、按某种文件顺序跑就失败」，例如
    test_dashboard_mode.py::TestEncryptionToggle 查不到 dashboard.algo_xchacha20。

    配置目录同时指向 tmp_path，避免重置单例后重新读取/写入真实用户配置。
    """
    from phonemic.utils.i18n import I18n
    from phonemic.utils.settings_manager import SettingsManager

    monkeypatch.setattr(
        "phonemic.utils.settings_manager.get_config_dir", lambda: tmp_path
    )

    def _reset():
        I18n._instance = None
        SettingsManager._instance = None

    _reset()
    yield
    _reset()


# ------------------------------------------------------------
# 测试目标：phonemic.utils.system_lang.detect_system_language
# ------------------------------------------------------------
def test_detect_system_language():
    from phonemic.utils.system_lang import detect_system_language
    
    # 测试用例： (getdefaultlocale返回值, getlocale返回值, 期望结果)
    test_cases = [
        (("zh_CN", "UTF-8"), None, "zh_CN"),
        (("zh-CN", "UTF-8"), None, "zh_CN"),   # 短横线转下划线
        (("zh_TW", "UTF-8"), None, "zh_TW"),
        (("zh-HK", "UTF-8"), None, "zh_HK"),
        (("zh", "UTF-8"), None, "zh"),
        (("en_US", "UTF-8"), None, "en_US"),
        (("en-US", "UTF-8"), None, "en_US"),
        (("en_GB", "UTF-8"), None, "en_GB"),
        (("fr_FR", "UTF-8"), None, "fr_FR"),
        (None, ("zh_CN", "UTF-8"), "zh_CN"),
        (None, ("en_US", "UTF-8"), "en_US"),
        (None, ("de_DE", "UTF-8"), "de_DE"),
        (None, None, "en_US"),  # 完全无法检测，回退
    ]

    for default_tuple, lc_tuple, expected in test_cases:
        with patch("locale.getdefaultlocale", return_value=default_tuple):
            if lc_tuple is not None and hasattr(locale, 'LC_MESSAGES'):
                with patch("locale.getlocale", return_value=lc_tuple):
                    result = detect_system_language()
                    assert result == expected, f"default={default_tuple}, lc={lc_tuple} -> {result}, expected {expected}"

    # 特殊：Windows 返回 ("Chinese (Simplified)_China.936", "cp936")
    with patch("locale.getdefaultlocale", return_value=("Chinese (Simplified)_China.936", "cp936")):
        result = detect_system_language()
        assert result == "Chinese (Simplified)_China.936"

    # 异常情况
    with patch("locale.getdefaultlocale", side_effect=Exception("error")):
        with patch("locale.getlocale", return_value=("en_US", "UTF-8")):
            result = detect_system_language()
            assert result == "en_US"


# ------------------------------------------------------------
# 测试目标：I18n 的 JSON 加载和回退逻辑（精确->zh_CN->en_US）
# ------------------------------------------------------------
def test_load_translations(qapp):
    from phonemic.utils.i18n import I18n
    from phonemic.utils.settings_manager import SettingsManager

    # 模拟翻译内容
    mock_zh_cn = {"dashboard": {"title": "简体中文标题"}}
    mock_en_us = {"dashboard": {"title": "English Title"}}
    mock_zh_tw = {"dashboard": {"title": "繁體中文標題"}}  # 假设不存在此文件

    # 模拟 get_res_path 返回固定路径
    with patch("phonemic.utils.paths.get_res_path") as mock_get_path:
        def get_path_side_effect(rel_path):
            if "zh_CN.json" in rel_path:
                return "/fake/zh_CN.json"
            elif "en_US.json" in rel_path:
                return "/fake/en_US.json"
            elif "zh_TW.json" in rel_path:
                return "/fake/zh_TW.json"
            return "/fake/other"
        mock_get_path.side_effect = get_path_side_effect

        # 1. 精确匹配成功
        with patch("builtins.open", mock_open(read_data=json.dumps(mock_zh_cn))):
            I18n._instance = None
            sm = SettingsManager.instance()
            sm._settings["language"] = "zh_CN"
            i18n = I18n.instance()
            assert i18n.get_language() == "zh_CN"
            assert i18n.tr("dashboard.title") == "简体中文标题"

        # 2. 精确匹配失败，但中文变体回退到 zh_CN（假设 zh_TW 文件不存在，zh_CN 存在）
        def open_side_effect(file, *args, **kwargs):
            if "zh_TW.json" in file:
                raise FileNotFoundError
            elif "zh_CN.json" in file:
                return mock_open(read_data=json.dumps(mock_zh_cn)).return_value
            elif "en_US.json" in file:
                return mock_open(read_data=json.dumps(mock_en_us)).return_value
            else:
                raise FileNotFoundError

        with patch("builtins.open", side_effect=open_side_effect):
            I18n._instance = None
            sm._settings["language"] = "zh_TW"
            i18n2 = I18n.instance()
            # 应回退到 zh_CN
            assert i18n2.get_language() == "zh_CN"
            assert i18n2.tr("dashboard.title") == "简体中文标题"

        # 3. 中文变体回退也失败（zh_CN 也不存在），最终回退 en_US
        def open_all_fail(file, *args, **kwargs):
            if "en_US.json" in file:
                return mock_open(read_data=json.dumps(mock_en_us)).return_value
            raise FileNotFoundError

        with patch("builtins.open", side_effect=open_all_fail):
            I18n._instance = None
            sm._settings["language"] = "zh_TW"
            i18n3 = I18n.instance()
            assert i18n3.get_language() == "en_US"
            assert i18n3.tr("dashboard.title") == "English Title"

        # 4. en_US 也加载失败（极端情况），应使用空字典
        with patch("builtins.open", side_effect=FileNotFoundError):
            I18n._instance = None
            sm._settings["language"] = "en_US"
            i18n4 = I18n.instance()
            assert i18n4.get_language() == "en_US"  # 语言代码记录为 en_US
            assert i18n4.tr("dashboard.title") == "dashboard.title"  # key 未找到返回自身


# ------------------------------------------------------------
# 测试目标：tr 方法的参数格式化
# ------------------------------------------------------------
def test_tr_format(qapp):
    from phonemic.utils.i18n import I18n
    from phonemic.utils.settings_manager import SettingsManager

    mock_data = {
        "welcome": "Hello, {name}!",
        "count": "You have {num} messages.",
        "nested": {"deep": "Deep {value}"},
        "no_format": "Plain text"
    }

    with patch("phonemic.utils.paths.get_res_path", return_value="/fake/en_US.json"):
        with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
            I18n._instance = None
            sm = SettingsManager.instance()
            sm._settings["language"] = "en_US"
            i18n = I18n.instance()

            # 正常格式化
            assert i18n.tr("welcome", name="World") == "Hello, World!"
            assert i18n.tr("count", num=5) == "You have 5 messages."
            assert i18n.tr("nested.deep", value="test") == "Deep test"
            assert i18n.tr("no_format") == "Plain text"

            # 缺少参数：保留原占位符
            assert i18n.tr("welcome") == "Hello, {name}!"

            # 不存在的 key
            assert i18n.tr("missing.key") == "missing.key"

            # key 存在但值不是字符串
            mock_data["not_string"] = 123
            with patch("builtins.open", mock_open(read_data=json.dumps(mock_data))):
                I18n._instance = None
                i18n2 = I18n.instance()
                assert i18n2.tr("not_string") == "not_string"


# ------------------------------------------------------------
# 测试目标：首次运行无配置时，SettingsManager 默认语言为系统语言
# ------------------------------------------------------------
def test_settings_default_language(qapp):
    from phonemic.utils.settings_manager import SettingsManager
    from phonemic.utils.system_lang import detect_system_language

    # 模拟系统语言为 zh_TW
    with patch("phonemic.utils.system_lang.detect_system_language", return_value="zh_TW"):
        with patch("pathlib.Path.exists", return_value=False):  # 配置文件不存在
            with patch("builtins.open", mock_open()):
                SettingsManager._instance = None
                sm = SettingsManager.instance()
                # 应写入 zh_TW
                assert sm.get("language") == "zh_TW"

    # 模拟系统语言为 zh_CN
    with patch("phonemic.utils.system_lang.detect_system_language", return_value="zh_CN"):
        with patch("pathlib.Path.exists", return_value=False):
            with patch("builtins.open", mock_open()):
                SettingsManager._instance = None
                sm = SettingsManager.instance()
                assert sm.get("language") == "zh_CN"

    # 模拟系统语言为 en_US
    with patch("phonemic.utils.system_lang.detect_system_language", return_value="en_US"):
        with patch("pathlib.Path.exists", return_value=False):
            with patch("builtins.open", mock_open()):
                SettingsManager._instance = None
                sm = SettingsManager.instance()
                assert sm.get("language") == "en_US"

    # 已有配置文件，包含 language 字段，不应覆盖
    existing = {"language": "fr_FR", "other": "value"}
    with patch("pathlib.Path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data=json.dumps(existing))):
            SettingsManager._instance = None
            sm = SettingsManager.instance()
            assert sm.get("language") == "fr_FR"
            assert sm.get("other") == "value"

    # 配置文件存在但缺少 language 字段，应补充系统语言
    existing_no_lang = {"other": "value"}
    with patch("phonemic.utils.system_lang.detect_system_language", return_value="zh_CN"):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=json.dumps(existing_no_lang))):
                SettingsManager._instance = None
                sm = SettingsManager.instance()
                assert sm.get("language") == "zh_CN"
                assert sm.get("other") == "value"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])