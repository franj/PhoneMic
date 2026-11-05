"""
HudWindow 预热（prewarm）单元测试。

首次 show() 的一次性初始化实测要 300~600ms，因此启动时会提前 show 一遍（用
WA_DontShowOnScreen 保证不上屏）。这里锁定四件事：
1. 预热必须真的建出原生窗口资源（否则等于空转，测试也得能发现）；
2. 预热跑完要回到隐藏状态、属性清理干净；
3. 结束预热必须先 hide 再撤 WA_DontShowOnScreen（顺序反了窗口永远上不了屏，
   详见 HudWindow._end_prewarm 的注释）；
4. 预热期间真实内容到达、以及已真实显示过的窗口，都不能把状态搞乱。

注意：offscreen 平台下 QWindow.isVisible()/isExposed() 无法反映真实上屏，第 3 点
在 CI 里只能靠"调用顺序"这条不变量来守；真机验证需手测 windowHandle().isExposed()。
"""

import pytest
from PySide6.QtCore import Qt

from phonemic.gui.hud import HudWindow
from phonemic.utils.settings_manager import SettingsManager


@pytest.fixture
def hud(qtbot, tmp_path, monkeypatch):
    # 隔离配置：指向临时目录并重置单例，避免读写真实用户配置
    import phonemic.utils.settings_manager as sm_mod
    monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
    SettingsManager._instance = None
    try:
        w = HudWindow()
        qtbot.addWidget(w)
        yield w
    finally:
        SettingsManager._instance = None


class TestPrewarm:
    def test_actually_creates_native_resources(self, hud, qtbot):
        # 预热前：什么都没建
        assert hud.testAttribute(Qt.WA_WState_Created) is False
        assert hud.windowHandle() is None
        assert hud.backingStore() is None

        hud.prewarm()
        qtbot.wait(50)

        # 预热后：原生窗口 / backing store 都在了，但没上屏
        assert hud.testAttribute(Qt.WA_WState_Created) is True
        assert hud.windowHandle() is not None
        assert hud.backingStore() is not None
        assert not hud.isVisible()

    def test_finishes_hidden_with_attribute_cleared(self, hud, qtbot):
        hud.prewarm()
        assert hud._prewarming is True
        assert hud.testAttribute(Qt.WA_DontShowOnScreen) is True

        qtbot.wait(50)  # 放行排队的 singleShot(0)

        assert hud._prewarmed is True
        assert hud._prewarming is False
        assert not hud.isVisible()
        assert hud.testAttribute(Qt.WA_DontShowOnScreen) is False

    def test_hide_happens_before_clearing_attribute(self, hud, mocker):
        """顺序不变量：先 hide 再撤属性。

        只清属性不 hide，Qt 仍认为窗口"已显示"，后续 show() 是空操作——widget
        isVisible() 为 True 但原生窗口不 map。offscreen 平台观察不到这一点，所以
        只能把调用顺序本身钉死。
        """
        hud.prewarm()  # 此时 _prewarming=True，窗口处于"已显示"状态

        order = []
        real_hide = hud.hide
        real_set_attribute = hud.setAttribute

        def spy_hide():
            order.append("hide")
            real_hide()

        def spy_set_attribute(attr, on=True):
            if attr == Qt.WA_DontShowOnScreen:
                order.append("set")
            real_set_attribute(attr, on)

        mocker.patch.object(hud, "hide", side_effect=spy_hide)
        mocker.patch.object(hud, "setAttribute", side_effect=spy_set_attribute)

        hud._end_prewarm()

        assert order == ["hide", "set"]

    def test_is_idempotent(self, hud, qtbot):
        hud.prewarm()
        qtbot.wait(50)
        hud.prewarm()

        assert hud._prewarmed is True
        assert not hud.isVisible()

    def test_preview_arriving_mid_prewarm_stays_visible(self, hud, qtbot):
        hud.prewarm()
        # 预热那一步的 singleShot 还没跑，此时真实内容到了
        hud.on_preview_text("你好")

        assert hud.label.text() == "你好"
        assert hud.isVisible()
        assert hud.testAttribute(Qt.WA_DontShowOnScreen) is False

        qtbot.wait(50)  # 挂起的 _end_prewarm 应当早退，不能隐藏窗口
        assert hud.isVisible()
        assert hud.label.text() == "你好"

    def test_skipped_when_window_already_shown(self, hud):
        hud.on_preview_text("已经显示过")
        assert hud.isVisible()

        hud.prewarm()

        assert hud._prewarming is False
        assert hud.testAttribute(Qt.WA_DontShowOnScreen) is False
        assert hud.isVisible()
        assert hud.label.text() == "已经显示过"

    def test_preview_after_prewarm_shows_content(self, hud, qtbot):
        hud.prewarm()
        qtbot.wait(50)

        hud.on_preview_text("预热之后的第一次")

        assert hud.isVisible()
        assert hud.label.text() == "预热之后的第一次"

    def test_empty_preview_still_hides(self, hud, qtbot):
        hud.prewarm()
        qtbot.wait(50)
        hud.on_preview_text("先显示")
        assert hud.isVisible()

        hud.on_preview_text("")

        assert not hud.isVisible()
