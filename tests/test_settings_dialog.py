"""
SettingsDialog 单元测试
运行: pytest tests/test_settings_dialog.py -v
"""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt, QStandardPaths
from PySide6.QtWidgets import QApplication

from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.utils.i18n import I18n
from phonemic.utils.settings_manager import SettingsManager
from phonemic.utils import startup  # 新增导入

@pytest.fixture(autouse=True)
def suppress_message_box(monkeypatch):
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.critical", lambda *args, **kwargs: None)

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def mock_config_path(tmp_path):
    """临时替换配置目录"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("phonemic.utils.paths._get_local_app_data", lambda: tmp_path)
        yield tmp_path


@pytest.fixture
def reset_singleton():
    SettingsManager._instance = None
    yield
    SettingsManager._instance = None


@pytest.fixture
def settings_manager(mock_config_path, reset_singleton):
    sm = SettingsManager.instance()
    sm.set("hud_timeout_sec", 5)
    sm.set("hud_font_size", 14)
    sm.set("hud_escape_enabled", True)
    sm.set("mobile_max_records", 10)
    sm.set("language", "zh_CN")
    return sm

@pytest.fixture
def mock_languages(monkeypatch):
    monkeypatch.setattr(
        "phonemic.utils.i18n.get_available_languages",
        lambda: [("en_US", "English (en_US)"), ("zh_CN", "简体中文 (zh_CN)")]
    )

def test_language_combo_populated(qtbot, mock_languages, settings_manager):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.lang_combo.count() == 2
    assert dialog.lang_combo.itemData(0) == "en_US"
    assert dialog.lang_combo.itemText(0) == "English (en_US)"

def test_language_combo_empty(mocker, qtbot, settings_manager):
    mocker.patch("phonemic.utils.i18n.get_available_languages", return_value=[])
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.lang_combo.count() == 0

def test_dialog_loads_current_settings(qtbot, settings_manager, mock_languages):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    assert dialog.timeout_spin.value() == 5
    assert dialog.font_combo.currentText() == "14"
    assert dialog.max_records_spin.value() == 10
    assert dialog.lang_combo.currentText() == "简体中文 (zh_CN)"

    dialog.close()


def test_dialog_saves_settings_on_accept(qtbot, settings_manager):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.timeout_spin.setValue(10)
    dialog.max_records_spin.setValue(20)

    dialog.accept()

    assert settings_manager.get("hud_timeout_sec") == 10
    assert settings_manager.get("mobile_max_records") == 20

    assert not dialog.isVisible()


def test_dialog_cancel_does_not_save(qtbot, settings_manager):
    original_timeout = settings_manager.get("hud_timeout_sec")
    original_font = settings_manager.get("hud_font_size")
    original_records = settings_manager.get("mobile_max_records")
    original_lang = settings_manager.get("language")

    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.timeout_spin.setValue(99)
    dialog.set_combo_index(dialog.font_combo, 20)
    dialog.max_records_spin.setValue(30)
    dialog.set_combo_index(dialog.lang_combo, "en_US")

    dialog.reject()

    assert settings_manager.get("hud_timeout_sec") == original_timeout
    assert settings_manager.get("hud_font_size") == original_font
    assert settings_manager.get("mobile_max_records") == original_records
    assert settings_manager.get("language") == original_lang

    assert not dialog.isVisible()


def test_font_size_conversion(qtbot, settings_manager):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.set_combo_index(dialog.font_combo, 18)
    dialog._save_settings()
    assert settings_manager.get("hud_font_size") == 18

    dialog.set_combo_index(dialog.font_combo, "system")
    dialog._save_settings()
    assert settings_manager.get("hud_font_size") == "system"

    dialog.close()


def test_language_conversion(qtbot, settings_manager, mock_languages):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.set_combo_index(dialog.lang_combo, "en_US")
    dialog._save_settings()
    assert settings_manager.get("language") == "en_US"

    dialog.set_combo_index(dialog.lang_combo, "zh_CN")
    dialog._save_settings()
    assert settings_manager.get("language") == "zh_CN"

    dialog.close()


def test_dialog_respects_existing_system_font_setting(qtbot, settings_manager):
    settings_manager.set("hud_font_size", "system")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.font_combo.currentData() == "system"
    dialog.close()


def test_dialog_respects_existing_int_font_setting(qtbot, settings_manager):
    settings_manager.set("hud_font_size", 20)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.font_combo.currentText() == "20"
    dialog.close()

# ===================== 关闭行为下拉框测试 =====================

def test_close_action_placeholder_exists_and_disabled(qtbot, settings_manager, mock_languages):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.count() >= 3
    assert combo.itemText(0) == dialog.i18n.tr("settings.close_placeholder")
    model = combo.model()
    item = model.item(0, 0)
    assert item is not None
    assert not (item.flags() & Qt.ItemIsSelectable)
    dialog.close()


def test_close_action_loads_placeholder_when_unset(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.currentIndex() == 0
    assert combo.currentData() is None
    dialog.close()


def test_close_action_loads_quit_when_set(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", "quit")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.currentIndex() == 1
    assert combo.currentData() == "quit"
    dialog.close()


def test_close_action_loads_tray_when_set(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", "tray")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.currentIndex() == 2
    assert combo.currentData() == "tray"
    dialog.close()


def test_close_action_save_skips_when_placeholder_selected(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    dialog.close_combo.setCurrentIndex(0)  # 占位
    dialog.accept()
    
    assert settings_manager.get("close_action") is None
    assert not dialog.isVisible()


def test_close_action_save_quit(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    dialog.close_combo.setCurrentIndex(1)
    dialog.accept()
    
    assert settings_manager.get("close_action") == "quit"
    assert not dialog.isVisible()


def test_close_action_save_tray(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    dialog.close_combo.setCurrentIndex(2)
    dialog.accept()
    
    assert settings_manager.get("close_action") == "tray"
    assert not dialog.isVisible()


def test_close_action_cancel_does_not_save(qtbot, settings_manager, mock_languages):
    original = settings_manager.get("close_action")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    dialog.close_combo.setCurrentIndex(1)
    dialog.reject()
    
    assert settings_manager.get("close_action") == original
    assert not dialog.isVisible()


def test_close_action_change_from_quit_to_tray(qtbot, settings_manager, mock_languages):
    settings_manager.set("close_action", "quit")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.currentIndex() == 1
    combo.setCurrentIndex(2)
    dialog.accept()
    
    assert settings_manager.get("close_action") == "tray"
    assert not dialog.isVisible()

# ===================== 新增启动选项测试 =====================

def test_startup_group_exists(qtbot, settings_manager, mock_languages):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert hasattr(dialog, 'auto_start_check')
    assert hasattr(dialog, 'mode_combo')
    dialog.close()

def test_startup_group_loads_mode_correctly(qtbot, settings_manager, mock_languages):
    settings_manager.set("network_selection_mode", "auto")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.mode_combo.currentData() == "auto"
    dialog.close()

    settings_manager.set("network_selection_mode", "last")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.mode_combo.currentData() == "last"
    dialog.close()

def test_startup_group_saves_mode(qtbot, settings_manager, mock_languages):
    settings_manager.set("network_selection_mode", "ask")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("auto"))
    dialog.accept()
    assert settings_manager.get("network_selection_mode") == "auto"

def test_auto_start_check_loads_from_startup_module(qtbot, settings_manager, mock_languages, monkeypatch):
    # 模拟 startup.is_auto_start_enabled 返回 True
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: True)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.auto_start_check.isChecked() is True
    dialog.close()

def test_auto_start_check_saves_to_startup_module(qtbot, settings_manager, mock_languages, monkeypatch):
    mock_set = MagicMock()
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.set_auto_start_enabled", mock_set)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog.auto_start_check.setChecked(True)
    dialog.accept()
    # 默认非静默，silent=False
    mock_set.assert_called_once_with(True, silent=False)
    dialog.close()


# ===================== 静默启动联动测试 =====================

def test_silent_check_disabled_when_auto_start_off(qtbot, settings_manager, mock_languages, monkeypatch):
    """开机自启关闭时，静默复选框应禁用且不勾选"""
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: False)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.auto_start_check.isChecked() is False
    assert dialog.silent_start_check.isEnabled() is False
    assert dialog.silent_start_check.isChecked() is False
    dialog.close()


def test_silent_check_enabled_when_auto_start_on(qtbot, settings_manager, mock_languages, monkeypatch):
    """开机自启开启时，静默复选框应可用"""
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: True)
    settings_manager.set("auto_start_silent", False)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.silent_start_check.isEnabled() is True
    assert dialog.silent_start_check.isChecked() is False
    dialog.close()


def test_silent_check_loads_preference_when_auto_start_on(qtbot, settings_manager, mock_languages, monkeypatch):
    """开机自启开启 + 配置中 auto_start_silent=True 时，复选框勾选"""
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: True)
    settings_manager.set("auto_start_silent", True)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.silent_start_check.isChecked() is True
    dialog.close()


def test_silent_check_unchecks_when_auto_start_toggled_off(qtbot, settings_manager, mock_languages, monkeypatch):
    """运行时取消开机自启，静默复选框应自动取消勾选并禁用"""
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: True)
    settings_manager.set("auto_start_silent", True)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.silent_start_check.isChecked() is True
    # 取消自启
    dialog.auto_start_check.setChecked(False)
    assert dialog.silent_start_check.isEnabled() is False
    assert dialog.silent_start_check.isChecked() is False
    dialog.close()


def test_silent_check_reenables_when_auto_start_toggled_on(qtbot, settings_manager, mock_languages, monkeypatch):
    """运行时勾选开机自启，静默复选框应重新可用"""
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: False)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog.silent_start_check.isEnabled() is False
    # 勾选自启
    dialog.auto_start_check.setChecked(True)
    assert dialog.silent_start_check.isEnabled() is True
    dialog.close()


def test_silent_preference_saved_on_accept(qtbot, settings_manager, mock_languages, monkeypatch):
    """勾选静默并确定后，auto_start_silent 持久化且注册表调用带 silent=True"""
    mock_set = MagicMock()
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.is_auto_start_enabled", lambda: False)
    monkeypatch.setattr("phonemic.gui.settings_dialog.startup.set_auto_start_enabled", mock_set)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    # 先勾选自启（这会启用静默复选框），再勾选静默
    dialog.auto_start_check.setChecked(True)
    dialog.silent_start_check.setChecked(True)
    dialog.accept()
    # 注册表调用带 silent=True
    mock_set.assert_called_once_with(True, silent=True)
    # 配置已持久化
    assert settings_manager.get("auto_start_silent") is True

if __name__ == "__main__":
    pytest.main([__file__, "-v"])