# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 aiohttp 作为 ASGI 服务器框架。
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

from aiohttp import web, WSMsgType

from phonemic.bridge_interface import EventBridge
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.utils.paths import get_res_path, is_frozen
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
        self.active_session = None
        self.bridge = bridge

        # 配置热重载支持
        self.sm = SettingsManager.instance()
        self.max_records = self.sm.get("mobile_max_records", 10)
        self.sm.connect_changed("mobile_max_records", self._on_max_records_changed)

    def _on_max_records_changed(self, new_value: int) -> None:
        self.max_records = new_value
        logger.info(f"Mobile max records updated to {new_value}")
        push_config("mobile_max_records", self.max_records)

    async def connect(self, ws: web.WebSocketResponse, session) -> None:
        """
        注册新的 WebSocket 连接。

        只应在该连接握手成功后调用：已认证的新连接才会抢占当前活动连接，
        握手中或认证失败的连接不会踢掉旧连接，避免把活动连接降级为明文。

        Args:
            ws: 已通过握手的 WebSocket 连接
            session: 该连接对应的 SecureSession，用于后续消息加解密
        """
        if self.active_websocket is not None:
            old_ws = self.active_websocket
            self.active_websocket = None
            self.active_session = None
            try:
                await old_ws.close(code=1000)
                self.bridge.emit("disconnect")
                logger.info("Old WebSocket connection replaced, disconnect event sent.")
            except Exception as e:
                logger.warning(f"Error closing old connection: {e}")

        self.active_websocket = ws
        self.active_session = session
        self.bridge.emit("connect")
        logger.info("WebSocket connected, connection established")

        # 发送当前配置（加密传输）
        try:
            await _send_json(ws, {
                "type": "config",
                "mobile_max_records": self.max_records,
            })
            logger.debug(f"Sent config to client: max_records={self.max_records}")
        except Exception as e:
            logger.warning(f"Failed to send initial config: {e}")

    def session_for(self, ws: web.WebSocketResponse):
        """返回该连接对应的 SecureSession，非活动连接返回 None。"""
        if self.active_websocket is ws:
            return self.active_session
        return None

    def disconnect(self, ws: web.WebSocketResponse) -> None:
        """
        清理连接状态，并通知主进程断开事件。
        仅当断开的连接是当前活动连接时才发送事件，以防止重复。
        """
        if self.active_websocket is ws:
            self.active_websocket = None
            self.active_session = None
            self.bridge.emit("disconnect")
            logger.info("Active WebSocket disconnected, event sent.")


# 全局通信管理（用于与主进程通信）
_manager: Optional[ConnectionManager] = None

# 安全通道（所有模式共用，PC 密钥对在启动时生成一次）
_secure_channel: Optional[SecureChannel] = None


def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message bridge set for backend service")


def set_secure_channel(sc: SecureChannel) -> None:
    """设置安全通道引用。"""
    global _secure_channel
    _secure_channel = sc


async def _send_json(ws: web.WebSocketResponse, message: dict) -> None:
    """发送 JSON 消息，按该连接自身的会话状态决定是否加密。

    加解密上下文取自连接自己的 SecureSession，而非共享对象，
    因此处于握手中的新连接不会改变活动连接的加密状态。
    """
    session = _manager.session_for(ws) if _manager else None
    if session is not None and session.is_authenticated:
        message = session.wrap(message)
    await ws.send_str(json.dumps(message, ensure_ascii=False))


# ---------- 公共 API：向手机端推送消息 ----------

def send_to_phone(message: dict) -> bool:
    """
    向已连接的手机端推送任意 JSON 消息。
    线程安全，可从主线程（Qt 回调）或任意线程调用。

    Args:
        message: 要发送的 JSON 消息字典

    Returns:
        True 如果消息已调度发送，False 如果没有连接或调度失败
    """
    if _manager is None or _manager.active_websocket is None:
        return False
    if _event_loop is None:
        return False

    async def _send():
        try:
            await _send_json(_manager.active_websocket, message)
            logger.debug(f"Pushed message to phone: {message.get('type', 'unknown')}")
        except Exception as e:
            logger.warning(f"Failed to push message to phone: {e}")

    asyncio.run_coroutine_threadsafe(_send(), _event_loop)
    return True


def push_config(key: str, value) -> bool:
    """
    向已连接的手机端推送配置更新。

    Args:
        key: 配置键名（如 "mobile_max_records"）
        value: 配置值

    Returns:
        True 如果消息已调度发送
    """
    return send_to_phone({"type": "config", key: value})


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

    async def test(request):
        """
        返回手机输入测试页面（test.html）。
        若模板文件不存在，则返回错误提示。
        """
        html_path = get_res_path("test.html")
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html = f.read()
            return web.Response(text=html, content_type='text/html', charset='utf-8')
        except Exception as e:
            logger.error(f"Failed to load test.html: {e}")
            return web.Response(
                text='<h3>Error: test.html not found. Please check resources/ directory.</h3>',
                status=404,
                content_type='text/html'
            )

    async def favicon(request):
        """返回 favicon"""
        favicon_path = get_res_path("favicon.ico")
        return web.FileResponse(favicon_path, headers={'Content-Type': 'image/x-icon'})

    async def sodium_js(request):
        """返回 libsodium.js（浏览器端加密库），支持 gzip"""
        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding:
            gz_path = get_res_path("sodium.js.gz")
            if os.path.exists(gz_path):
                return web.FileResponse(
                    gz_path,
                    headers={
                        'Content-Type': 'application/javascript',
                        'Content-Encoding': 'gzip',
                    }
                )
        sodium_path = get_res_path("sodium.js")
        return web.FileResponse(
            sodium_path,
            headers={'Content-Type': 'application/javascript'}
        )

    async def crypto_providers_js(request):
        """返回 crypto_providers.js（加密提供者类）"""
        path = get_res_path("crypto_providers.js")
        return web.FileResponse(
            path,
            headers={'Content-Type': 'application/javascript'}
        )

    async def websocket_endpoint(request):
        """WebSocket 端点：状态机强制握手流程。"""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        if _manager is None:
            logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
            await ws.close(code=1011, message=b"Server not ready")
            return ws

        if _secure_channel is None:
            logger.error("Secure channel not initialized.")
            await ws.close(code=1011, message=b"Server not ready")
            return ws

        # 该连接独立的握手上下文，与活动连接互不干扰
        session = _secure_channel.new_session()

        # ---- S0: 等待 auth（仅在 needs_auth 时）----
        if session.needs_auth:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Auth timeout (10s), closing connection")
                await ws.close()
                return ws
            except Exception as e:
                logger.warning(f"Auth receive error: {e}")
                await ws.close()
                return ws

            if msg.type != WSMsgType.TEXT:
                await ws.close()
                return ws

            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.close()
                return ws

            # S0: 第一条消息必须是 auth
            if data.get("type") != "auth":
                logger.warning(f"Expected auth, got: {data.get('type')}, closing")
                await ws.close()
                return ws

            # 尝试认证；失败时不注册连接，活动连接保持原状
            if not session.receive_auth(data):
                logger.warning(f"Auth failed: {session.reject_reason}, closing")
                ack = session.make_auth_ack()
                await ws.send_str(json.dumps(ack, ensure_ascii=False))
                await ws.close()
                return ws

            # 握手成功：发送 auth_ack
            ack = session.make_auth_ack()
            await ws.send_str(json.dumps(ack, ensure_ascii=False))
            logger.info(f"Auth succeeded, algorithm={ack.get('algo')}, auth_ack sent")

        # ---- S1: 注册连接（认证后才抢占旧连接）+ 发送配置 ----
        await _manager.connect(ws, session)

        # ---- S1: 处理后续消息 ----
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)

                        # S1: 拒绝重复 auth
                        if data.get("type") == "auth":
                            logger.warning("Received auth after authentication, closing")
                            await ws.close()
                            break

                        if session.is_encrypted:
                            # 加密模式：仅接受 data 信封
                            if data.get("type") != "data":
                                logger.warning(f"Expected data, got: {data.get('type')}, closing")
                                await ws.close()
                                break

                            inner = session.unwrap(data)
                            if inner is None:
                                logger.warning("Decryption failed, closing")
                                await ws.close()
                                break
                        else:
                            # none 模式：直接处理明文 JSON
                            inner = data

                        # 处理内部消息
                        msg_type = inner.get("type")
                        text = inner.get("text", "")

                        if msg_type in ("preview", "send"):
                            _manager.bridge.emit(msg_type, text)
                            logger.debug(f"Received {msg_type}: {text[:50]}...")
                        else:
                            logger.warning(f"Unknown inner message type: {msg_type}")

                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON: {msg.data}, error: {e}")
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
    if not is_frozen():
        app.router.add_get('/test', test)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_get('/sodium.js', sodium_js)
    app.router.add_get('/crypto_providers.js', crypto_providers_js)
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
    _event_loop = None
    _serve_task = None
    _runner = None

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
    global _event_loop, _serve_task, _server_thread
    if _event_loop and _serve_task:
        try:
            _event_loop.call_soon_threadsafe(_serve_task.cancel)
        except RuntimeError:
            pass
    if _server_thread:
        _server_thread.join(timeout=5.0)
    _event_loop = None
    _serve_task = None
    _server_thread = None


def restart_server(host: str, port: int, bridge: EventBridge) -> None:
    """重启服务端，切换绑定地址。"""
    stop_server()
    time.sleep(0.5)
    start_server(host, port, bridge)


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
