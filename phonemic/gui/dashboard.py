from typing import Callable
import os
import subprocess
import sys

import qrcode
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QAction, QPainter, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QLabel,
    QFrame, QMenuBar, QMessageBox, QApplication,
    QDialog, QRadioButton, QCheckBox, QDialogButtonBox
)
from PySide6.QtWidgets import QSystemTrayIcon  # 新增

from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.gui.commands_dialog import CommandsDialog
from phonemic.utils.paths import get_app_root, get_build_info
from phonemic.utils.i18n import I18n
from phonemic.utils.settings_manager import SettingsManager


def make_qr_pixmap(data: str, size: int = 250) -> QPixmap:
    """直接从 qrcode 矩阵生成 QPixmap，不依赖 PIL/Pillow。"""
    qr = qrcode.QRCode(box_size=1, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()

    matrix_size = len(matrix)
    scale = max(1, size // matrix_size)
    actual_size = matrix_size * scale

    pixmap = QPixmap(actual_size, actual_size)
    pixmap.fill(Qt.white)

    painter = QPainter(pixmap)
    painter.setBrush(QColor(0, 0, 0))
    painter.setPen(Qt.NoPen)
    for y, row in enumerate(matrix):
        for x, is_dark in enumerate(row):
            if is_dark:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()

    if pixmap.width() != size or pixmap.height() != size:
        pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pixmap


class Dashboard(QMainWindow):
    # ===== 改动：构造函数增加 tray 参数（可选）=====
    def __init__(self, ip: str, port: int, tray=None, parent=None):
        super().__init__(parent)
        self.i18n = I18n.instance()
        self.sm = SettingsManager.instance()
        self.tray = tray  # 保存托盘对象引用
        self.setWindowTitle(self.i18n.tr("dashboard.title"))
        self.setFixedSize(400, 480)
        self.setWindowFlags(self.windowFlags() & (~Qt.WindowMaximizeButtonHint) | Qt.WindowCloseButtonHint)
        self._setup_ui(ip, port)
        self._setup_menu()

    def set_restart_network_callback(self, callback):
        """设置切换网络的回调函数"""
        self._restart_network_callback = callback

    def _setup_ui(self, ip, port) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # ----- 二维码（修复倾斜问题）-----
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)

        qr_url = f"http://{ip}:{port}"
        pixmap = make_qr_pixmap(qr_url)
        qr_label.setPixmap(pixmap)
        layout.addWidget(qr_label)

        # ----- 分隔线 -----
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        ip_label = QLabel(self.i18n.tr("dashboard.ip_label") + f": {ip}:{port}")
        ip_label.setAlignment(Qt.AlignCenter)
        ip_label.setWordWrap(True)
        layout.addWidget(ip_label)

        info_label = QLabel(self.i18n.tr("dashboard.info"))
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: blue; font-size: 10px;")
        layout.addWidget(info_label)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line2)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        self.update_connection_status(False)
        layout.addWidget(self.status_label)
        layout.addStretch()

        self.qr_label = qr_label
        self.ip_label = ip_label

    def _setup_menu(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)

        program_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_program"))
        help_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_help"))

        # 偏好设置
        settings_action = QAction(self.i18n.tr("dashboard.menu_action"), self)
        settings_action.triggered.connect(self._open_settings)
        program_menu.addAction(settings_action)

        # 命令配置
        commands_action = QAction(self.i18n.tr("dashboard.menu_command"), self)
        commands_action.triggered.connect(self._open_commands_dialog)
        program_menu.addAction(commands_action)
        # 切换网络地址

        switch_network_action = QAction(self.i18n.tr("dashboard.menu_switch_network"), self)
        switch_network_action.triggered.connect(self._on_switch_network)
        program_menu.addAction(switch_network_action)

        # 分隔线 + 退出
        program_menu.addSeparator()
        exit_action = QAction(self.i18n.tr("dashboard.menu_exit"), self)
        exit_action.triggered.connect(self._quit_app)
        program_menu.addAction(exit_action)

        # 帮助菜单
        help_action = QAction(self.i18n.tr("dashboard.menu_help_guide"), self)
        help_action.triggered.connect(self.open_user_guide)
        help_menu.addAction(help_action)

        about_action = QAction(self.i18n.tr("dashboard.menu_about"), self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def _open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()

    def _open_commands_dialog(self):
        dlg = CommandsDialog(self)
        dlg.exec_()

    def _on_switch_network(self):
        """触发切换网络回调"""
        if self._restart_network_callback:
            self._restart_network_callback()

    def update_network(self, ip: str, port: int):
        """更新主界面的 IP 和二维码显示"""
        # 更新二维码
        qr_url = f"http://{ip}:{port}"
        pixmap = make_qr_pixmap(qr_url)
        self.qr_label.setPixmap(pixmap)

        # 更新 IP 标签
        self.ip_label.setText(self.i18n.tr("dashboard.ip_label") + f": {ip}:{port}")

    def show_about(self):
        version, commit, _ = get_build_info()
        content = self.i18n.tr("about.content", version=version, commit=commit)
        QMessageBox.about(self, self.i18n.tr("about.title"), content)

    def open_user_guide(self):
        guide_path = get_app_root() / "USER_GUIDE.md"
        if guide_path.exists():
            if sys.platform == "win32":
                subprocess.Popen(["notepad.exe", str(guide_path)],
                                 shell=False,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.run(["open", str(guide_path)] if sys.platform == "darwin" else ["xdg-open", str(guide_path)])
        else:
            QMessageBox.warning(self, self.i18n.tr("help.warning"),
                                self.i18n.tr("help.file_not_found"))

    def update_connection_status(self, connected: bool) -> None:
        self.connected = connected
        if connected:
            self.status_label.setText('<span style="color:green;">●</span> ' + self.i18n.tr("dashboard.status_connected"))
        else:
            self.status_label.setText('<span style="color:red;">●</span> ' + self.i18n.tr("dashboard.status_disconnected"))

    def show_hide_on_tray_message(self):
        if getattr(self, '_already_show_hide_on_tray_message', False):
            return
        self._already_show_hide_on_tray_message = True
        self.tray.show_message(
            self.i18n.tr("tray.minimized_title"),
            self.i18n.tr("tray.minimized_message"),
            timeout=3000
        )

    def _quit_app(self):
        """从菜单点击退出，直接终止程序"""
        self._is_force_quitting = True
        QApplication.quit()

    def closeEvent(self, event):
        """重写关闭事件：根据配置决定行为。若为菜单强制退出则直接放行。"""
        # 如果是菜单触发的强制退出，直接放行，不做任何花活
        if getattr(self, '_is_force_quitting', False):
            event.accept()
            return

        close_action = self.sm.get("close_action", None)

        if close_action == "quit":
            event.accept()
            return
        event.ignore()
        if close_action == "tray":
            self.hide()
            self.show_hide_on_tray_message()
        else:
            self._show_close_choice_dialog()

    def _show_close_choice_dialog(self):
        """显示关闭行为选择对话框（首次使用）"""
        dialog = QDialog(self)
        dialog.setWindowTitle(self.i18n.tr("close_choice.title"))
        dialog.setModal(True)
        dialog.setMinimumWidth(350)

        layout = QVBoxLayout(dialog)

        label = QLabel(self.i18n.tr("close_choice.prompt"))
        label.setWordWrap(True)
        layout.addWidget(label)

        quit_radio = QRadioButton(self.i18n.tr("close_choice.quit"))
        tray_radio = QRadioButton(self.i18n.tr("close_choice.tray"))
        quit_radio.setChecked(True)
        layout.addWidget(quit_radio)
        layout.addWidget(tray_radio)

        remember_check = QCheckBox(self.i18n.tr("close_choice.remember"))
        layout.addWidget(remember_check)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        if dialog.exec() == QDialog.Accepted:
            action = "quit" if quit_radio.isChecked() else "tray"
            if remember_check.isChecked():
                self.sm.set("close_action", action)
            if action == "quit":
                QApplication.quit()
            else:
                self.hide()
                self.show_hide_on_tray_message()
