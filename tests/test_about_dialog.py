"""
AboutDialog 测试。

只断言两类「用户能感知」的事，不碰间距/颜色/字号这类外观（要看得截图）：
- 版本号必须来自真实构建信息（写死一个版本号，用户反馈问题时给的就是错的信息）；
- 项目主页链接必须是「外部可打开」的（点不动等于没有这个链接）；
- 四个语言包都得有 about.* 的键（缺了界面上会直接显示 about.close 这种键名）。
"""

import json
import re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

import phonemic.gui.about_dialog as about_module
from phonemic.gui.about_dialog import AboutDialog, PROJECT_URL

LOCALES = ("zh_CN", "zh_HK", "zh_TW", "en_US")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = PROJECT_ROOT / "phonemic" / "resources" / "locales"


def _load_locale(code: str) -> dict:
    return json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class TestAboutLocaleParity:
    def test_all_locales_share_the_same_about_keys(self):
        key_sets = {code: set(_load_locale(code)["about"].keys()) for code in LOCALES}
        base = key_sets["en_US"]
        for code, keys in key_sets.items():
            assert keys == base, f"{code} 与 en_US 的 about 键不一致：{keys ^ base}"

    def test_no_empty_about_values(self):
        for code in LOCALES:
            for key, value in _load_locale(code)["about"].items():
                assert isinstance(value, str) and value.strip(), f"{code}.about.{key} 为空"

    def test_every_key_used_in_source_exists(self):
        """扫描源码里的 about.xxx 引用，逐个到语言包里核对"""
        used = set(re.findall(r'["\']about\.([A-Za-z0-9_]+)["\']',
                              (PROJECT_ROOT / "phonemic" / "gui" / "about_dialog.py").read_text(encoding="utf-8")))
        assert used, "没有扫描到任何 about.* 引用，正则可能已失效"

        for code in LOCALES:
            missing = used - set(_load_locale(code)["about"].keys())
            assert not missing, f"{code} 缺少翻译键：{sorted(missing)}"


class TestAboutDialog:
    def test_version_line_comes_from_build_info(self, qapp, monkeypatch):
        """版本号写死的话，用户报问题时会给出一个不存在的版本"""
        monkeypatch.setattr(about_module, "get_build_info", lambda: ("9.9.9", "deadbee", ""))

        dlg = AboutDialog()
        text = dlg.version_label.text()
        dlg.close()

        assert "9.9.9" in text
        assert "deadbee" in text

    def test_project_link_opens_externally(self, qapp):
        """内嵌显示（点不开）对这个链接来说等于不存在"""
        dlg = AboutDialog()
        label = dlg.link_label
        opens_externally = label.openExternalLinks()
        text = label.text()
        dlg.close()

        assert opens_externally is True
        assert PROJECT_URL in text
