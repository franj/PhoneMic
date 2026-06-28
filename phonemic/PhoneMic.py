import logging
import multiprocessing
import sys
import ctypes
import threading
import time
from pathlib import Path
from typing import Any

import requests
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox

from phonemic.bridge_qt import QtEventBridge
from phonemic.bridge_queue import QueueEventBridge
from phonemic.gui.dashboard import Dashboard
from phonemic.gui.hud import HudWindow, get_hud_signals
from phonemic.gui.ip_selector import IpSelector
from phonemic.gui.keyboard import flash_insert
from phonemic.gui.tray import SystemTray
from phonemic.server.api import start_server, stop_server  # 待确认函数名
from phonemic.utils.network import get_all_lan_ips, find_free_port
from phonemic.utils.paths import get_res_path
from phonemic.utils.i18n import I18n
from phonemic.utils.command_processor import CommandInterceptor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class QueueSignals(QObject):
    event_signal = Signal(str, object)

    def queue_monitor(self, queue: multiprocessing.Queue):
        while True:
            try:
                event, data = queue.get()
                self.event_signal.emit(event, data)
            except Exception as e:
                logger.error(f"Monitor error: {e}")
    def start_thread_pool_queue(self, queue: multiprocessing.Queue):        
        self.monitor_thread = threading.Thread(
            target=self.queue_monitor,
            args=(queue),
            daemon=True
        )
        self.monitor_thread.start()

def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    """等待服务器就绪，返回是否成功"""
    url = f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(url, timeout=0.5)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.2)
    return False

def main():
    # 尝试获取已有的 QApplication 实例，在pytest-qt的情况下，QApplication通常已经初始化实例
    app = QApplication.instance()
    if app is None:
        # 如果还没有实例，则创建一个新的 (生产环境运行时的正常情况)
        app = QApplication(sys.argv)
    #app.setQuitOnLastWindowClosed(False)
    # 设置应用名称（影响托盘气泡标题、任务管理器等）
    app_id = "PhoneMic"
    app.setApplicationName(app_id)          # 系统托盘气泡标题
    app.setApplicationDisplayName(app_id)   # 任务管理器/窗口标题（可选）
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    app.setWindowIcon(QIcon(get_res_path("favicon.ico")))

    i18n = I18n.instance()

    # 1. 获取IP候选
    candidates = get_all_lan_ips()
    if not candidates:
        QMessageBox.critical(None, "错误", "未检测到可用局域网IP，程序将退出。")
        sys.exit(1)
    elif len(candidates) == 1:
        selected_ip = candidates[0].ip
    else:
        selector = IpSelector(candidates)
        if selector.exec() != IpSelector.Accepted:
            sys.exit(0)
        selected_ip = selector.get_selected_ip()
        if not selected_ip:
            sys.exit(0)

    logger.info(f"Selected IP: {selected_ip}")

    # 2. 查找可用端口并启动后端
    actual_port = find_free_port(start_port = 12000)
    if actual_port is None:
        QMessageBox.critical(None, "错误", "未找到可用端口（从 12000 开始），程序将退出。")
        sys.exit(1)
    
    logger.info(f"Using port: {actual_port}")

    # 3. 准备后端通信
    use_queue = False
    if use_queue:
        bridge = QueueEventBridge(multiprocessing.Queue())
    else:
        bridge = QtEventBridge()

    # 启动服务器线程
    start_server(selected_ip, actual_port, bridge)

    if not wait_for_server(selected_ip, actual_port):
        QMessageBox.critical(None, "错误", f"服务器启动超时，请检查端口 {actual_port} 是否可用。")
        sys.exit(1)

    # 4. 创建主界面并连接信号
    dashboard = Dashboard(selected_ip, actual_port)
    
    hud = HudWindow()
    dashboard.show()

    # 5. 初始化系统托盘
    tray = SystemTray(dashboard, get_res_path("favicon.ico"))   # 确保路径正确
    # 把 tray 回传给 dashboard（用于显示气泡通知）
    dashboard.tray = tray

    command_interceptor = CommandInterceptor()
    # 6. 启动队列监控
    def on_backend_event(event_type: str, payload: Any):
        if event_type == "preview":
            hud.on_preview_text(payload)   # 需提前获取 hud 实例
        elif event_type == "send":
            if not command_interceptor.process_send_text(payload):
                flash_insert(payload)
            hud.hide()
        elif event_type == "connect":
            dashboard.update_connection_status(True)
            tray.update_connection_status(True)
        elif event_type == "disconnect":
            dashboard.update_connection_status(False)
            tray.update_connection_status(False)
        else:
            logger.warning(f"Unknown event: {event_type}")
    if (use_queue):
        queue_signals = QueueSignals()
        queue_signals.start_thread_pool_queue(bridge.queue)
        queue_signals.event_signal.connect(on_backend_event)
    else:
        bridge.event_signal.connect(on_backend_event)
        pass

    # 5. 退出清理
    def on_quit():
        logger.info("Shutting down...")
        stop_server()
    app.aboutToQuit.connect(on_quit)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()