# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 Starlette + Uvicorn 作为 HTTP 服务器框架。
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

import uvicorn

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

    async def connect(self, websocket, session) -> None:
        """
        注册新的 WebSocket 连接。

        只应在该连接握手成功后调用：已认证的新连接才会抢占当前活动连接，
        握手中或认证失败的连接不会踢掉旧连接，避免把活动连接降级为明文。

        Args:
            websocket: 已通过握手的 WebSocket 连接
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

        self.active_websocket = websocket
        self.active_session = session
        # connect 事件携带本次握手协商出的算法，供状态栏透明展示
        self.bridge.emit("connect", session.negotiated_algorithm)
        logger.info(
            f"WebSocket connected, connection established "
            f"(algorithm={session.negotiated_algorithm})"
        )

        # 发送当前配置（加密传输）
        try:
            await _send_json(websocket, {
                "type": "config",
                "mobile_max_records": self.max_records,
            })
            logger.debug(f"Sent config to client: max_records={self.max_records}")
        except Exception as e:
            logger.warning(f"Failed to send initial config: {e}")

    def session_for(self, websocket):
        """返回该连接对应的 SecureSession，非活动连接返回 None。"""
        if self.active_websocket is websocket:
            return self.active_session
        return None

    def disconnect(self, websocket) -> None:
        """
        清理连接状态，并通知主进程断开事件。
        仅当断开的连接是当前活动连接时才发送事件，以防止重复。
        """
        if self.active_websocket is websocket:
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


def get_secret_path() -> str:
    """返回当前安全通道的 secret_path（未设置时为空串）。

    算法/模式切换会重建 SecureChannel 并更新全局 _secure_channel，
    调用方应始终通过本函数读取最新值，避免持有过期引用。
    """
    return _secure_channel.secret_path if _secure_channel else ""


async def _send_json(websocket, message: dict) -> None:
    """发送 JSON 消息，按该连接自身的会话状态决定是否加密。

    加解密上下文取自连接自己的 SecureSession，而非共享对象，
    因此处于握手中的新连接不会改变活动连接的加密状态。
    """
    session = _manager.session_for(websocket) if _manager else None
    if session is not None and session.is_authenticated:
        message = session.wrap(message)
    await websocket.send_text(json.dumps(message, ensure_ascii=False))


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


def request_client_rescan() -> bool:
    """
    通知已连接的手机端重新扫码（配置已变更），随后关闭连接。

    算法/模式切换后 URL（随机路径、公钥、token）已变化，旧连接与新配置
    不一致且重连必然失败。先通过现有连接推送 reconnect 消息（按该连接
    自身会话加密/明文），手机端收到后停止自动重连并提示重新扫码。

    Returns:
        True 如果已调度发送，False 如果没有活动连接或调度失败
    """
    if _manager is None or _manager.active_websocket is None:
        return False
    if _event_loop is None:
        return False

    async def _do():
        try:
            ws = _manager.active_websocket
            session = _manager.session_for(ws)
            if session is None:
                return
            message = {"type": "reconnect", "reason": "config_changed"}
            if session.is_authenticated:
                message = session.wrap(message)
            await ws.send_text(json.dumps(message, ensure_ascii=False))
            await ws.close(code=1000)
            logger.info("Client notified to rescan, connection closed.")
        except Exception as e:
            logger.warning(f"Failed to notify client rescan: {e}")

    asyncio.run_coroutine_threadsafe(_do(), _event_loop)
    return True


# ---------- WebSocket 连接处理 ----------

async def _handle_auth(websocket, session) -> bool:
    """
    S0：处理认证握手。

    仅在 session.needs_auth 时等待并校验首条 auth 消息。
    认证失败或消息非法时关闭连接并返回 False，此时连接未注册，
    不会影响活动连接的加密状态。

    Returns:
        True 表示认证通过（或该模式无需认证），可进入 S1
    """
    if not session.needs_auth:
        return True

    try:
        message = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Auth timeout, closing")
        await websocket.close(code=1000)
        return False
    except WebSocketDisconnect:
        logger.warning("Auth: client closed connection before auth")
        return False
    except Exception as e:
        logger.warning(f"Auth receive error: {e}")
        await websocket.close(code=1000)
        return False

    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        await websocket.close(code=1000)
        return False

    if data.get("type") != "auth":
        logger.warning(f"Expected auth, got: {data.get('type')}, closing")
        await websocket.close(code=1000)
        return False

    if not session.receive_auth(data):
        logger.warning(f"Auth failed: {session.reject_reason}, closing")
        await websocket.send_text(json.dumps(session.make_auth_ack(), ensure_ascii=False))
        await websocket.close(code=1000)
        return False

    ack = session.make_auth_ack()
    await websocket.send_text(json.dumps(ack, ensure_ascii=False))
    logger.info(f"Auth succeeded, algorithm={ack.get('algo')}, auth_ack sent")
    return True


async def _handle_client_message(websocket, session, raw: str) -> bool:
    """
    S1：处理认证后的单条文本消息。

    校验消息形态（拒绝重复 auth；加密模式只接受 data 信封），
    解密后把内部消息转发到事件桥。

    Returns:
        True 表示继续接收下一条；False 表示需要关闭连接
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid JSON: {raw}, error: {e}")
        return True

    # 拒绝重复 auth
    if data.get("type") == "auth":
        logger.warning("Received auth after authentication, closing")
        await websocket.close(code=1000)
        return False

    if session.is_encrypted:
        if data.get("type") != "data":
            logger.warning(f"Expected data, got: {data.get('type')}, closing")
            await websocket.close(code=1000)
            return False
        inner = session.unwrap(data)
        if inner is None:
            logger.warning("Decryption failed, closing")
            await websocket.close(code=1000)
            return False
    else:
        inner = data

    msg_type = inner.get("type")
    text = inner.get("text", "")
    if msg_type in ("preview", "send"):
        _manager.bridge.emit(msg_type, text)
        logger.debug(f"Received {msg_type}: {text[:50]}...")
    else:
        logger.warning(f"Unknown inner message type: {msg_type}")
    return True


async def _serve_messages(websocket, session) -> None:
    """S1：循环接收并处理消息，直到连接关闭或出错。"""
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            logger.info("WebSocket closed by client")
            break
        except Exception as e:
            logger.error(f"WebSocket receive error: {e}")
            break

        if message["type"] == "websocket.disconnect":
            logger.info("WebSocket disconnected")
            break

        text = message.get("text")
        if text is None:
            # 二进制消息：忽略（协议层面手机端只发 UTF-8 文本）
            logger.debug("Ignoring binary WebSocket message")
            continue

        if not await _handle_client_message(websocket, session, text):
            break


# ---------- HTTP 资源处理 ----------

def _serve_mobile() -> Response:
    """返回手机端聊天页面（mobile.html），并替换 i18n 占位符。"""
    html_path = get_res_path("mobile.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        i18n_data = I18n.instance().get_section("mobile")
        html = html.replace("__I18N_JSON__", json.dumps(i18n_data, ensure_ascii=False))
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Failed to load mobile.html: {e}")
        return HTMLResponse(
            content='<h3>Error: mobile.html not found. Please check resources/ directory.</h3>',
            status_code=404,
        )


def _serve_test() -> Response:
    """返回手机输入测试页面（test.html）。"""
    html_path = get_res_path("test.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Failed to load test.html: {e}")
        return HTMLResponse(
            content='<h3>Error: test.html not found. Please check resources/ directory.</h3>',
            status_code=404,
        )


def _serve_favicon() -> Response:
    """返回 favicon。"""
    favicon_path = get_res_path("favicon.ico")
    return FileResponse(favicon_path, media_type="image/x-icon")


def _serve_sodium(request: Request) -> Response:
    """返回 libsodium.js（浏览器端加密库），支持 gzip。"""
    accept_encoding = request.headers.get("accept-encoding", "")
    if "gzip" in accept_encoding:
        gz_path = get_res_path("sodium.js.gz")
        if os.path.exists(gz_path):
            logger.info("Serving sodium.js.gz (gzip)")
            return FileResponse(
                gz_path,
                media_type="application/javascript",
                headers={"Content-Encoding": "gzip"},
            )
    logger.info("Serving sodium.js (uncompressed)")
    sodium_path = get_res_path("sodium.js")
    return FileResponse(sodium_path, media_type="application/javascript")


def _serve_crypto_providers() -> Response:
    """返回 crypto_providers.js（加密提供者类）。"""
    path = get_res_path("crypto_providers.js")
    return FileResponse(path, media_type="application/javascript")


# 明文模式放行的白名单（根路径入口）
_PUBLIC_PATHS = {"/", "/favicon.ico", "/sodium.js", "/crypto_providers.js", "/ws"}


def _normalize_path(path: str) -> Optional[str]:
    """校验入口路径并归一化为内部路径，非法路径返回 None。

    加密模式：请求路径必须带 /{secret_path} 前缀（防扫描，根路由 404）；
    明文模式：只放行白名单内的已知路径。
    secret 每次现读，算法切换时改值即时生效，无需重启服务器。
    """
    secret = _secure_channel.secret_path if _secure_channel else ""

    if secret:
        if path == "/" + secret or path == "/" + secret + "/":
            return "/"
        if path.startswith("/" + secret + "/"):
            return path[len(secret) + 1:]
        return None
    else:
        if path in _PUBLIC_PATHS:
            return path
        if not is_frozen() and path == "/test":
            return path
        return None


async def _dispatch_http(request: Request, path: str) -> Response:
    """HTTP 统一入口：校验路径合法性后分发到具体资源。"""
    normalized = _normalize_path(path)
    if normalized is None:
        return Response(status_code=404)

    if normalized == "/":
        return _serve_mobile()
    if normalized == "/sodium.js":
        return _serve_sodium(request)
    if normalized == "/crypto_providers.js":
        return _serve_crypto_providers()
    if normalized == "/favicon.ico":
        return _serve_favicon()
    if normalized == "/test":
        return _serve_test()
    return Response(status_code=404)


# ---------- Starlette 应用与路由 ----------

app = Starlette()


async def root(request: Request) -> Response:
    """根路径入口。"""
    return await _dispatch_http(request, "/")


async def http_catchall(request: Request) -> Response:
    """catch-all HTTP 路由：处理带 /{secret_path} 前缀的所有静态资源。"""
    full_path = request.path_params["full_path"]
    return await _dispatch_http(request, "/" + full_path)


app.add_route("/", root, methods=["GET", "HEAD"])
app.add_route("/{full_path:path}", http_catchall, methods=["GET", "HEAD"])


async def _websocket_endpoint(websocket: WebSocket, path: str) -> None:
    """WebSocket 端点：路径校验通过后认证、注册连接并进入消息循环。"""
    normalized = _normalize_path(path)
    if normalized != "/ws":
        await websocket.close(code=1008)
        return

    if _manager is None:
        logger.error("Message bridge not initialized. Call set_bridge() before starting server.")
        await websocket.close(code=1011)
        return

    if _secure_channel is None:
        logger.error("Secure channel not initialized.")
        await websocket.close(code=1011)
        return

    # 完成 WebSocket 握手
    await websocket.accept()

    # 该连接独立的握手上下文，与活动连接互不干扰
    session = _secure_channel.new_session()

    # S0：认证握手（失败时连接已关闭，且不影响活动连接）
    if not await _handle_auth(websocket, session):
        return

    # S1：注册连接（认证后才抢占旧连接）+ 消息循环
    try:
        await _manager.connect(websocket, session)
        await _serve_messages(websocket, session)
    except Exception as e:
        logger.exception(f"Unexpected error in receive_loop: {e}")
    finally:
        _manager.disconnect(websocket)


async def websocket_catchall(websocket: WebSocket) -> None:
    """catch-all WebSocket 路由：处理明文 /ws 或加密 /{secret}/ws。"""
    full_path = websocket.path_params["full_path"]
    await _websocket_endpoint(websocket, "/" + full_path)


app.router.add_websocket_route("/{full_path:path}", websocket_catchall)


# ---------- 线程管理（用于启动/停止服务）----------
_server_thread: Optional[threading.Thread] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_server: Optional[uvicorn.Server] = None

# PyInstaller/Nuitka-safe logging config（打包后 uvicorn 默认 logging 不可用）
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
    """在后台线程中启动 Starlette 服务（非阻塞）。"""
    global _server_thread, _event_loop, _server
    set_bridge(bridge)
    _event_loop = None
    _server = None

    def _run():
        global _event_loop, _server
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _event_loop = loop

        is_packaged = is_frozen()
        log_config = LOGGING_CONFIG if is_packaged else None
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=log_config,
            log_level="info",
            loop="asyncio",
            # WebSocket 心跳：15 秒 ping，30 秒无响应判定断开
            ws_ping_interval=15.0,
            ws_ping_timeout=30.0,
        )
        _server = uvicorn.Server(config)
        try:
            loop.run_until_complete(_server.serve())
        finally:
            try:
                loop.close()
            except Exception:
                pass

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    logger.info(f"Starting PhoneMic backend server on {host}:{port}")


def stop_server() -> None:
    """停止后台服务。"""
    global _event_loop, _server, _server_thread
    if _server is not None:
        _server.should_exit = True
    if _server_thread is not None:
        _server_thread.join(timeout=5.0)
    _event_loop = None
    _server = None
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

    uvicorn.run(app, host=host, port=port, log_level="info")
