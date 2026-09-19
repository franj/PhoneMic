# tests/test_i18n.py
"""
国际化模块单元测试（适配回退链逻辑）
"""

import json
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
# 测试目标：phonemic.utils.system_lang
# ------------------------------------------------------------
def test_normalize_language_code():
    """格式规整：只改分隔符、剥编码/修饰符，不做语言变体归一化"""
    from phonemic.utils.system_lang import _normalize_language_code as norm

    cases = [
        ("zh_CN", "zh_CN"),
        ("zh-CN", "zh_CN"),             # 短横线转下划线
        ("zh_TW", "zh_TW"),
        ("zh-HK", "zh_HK"),
        ("en_US", "en_US"),
        ("en-US", "en_US"),
        ("en_GB", "en_GB"),
        ("fr_FR", "fr_FR"),
        ("zh", "zh"),                   # 只有语言、没有地区
        ("zh_Hans_CN", "zh_Hans_CN"),   # 多段（BCP-47 带 script）
        ("zh_CN.UTF-8", "zh_CN"),       # 剥编码
        ("zh_CN.GB18030", "zh_CN"),
        ("sr_RS@latin", "sr_RS"),       # 剥修饰符
        ("sr_RS.UTF-8@latin", "sr_RS"),
        ("C", None),                    # C/POSIX 只表示「没有语言偏好」
        ("C.UTF-8", None),
        ("POSIX", None),
        ("c", None),
        ("", None),
        ("   ", None),
        (None, None),
    ]
    for raw, expected in cases:
        assert norm(raw) == expected, f"{raw!r} -> {norm(raw)!r}, 期望 {expected!r}"


class _FakeKernel32:
    """替掉 kernel32：Windows 分支只用到 GetUserDefaultUILanguage 一个入口"""

    def __init__(self, langid, exc=None):
        self._langid = langid
        self._exc = exc

    def GetUserDefaultUILanguage(self):
        if self._exc is not None:
            raise self._exc
        return self._langid


def _fake_windll(monkeypatch, langid, exc=None):
    """把 ctypes.windll 换成假的，让 Windows 分支在任意平台上都能测"""
    import ctypes
    from types import SimpleNamespace

    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=_FakeKernel32(langid, exc)), raising=False
    )


@pytest.mark.parametrize(
    "langid,expected",
    [
        (0x0804, "zh_CN"),   # 简体中文（中国）
        (0x0404, "zh_TW"),   # 繁体中文（台湾）
        (0x0C04, "zh_HK"),   # 繁体中文（中国香港）
        (0x0409, "en_US"),   # 英语（美国）
        (0x0411, "ja_JP"),   # 日语（日本）
    ],
)
def test_detect_windows_ui_language(monkeypatch, langid, expected):
    """Windows 分支：取 UI 语言的 LANGID，翻成与语言包文件名一致的 POSIX 代码"""
    import phonemic.utils.system_lang as sl

    _fake_windll(monkeypatch, langid)
    assert sl._detect_on_windows() == expected


def test_detect_windows_unknown_langid(monkeypatch):
    """0x1000 = LOCALE_CUSTOM_UI_DEFAULT（UI 语言来自语言包）：表里没有，返回 None"""
    import phonemic.utils.system_lang as sl

    _fake_windll(monkeypatch, 0x1000)
    assert sl._detect_on_windows() is None


def test_detect_system_language_posix(monkeypatch):
    """POSIX 分支：只认第一个有值的语言环境变量，LC_ALL 优先级最高"""
    import phonemic.utils.system_lang as sl

    def _set_env(**kv):
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            monkeypatch.delenv(var, raising=False)
        for key, value in kv.items():
            monkeypatch.setenv(key, value)

    _set_env(LANG="zh_TW.UTF-8")
    assert sl._detect_on_posix() == "zh_TW"

    _set_env(LANG="en_US.UTF-8", LC_MESSAGES="zh_CN.UTF-8")
    assert sl._detect_on_posix() == "zh_CN"      # LC_MESSAGES 压过 LANG

    _set_env(LANG="en_US.UTF-8", LC_MESSAGES="en_US.UTF-8", LC_ALL="zh_HK.UTF-8")
    assert sl._detect_on_posix() == "zh_HK"      # LC_ALL 压过一切

    # LC_ALL=C 表示「不要本地化」：此时 LANG 不作数，视为检测不出来
    _set_env(LANG="zh_CN.UTF-8", LC_ALL="C.UTF-8")
    assert sl._detect_on_posix() is None

    _set_env()                                   # 三个都没设
    assert sl._detect_on_posix() is None


def test_detect_system_language_routes_by_platform(monkeypatch):
    """按平台分流：win32 走 Win32 API，其余（含 macOS）走 POSIX 环境变量"""
    import sys
    import phonemic.utils.system_lang as sl

    calls = []

    def _win():
        calls.append("windows")
        return "zh_HK"

    def _posix():
        calls.append("posix")
        return "zh_TW"

    monkeypatch.setattr(sl, "_detect_on_windows", _win)
    monkeypatch.setattr(sl, "_detect_on_posix", _posix)

    monkeypatch.setattr(sys, "platform", "win32")
    assert sl.detect_system_language() == "zh_HK"

    monkeypatch.setattr(sys, "platform", "linux")
    assert sl.detect_system_language() == "zh_TW"

    monkeypatch.setattr(sys, "platform", "darwin")
    assert sl.detect_system_language() == "zh_TW"

    assert calls == ["windows", "posix", "posix"]


def test_detect_system_language_falls_back(monkeypatch):
    """检测抛异常时回落 en_US，不把异常抛给调用方（不能因为检测失败起不来）"""
    import phonemic.utils.system_lang as sl

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(sl, "_detect_on_windows", _boom)
    monkeypatch.setattr(sl, "_detect_on_posix", _boom)
    assert sl.detect_system_language() == "en_US"


def test_detect_uses_no_deprecated_locale_api():
    """
    改造动机守卫：整条检测路径不得再调用 locale.getdefaultlocale()。

    用 record=True 而不是把警告升级成异常：老代码把调用包在 try/except 里，
    升级成异常会被它自己吞掉，只有「有没有发出过警告」才靠得住。
    （3.15 已收回该弃用，届时本条会自然失效，靠上面几条用例继续兜底。）
    """
    import warnings
    from phonemic.utils.system_lang import detect_system_language

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = detect_system_language()

    assert isinstance(result, str) and result
    deprecated = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not deprecated, f"检测路径用到了已弃用的 API: {[str(w.message) for w in deprecated]}"


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