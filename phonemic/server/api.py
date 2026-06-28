# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 FastAPI + Uvicorn，通过 multiprocessing.Queue 与主进程（Flet UI）通信。
"""

import json
import logging
import sys
import threading
from typing import Optional
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import copy

from phonemic.bridge_interface import EventBridge
from phonemic.utils.paths import get_res_path, is_frozen
from phonemic.utils.settings_manager import SettingsManager
from phonemic.utils.i18n import I18n

# 配置日志
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
        """
        Args:
            bridge: 用于向主进程发送事件
        """
        self.active_connection: Optional[WebSocket] = None
        self.bridge = bridge

        # 配置热重载支持
        self.sm = SettingsManager.instance()
        self.max_records = self.sm.get("mobile_max_records", 10)
        # 监听配置变更
        self.sm.connect_changed("mobile_max_records", self._on_max_records_changed)

    def _on_max_records_changed(self, new_value: int) -> None:
        """当 mobile_max_records 配置变更时更新内存值（无需重启）"""
        self.max_records = new_value
        logger.info(f"Mobile max records updated to {new_value}")

    async def connect(self, websocket: WebSocket) -> None:
        """
        接受新的 WebSocket 连接。
        如果已有连接，先关闭旧连接（保证同时只有一个手机连接）。
        """
        # 关闭已有连接（若有）
        if self.active_connection is not None:
            old_ws = self.active_connection
            self.active_connection = None  # 立即标记旧连接不再活动
            try:
                await old_ws.close(code=1000, reason="New connection replaces old one")
                # 显式发送断开连接事件
                self.bridge.emit("disconnect")
                logger.info("Old WebSocket connection replaced, disconnect event sent.")
            except Exception as e:
                logger.warning(f"Error closing old connection: {e}")

        await websocket.accept()
        self.active_connection = websocket
        self.bridge.emit("connect")
        logger.info("WebSocket connected, connection established")

        # 发送当前配置（手机端初始化使用）
        try:
            await websocket.send_json({
                "type": "config",
                "mobile_max_records": self.max_records
            })
            logger.debug(f"Sent config to client: max_records={self.max_records}")
        except Exception as e:
            logger.warning(f"Failed to send initial config: {e}")

    def disconnect(self, websocket: WebSocket) -> None:
        """
        清理连接状态，并通知主进程断开事件。
        仅当断开的连接是当前活动连接时才发送事件，以防止重复。
        """
        if self.active_connection is websocket:
            self.active_connection = None
            self.bridge.emit("disconnect")
            logger.info("Active WebSocket disconnected, event sent.")

    async def receive_loop(self, websocket: WebSocket) -> None:
        """
        持续接收 WebSocket 消息，解析 JSON 并推送到队列。
        遇到异常（断开、JSON 错误）时自动断开连接。
        """
        try:
            while True:
                raw_data = await websocket.receive_text()
                try:
                    message = json.loads(raw_data)
                    msg_type = message.get("type")
                    text = message.get("text", "")

                    if msg_type in ("preview", "send"):
                        self.bridge.emit(msg_type, text)
                        logger.debug(f"Received {msg_type}: {text[:50]}...")
                    else:
                        logger.warning(f"Unknown message type: {msg_type}")
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON: {raw_data}, error: {e}")
                    # 不关闭连接，继续接收下一条
        except WebSocketDisconnect:
            self.disconnect(websocket)
        except Exception as e:
            logger.exception(f"Unexpected error in receive_loop: {e}")
            self.disconnect(websocket)


# 初始化 FastAPI 应用
app = FastAPI(title="PhoneMic Backend", description="手机语音输入桥接服务")

templates = Jinja2Templates(directory=get_res_path(""))

# 全局通信管理（用于与主进程通信）
_manager: Optional[ConnectionManager] = None

def set_bridge(bridge: EventBridge) -> None:
    """
    设置进程通信队列（需在启动服务前调用）。
    """
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message queue set for backend service")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(get_res_path("favicon.ico"))

@app.get("/", response_class=HTMLResponse)
async def get_mobile(req: Request) -> HTMLResponse:
    """
    返回手机端聊天页面（mobile.html）。
    若模板文件不存在，则返回错误提示。
    """
    try:
        return templates.TemplateResponse(
            request=req,
            name="mobile.html",
            context={"__I18N__": I18n.instance().get_section("mobile")}
        )
    except Exception as e:
        logger.error(f"Failed to load mobile.html: {e}")
        return HTMLResponse(
            content="<h3>Error: mobile.html not found. Please check resources/ directory.</h3>",
            status_code=404,
        )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket 端点，处理手机端的实时消息。
    """
    if _manager is None:
        logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
        await websocket.close(code=1011, reason="Server not ready")
        return

    await _manager.connect(websocket)
    await _manager.receive_loop(websocket)


def run_server(host: str, port: int = 7979, bridge: Optional[EventBridge] = None) -> None:
    """
    启动 FastAPI 服务（阻塞运行，通常放在独立线程中）。

    Args:
        host: 绑定的 IP 地址（例如 "192.168.1.100"），不能是 "0.0.0.0"
        port: 监听端口，默认 7979
        bridge: EventBridge实例，若不通过 set_bridge 预设置则在此传入
    """
    if _manager is not None:
        set_bridge(bridge)
    elif _manager is None:
        raise RuntimeError("Bridge must be provided either via set_bridge() or run_server(bridge=...)")

    logger.info(f"Starting PhoneMic backend server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


# ---------- 线程管理（用于启动/停止服务）----------
_server: Optional[uvicorn.Server] = None
_server_thread: Optional[threading.Thread] = None

# PyInstaller-safe logging config
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "use_colors": False,
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def start_server(host: str, port: int, bridge: EventBridge) -> None:
    global _server, _server_thread
    set_bridge(bridge)   # 确保队列已设置

    is_packaged = is_frozen()
    log_config = LOGGING_CONFIG if is_packaged else None

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=log_config,
        log_level="info",
        loop="asyncio"
    )
    _server = uvicorn.Server(config)
    _server_thread = threading.Thread(target=_server.run, daemon=True)
    _server_thread.start()

def stop_server() -> None:
    global _server
    if _server:
        _server.should_exit = True
        _server_thread.join(timeout=2.0)