import argparse
import logging
import multiprocessing
import sys
import ctypes
import threading
import time
from typing import Any, Optional

import urllib.request
import urllib.error
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from phonemic.bridge_qt import QtEventBridge
from phonemic.bridge_queue import QueueEventBridge
from phonemic.gui.dashboard import Dashboard
from phonemic.gui.hud import HudWindow
from phonemic.gui.ip_selector import select_lan_ip
from phonemic.gui.keyboard import flash_insert
from phonemic.gui.tray import SystemTray
from phonemic.server.api import start_server, stop_server
from phonemic.utils.network import get_all_lan_ips, find_free_port, find_candidate_by_mac
from phonemic.utils.paths import get_res_path
from phonemic.utils.i18n import I18n
from phonemic.utils.command_processor import CommandInterceptor
from phonemic.utils.settings_manager import SettingsManager

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
            args=(queue,),
            daemon=True
        )
        self.monitor_thread.start()


def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    """等待服务器就绪，返回是否成功"""
    url = f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=0.5)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def parse_args():
    parser = argparse.ArgumentParser(description="PhoneMic - Voice input bridge from phone to PC")
    parser.add_argument("--silent", action="store_true", help="Start minimized to system tray (no main window)")
    parser.add_argument("--select-mode", choices=["auto", "last", "ask"],
                        help="Override network selection strategy: auto=best, last=previous, ask=prompt")
    return parser.parse_args()


def main():
    args = parse_args()

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    app_id = "PhoneMic"
    app.setApplicationName(app_id)
    app.setApplicationDisplayName(app_id)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass
    app.setWindowIcon(QIcon(get_res_path("favicon.ico")))

    # 配置管理器
    sm = SettingsManager.instance()
    i18n = I18n.instance()

    # 1. 确定网络选择模式
    mode_override = args.select_mode
    if mode_override:
        # 命令行覆盖，但不保存到配置（本次生效）
        mode = mode_override
    else:
        mode = sm.get("network_selection_mode", "ask")
    last_mac = sm.get("last_network_mac", None)

    # 2. 根据模式选择 IP
    candidates = get_all_lan_ips()
    if not candidates:
        QMessageBox.critical(None, i18n.tr("error.title"), i18n.tr("error.no_lan_ip"))
        sys.exit(1)

    selected_ip = None
    selected_mac = None

    if mode == "auto":
        # 自动选择最佳（第一个候选）
        selected_ip = candidates[0].ip
        selected_mac = candidates[0].mac
    elif mode == "last":
        # 尝试用上次的 MAC 匹配
        if last_mac:
            matched = find_candidate_by_mac(last_mac, candidates)
            if matched:
                selected_ip = matched.ip
                selected_mac = matched.mac
            else:
                logger.warning(i18n.tr("log.last_mac_not_found"))
                selected_ip = candidates[0].ip
                selected_mac = candidates[0].mac
        else:
            # 没有上次记录，回退到 auto
            selected_ip = candidates[0].ip
            selected_mac = candidates[0].mac
    else:  # "ask"
        ip, mac = select_lan_ip()
        if ip is None:
            # 用户取消，退出
            sys.exit(0)
        selected_ip = ip
        selected_mac = mac
    # 保存 MAC（如果非空）到 last_network_mac，以便 last 模式后续使用
    if selected_mac:
        sm.set("last_network_mac", selected_mac)

    # 3. 查找端口
    actual_port = find_free_port(start_port=12000)
    if actual_port is None:
        QMessageBox.critical(None, i18n.tr("error.title"), i18n.tr("error.no_free_port"))
        sys.exit(1)

    logger.info(i18n.tr("log.selected_ip_port", ip=selected_ip, port=actual_port))

    # 4. 准备后端
    use_queue = False
    bridge = QueueEventBridge(multiprocessing.Queue()) if use_queue else QtEventBridge()

    start_server(selected_ip, actual_port, bridge)
    if not wait_for_server(selected_ip, actual_port):
        QMessageBox.critical(None, i18n.tr("error.title"), i18n.tr("error.server_timeout", port=actual_port))
        sys.exit(1)

    # 5. 创建 Dashboard 和 Tray
    dashboard = Dashboard(selected_ip, actual_port)
    hud = HudWindow()
    tray = SystemTray(dashboard, get_res_path("favicon.ico"))
    dashboard.tray = tray

    if args.silent:
        dashboard.hide()
    else:
        dashboard.show()

    # 注册网络切换回调
    def restart_network():
        nonlocal selected_ip, actual_port, selected_mac
        candidates_now = get_all_lan_ips()
        if len(candidates_now) <= 1:
            QMessageBox.information(dashboard, i18n.tr("info.title"), i18n.tr("info.single_network_no_switch"))
            return

        stop_server()
        new_ip, new_mac = select_lan_ip(dashboard)
        if new_ip is None:
            start_server(selected_ip, actual_port, bridge)
            wait_for_server(selected_ip, actual_port)
            return
        if new_ip == selected_ip:
            start_server(selected_ip, actual_port, bridge)
            wait_for_server(selected_ip, actual_port)
            return

        old_ip = selected_ip
        selected_ip = new_ip
        selected_mac = new_mac
        start_server(selected_ip, actual_port, bridge)
        if not wait_for_server(selected_ip, actual_port):
            QMessageBox.critical(dashboard, i18n.tr("error.title"), i18n.tr("error.switch_network_fail", old_ip=old_ip))
            selected_ip = old_ip
            start_server(selected_ip, actual_port, bridge)
            wait_for_server(selected_ip, actual_port)
        else:
            dashboard.update_network(selected_ip, actual_port)
            if selected_mac:
                sm.set("last_network_mac", selected_mac)
            QMessageBox.information(dashboard, i18n.tr("info.title"), i18n.tr("info.switch_network_success", ip=selected_ip))

    dashboard.set_restart_network_callback(restart_network)

    # 6. 事件处理
    command_interceptor = CommandInterceptor()

    def on_backend_event(event_type: str, payload: Any):
        if event_type == "preview":
            hud.on_preview_text(payload)
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

    if use_queue:
        queue_signals = QueueSignals()
        queue_signals.start_thread_pool_queue(bridge.queue)
        queue_signals.event_signal.connect(on_backend_event)
    else:
        bridge.event_signal.connect(on_backend_event)

    # 7. 退出清理
    def on_quit():
        logger.info("Shutting down...")
        stop_server()

    app.aboutToQuit.connect(on_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()