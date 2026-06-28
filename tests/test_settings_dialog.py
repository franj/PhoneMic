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
    mock_app_config = tmp_path / "PhoneMic"
    mock_app_config.mkdir()

    def mock_writable_location(location):
        if location == QStandardPaths.AppConfigLocation:
            return str(tmp_path)
        return QStandardPaths.writableLocation(location)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(QStandardPaths, "writableLocation", mock_writable_location)
        yield mock_app_config


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
    # 假设 dialog 中有一个 lang_combo
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

    # 直接调用 accept 触发保存
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
    dialog.set_combo_index(dialog.font_combo, 20);
    dialog.max_records_spin.setValue(30)
    dialog.set_combo_index(dialog.lang_combo, "en_US");

    # 直接调用 reject 取消
    dialog.reject()

    assert settings_manager.get("hud_timeout_sec") == original_timeout
    assert settings_manager.get("hud_font_size") == original_font
    assert settings_manager.get("mobile_max_records") == original_records
    assert settings_manager.get("language") == original_lang

    assert not dialog.isVisible()


def test_font_size_conversion(qtbot, settings_manager):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.set_combo_index(dialog.font_combo, 18);
    dialog._save_settings()
    assert settings_manager.get("hud_font_size") == 18

    dialog.set_combo_index(dialog.font_combo, "system");
    dialog._save_settings()
    assert settings_manager.get("hud_font_size") == "system"

    dialog.close()


def test_language_conversion(qtbot, settings_manager, mock_languages):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)

    dialog.set_combo_index(dialog.lang_combo, "en_US");
    dialog._save_settings()
    assert settings_manager.get("language") == "en_US"

    dialog.set_combo_index(dialog.lang_combo, "zh_CN");
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
    """测试关闭行为下拉框中占位项存在且不可选"""
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    # 占位项在索引0
    assert combo.count() >= 3  # 占位 + 两个有效选项
    assert combo.itemText(0) == dialog.i18n.tr("settings.close_placeholder")
    # 检查占位项不可选
    model = combo.model()
    item = model.item(0, 0)
    assert item is not None
    # 检查是否没有 Selectable 标志
    assert not (item.flags() & Qt.ItemIsSelectable)
    dialog.close()


def test_close_action_loads_placeholder_when_unset(qtbot, settings_manager, mock_languages):
    """当 close_action 为 None 时，下拉框显示占位项（索引0）"""
    # 确保配置中没有 close_action
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    assert combo.currentIndex() == 0
    assert combo.currentData() is None
    dialog.close()


def test_close_action_loads_quit_when_set(qtbot, settings_manager, mock_languages):
    """当 close_action 为 'quit' 时，下拉框选中 '退出程序'"""
    settings_manager.set("close_action", "quit")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    # 索引1是 "退出程序"
    assert combo.currentIndex() == 1
    assert combo.currentData() == "quit"
    dialog.close()


def test_close_action_loads_tray_when_set(qtbot, settings_manager, mock_languages):
    """当 close_action 为 'tray' 时，下拉框选中 '最小化到系统托盘'"""
    settings_manager.set("close_action", "tray")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    # 索引2是 "最小化到系统托盘"
    assert combo.currentIndex() == 2
    assert combo.currentData() == "tray"
    dialog.close()


def test_close_action_save_skips_when_placeholder_selected(qtbot, settings_manager, mock_languages):
    """
    当关闭行为未设置（占位项选中）且用户点击确定时，不修改配置（保持 None）
    注意：由于占位项不可选，正常情况下用户无法选中它，但我们可以通过代码强制设置索引0来模拟未选择的情况。
    """
    # 初始配置为 None
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    # 确保当前索引为0（占位）
    dialog.close_combo.setCurrentIndex(0)  # 强制选占位，实际用户无法操作，但测试需要覆盖
    
    # 点击确定
    dialog.accept()
    
    # 配置应该仍然为 None（未被写入）
    assert settings_manager.get("close_action") is None
    assert not dialog.isVisible()


def test_close_action_save_quit(qtbot, settings_manager, mock_languages):
    """用户选择 '退出程序' 并确定后，配置变为 'quit'"""
    # 初始未设置
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    # 选择 '退出程序'（索引1）
    combo.setCurrentIndex(1)
    
    dialog.accept()
    
    assert settings_manager.get("close_action") == "quit"
    assert not dialog.isVisible()


def test_close_action_save_tray(qtbot, settings_manager, mock_languages):
    """用户选择 '最小化到系统托盘' 并确定后，配置变为 'tray'"""
    settings_manager.set("close_action", None)
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    combo.setCurrentIndex(2)  # 索引2为 tray
    
    dialog.accept()
    
    assert settings_manager.get("close_action") == "tray"
    assert not dialog.isVisible()


def test_close_action_cancel_does_not_save(qtbot, settings_manager, mock_languages):
    """取消对话框时，无论下拉框如何修改，配置保持不变"""
    original = settings_manager.get("close_action")  # 假设为 None
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    # 修改为 'quit'
    dialog.close_combo.setCurrentIndex(1)
    
    # 取消
    dialog.reject()
    
    # 配置不变
    assert settings_manager.get("close_action") == original
    assert not dialog.isVisible()


def test_close_action_change_from_quit_to_tray(qtbot, settings_manager, mock_languages):
    """已设置为 'quit'，用户改为 'tray' 并确定后更新"""
    settings_manager.set("close_action", "quit")
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    
    combo = dialog.close_combo
    # 初始应选中 'quit'（索引1）
    assert combo.currentIndex() == 1
    # 改为 'tray'（索引2）
    combo.setCurrentIndex(2)
    
    dialog.accept()
    
    assert settings_manager.get("close_action") == "tray"
    assert not dialog.isVisible()

if __name__ == "__main__":
    pytest.main([__file__, "-v"])