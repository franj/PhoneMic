"""
使用帮助窗口测试。

只断言「用户能感知」的事：
- 导航条目必须来自文档里现成的标题（文档加了章节，程序不用改，用户就该看到新条目）；
- 点目录、点正文里的跳转链接，必须真的滚到对应章节（不然导航只是装饰）；
- 某一语言缺说明书时回退中文原版，而不是打不开；
- 从菜单重复点「使用帮助」只应有一个窗口。

字号、配色、间距这类外观不在这里守护——那种断言换个画法就会红，真画歪了它又拦不住，
要看得截图。
"""

from pathlib import Path
from urllib.parse import quote

import pytest
from markdown_it import MarkdownIt
from PySide6.QtCore import Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from phonemic.gui.help_window import (
    TOC_MAX_LEVEL, HelpWindow, build_html, guide_path, slugify,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 语言 -> 文件名的约定见 help_window.guide_path：中文原版不带后缀。
GUIDES = {
    "zh_CN": "USER_GUIDE.md",
    "en_US": "USER_GUIDE_en_US.md",
    "zh_TW": "USER_GUIDE_zh_TW.md",
    "zh_HK": "USER_GUIDE_zh_HK.md",
}


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    """真实文档 + 隔离配置（不读写用户目录里的设置）。"""
    import phonemic.utils.settings_manager as sm_mod
    from phonemic.utils.settings_manager import SettingsManager

    monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
    SettingsManager._instance = None
    try:
        win = HelpWindow()
        qtbot.addWidget(win)
        yield win
    finally:
        SettingsManager._instance = None


def _flatten(tree) -> list:
    """按显示顺序把目录树摊平成文本列表。"""

    def walk(item):
        yield item
        for index in range(item.childCount()):
            yield from walk(item.child(index))

    out = []
    for index in range(tree.topLevelItemCount()):
        out.extend(walk(tree.topLevelItem(index)))
    return [item.text(0) for item in out]


def _headings_of(path: Path):
    return build_html(path.read_text(encoding="utf-8"))[1]


def _internal_links(path: Path):
    """取出文档里所有 `#锚点` 链接（markdown-it 会把中文百分号编码，这里解回来）。"""
    from urllib.parse import unquote

    md = MarkdownIt("commonmark", {"html": False})
    md.enable(["table", "strikethrough"])
    tokens = md.parse(path.read_text(encoding="utf-8"))

    stack = list(tokens)
    frags = []
    while stack:
        token = stack.pop()
        if token.type == "link_open":
            href = token.attrs.get("href", "")
            if href.startswith("#"):
                frags.append(unquote(href[1:]))
        if token.children:
            stack.extend(token.children)
    return frags


class TestGuideDocuments:
    """四份说明书都得能喂给同一个渲染器，否则切语言就会缺章。"""

    @pytest.mark.parametrize("lang", list(GUIDES))
    def test_guide_file_exists(self, lang):
        assert (PROJECT_ROOT / GUIDES[lang]).is_file()

    def test_all_guides_share_the_same_structure(self):
        shapes = {}
        for lang, name in GUIDES.items():
            headings = _headings_of(PROJECT_ROOT / name)
            shapes[lang] = [h.level for h in headings]
        base = shapes["zh_CN"]
        for lang, levels in shapes.items():
            assert levels == base, f"{lang} 与中文原版的标题层级不一致"

    def test_every_guide_has_a_usable_outline(self):
        """目录为空等于没有导航：每份文档都得有足够的 h1~h3。"""
        for lang, name in GUIDES.items():
            headings = _headings_of(PROJECT_ROOT / name)
            toc = [h for h in headings if h.level <= TOC_MAX_LEVEL]
            assert len(toc) >= 30, f"{lang} 只有 {len(toc)} 个可导航标题"

    @pytest.mark.parametrize("lang", list(GUIDES))
    def test_internal_links_point_at_real_headings(self, lang):
        """文档里写死的 `#锚点` 必须命中实际标题，否则点了没反应。"""
        path = PROJECT_ROOT / GUIDES[lang]
        slugs = {h.slug for h in _headings_of(path)}
        broken = [frag for frag in _internal_links(path) if frag not in slugs]
        assert not broken, f"{lang} 的跳转链接指向了不存在的标题：{broken}"


class TestGuidePathSelection:
    def test_missing_translation_falls_back_to_chinese(self, monkeypatch, tmp_path):
        (tmp_path / "USER_GUIDE.md").write_text("# 中文", encoding="utf-8")
        import phonemic.gui.help_window as module
        monkeypatch.setattr(module, "get_app_root", lambda: tmp_path)

        assert guide_path("en_US") == tmp_path / "USER_GUIDE.md"
        assert guide_path("zh_CN") == tmp_path / "USER_GUIDE.md"

    def test_translation_is_preferred_when_present(self, monkeypatch, tmp_path):
        (tmp_path / "USER_GUIDE.md").write_text("# 中文", encoding="utf-8")
        (tmp_path / "USER_GUIDE_en_US.md").write_text("# English", encoding="utf-8")
        import phonemic.gui.help_window as module
        monkeypatch.setattr(module, "get_app_root", lambda: tmp_path)

        assert guide_path("en_US") == tmp_path / "USER_GUIDE_en_US.md"


class TestHelpWindow:
    def test_outline_mirrors_document_headings(self, window):
        """目录必须等于文档里 h1~h3 的标题序列，一条不多一条不少。"""
        assert window.show_help() is True
        headings = _headings_of(guide_path(window._loaded_lang))
        expected = [h.text for h in headings if h.level <= TOC_MAX_LEVEL]
        assert _flatten(window.tree) == expected

    def test_selecting_outline_entry_scrolls_into_view(self, window):
        """点目录里任意一节，那一节的标题必须出现在可见区域里。"""
        assert window.show_help() is True
        root = window.tree.topLevelItem(0)

        for index in (0, root.childCount() // 2, root.childCount() - 1):
            item = root.child(index)
            window.tree.setCurrentItem(item)
            block = window._block_by_slug[item.data(0, 0x0100)]
            top = window.view.document().documentLayout().blockBoundingRect(block).top()

            bar = window.view.verticalScrollBar()
            assert bar.value() <= top <= bar.value() + window.view.viewport().height(), (
                f"「{item.text(0)}」没有滚动到可见区域"
            )

    def test_in_document_link_jumps_to_its_section(self, window):
        """正文里的「详见 2.7」这类链接，点下去要落到对应章节并同步目录。"""
        assert window.show_help() is True
        headings = _headings_of(guide_path(window._loaded_lang))
        target = next(h for h in headings if h.level == 2 and h.slug.startswith("27-"))

        window._on_anchor_clicked(QUrl("#" + quote(target.slug)))

        assert window.tree.currentItem().data(0, 0x0100) == target.slug
        assert window.view.verticalScrollBar().value() > 0

    def test_scrolling_highlights_the_section_under_the_viewport(self, window):
        assert window.show_help() is True
        window.view.verticalScrollBar().setValue(window.view.verticalScrollBar().maximum())

        # 文末应当高亮最后一节，而不是倒数第几节
        assert window.tree.currentItem() is not None
        last_slug = next(reversed(window._item_by_slug))
        assert window.tree.currentItem().data(0, 0x0100) == last_slug

    def test_missing_guide_reports_failure_instead_of_raising(
        self, window, monkeypatch, tmp_path
    ):
        import phonemic.gui.help_window as module
        monkeypatch.setattr(module, "get_app_root", lambda: tmp_path)
        assert window.show_help() is False


class TestSearch:
    """搜索要同时管好两件事：左栏目录的显隐，和正文里的定位与计数。"""

    @staticmethod
    def _visible(window) -> list:
        out = []

        def walk(item):
            if not item.isHidden():
                out.append(item)
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(window.tree.topLevelItemCount()):
            walk(window.tree.topLevelItem(index))
        return out

    def test_typing_filters_outline_to_matching_sections(self, window):
        assert window.show_help() is True
        total = len(self._visible(window))

        window.search_edit.setText("加密")
        visible = self._visible(window)
        texts = [item.text(0) for item in visible]

        assert any("加密" in text for text in texts)
        assert len(visible) < total
        # 可见项要么自己命中，要么是命中项的上层目录（否则用户无法从树里点到它）
        for item in visible:
            if "加密" in item.text(0):
                continue
            assert self._has_matching_descendant(item, "加密"), item.text(0)

    @staticmethod
    def _has_matching_descendant(item, query: str) -> bool:
        for index in range(item.childCount()):
            child = item.child(index)
            if query in child.text(0) or TestSearch._has_matching_descendant(child, query):
                return True
        return False

    def test_clearing_the_box_restores_the_full_outline(self, window):
        assert window.show_help() is True
        total = len(self._visible(window))
        window.search_edit.setText("加密")
        window.search_edit.clear()
        assert len(self._visible(window)) == total

    def test_unknown_word_does_not_empty_the_outline(self, window):
        """正文里可能命中、标题里没有：这时不能把目录清空，否则用户既以为没这词、
        也失去了继续浏览的入口。"""
        assert window.show_help() is True
        total = len(self._visible(window))
        window.search_edit.setText("这个词标题里肯定没有")
        assert len(self._visible(window)) == total

    def test_enter_jumps_to_the_first_hit_and_counts_matches(self, window):
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_edit.returnPressed.emit()

        matches = window._search_matches
        assert matches, "文档里应当有「手机」"
        assert window.search_count.text() == f"1/{len(matches)}"
        assert window.view.textCursor().selectedText() == "手机"
        top = window.view.cursorRect().top()
        assert 0 <= top <= window.view.viewport().height(), "命中处必须在视口里"

    def test_enter_again_walks_through_hits_and_wraps_around(self, window):
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_edit.returnPressed.emit()
        total = len(window._search_matches)
        assert total > 2

        window.search_edit.returnPressed.emit()
        assert window.search_count.text() == f"2/{total}"

        for _ in range(total - 1):
            window.search_edit.returnPressed.emit()
        assert window.search_count.text() == f"1/{total}"

    def test_changing_the_word_resets_the_counter(self, window):
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_edit.returnPressed.emit()
        window.search_edit.returnPressed.emit()
        assert window.search_count.text().startswith("2/")

        window.search_edit.setText("加密")
        assert window.search_count.text() == ""
        window.search_edit.returnPressed.emit()
        assert window.search_count.text().startswith("1/")

    def test_word_with_no_hit_reports_zero(self, window):
        assert window.show_help() is True
        window.search_edit.setText("zzzz-not-in-guide")
        window.search_edit.returnPressed.emit()
        assert window.search_count.text() == "0/0"
        assert window.view.extraSelections() == []

    def test_arrow_buttons_stay_disabled_until_something_is_typed(self, window):
        """空搜索框旁边两个箭头没有意义，别让它们看起来可点。"""
        assert window.show_help() is True
        assert window.search_prev.isEnabled() is False
        assert window.search_next.isEnabled() is False

        window.search_edit.setText("手机")
        assert window.search_prev.isEnabled() is True
        assert window.search_next.isEnabled() is True

        window.search_edit.clear()
        assert window.search_prev.isEnabled() is False

    def test_next_button_walks_forward_like_enter(self, window):
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_next.click()
        total = len(window._search_matches)
        assert window.search_count.text() == f"1/{total}"

        window.search_next.click()
        assert window.search_count.text() == f"2/{total}"

    def test_prev_button_starts_at_the_last_hit(self, window):
        """新词第一下按「上一处」，按浏览器的习惯应该落到最后一处。"""
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_prev.click()
        total = len(window._search_matches)
        assert window.search_count.text() == f"{total}/{total}"

        window.search_prev.click()
        assert window.search_count.text() == f"{total - 1}/{total}"

    def test_shift_enter_walks_backward(self, window):
        """键盘也要能回头：Shift+Enter 与 ↑ 等效。"""
        assert window.show_help() is True
        window.search_edit.setText("手机")
        window.search_next.click()
        window.search_next.click()
        assert window.search_count.text().startswith("2/")

        window.search_edit.setFocus()
        QTest.keyClick(window.search_edit, Qt.Key_Return, Qt.ShiftModifier)
        assert window.search_count.text().startswith("1/")

    def test_arrows_report_zero_hits_instead_of_doing_nothing(self, window):
        assert window.show_help() is True
        window.search_edit.setText("zzzz-not-in-guide")
        window.search_next.click()
        assert window.search_count.text() == "0/0"


class TestDashboardIntegration:
    def test_help_menu_reuses_a_single_modeless_window(
        self, qtbot, tmp_path, monkeypatch
    ):
        """连点两次「使用帮助」只应有一个窗口，且不能是模态（否则挡住要照做的界面）。"""
        import phonemic.utils.settings_manager as sm_mod
        from phonemic.utils.settings_manager import SettingsManager
        from phonemic.gui.dashboard import Dashboard
        from phonemic.tunnel.mode import TunnelMode
        from unittest.mock import patch

        monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
        SettingsManager._instance = None
        try:
            with patch("phonemic.gui.dashboard.get_mode", return_value=TunnelMode.LAN):
                dashboard = Dashboard("192.168.1.100", 12000)
                dashboard._is_force_quitting = True
                qtbot.addWidget(dashboard)

                dashboard.open_user_guide()
                first = dashboard._help_window
                dashboard.open_user_guide()

                assert dashboard._help_window is first
                assert first.isModal() is False
        finally:
            SettingsManager._instance = None

    def test_help_window_is_not_owned_by_the_dashboard(
        self, qtbot, tmp_path, monkeypatch
    ):
        """说明书不能挂在主界面下面当 owned window，否则永远压着它。

        Windows 上「有父窗口的对话框」属于父窗口的客户区扩展：切回主界面，说明书还
        盖在上面，用户想照步骤操作得先把它挪开。这里守住「没有父窗口」这条线。
        """
        import phonemic.utils.settings_manager as sm_mod
        from phonemic.utils.settings_manager import SettingsManager
        from phonemic.gui.dashboard import Dashboard
        from phonemic.tunnel.mode import TunnelMode
        from unittest.mock import patch

        monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
        SettingsManager._instance = None
        try:
            with patch("phonemic.gui.dashboard.get_mode", return_value=TunnelMode.LAN):
                dashboard = Dashboard("192.168.1.100", 12000)
                dashboard._is_force_quitting = True
                qtbot.addWidget(dashboard)

                dashboard.open_user_guide()
                assert dashboard._help_window.parent() is None

                # 主界面收进托盘，说明书不能孤零零留在桌面上
                dashboard.show()
                assert dashboard._help_window.isVisible()
                dashboard.hide()
                assert dashboard._help_window.isHidden()
        finally:
            SettingsManager._instance = None
