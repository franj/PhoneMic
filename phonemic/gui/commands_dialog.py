"""
命令配置图形界面（用户友好版）
提供命令的增删改查、启用/禁用、顺序调整。
所有修改立即通过 CommandsManager 持久化到 commands.json。
"""

import logging
import re
from typing import Optional, Dict, Any
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHeaderView, QMessageBox, QComboBox, QLineEdit,
    QCheckBox, QFormLayout, QDialogButtonBox, QWidget, QLabel
)

from phonemic.utils.commands_manager import CommandsManager, VoiceCommand
from phonemic.utils.i18n import I18n

logger = logging.getLogger(__name__)


class CommandEditDialog(QDialog):
    """新增/编辑命令的对话框，带详细帮助说明"""

    def __init__(self, parent=None, command: Optional[VoiceCommand] = None):
        super().__init__(parent)
        self.command = command
        self.i18n = I18n.instance()
        self.is_new = command is None

        self.setWindowTitle(self.i18n.tr("commands.edit_title_new") if self.is_new
                            else self.i18n.tr("commands.edit_title_edit"))
        self.setModal(True)
        self.setMinimumWidth(550)

        self._setup_ui()
        self._load_data()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        form_layout.setSpacing(12)
        form_layout.setLabelAlignment(Qt.AlignRight)

        # 名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(self.i18n.tr("commands.name_placeholder"))
        help_name = QLabel(self.i18n.tr("commands.name_help"))
        help_name.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow(self.i18n.tr("commands.name") + ":", self.name_edit)
        form_layout.addRow("", help_name)

        # 匹配类型
        self.match_type_combo = QComboBox()
        self.match_type_combo.addItem(self.i18n.tr("commands.match_exact"), "exact")
        self.match_type_combo.addItem(self.i18n.tr("commands.match_prefix"), "prefix")
        self.match_type_combo.addItem(self.i18n.tr("commands.match_regex"), "regex")
        help_match_type = QLabel(self.i18n.tr("commands.match_type_help"))
        help_match_type.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow(self.i18n.tr("commands.match_type") + ":", self.match_type_combo)
        form_layout.addRow("", help_match_type)

        # 匹配模式
        self.match_pattern_edit = QLineEdit()
        self.match_pattern_edit.setPlaceholderText(self.i18n.tr("commands.match_pattern_placeholder"))
        help_pattern = QLabel(self.i18n.tr("commands.match_pattern_help"))
        help_pattern.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow(self.i18n.tr("commands.match_pattern") + ":", self.match_pattern_edit)
        form_layout.addRow("", help_pattern)

        # 动作类型
        self.action_type_combo = QComboBox()
        self.action_type_combo.addItem(self.i18n.tr("commands.action_key"), "key")
        self.action_type_combo.addItem(self.i18n.tr("commands.action_exec"), "exec")
        help_action_type = QLabel(self.i18n.tr("commands.action_type_help"))
        help_action_type.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow(self.i18n.tr("commands.action_type") + ":", self.action_type_combo)
        form_layout.addRow("", help_action_type)

        # 动作参数（带示例按钮）
        param_layout = QHBoxLayout()
        self.action_param_edit = QLineEdit()
        self.action_param_edit.setPlaceholderText(self.i18n.tr("commands.action_param_placeholder"))
        param_layout.addWidget(self.action_param_edit)
        self.example_btn = QPushButton(self.i18n.tr("commands.example_btn"))
        self.example_btn.clicked.connect(self._show_examples)
        param_layout.addWidget(self.example_btn)
        help_param = QLabel(self.i18n.tr("commands.action_param_help"))
        help_param.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow(self.i18n.tr("commands.action_param") + ":", param_layout)
        form_layout.addRow("", help_param)

        # 启用
        self.enabled_check = QCheckBox(self.i18n.tr("commands.enabled"))
        help_enabled = QLabel(self.i18n.tr("commands.enabled_help"))
        help_enabled.setStyleSheet("color: gray; font-size: 11px;")
        form_layout.addRow("", self.enabled_check)
        form_layout.addRow("", help_enabled)

        main_layout.addLayout(form_layout)

        # 按钮
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self._validate_and_accept)
        self.button_box.rejected.connect(self.reject)
        main_layout.addWidget(self.button_box)

    def _load_data(self):
        if not self.command:
            # 新增模式的默认值
            self.match_type_combo.setCurrentIndex(0)
            self.action_type_combo.setCurrentIndex(0)
            self.enabled_check.setChecked(True)
            return

        # 编辑模式，填充现有数据（使用属性访问）
        self.name_edit.setText(self.command.name or "")
        match_type = self.command.matchType
        idx = self.match_type_combo.findData(match_type)
        if idx >= 0:
            self.match_type_combo.setCurrentIndex(idx)
        self.match_pattern_edit.setText(self.command.matchPattern or "")
        action_type = self.command.actionType
        idx = self.action_type_combo.findData(action_type)
        if idx >= 0:
            self.action_type_combo.setCurrentIndex(idx)
        self.action_param_edit.setText(self.command.actionParams or "")
        self.enabled_check.setChecked(self.command.enabled)

    def _show_examples(self):
        examples = self.i18n.tr("commands.example_content")
        QMessageBox.information(self, self.i18n.tr("commands.example_title"), examples)

    def _validate_and_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, self.i18n.tr("error"),
                                self.i18n.tr("commands.error_name_empty"))
            self.name_edit.setFocus()
            return

        match_pattern = self.match_pattern_edit.text().strip()
        if not match_pattern:
            QMessageBox.warning(self, self.i18n.tr("error"),
                                self.i18n.tr("commands.error_pattern_empty"))
            self.match_pattern_edit.setFocus()
            return

        # 正则匹配类型：提前校验正则是否能编译，避免保存后静默失效
        if self.match_type_combo.currentData() == "regex":
            try:
                re.compile(match_pattern)
            except re.error as e:
                QMessageBox.warning(self, self.i18n.tr("error"),
                                    self.i18n.tr("commands.error_regex_invalid").format(e))
                self.match_pattern_edit.setFocus()
                return

        action_params = self.action_param_edit.text().strip()
        if not action_params:
            QMessageBox.warning(self, self.i18n.tr("error"),
                                self.i18n.tr("commands.error_param_empty"))
            self.action_param_edit.setFocus()
            return

        self.accept()

    def get_command_data(self) -> Dict[str, Any]:
        return {
            "name": self.name_edit.text().strip(),
            "matchType": self.match_type_combo.currentData(),
            "matchPattern": self.match_pattern_edit.text().strip(),
            "actionType": self.action_type_combo.currentData(),
            "actionParams": self.action_param_edit.text().strip(),
            "enabled": self.enabled_check.isChecked(),
        }


class CommandsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = CommandsManager.instance()
        self.i18n = I18n.instance()

        self.setWindowTitle(self.i18n.tr("commands.title"))
        self.setModal(True)
        self.setMinimumSize(900, 500)

        self._setup_ui()
        self._refresh_table()

        self.manager.commands_changed.connect(self._refresh_table)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            self.i18n.tr("commands.column_enabled"),
            self.i18n.tr("commands.column_name"),
            self.i18n.tr("commands.column_match_type"),
            self.i18n.tr("commands.column_match_pattern"),
            self.i18n.tr("commands.column_action_type"),
            self.i18n.tr("commands.column_action_params"),
            self.i18n.tr("commands.column_actions"),
        ])
        for col, tip_key in enumerate([
            "col_enabled_tip", "col_name_tip", "col_match_type_tip",
            "col_match_pattern_tip", "col_action_type_tip", "col_action_params_tip", "col_actions_tip"
        ]):
            self.table.horizontalHeaderItem(col).setToolTip(self.i18n.tr(f"commands.{tip_key}"))

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(6, 120)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)

        self.table.cellDoubleClicked.connect(self._edit_command)
        layout.addWidget(self.table)

        bottom_layout = QHBoxLayout()
        help_label = QLabel(self.i18n.tr("commands.footer_hint"))
        help_label.setStyleSheet("color: #666; font-style: italic;")
        bottom_layout.addWidget(help_label)
        bottom_layout.addStretch()

        self.add_btn = QPushButton(self.i18n.tr("commands.add"))
        self.add_btn.clicked.connect(self._add_command)
        self.close_btn = QPushButton(self.i18n.tr("commands.close"))
        self.close_btn.clicked.connect(self.accept)
        bottom_layout.addWidget(self.add_btn)
        bottom_layout.addWidget(self.close_btn)

        layout.addLayout(bottom_layout)

    def _set_item(self, row, col, text, center=False):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if center:
            item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, col, item)

    def _refresh_table(self):
        commands = self.manager.get_commands()
        self.table.setRowCount(len(commands))

        for row, cmd in enumerate(commands):
            # 启用复选框
            cb = QCheckBox()
            cb.setChecked(cmd.enabled)
            # 使用默认参数捕获当前命令ID和状态，避免闭包问题
            cb.toggled.connect(lambda checked, captured_id=cmd.id: self.manager.set_enabled(captured_id, checked))
            self.table.setCellWidget(row, 0, cb)

            # 名称列（同时存储命令ID用于编辑）
            self._set_item(row, 1, cmd.name or "")
            self.table.item(row, 1).setData(Qt.UserRole, cmd.id)

            # 匹配类型
            match_type_display = {
                "exact": self.i18n.tr("commands.match_exact"),
                "prefix": self.i18n.tr("commands.match_prefix"),
                "regex": self.i18n.tr("commands.match_regex"),
            }.get(cmd.matchType, cmd.matchType)
            self._set_item(row, 2, match_type_display, center=True)

            # 匹配模式
            self._set_item(row, 3, cmd.matchPattern or "")

            # 动作类型
            action_type_display = self.i18n.tr("commands.action_key") if cmd.actionType == "key" else self.i18n.tr("commands.action_exec")
            self._set_item(row, 4, action_type_display, center=True)

            # 动作参数
            param = cmd.actionParams or ""
            display_param = param[:50] + "..." if len(param) > 50 else param
            self._set_item(row, 5, display_param)
            self.table.item(row, 5).setToolTip(param)

            # 操作按钮
            widget = QWidget()
            layout = QHBoxLayout(widget)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(4)

            # 上移：使用命令ID而不是行号
            up_btn = QPushButton("▲")
            up_btn.setFixedSize(24, 24)
            up_btn.setToolTip(self.i18n.tr("commands.move_up"))
            up_btn.clicked.connect(lambda checked, cid=cmd.id: self.manager.move_up(cid))

            # 下移
            down_btn = QPushButton("▼")
            down_btn.setFixedSize(24, 24)
            down_btn.setToolTip(self.i18n.tr("commands.move_down"))
            down_btn.clicked.connect(lambda checked, cid=cmd.id: self.manager.move_down(cid))

            # 删除
            del_btn = QPushButton("✖")
            del_btn.setFixedSize(24, 24)
            del_btn.setToolTip(self.i18n.tr("commands.delete"))
            del_btn.clicked.connect(lambda checked, cid=cmd.id: self._delete_command(cid))

            layout.addWidget(up_btn)
            layout.addWidget(down_btn)
            layout.addWidget(del_btn)
            layout.addStretch()
            self.table.setCellWidget(row, 6, widget)

        self.table.resizeRowsToContents()

    def _delete_command(self, cmd_id: str):
        reply = QMessageBox.question(self, self.i18n.tr("commands.confirm_delete_title"),
                                     self.i18n.tr("commands.confirm_delete_msg"),
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.manager.delete_command(cmd_id)
    def _add_command(self):
        dlg = CommandEditDialog(self)
        if dlg.exec() == QDialog.Accepted:
            data = dlg.get_command_data()
            self.manager.add_command(VoiceCommand.from_dict(data))

    def _edit_command(self, row, column):
        # 从第1列（名称列）的UserRole中获取命令ID
        item = self.table.item(row, 1)
        if not item:
            return
        cmd_id = item.data(Qt.UserRole)
        commands = self.manager.get_commands()
        cmd = next((c for c in commands if c.id == cmd_id), None)
        if not cmd:
            return
        dlg = CommandEditDialog(self, command=cmd)
        if dlg.exec() == QDialog.Accepted:
            new_data = dlg.get_command_data()
            self.manager.update_command(cmd_id, **new_data)