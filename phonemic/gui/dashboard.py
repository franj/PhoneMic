from typing import Callable, Optional
import os
import subprocess
import sys

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap, QAction, QActionGroup, QPainter, QColor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QMenuBar, QMessageBox, QApplication,
    QDialog, QRadioButton, QCheckBox, QDialogButtonBox
)
from PySide6.QtWidgets import QSystemTrayIcon  # 新增

from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.gui.commands_dialog import CommandsDialog
from phonemic.tunnel.mode import TunnelMode, get_mode, set_mode, effective_algorithm
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
        self.setFixedSize(400, 520)
        self.setWindowFlags(self.windowFlags() & (~Qt.WindowMaximizeButtonHint) | Qt.WindowCloseButtonHint)
        self._mode: TunnelMode = get_mode()
        self._tunnel_url: Optional[str] = None
        self._qr_url: Optional[str] = f"http://{ip}:{port}"
        self._lan_ip = ip
        self._lan_port = port
        self._switching = False
        self._mode_switch_callback: Optional[Callable[[TunnelMode], None]] = None
        self._secure_channel = None  # SecureChannel 引用，由外部设置
        self._algorithm: str = self.sm.get("e2ee_algorithm", "none")
        self._negotiated_algo: Optional[str] = None  # 本次连接握手协商出的算法，由 connect 事件携带
        self._algorithm_change_callback: Optional[Callable[[str], None]] = None
        self._setup_ui(ip, port)
        self._setup_menu()
        self._apply_mode_ui()

    def set_restart_network_callback(self, callback):
        """设置切换网络的回调函数"""
        self._restart_network_callback = callback

    def set_mode_switch_callback(self, callback: Callable[[TunnelMode], None]):
        """设置模式切换回调函数"""
        self._mode_switch_callback = callback

    def set_secure_channel(self, sc):
        """设置安全通道引用，并刷新 QR 码以包含公钥。"""
        self._secure_channel = sc
        self._refresh_qr()

    def set_algorithm_change_callback(self, callback: Callable[[str], None]):
        """设置算法变更回调函数。"""
        self._algorithm_change_callback = callback

    def _setup_ui(self, ip, port) -> None:
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # ----- 二维码 -----
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

        # 地址栏：QLabel，居中 + 自动换行，可用鼠标选中复制
        self.ip_label = QLabel(f"http://{ip}:{port}")
        self.ip_label.setAlignment(Qt.AlignCenter)
        self.ip_label.setWordWrap(True)
        self.ip_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.ip_label)

        # ----- Cloudflare 说明（仅 Cloudflare 模式可见）-----
        self.cf_info_label = QLabel(self.i18n.tr("dashboard.cf_info"))
        self.cf_info_label.setAlignment(Qt.AlignCenter)
        self.cf_info_label.setWordWrap(True)
        self.cf_info_label.setStyleSheet("color: blue; font-size: 10px;")
        self.cf_info_label.setVisible(False)
        layout.addWidget(self.cf_info_label)

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
        self.info_label = info_label

    def _apply_mode_ui(self) -> None:
        """根据当前模式更新菜单勾选状态和元素显隐。"""
        if self._switching:
            blank = QPixmap(250, 250)
            blank.fill(Qt.white)
            self.qr_label.setPixmap(blank)
            return

        if self._mode == TunnelMode.LAN:
            self.info_label.setVisible(True)
            self.cf_info_label.setVisible(False)
            self.switch_network_action.setEnabled(True)
            self._refresh_qr()
        else:
            self.info_label.setVisible(False)
            self.cf_info_label.setVisible(True)
            self.switch_network_action.setEnabled(False)
            if self._tunnel_url:
                self._refresh_qr()
            else:
                self.ip_label.setText(self.i18n.tr("dashboard.cf_connecting"))

    def _sync_menu_checks(self) -> None:
        """根据当前模式同步菜单勾选状态。"""
        self.act_lan.setChecked(self._mode == TunnelMode.LAN)
        self.act_cf.setChecked(self._mode == TunnelMode.CLOUDFLARE)
        # Cloudflare 模式下禁用明文（none）选项
        self.act_algo_none.setEnabled(self._mode == TunnelMode.LAN)
        # 复选框跟随实际生效的加密状态（CF + 配置 none 实际强制加密）
        eff = effective_algorithm(self._algorithm, self._mode)
        self.act_algo_none.setChecked(eff == "none")
        self.act_algo_encrypted.setChecked(eff == "auto")

    def _on_mode_clicked(self, target_mode: TunnelMode) -> None:
        """点击模式切换菜单项。"""
        if target_mode == self._mode:
            return
        if self._switching:
            self._sync_menu_checks()
            return
        self._switching = True
        self.act_lan.setEnabled(False)
        self.act_cf.setEnabled(False)
        self.ip_label.setText(self.i18n.tr("dashboard.switching"))
        self._mode = target_mode
        set_mode(target_mode)
        self._apply_mode_ui()
        if self._mode_switch_callback:
            self._mode_switch_callback(target_mode)

    def _on_algorithm_clicked(self, algo: str) -> None:
        """点击加密开关菜单项（"none" 不加密 / "auto" 加密，具体算法由客户端协商）。"""
        if algo == self._algorithm:
            return
        # Cloudflare 模式拒绝明文，防止明文 token 在公网泄漏
        if algo == "none" and self._mode == TunnelMode.CLOUDFLARE:
            # QActionGroup exclusive 会自动勾选 none，恢复到实际生效的勾选状态
            self._sync_menu_checks()
            return
        self._algorithm = algo
        self.sm.set("e2ee_algorithm", algo)
        if self._algorithm_change_callback:
            self._algorithm_change_callback(algo)
        self._refresh_qr()
        self.update_connection_status(self.connected)
        self._sync_menu_checks()

    def on_switch_completed(self) -> None:
        """模式切换完成（成功或失败），恢复菜单可用状态。"""
        self._switching = False
        self.act_lan.setEnabled(True)
        self.act_cf.setEnabled(True)
        self._sync_menu_checks()
        self._apply_mode_ui()

    def _get_qr_url(self) -> str:
        """获取当前 QR 码 URL（含 PC 公钥 fragment）。"""
        if self._mode == TunnelMode.CLOUDFLARE and self._tunnel_url:
            url = self._tunnel_url
        else:
            url = f"http://{self._lan_ip}:{self._lan_port}"
        if self._secure_channel:
            url = self._secure_channel.append_to_url(url)
        return url

    def _refresh_qr(self) -> None:
        """刷新 QR 码和地址栏。"""
        if self._switching:
            return
        url = self._get_qr_url()
        self.qr_label.setPixmap(make_qr_pixmap(url))
        self.ip_label.setText(url)

    def update_tunnel_url(self, url: Optional[str]) -> None:
        """更新隧道 URL（Cloudflare 模式下更新二维码和地址）。

        收到有效 URL 意味着隧道已建立，自动结束切换状态。
        """
        self._tunnel_url = url
        if self._mode == TunnelMode.CLOUDFLARE:
            if url:
                if self._switching:
                    self.on_switch_completed()
                else:
                    self._refresh_qr()
            else:
                self.ip_label.setText("Cloudflare: " + self.i18n.tr("dashboard.status_disconnected"))

    def get_mode(self) -> TunnelMode:
        """返回当前模式。"""
        return self._mode

    def _setup_menu(self):
        menubar = self.menuBar()
        menubar.setNativeMenuBar(False)

        program_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_program"))
        network_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_network"))
        help_menu = menubar.addMenu(self.i18n.tr("dashboard.menu_help"))

        # 偏好设置
        settings_action = QAction(self.i18n.tr("dashboard.menu_action"), self)
        settings_action.triggered.connect(self._open_settings)
        program_menu.addAction(settings_action)

        # 命令配置
        commands_action = QAction(self.i18n.tr("dashboard.menu_command"), self)
        commands_action.triggered.connect(self._open_commands_dialog)
        program_menu.addAction(commands_action)

        # 分隔线 + 退出
        program_menu.addSeparator()
        exit_action = QAction(self.i18n.tr("dashboard.menu_exit"), self)
        exit_action.triggered.connect(self._quit_app)
        program_menu.addAction(exit_action)

        # 网络菜单 - 模式切换
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)

        self.act_lan = QAction(self.i18n.tr("dashboard.btn_lan"), self)
        self.act_lan.setCheckable(True)
        self.act_lan.setChecked(self._mode == TunnelMode.LAN)
        self.act_lan.triggered.connect(lambda: self._on_mode_clicked(TunnelMode.LAN))
        mode_group.addAction(self.act_lan)
        network_menu.addAction(self.act_lan)

        self.act_cf = QAction(self.i18n.tr("dashboard.btn_cloudflare"), self)
        self.act_cf.setCheckable(True)
        self.act_cf.setChecked(self._mode == TunnelMode.CLOUDFLARE)
        self.act_cf.triggered.connect(lambda: self._on_mode_clicked(TunnelMode.CLOUDFLARE))
        mode_group.addAction(self.act_cf)
        network_menu.addAction(self.act_cf)

        network_menu.addSeparator()

        # 加密方式平铺：只暴露加密/不加密，具体算法由客户端从 a= 列表协商
        algo_group = QActionGroup(self)
        algo_group.setExclusive(True)

        self.act_algo_none = QAction(self.i18n.tr("dashboard.algo_none"), self)
        self.act_algo_none.setCheckable(True)
        self.act_algo_none.triggered.connect(lambda: self._on_algorithm_clicked("none"))
        algo_group.addAction(self.act_algo_none)
        network_menu.addAction(self.act_algo_none)

        self.act_algo_encrypted = QAction(self.i18n.tr("dashboard.algo_encrypted"), self)
        self.act_algo_encrypted.setCheckable(True)
        self.act_algo_encrypted.triggered.connect(lambda: self._on_algorithm_clicked("auto"))
        algo_group.addAction(self.act_algo_encrypted)
        network_menu.addAction(self.act_algo_encrypted)

        network_menu.addSeparator()

        # 切换网络地址（仅局域网模式可用）
        self.switch_network_action = QAction(self.i18n.tr("dashboard.menu_switch_network"), self)
        self.switch_network_action.triggered.connect(self._on_switch_network)
        self.switch_network_action.setEnabled(self._mode == TunnelMode.LAN)
        network_menu.addAction(self.switch_network_action)

        # 初始化勾选状态（包括 CF 模式下 none 强制变为加密的显示）
        self._sync_menu_checks()

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
        self._lan_ip = ip
        self._lan_port = port
        self._refresh_qr()

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

    def _algo_display_name(self, algo: str) -> str:
        """算法显示名：优先取 locale，缺失时回退为原始算法名。"""
        key = f"dashboard.algo_{algo}"
        tr = self.i18n.tr(key)
        return tr if tr != key else algo

    def update_connection_status(self, connected: bool, algorithm: Optional[str] = None) -> None:
        self.connected = connected
        # algorithm 仅在 connect 事件中携带；断开时清空协商结果
        if connected and algorithm is not None:
            self._negotiated_algo = algorithm
        elif not connected:
            self._negotiated_algo = None
        if connected:
            text = '<span style="color:green;">●</span> ' + self.i18n.tr("dashboard.status_connected")
            if effective_algorithm(self._algorithm, self._mode) == "none":
                text += ' <span style="color:#666;">| ' + self.i18n.tr("dashboard.status_plaintext") + '</span>'
            elif self._negotiated_algo and self._negotiated_algo != "none":
                # 算法由客户端协商决定，状态栏透明展示实际算法
                algo_display = self._algo_display_name(self._negotiated_algo)
                text += ' <span style="color:#666;">| ' + self.i18n.tr(
                    "dashboard.status_encrypted_algo", algo=algo_display) + '</span>'
            else:
                # 尚未收到协商结果（如模式切换后状态刷新）时退化为通用文案
                text += ' <span style="color:#666;">| ' + self.i18n.tr("dashboard.status_encrypted") + '</span>'
            self.status_label.setText(text)
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
