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
from phonemic.tunnel.e2ee import E2EEManager
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
        self.bridge = bridge

        # 配置热重载支持
        self.sm = SettingsManager.instance()
        self.max_records = self.sm.get("mobile_max_records", 10)
        self.sm.connect_changed("mobile_max_records", self._on_max_records_changed)

    def _on_max_records_changed(self, new_value: int) -> None:
        self.max_records = new_value
        logger.info(f"Mobile max records updated to {new_value}")
        push_config("mobile_max_records", self.max_records)

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
            await _send_json(ws, {
                "type": "config",
                "mobile_max_records": self.max_records,
                "e2ee_enabled": _e2ee_manager.enabled if _e2ee_manager else False,
            })
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

# 隧道认证状态（Cloudflare 模式下启用）
_tunnel_auth_enabled = False
_pairing_manager = None
_token_manager = None


def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message bridge set for backend service")


def set_tunnel_auth(enabled: bool, pairing=None, tokens=None) -> None:
    """设置隧道认证状态。Cloudflare 模式下需要配对码或令牌认证。"""
    global _tunnel_auth_enabled, _pairing_manager, _token_manager
    _tunnel_auth_enabled = enabled
    _pairing_manager = pairing
    _token_manager = tokens
    logger.info(f"Tunnel auth {'enabled' if enabled else 'disabled'}")


# E2EE 状态（所有模式可用）
_e2ee_manager: Optional[E2EEManager] = None

# E2EE 控制消息，始终明文发送（不加密）
_E2EE_CONTROL_TYPES = {
    "e2ee_enabled", "e2ee_disabled", "e2ee_required",
    "auth_required", "auth_success", "auth_failed",
    "config",
}


def set_e2ee_manager(mgr: E2EEManager) -> None:
    """设置 E2EE 管理器引用。"""
    global _e2ee_manager
    _e2ee_manager = mgr


async def _send_json(ws: web.WebSocketResponse, message: dict) -> None:
    """发送 JSON 消息，E2EE 启用时自动加密（控制消息除外）。

    在事件循环内调用（async 上下文），直接 await 发送。
    """
    if _e2ee_manager and _e2ee_manager.enabled and message.get("type") not in _E2EE_CONTROL_TYPES:
        message = _e2ee_manager.wrap(message)
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


async def _handle_auth(ws: web.WebSocketResponse) -> bool:
    """
    处理 WebSocket 认证流程。
    Cloudflare 模式下，客户端需发送配对码或令牌才能建立连接。

    流程：
    1. 服务端发送 auth_required
    2. 客户端回复 auth（method=token 或 pairing_code）
    3. 服务端验证，回复 auth_success 或 auth_failed
    """
    await _send_json(ws, {"type": "auth_required"})

    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=30.0)
    except asyncio.TimeoutError:
        await _send_json(ws, {"type": "auth_failed", "message": I18n.instance().tr("tunnel.auth_timeout")})
        await ws.close()
        return False
    except Exception as e:
        logger.warning(f"Auth receive error: {e}")
        await ws.close()
        return False

    if msg.type != WSMsgType.TEXT:
        await ws.close()
        return False

    try:
        data = json.loads(msg.data)
    except json.JSONDecodeError:
        await ws.close()
        return False

    # E2EE 启用时，客户端消息应该是加密的
    if _e2ee_manager and _e2ee_manager.enabled and _e2ee_manager.is_encrypted(data):
        data = _e2ee_manager.unwrap(data)

    if data.get("type") != "auth":
        await _send_json(ws, {"type": "auth_failed", "message": I18n.instance().tr("tunnel.auth_expected")})
        await ws.close()
        return False

    method = data.get("method")

    if method == "token" and _token_manager:
        token = data.get("token", "")
        if _token_manager.validate(token):
            logger.info("Token auth succeeded")
            await _send_json(ws, {"type": "auth_success"})
            return True

    elif method == "pairing_code" and _pairing_manager and _token_manager:
        code = data.get("code", "")
        if _pairing_manager.validate(code):
            token = _token_manager.generate_token()
            logger.info("Pairing code auth succeeded, token issued")
            await _send_json(ws, {"type": "auth_success", "token": token})
            if _manager:
                _manager.bridge.emit("pairing_success")
            return True

    await _send_json(ws, {"type": "auth_failed", "message": I18n.instance().tr("tunnel.auth_failed")})
    await ws.close()
    return False


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

    async def websocket_endpoint(request):
        """WebSocket 端点，处理手机端的实时消息。"""
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)

        if _manager is None:
            logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
            await ws.close(code=1011, message=b"Server not ready")
            return ws

        # Cloudflare 模式下需要认证
        if _tunnel_auth_enabled:
            if not await _handle_auth(ws):
                return ws

        await _manager.connect(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)

                        # E2EE 启用时：解密收到的消息
                        if _e2ee_manager and _e2ee_manager.enabled:
                            if _e2ee_manager.is_encrypted(data):
                                data = _e2ee_manager.unwrap(data)
                            elif data.get("type") not in _E2EE_CONTROL_TYPES:
                                # 收到明文消息但 E2EE 已开启 → 通知客户端切换
                                await _send_json(ws, {
                                    "type": "e2ee_required",
                                    "message": "请重新扫码以启用加密"
                                })
                                continue
                        elif data.get("type") == "encrypted":
                            # E2EE 已禁用但收到加密消息 → 客户端尚未同步，忽略
                            logger.debug("Received encrypted message while E2EE disabled, ignoring")
                            continue

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
    if not is_frozen():
        app.router.add_get('/test', test)
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


def restart_server(host: str, port: int, bridge: EventBridge) -> None:
    """重启服务端，切换绑定地址。"""
    stop_server()
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
