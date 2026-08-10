# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 aiohttp 作为 ASGI 服务器框架。
"""

import asyncio
import json
import logging
import threading
from typing import Optional

from aiohttp import web, WSMsgType

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

    async def connect(self, ws: web.WebSocketResponse) -> None:
        """
        注册新的 WebSocket 连接。
        如果已有连接，先关闭旧连接（保证同时只有一个手机连接）。
        """
        if self.active_websocket is not None:
            old_ws = self.active_websocket
            self.active_websocket = None
            try:
                await old_ws.close(code=1000)
                self.bridge.emit("disconnect")
                logger.info("Old WebSocket connection replaced, disconnect event sent.")
            except Exception as e:
                logger.warning(f"Error closing old connection: {e}")

        self.active_websocket = ws
        self.bridge.emit("connect")
        logger.info("WebSocket connected, connection established")

        # 发送当前配置（手机端初始化使用）
        try:
            await ws.send_str(json.dumps({
                "type": "config",
                "mobile_max_records": self.max_records
            }))
            logger.debug(f"Sent config to client: max_records={self.max_records}")
        except Exception as e:
            logger.warning(f"Failed to send initial config: {e}")

    def disconnect(self, ws: web.WebSocketResponse) -> None:
        """
        清理连接状态，并通知主进程断开事件。
        仅当断开的连接是当前活动连接时才发送事件，以防止重复。
        """
        if self.active_websocket is ws:
            self.active_websocket = None
            self.bridge.emit("disconnect")
            logger.info("Active WebSocket disconnected, event sent.")


# 全局通信管理（用于与主进程通信）
_manager: Optional[ConnectionManager] = None

def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message bridge set for backend service")


def _create_app() -> web.Application:
    """
    创建 aiohttp Application 实例并注册路由。
    每次调用创建新实例，避免多次启停时共享状态被污染。
    """
    app = web.Application()

    async def index(request):
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
            return web.Response(text=html, content_type='text/html', charset='utf-8')
        except Exception as e:
            logger.error(f"Failed to load mobile.html: {e}")
            return web.Response(
                text='<h3>Error: mobile.html not found. Please check resources/ directory.</h3>',
                status=404,
                content_type='text/html'
            )

    async def favicon(request):
        """返回 favicon"""
        favicon_path = get_res_path("favicon.ico")
        return web.FileResponse(favicon_path, headers={'Content-Type': 'image/x-icon'})

    async def websocket_endpoint(request):
        """WebSocket 端点，处理手机端的实时消息。"""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        if _manager is None:
            logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
            await ws.close(code=1011, message=b"Server not ready")
            return ws

        await _manager.connect(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type")
                        text = data.get("text", "")

                        if msg_type in ("preview", "send"):
                            _manager.bridge.emit(msg_type, text)
                            logger.debug(f"Received {msg_type}: {text[:50]}...")
                        else:
                            logger.warning(f"Unknown message type: {msg_type}")
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON: {msg.data}, error: {e}")
                        # 不关闭连接，继续接收下一条
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    logger.info(f"WebSocket closing, type={msg.type}")
                    break
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        except Exception as e:
            logger.exception(f"Unexpected error in receive_loop: {e}")
        finally:
            _manager.disconnect(ws)

        return ws

    app.router.add_get('/', index)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_get('/ws', websocket_endpoint)

    return app


# ---------- 线程管理（用于启动/停止服务）----------
_server_thread: Optional[threading.Thread] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_serve_task = None
_runner: Optional[web.AppRunner] = None
_running_app: Optional[web.Application] = None


def start_server(host: str, port: int, bridge: EventBridge) -> None:
    """在后台线程中启动 aiohttp 服务（非阻塞）。"""
    global _server_thread, _event_loop, _serve_task, _runner, _running_app
    set_bridge(bridge)
    _running_app = _create_app()

    def _run():
        global _event_loop, _serve_task, _runner

        async def _start():
            global _runner
            _runner = web.AppRunner(_running_app)
            await _runner.setup()
            site = web.TCPSite(_runner, host, port)
            await site.start()
            logger.info(f"Serving on http://{host}:{port}")
            # 阻塞直到被取消
            await asyncio.Event().wait()

        _event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_event_loop)
        _serve_task = _event_loop.create_task(_start())
        try:
            _event_loop.run_until_complete(_serve_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            # 清理 AppRunner
            if _runner is not None:
                try:
                    _event_loop.run_until_complete(_runner.cleanup())
                except Exception:
                    pass
                _runner = None
            try:
                _event_loop.stop()
            except Exception:
                pass
            try:
                _event_loop.close()
            except Exception:
                pass

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

    run_app = _create_app()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _start_blocking():
        runner = web.AppRunner(run_app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Serving on http://{host}:{port}")
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(_start_blocking())
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        try:
            loop.stop()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
