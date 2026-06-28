# settings_dialog.py (完整文件)

"""
设置对话框 - 通用配置
支持窗口水平拉伸，便于后续扩展复杂配置（如动作映射表）
支持国际化（I18n）
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout,
    QGroupBox, QSpinBox, QComboBox, QCheckBox,
    QDialogButtonBox, QScrollArea, QWidget, QMessageBox
)

from phonemic.utils.settings_manager import SettingsManager
from phonemic.utils.i18n import I18n
from phonemic.utils import i18n
# 暂时导入 startup 桩（后续实现）
from phonemic.utils import startup

logger = logging.getLogger(__name__)

class SettingsDialog(QDialog):
    """应用设置对话框（可调整宽度）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sm = SettingsManager.instance()
        self.i18n = I18n.instance()

        self.setWindowTitle(self.i18n.tr("settings.title"))
        self.setModal(True)

        self.setMinimumWidth(400)

        self._setup_ui()
        self._load_settings()
        self.accepted.connect(self._save_settings)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        content_widget = QWidget()
        main_layout.addWidget(content_widget)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(16)
        content_layout.setContentsMargins(0, 0, 0, 0)

        content_layout.addWidget(self._create_hud_group())
        content_layout.addWidget(self._create_chat_group())
        content_layout.addWidget(self._create_close_action_group())
        content_layout.addWidget(self._create_startup_group())   # 新增
        content_layout.addWidget(self._create_other_group())
        content_layout.addStretch()

        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        main_layout.addWidget(self.button_box)
        self.adjustSize()

    @staticmethod
    def set_combo_index(combo, data):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(0)

    def _create_hud_group(self):
        group = QGroupBox(self.i18n.tr("settings.hud_group"))
        layout = QFormLayout(group)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 30)
        self.timeout_spin.setSuffix(self.i18n.tr("settings.seconds_suffix"))
        self.timeout_spin.setToolTip(self.i18n.tr("settings.hud_timeout_tooltip"))
        layout.addRow(self.i18n.tr("settings.hud_timeout") + ":", self.timeout_spin)

        self.font_combo = QComboBox()
        self.font_combo.addItem(self.i18n.tr("settings.font_follow_system"), "system")
        for size in [12, 14, 16, 18, 20, 22, 24]:
            self.font_combo.addItem(str(size), size)
        self.font_combo.setToolTip(self.i18n.tr("settings.font_size_tooltip"))
        layout.addRow(self.i18n.tr("settings.hud_font_size") + ":", self.font_combo)

        return group

    def _create_chat_group(self):
        group = QGroupBox(self.i18n.tr("settings.chat_group"))
        layout = QFormLayout(group)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)

        self.max_records_spin = QSpinBox()
        self.max_records_spin.setRange(5, 50)
        self.max_records_spin.setSuffix(self.i18n.tr("settings.records_suffix"))
        self.max_records_spin.setToolTip(self.i18n.tr("settings.max_records_tooltip"))
        layout.addRow(self.i18n.tr("settings.max_records") + ":", self.max_records_spin)

        return group

    def _create_close_action_group(self):
        group = QGroupBox(self.i18n.tr("settings.close_group"))
        layout = QFormLayout(group)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)

        self.close_combo = QComboBox()
        # 占位项
        self.close_combo.addItem(self.i18n.tr("settings.close_placeholder"), None)
        model = self.close_combo.model()
        if model:
            item = model.item(0, 0)
            if item:
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        # 有效选项
        self.close_combo.addItem(self.i18n.tr("settings.close_quit"), "quit")
        self.close_combo.addItem(self.i18n.tr("settings.close_tray"), "tray")

        self.close_combo.setToolTip(self.i18n.tr("settings.close_tooltip"))
        layout.addRow(self.i18n.tr("settings.close_behavior") + ":", self.close_combo)

        return group

    def _create_startup_group(self):
        group = QGroupBox(self.i18n.tr("settings.startup_group"))
        layout = QFormLayout(group)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)

        # 开机自启动复选框
        self.auto_start_check = QCheckBox(self.i18n.tr("settings.auto_start"))
        self.auto_start_check.setToolTip(self.i18n.tr("settings.auto_start_tooltip"))
        layout.addRow(self.auto_start_check)

        # 开机静默启动复选框（依赖开机自启）
        self.silent_start_check = QCheckBox(self.i18n.tr("settings.auto_start_silent"))
        self.silent_start_check.setToolTip(self.i18n.tr("settings.auto_start_silent_tooltip"))
        layout.addRow(self.silent_start_check)

        # 联动：自启状态变化时，启用/禁用静默复选框
        self.auto_start_check.toggled.connect(self._update_silent_check_state)

        # 网络选择策略下拉框
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(self.i18n.tr("settings.mode_auto"), "auto")
        self.mode_combo.addItem(self.i18n.tr("settings.mode_last"), "last")
        self.mode_combo.addItem(self.i18n.tr("settings.mode_ask"), "ask")
        self.mode_combo.setToolTip(self.i18n.tr("settings.mode_tooltip"))
        layout.addRow(self.i18n.tr("settings.mode_label") + ":", self.mode_combo)

        return group

    def _update_silent_check_state(self, auto_start_enabled: bool):
        """根据开机自启状态联动静默复选框：禁用时自动取消勾选"""
        self.silent_start_check.setEnabled(auto_start_enabled)
        if not auto_start_enabled:
            self.silent_start_check.setChecked(False)

    def _create_other_group(self):
        group = QGroupBox(self.i18n.tr("settings.other_group"))
        layout = QFormLayout(group)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 16, 12, 12)

        self.lang_combo = QComboBox()
        self.lang_combo.clear()
        languages = i18n.get_available_languages()
        for code, display in languages:
            self.lang_combo.addItem(display, code)
        layout.addRow(self.i18n.tr("settings.language") + ":", self.lang_combo)

        return group

    def _load_settings(self):
        self.timeout_spin.setValue(self.sm.get("hud_timeout_sec", 5))

        font_val = self.sm.get("hud_font_size", 14)
        self.set_combo_index(self.font_combo, font_val)

        self.max_records_spin.setValue(self.sm.get("mobile_max_records", 10))

        # 关闭行为
        close_action = self.sm.get("close_action", None)
        if close_action == "quit":
            self.close_combo.setCurrentIndex(1)
        elif close_action == "tray":
            self.close_combo.setCurrentIndex(2)
        else:
            self.close_combo.setCurrentIndex(0)

        # 启动选项
        # 开机自启动状态从注册表读取
        auto_start_on = startup.is_auto_start_enabled()
        self.auto_start_check.setChecked(auto_start_on)
        # 静默启动偏好从配置读取（仅在自启开启时有意义）
        self.silent_start_check.setChecked(
            auto_start_on and self.sm.get("auto_start_silent", False)
        )
        # 同步初始启用/禁用状态
        self._update_silent_check_state(auto_start_on)
        # 网络选择策略
        mode = self.sm.get("network_selection_mode", "ask")
        self.set_combo_index(self.mode_combo, mode)

        lang = self.sm.get("language", "zh_CN")
        self.set_combo_index(self.lang_combo, lang)

    def _save_settings(self):
        self.sm.set("hud_timeout_sec", self.timeout_spin.value())

        selected_data = self.font_combo.currentData()
        if selected_data == "system":
            self.sm.set("hud_font_size", "system")
        else:
            self.sm.set("hud_font_size", selected_data)

        self.sm.set("mobile_max_records", self.max_records_spin.value())

        # 关闭行为
        current_index = self.close_combo.currentIndex()
        if current_index != 0:
            self.sm.set("close_action", self.close_combo.currentData())

        # 启动选项
        # 开机自启动：注册表实际生效，静默偏好作为启动参数
        auto_start_on = self.auto_start_check.isChecked()
        silent_pref = self.silent_start_check.isChecked()
        startup.set_auto_start_enabled(auto_start_on, silent=silent_pref)
        # 持久化静默偏好（即使自启关闭也存，下次开启自启时恢复更友好）
        self.sm.set("auto_start_silent", silent_pref)
        # 网络选择策略
        self.sm.set("network_selection_mode", self.mode_combo.currentData())

        old_lang = self.sm.get("language", "zh_CN")
        new_lang = self.lang_combo.currentData()
        if old_lang != new_lang:
            self.sm.set("language", new_lang)
            QMessageBox.information(
                self,
                self.i18n.tr("settings.restart_title"),
                self.i18n.tr("settings.restart_message")
            )