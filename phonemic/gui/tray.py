import logging
import os

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtCore import QObject, Slot, Qt
from phonemic.gui.dashboard import Dashboard
from phonemic.gui.settings_dialog import SettingsDialog
from phonemic.gui.commands_dialog import CommandsDialog
from phonemic.utils.i18n import I18n

logger = logging.getLogger(__name__)

class SystemTray(QObject):
    def __init__(self, dashboard: Dashboard, icon_path: str):
        super().__init__()
        self.dashboard = dashboard
        self.icon_path = icon_path
        self.connected = False
        self.tray_icon = None
        self.i18n = I18n.instance()
        # 最近一次收到的文件所在目录（点击"收到文件"通知时打开）
        self._last_file_dir = None
        self._create_tray()

    def _create_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning(self.i18n.tr("tray.tray_unavailable"))
            return

        # 加载基础图标
        base_icon = QIcon(self.icon_path)
        if base_icon.isNull():
            logger.warning(self.i18n.tr("tray.icon_load_failed", path=self.icon_path))
            base_icon = QIcon.fromTheme("computer")

        self.tray_icon = QSystemTrayIcon(base_icon)
        self.tray_icon.setToolTip(self.i18n.tr("tray.tooltip_disconnected"))

        self.tray_icon.setContextMenu(self._create_tray_menu())
        self.tray_icon.show()

        # 初始化连接状态图标
        self.update_connection_status(False)
        # 连接激活信号（左键单击、右键单击等）
        self.tray_icon.activated.connect(self._on_tray_activated)
        # 点击"收到文件"通知 → 打开所在目录
        self.tray_icon.messageClicked.connect(self._on_message_clicked)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """处理托盘图标的点击事件"""
        if reason == QSystemTrayIcon.Trigger:   # 左键单击
            self.toggle_main_window()

    def toggle_main_window(self):
        """切换主界面的显示/隐藏"""
        if self.dashboard.isVisible():
            self.dashboard.hide()
        else:
            self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()

    def show_main_window(self):
        if self.dashboard.isHidden():
            self.dashboard.show()
            self.dashboard.raise_()
            self.dashboard.activateWindow()

    def _create_status_icon(self, connected: bool) -> QIcon:
        """根据连接状态生成带圆点的图标（包含多分辨率版本）"""
        # 加载基础图标（QIcon 会保留 .ico 文件中的多个分辨率）
        base_icon = QIcon(self.icon_path)
        
        # 创建新图标并添加多个标准尺寸的版本
        icon = QIcon()
        
        # Windows 托盘图标需要的标准尺寸（适配不同 DPI）
        # 16=100%, 20=125%, 24=150%, 32=200%
        sizes = [16, 20, 24, 32, 40, 48, 64]
        
        for size in sizes:
            # 从基础图标获取对应尺寸的 pixmap（Qt 会自动选择最佳分辨率）
            base_pixmap = base_icon.pixmap(size, size)
            
            # 创建透明画布
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            
            # 绘制基础图标
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(0, 0, base_pixmap)
            
            # 绘制状态圆点（尺寸按比例计算，保持在右下角）
            dot_radius = max(3, int(size * 0.234))  # 15/64 ≈ 0.234
            dot_x = size - dot_radius - 1
            dot_y = size - dot_radius - 1
            
            color = QColor(0, 255, 0) if connected else QColor(255, 0, 0)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(dot_x, dot_y, dot_radius, dot_radius)
            painter.end()
            
            icon.addPixmap(pixmap)
        
        return icon

    @Slot(bool)
    def update_connection_status(self, connected: bool):
        self.connected = connected
        if self.tray_icon is None:
            return
        icon = self._create_status_icon(connected)
        self.tray_icon.setIcon(icon)
        if connected:
            tooltip = self.i18n.tr("tray.tooltip_connected")
        else:
            tooltip = self.i18n.tr("tray.tooltip_disconnected")
        self.tray_icon.setToolTip(tooltip)

    def _open_settings(self):
        # 修复 parent 问题，使用 self.dashboard 作为父窗口
        dialog = SettingsDialog(self.dashboard)
        dialog.exec()
    def _open_commands_dialog(self):
        dlg = CommandsDialog(self)
        dlg.exec_()

    def _create_tray_menu(self):
        menu = QMenu()
        menu.addAction(self.i18n.tr("tray.menu_show")).triggered.connect(self.show_main_window)
        menu.addSeparator()
        menu.addAction(self.i18n.tr("tray.menu_settings")).triggered.connect(self._open_settings)
        menu.addAction(self.i18n.tr("dashboard.menu_command")).triggered.connect(self._open_commands_dialog)
        menu.addSeparator()
        menu.addAction(self.i18n.tr("tray.menu_about")).triggered.connect(self.dashboard.show_about)
        menu.addAction(self.i18n.tr("tray.menu_quit")).triggered.connect(self.dashboard._quit_app)
        return menu

    def show_message(self, title: str, message: str, icon=QSystemTrayIcon.Information, timeout: int = 1000,
                     open_dir = None):
        self._last_file_dir = open_dir
        if self.tray_icon and self.tray_icon.supportsMessages():
            self.tray_icon.showMessage(title, message, icon, timeout)

    def notify_file_saved(self, path: str, name: str) -> None:
        """手机端传来文件已落盘：弹托盘通知，点击通知打开所在目录。"""
        if not self.tray_icon:
            return
        self.show_message(
            self.i18n.tr("tray.file_saved_title"),
            self.i18n.tr("tray.file_saved_msg", name=name),
            QSystemTrayIcon.Information,
            timeout=5000,
            open_dir = os.path.dirname(path) or None
        )

    def notify_photo_copied(self, name: str) -> None:
        """手机端图片已写入剪贴板：弹托盘通知（无目录可打开，点击不动作）。"""
        if not self.tray_icon:
            return
        self.show_message(
            self.i18n.tr("tray.photo_copied_title"),
            self.i18n.tr("tray.photo_copied_msg", name=name or ""),
            QSystemTrayIcon.Information,
            timeout=5000,
        )

    def notify_photo_failed(self, name: str) -> None:
        """图片字节无法写入剪贴板（解码失败/剪贴板不可用）。"""
        if not self.tray_icon:
            return
        self.show_message(
            self.i18n.tr("tray.photo_failed_title"),
            self.i18n.tr("tray.photo_failed_msg", name=name or ""),
            QSystemTrayIcon.Warning,
            timeout=5000,
        )

    def _on_message_clicked(self):
        """点击托盘通知：若是文件通知则打开文件所在目录。"""
        if self._last_file_dir and os.path.isdir(self._last_file_dir):
            try:
                os.startfile(self._last_file_dir)  # Windows 资源管理器打开
            except Exception as e:
                logger.exception(f"打开文件目录失败: {e}")
