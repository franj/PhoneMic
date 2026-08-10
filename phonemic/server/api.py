# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 Tremolo（纯 Python，零依赖）替代 FastAPI + Uvicorn。
"""

import asyncio
import json
import logging
import threading
from typing import Optional

from tremolo import Application
from tremolo.exceptions import WebSocketClientClosed

from phonemic.bridge_interface import EventBridge
from phonemic.utils.paths import get_res_path
from phonemic.utils.settings_manager import SettingsManager
from phonemic.utils.i18n import I18n

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    管理单个 WebSocket 连接的生命周期。

    职责：
    - 接受/关闭 WebSocket 连接
    - 循环接收客户端消息并解析为 (type, text) 推送到队列
    - 处理连接断开事件
    - 支持配置热重载（手机端聊天记录上限）
    """

    def __init__(self, bridge: EventBridge):
        self.active_websocket = None
        self.bridge = bridge

        # 配置热重载支持
        self.sm = SettingsManager.instance()
        self.max_records = self.sm.get("mobile_max_records", 10)
        self.sm.connect_changed("mobile_max_records", self._on_max_records_changed)

    def _on_max_records_changed(self, new_value: int) -> None:
        self.max_records = new_value
        logger.info(f"Mobile max records updated to {new_value}")

    async def connect(self, websocket) -> None:
        """
        接受新的 WebSocket 连接。
        如果已有连接，先关闭旧连接（保证同时只有一个手机连接）。
        """
        if self.active_websocket is not None:
            old_ws = self.active_websocket
            self.active_websocket = None
            try:
                await old_ws.close(code=1000, reason="New connection replaces old one")
                self.bridge.emit("disconnect")
                logger.info("Old WebSocket connection replaced, disconnect event sent.")
            except Exception as e:
                logger.warning(f"Error closing old connection: {e}")

        await websocket.accept()
        self.active_websocket = websocket
        self.bridge.emit("connect")
        logger.info("WebSocket connected, connection established")

        # 发送当前配置（手机端初始化使用）
        try:
            await websocket.send(json.dumps({
                "type": "config",
                "mobile_max_records": self.max_records
            }))
            logger.debug(f"Sent config to client: max_records={self.max_records}")
        except Exception as e:
            logger.warning(f"Failed to send initial config: {e}")

    def disconnect(self, websocket) -> None:
        """
        清理连接状态，并通知主进程断开事件。
        仅当断开的连接是当前活动连接时才发送事件，以防止重复。
        """
        if self.active_websocket is websocket:
            self.active_websocket = None
            self.bridge.emit("disconnect")
            logger.info("Active WebSocket disconnected, event sent.")


# 创建 Tremolo 应用
app = Application()

# 全局通信管理（用于与主进程通信）
_manager: Optional[ConnectionManager] = None

def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message bridge set for backend service")


@app.route('/favicon.ico')
async def favicon(response):
    """返回 favicon"""
    await response.sendfile(get_res_path("favicon.ico"), content_type='image/x-icon')
    return True  # 保持 keep-alive


@app.route('/')
async def index(response):
    """
    返回手机端聊天页面（mobile.html）。
    若模板文件不存在，则返回错误提示。
    """
    html_path = get_res_path("mobile.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        # 替换 i18n 占位符为 JSON 数据
        i18n_data = I18n.instance().get_section("mobile")
        html = html.replace("__I18N_JSON__", json.dumps(i18n_data, ensure_ascii=False))
        response.set_content_type(b'text/html; charset=utf-8')
        return html
    except Exception as e:
        logger.error(f"Failed to load mobile.html: {e}")
        response.set_content_type(b'text/html; charset=utf-8')
        response.set_status(404)
        return '<h3>Error: mobile.html not found. Please check resources/ directory.</h3>'


@app.route('/ws')
async def websocket_endpoint(websocket=None):
    """WebSocket 端点，处理手机端的实时消息。"""
    if websocket is None:
        return  # 非 WebSocket 升级请求

    if _manager is None:
        logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
        await websocket.close(code=1011, reason="Server not ready")
        return

    await _manager.connect(websocket)

    try:
        while True:
            message = await websocket.receive()
            try:
                data = json.loads(message)
                msg_type = data.get("type")
                text = data.get("text", "")

                if msg_type in ("preview", "send"):
                    _manager.bridge.emit(msg_type, text)
                    logger.debug(f"Received {msg_type}: {text[:50]}...")
                else:
                    logger.warning(f"Unknown message type: {msg_type}")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {message}, error: {e}")
                # 不关闭连接，继续接收下一条
    except WebSocketClientClosed:
        _manager.disconnect(websocket)
    except Exception as e:
        logger.exception(f"Unexpected error in receive_loop: {e}")
        _manager.disconnect(websocket)


# ---------- 线程管理（用于启动/停止服务）----------
_server_thread: Optional[threading.Thread] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_serve_task = None


def start_server(host: str, port: int, bridge: EventBridge) -> None:
    """在后台线程中启动 Tremolo 服务（非阻塞）。"""
    global _server_thread, _event_loop, _serve_task
    set_bridge(bridge)

    def _run():
        global _event_loop, _serve_task
        _event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_event_loop)
        _serve_task = _event_loop.create_task(app.serve(host, port))
        try:
            _event_loop.run_until_complete(_serve_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            _event_loop.close()

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    logger.info(f"Starting PhoneMic backend server on {host}:{port}")


def stop_server() -> None:
    """停止后台服务。"""
    global _event_loop, _serve_task
    if _event_loop and _serve_task:
        _event_loop.call_soon_threadsafe(_serve_task.cancel)
    if _server_thread:
        _server_thread.join(timeout=5.0)


def run_server(host: str, port: int = 7979, bridge: Optional[EventBridge] = None) -> None:
    """
    阻塞运行服务（用于测试）。
    通常放在独立线程中调用。
    """
    if bridge is not None:
        set_bridge(bridge)
    elif _manager is None:
        raise RuntimeError("Bridge must be provided either via set_bridge() or run_server(bridge=...)")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(app.serve(host, port))
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        loop.close()
