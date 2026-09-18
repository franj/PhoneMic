"""
SystemTray 托盘菜单单元测试。

托盘图标在无头 / offscreen 环境下不可用（QSystemTrayIcon.isSystemTrayAvailable()
返回 False），此时 _create_tray 会提前返回、不构建菜单。因此这里直接调用
_create_tray_menu() 构建菜单，验证菜单结构与「上屏方式」勾选逻辑。
"""

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QWidget

from phonemic.gui.commands_dialog import CommandsDialog
from phonemic.gui.dashboard import Dashboard
from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.gui.tray import SystemTray
from phonemic.tunnel.mode import TunnelMode
from phonemic.utils.settings_manager import SettingsManager


@pytest.fixture
def tray(qtbot, tmp_path, monkeypatch):
    # 隔离配置：指向临时目录并重置单例，避免读写真实用户配置
    import phonemic.utils.settings_manager as sm_mod
    monkeypatch.setattr(sm_mod, "get_config_dir", lambda: tmp_path)
    SettingsManager._instance = None
    try:
        with patch("phonemic.gui.dashboard.get_mode", return_value=TunnelMode.LAN):
            d = Dashboard("192.168.1.100", 12000)
            d._is_force_quitting = True  # prevent closeEvent tray access
            qtbot.addWidget(d)
            t = SystemTray(d, icon_path="")
            yield t
    finally:
        SettingsManager._instance = None


@pytest.fixture
def isolated_commands_dir(tmp_path, monkeypatch):
    """把 CommandsManager 的配置目录指向 tmp_path 并复位单例。

    CommandsDialog 真实构造时会读 commands.json，不隔离就会写进真实用户配置。
    """
    from phonemic.utils import commands_manager as cm_mod
    from phonemic.utils import paths as paths_mod

    monkeypatch.setattr(paths_mod, "_get_local_app_data", lambda: tmp_path)
    cm_mod.CommandsManager._instance = None
    yield tmp_path
    cm_mod.CommandsManager._instance = None


def _item_labels(menu):
    """按顺序返回菜单项标签，分隔线记为 None。"""
    return [None if a.isSeparator() else a.text() for a in menu.actions()]


class TestTrayMenuStructure:
    def test_labels_and_separators(self, tray):
        i18n = tray.i18n
        menu = tray._create_tray_menu()
        assert _item_labels(menu) == [
            i18n.tr("tray.menu_show"),
            None,
            i18n.tr("tray.menu_settings"),
            i18n.tr("dashboard.menu_command"),
            None,
            i18n.tr("dashboard.input_clipboard"),
            i18n.tr("dashboard.input_type"),
            i18n.tr("dashboard.input_direct"),
            None,
            i18n.tr("tray.menu_about"),
            i18n.tr("tray.menu_quit"),
        ]


class TestTrayInputModeMenu:
    def test_default_is_paste(self, tray):
        tray._create_tray_menu()
        assert tray._act_input_paste.isChecked() is True
        assert tray._act_input_type.isChecked() is False
        assert tray._act_input_direct.isChecked() is False

    def test_set_type_persists_and_checks(self, tray):
        tray._create_tray_menu()
        tray._set_input_mode("type")
        assert tray.sm.get("text_input_mode") == "type"
        assert tray._act_input_type.isChecked() is True
        assert tray._act_input_paste.isChecked() is False

    def test_switch_back_to_paste(self, tray):
        tray._create_tray_menu()
        tray._set_input_mode("type")
        tray._set_input_mode("paste")
        assert tray.sm.get("text_input_mode") == "paste"
        assert tray._act_input_paste.isChecked() is True
        assert tray._act_input_type.isChecked() is False

    def test_triggering_action_switches(self, tray):
        """直接触发菜单项（等价于用户点击）也能切到模拟键盘。"""
        tray._create_tray_menu()
        tray._act_input_type.trigger()
        assert tray.sm.get("text_input_mode") == "type"
        assert tray._act_input_type.isChecked() is True

    def test_external_change_syncs_checks(self, tray):
        """主界面菜单 / 偏好设置改动配置后，托盘勾选应同步。"""
        tray._create_tray_menu()
        tray.sm.set("text_input_mode", "type")
        assert tray._act_input_type.isChecked() is True
        assert tray._act_input_paste.isChecked() is False

    def test_uses_exclusive_group_like_dashboard(self, tray):
        """与主界面「上屏方式」菜单外观统一：用互斥组（Qt 画成单选圆点）。"""
        tray._create_tray_menu()
        group = tray._act_input_paste.actionGroup()
        assert group is not None
        assert group.isExclusive() is True
        assert tray._act_input_type.actionGroup() is group
        assert len(group.actions()) == 3

    def test_reclicking_selected_item_keeps_checked(self, tray):
        """互斥组：重复点击已选中项不会被取消勾选。"""
        tray._create_tray_menu()
        tray._act_input_type.trigger()
        assert tray._act_input_type.isChecked() is True
        assert tray._act_input_paste.isChecked() is False

    def test_sync_without_menu_is_safe(self, tray):
        """托盘不可用、菜单未构建时同步不应抛异常。"""
        tray._sync_input_checks()  # 无 _act_input_paste，应安全返回


class TestTrayDirectInputMenu:
    def test_set_direct_persists_and_checks(self, tray):
        tray._create_tray_menu()
        tray._set_input_mode("direct")
        assert tray.sm.get("text_input_mode") == "direct"
        assert tray._act_input_direct.isChecked() is True
        assert tray._act_input_paste.isChecked() is False
        assert tray._act_input_type.isChecked() is False

    def test_triggering_action_switches(self, tray):
        tray._create_tray_menu()
        tray._act_input_direct.trigger()
        assert tray.sm.get("text_input_mode") == "direct"
        assert tray._act_input_direct.isChecked() is True

    def test_external_change_syncs_checks(self, tray):
        tray._create_tray_menu()
        tray.sm.set("text_input_mode", "direct")
        assert tray._act_input_direct.isChecked() is True
        assert tray._act_input_paste.isChecked() is False


class TestTrayDialogEntries:
    """托盘菜单里的两个对话框入口：parent 必须是 QWidget。

    回归：_open_commands_dialog 曾把 SystemTray（QObject）当 parent 传给
    QDialog —— QDialog.__init__(parent) 只接受 QWidget | None，于是**从托盘
    右键点「命令配置」必抛 TypeError**；从主窗口菜单进入则正常（那里 self 是
    QMainWindow），所以这条路径长期没人碰到。
    """

    @staticmethod
    def _spy_on(dialog_cls, mocker):
        """记录 parent 但走**真实** __init__，并让 exec 不进入事件循环。"""
        recorded = []
        real_init = dialog_cls.__init__

        def recording_init(self, parent=None, *args, **kwargs):
            recorded.append(parent)
            real_init(self, parent, *args, **kwargs)

        mocker.patch.object(dialog_cls, "__init__", recording_init)
        mocker.patch.object(dialog_cls, "exec", return_value=0)
        return recorded

    def test_open_commands_dialog_passes_dashboard(self, tray, isolated_commands_dir, mocker):
        recorded = self._spy_on(CommandsDialog, mocker)

        tray._open_commands_dialog()

        assert recorded == [tray.dashboard]

    def test_open_settings_dialog_passes_dashboard(self, tray, isolated_commands_dir, mocker):
        recorded = self._spy_on(SettingsDialog, mocker)

        tray._open_settings()

        assert recorded == [tray.dashboard]

    def test_dialog_parent_must_be_qwidget(self, tray, mocker):
        """用替身锁住类型契约：即便将来 Qt 放宽构造签名，也不该传 QObject。"""
        seen = []

        class _FakeDialog:
            def __init__(self, parent=None, *args, **kwargs):
                seen.append(parent)

            def exec(self, *args, **kwargs):
                return 0

        mocker.patch("phonemic.gui.tray.CommandsDialog", _FakeDialog)
        mocker.patch("phonemic.gui.tray.SettingsDialog", _FakeDialog)

        tray._open_commands_dialog()
        tray._open_settings()

        assert len(seen) == 2
        for parent in seen:
            assert isinstance(parent, QWidget)
