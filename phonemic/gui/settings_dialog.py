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
from phonemic.utils.i18n import I18n  # 国际化单例
from phonemic.utils import i18n  # 国际化单例

logger = logging.getLogger(__name__)

class SettingsDialog(QDialog):
    """应用设置对话框（可调整宽度）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sm = SettingsManager.instance()
        self.i18n = I18n.instance()

        self.setWindowTitle(self.i18n.tr("settings.title"))
        self.setModal(True)

        # 允许窗口自由调整大小，但限制最小尺寸
        self.setMinimumWidth(400)
        self.resize(500, 700)

        self._setup_ui()
        self._load_settings()
        self.accepted.connect(self._save_settings)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        main_layout.addWidget(scroll)

        content_widget = QWidget()
        scroll.setWidget(content_widget)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setSpacing(16)
        content_layout.setContentsMargins(0, 0, 0, 0)

        content_layout.addWidget(self._create_hud_group())
        content_layout.addWidget(self._create_chat_group())
        content_layout.addWidget(self._create_close_action_group())
        content_layout.addWidget(self._create_other_group())
        content_layout.addStretch()

        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        main_layout.addWidget(self.button_box)

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
        # 1. 添加占位项（索引0），不可选
        self.close_combo.addItem(self.i18n.tr("settings.close_placeholder"), None)
        # 设置占位项为不可选
        model = self.close_combo.model()
        if model:
            item = model.item(0, 0)
            if item:
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)

        # 2. 添加有效选项
        self.close_combo.addItem(self.i18n.tr("settings.close_quit"), "quit")
        self.close_combo.addItem(self.i18n.tr("settings.close_tray"), "tray")

        self.close_combo.setToolTip(self.i18n.tr("settings.close_tooltip"))
        layout.addRow(self.i18n.tr("settings.close_behavior") + ":", self.close_combo)

        return group

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

        # 加载关闭行为配置
        close_action = self.sm.get("close_action", None)
        if close_action == "quit":
            self.close_combo.setCurrentIndex(1)   # 索引1是"退出程序"
        elif close_action == "tray":
            self.close_combo.setCurrentIndex(2)   # 索引2是"最小化到托盘"
        else:
            # 未设置，显示占位项（索引0）
            self.close_combo.setCurrentIndex(0)

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

        # 保存关闭行为：如果当前选中的是占位项（索引0），则不保存（保持原值）
        current_index = self.close_combo.currentIndex()
        if current_index != 0:
            self.sm.set("close_action", self.close_combo.currentData())
        # 否则不修改配置（即如果之前未设置，保持None；如果之前有值，也不应被清空，但这里占位项不可选，用户无法主动选它，所以不会进入此分支）

        old_lang = self.sm.get("language", "zh_CN")
        new_lang = self.lang_combo.currentData()
        if old_lang != new_lang:
            self.sm.set("language", new_lang)
            QMessageBox.information(
                self,
                self.i18n.tr("settings.restart_title"),
                self.i18n.tr("settings.restart_message")
            )
