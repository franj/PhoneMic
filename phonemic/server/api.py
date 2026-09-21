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
from starlette.requests import ClientDisconnect, Request
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

import uvicorn

from phonemic.bridge_interface import EventBridge
from phonemic.gui.file import FileReceiver
from phonemic.gui.photo import PhotoReceiver
from phonemic.server.transfer import TransferQueue
from phonemic.tunnel.e2ee import AUTH_TIMEOUT, APPROVAL_TIMEOUT, SecureChannel
from phonemic.tunnel.crypto.errors import CryptoError
from phonemic.tunnel.frame import FrameError
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode
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
            await _send_frame(websocket, {
                "type": "config",
                "mobile_max_records": self.max_records,
                "max_frame_size": WS_MAX_FRAME_SIZE,
            })
            logger.debug(
                f"Sent config to client: max_records={self.max_records}, "
                f"max_frame_size={WS_MAX_FRAME_SIZE}"
            )
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
            # 断连时丢弃未完成的文件/图片接收会话（wire-protocol.md §9）
            _get_transfer_queue().abort_all()
            self.bridge.emit("disconnect")
            logger.info("Active WebSocket disconnected, event sent.")


# 全局通信管理（用于与主进程通信）
_manager: Optional[ConnectionManager] = None

# 安全通道（所有模式共用，PC 密钥对在启动时生成一次）
_secure_channel: Optional[SecureChannel] = None

# 文件接收状态机（wire-protocol.md §9）。落盘回调走 bridge 发 file_saved 事件，
# 主进程弹托盘通知；dest_dir 默认 ~/Downloads/PhoneMic（§9.2）。
_file_receiver = FileReceiver()

# 图片到剪贴板接收状态机（wire-protocol.md §9.1）：内存重组不落盘，
# end 回调走 bridge 发 photo_received 事件，主进程写系统剪贴板 + 托盘通知。
_photo_receiver = PhotoReceiver()


# ---------- 分块传输：写盘队列（wire-protocol.md §9） ----------
# 接收侧只入队，落盘与 ack 交给后台消费者串行执行，收帧循环立刻回去处理
# mouse / key / preview（"传文件时鼠标卡"的根因）。
# 队列按字节限流，满则入队方 await —— 天然背压，不丢帧不爆内存。
# WS_MAX_FRAME_SIZE 是 WebSocket 单帧上限，同时通过 config 帧下发给手机端。
WS_MAX_FRAME_SIZE = 16 * 1024 * 1024

# 惰性实例：首次用到才创建（生命周期跟随服务，换事件循环时内部自动重建 worker）
_transfer_queue: Optional[TransferQueue] = None


def _get_transfer_queue() -> TransferQueue:
    """返回传输队列单例（首次调用时实例化）。"""
    global _transfer_queue
    if _transfer_queue is None:
        _transfer_queue = TransferQueue(
            _file_receiver, _photo_receiver, _send_frame)
    return _transfer_queue


def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    _file_receiver.on_done = lambda path, name, size: _manager.bridge.emit(
        "file_saved", {"path": path, "name": name, "size": size}
    )
    # photo end 回调：把重组好的图片字节交给主进程（Qt 剪贴板写入必须在 GUI 线程）
    _photo_receiver.on_done = lambda data, name, size: _manager.bridge.emit(
        "photo_received", {"data": data, "name": name, "size": size}
    )
    logger.info("Message bridge set for backend service")


def set_secure_channel(sc: SecureChannel) -> None:
    """设置安全通道引用。"""
    global _secure_channel
    _secure_channel = sc


# ---------- TOFU 审批机制 ----------

# 当前待审批的 Future（同一时间只允许一个待审批请求，新请求替换旧的）
_pending_approval: Optional[asyncio.Future] = None


def resolve_approval(approved: bool) -> None:
    """Dashboard（Qt 线程）调用：解决当前待审批的 TOFU 请求。

    通过 ``loop.call_soon_threadsafe`` 将结果投递到 asyncio 事件循环，
    唤醒正在 ``_await_approval`` 中等待的 WebSocket handler。
    """
    global _pending_approval
    if _pending_approval is not None and not _pending_approval.done():
        if _event_loop is not None:
            _event_loop.call_soon_threadsafe(_set_approval_result, approved)
    else:
        logger.debug("resolve_approval called but no pending approval")


def _set_approval_result(approved: bool) -> None:
    """在事件循环线程中设置 Future 结果。"""
    global _pending_approval
    if _pending_approval is not None and not _pending_approval.done():
        _pending_approval.set_result(approved)


async def _await_approval(pin: str, client_ip: str) -> bool:
    """等待 dashboard 审批 TOFU 连接请求。

    向 bridge 发射 ``approval_request`` 事件，dashboard 据此显示审批通知。
    调用 ``resolve_approval()`` 设置结果。新请求替换旧的待审批 Future。
    """
    global _pending_approval
    loop = asyncio.get_running_loop()
    _pending_approval = loop.create_future()
    _manager.bridge.emit("approval_request", {"pin": pin, "ip": client_ip})
    logger.info(f"TOFU approval requested: pin={pin}, ip={client_ip}")
    return await _pending_approval


def cancel_pending_approval() -> None:
    """取消当前待审批请求（如客户端断开连接时）。"""
    global _pending_approval
    if _pending_approval is not None and not _pending_approval.done():
        if _event_loop is not None:
            _event_loop.call_soon_threadsafe(
                lambda: _pending_approval.set_result(False) if not _pending_approval.done() else None
            )



def get_secret_path() -> str:
    """返回当前安全通道的 secret_path（未设置时为空串）。

    算法/模式切换会重建 SecureChannel 并更新全局 _secure_channel，
    调用方应始终通过本函数读取最新值，避免持有过期引用。
    """
    return _secure_channel.secret_path if _secure_channel else ""


async def _send_frame(websocket, message: dict) -> None:
    """发送一帧消息，按该连接自身的会话状态决定是否加密。

    线上字节一律由 session.wrap() 产出：加密模式整帧加密，明文模式 msgpack
    编码，没有外层信封。加解密上下文取自连接自己的 SecureSession，而非共享
    对象，因此处于握手中的新连接不会改变活动连接的加密状态。
    """
    session = _manager.session_for(websocket) if _manager else None
    if session is not None:
        payload = session.wrap(message)
    else:
        payload = frame_encode(message)
    await websocket.send_bytes(payload)


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
            await _send_frame(_manager.active_websocket, message)
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
            # wrap 已产出线上字节（认证后=整帧加密 / 未认证=明文 msgpack），不再二次编码
            payload = session.wrap(message)
            await ws.send_bytes(payload)
            await ws.close(code=1000)
            logger.info("Client notified to rescan, connection closed.")
        except Exception as e:
            logger.warning(f"Failed to notify client rescan: {e}")

    asyncio.run_coroutine_threadsafe(_do(), _event_loop)
    return True


# ---------- WebSocket 连接处理 ----------

def _close_reason(reason: str, limit: int = 100) -> str:
    """把拒绝原因裁到 WS close 帧能装下的长度（协议上限 123 字节）。

    必须按**字节**裁而不是按字符：拒绝原因里可能带对端提供的算法名
    （`algorithm '<algo>' not allowed`），长度与字符集都不受控；按字符裁会让
    非 ASCII 膨胀到上限之外，close 直接抛异常——那等于把「拒绝了对端」变成
    「服务端自己出错」。留 23 字节余量给 reason 之外的帧头。
    """
    return reason.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


async def _recv_handshake_frame(websocket, deadline: float):
    """在握手的绝对截止时刻前读取一条 binary 帧。

    两次握手等待（auth / auth_proof）共用同一个 deadline，因此慢速或恶意的
    客户端无法靠「每一步都拖到超时」把握手时长翻倍。

    Returns:
        ``(raw_bytes, None)`` 成功；``(None, reason)`` 失败（reason 供日志）。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "timed out"
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
    except asyncio.TimeoutError:
        return None, "timed out"
    except WebSocketDisconnect:
        return None, "client closed connection"
    except Exception as e:
        return None, f"receive error: {e}"

    if message.get("type") == "websocket.disconnect":
        return None, "client disconnected"
    raw = message.get("bytes")
    if raw is None:
        # 协议全部走 binary：text 帧只可能来自未刷新的旧页面（旧 JSON 协议）
        return None, "non-binary frame (stale client?)"
    return raw, None


async def _handle_auth(websocket, session) -> bool:
    """S0：握手 —— auth → [审批] → auth_challenge → auth_proof。

    三种路径：
      URL fragment / TOFU 重连：auth(SealedBox) → create_provider →
        auth_challenge(Provider加密) → auth_proof
      TOFU 首次：auth(明文pk+pin) → 审批 → complete_tofu_auth →
        auth_challenge(SealedBox加密) → auth_proof

    所有失败都发生在 `_manager.connect()` 之前（调用方在返回 False 时短路），
    这正是「重放 auth 帧顶掉真机连接」那条 DoS 被顺带消掉的原因。

    Returns:
        True 表示握手完成，可进入 S1
    """
    deadline = time.monotonic() + AUTH_TIMEOUT

    # ---- 第 1 步：auth ----
    raw, err = await _recv_handshake_frame(websocket, deadline)
    if raw is None:
        logger.warning(f"Auth: {err}, closing")
        await websocket.close(code=1000)
        return False

    try:
        data = frame_decode(raw)
    except FrameError as e:
        logger.warning(f"Invalid frame: {e}")
        await websocket.close(code=1000)
        return False

    if data.get("type") != "auth":
        logger.warning(f"Expected auth, got: {data.get('type')}, closing")
        await websocket.close(code=1000)
        return False

    try:
        algo, session_key, pin, phone_pk = session.receive_auth(data)
    except CryptoError as e:
        reason = str(e) or "auth data processing failed"
        logger.warning(f"Auth rejected: {reason}, closing 4001")
        await websocket.close(code=4001, reason=_close_reason(reason))
        return False

    if pin is not None:
        # TOFU 首次：等待用户审批（尚未做 ECDH）
        client_ip = websocket.client.host if websocket.client else "unknown"
        try:
            approved = await asyncio.wait_for(
                _await_approval(pin, client_ip),
                timeout=APPROVAL_TIMEOUT
            )
        except asyncio.TimeoutError:
            approved = False
        if not approved:
            cancel_pending_approval()
            reason = "timeout" if not approved else "rejected"
            logger.warning(f"TOFU approval {reason}, closing 4032")
            await websocket.close(code=4032, reason=reason)
            return False
        # 审批通过：现在才做 ECDH + 创建 Provider
        session.complete_tofu_auth(algo, phone_pk)
    else:
        # URL fragment / TOFU 重连：session_key 已就绪
        session.create_provider(algo, session_key)

    # ---- 第 2 步：auth_challenge ----
    challenge_bytes = session.make_auth_challenge()
    await websocket.send_bytes(challenge_bytes)

    # ---- 第 3 步：auth_proof ----
    raw, err = await _recv_handshake_frame(websocket, deadline)
    proof = session.unwrap(raw) if raw is not None else None
    if not session.verify_auth_proof(proof):
        logger.warning(f"Auth proof rejected: {err or 'nonce mismatch'}, closing")
        await websocket.send_bytes(session.wrap({"type": "error", "code": "auth"}))
        await websocket.close(code=1000)
        return False

    logger.info(f"Auth succeeded, algorithm={session.negotiated_algorithm}")
    return True


async def _handle_client_message(websocket, session, raw: bytes) -> bool:
    """
    S1：处理认证后的单条 binary 消息。

    加密与否由会话状态机决定（session.unwrap 内部按 Provider 是否存在判断），
    不靠帧内容判别——因此加密帧没有外层信封，无法也不需要在解密前识别类型。
    解密后拒绝重复 auth，其余转发到事件桥。

    Returns:
        True 表示继续接收下一条；False 表示需要关闭连接
    """
    message = session.unwrap(raw)
    if message is None:
        logger.warning("Decryption failed, closing")
        await websocket.close(code=1000)
        return False

    # 拒绝重复 auth
    if message.get("type") == "auth":
        logger.warning("Received auth after authentication, closing")
        await websocket.close(code=1000)
        return False

    inner = message

    msg_type = inner.get("type")
    text = inner.get("text", "")
    if msg_type in ("preview", "send"):
        _manager.bridge.emit(msg_type, text)
        logger.debug(f"Received {msg_type}: {text[:50]}...")
    elif msg_type == "key":
        # keys 直接喂给 phonemic/gui/keyboard.py:send_keys()
        _manager.bridge.emit("key", inner.get("keys", ""))
        logger.debug(f"Received key: {inner.get('keys', '')}")
    elif msg_type == "mouse":
        # 整帧交给 PC 端，a 决定动作（wire-protocol.md §7）
        _manager.bridge.emit("mouse", inner)
        logger.debug(f"Received mouse: a={inner.get('a')}")
    elif msg_type in ("file", "photo"):
        # 分块传输（wire-protocol.md §9 / §9.1）：只入队，落盘与 ack 都交给
        # 后台任务串行执行——收帧循环立刻回去处理 mouse/key/preview，
        # 不再被写盘堵住。队列满时这里 await，形成背压。
        await _get_transfer_queue().enqueue(msg_type, inner, websocket)
    elif msg_type == "pong":
        # 应用层心跳应答（wire-protocol.md §7）。存活时间戳已在 _serve_messages
        # 的收帧处统一刷新，这里只需认下类型，避免落到 else 回一帧 malformed。
        logger.debug("Received pong")
    elif msg_type == "hello":
        # 取消回执的兜底探活（wire-protocol.md §9）：原样回显探活号 t。
        # 手机端据此认定「排在 hello 之前的 cancel 帧已被服务端读出」——同一
        # 条可靠有序通道，本函数按收帧顺序执行，能读到 hello 就说明先读到了
        # cancel（cancel 已入队，随后必被消费者处理）。
        await _send_frame(websocket, {"type": "hello", "t": inner.get("t")})
        logger.debug(f"Received hello (t={inner.get('t')})")
    else:
        logger.warning(f"Unknown inner message type: {msg_type}")
        await _send_frame(websocket, {
            "type": "error",
            "code": "malformed",
            "msg": f"unknown type: {msg_type}",
        })
    return True


# ---------- 应用层保活（wire-protocol.md §7 ping / pong） ----------
# 原生心跳的 pong 会排在文件数据之后（见 uvicorn.Config 处的注释），故整体改为
# 应用层判活：依据是「最近一次收到对端消息的时刻」，而文件数据块本身就在刷新它
# ⇒ 传输期间永远不会触发心跳，也就不存在与业务数据争抢同一条有序通道的问题。
# 三个阈值集中在这里，要调整只改这一处。
_KEEPALIVE_IDLE_BEFORE_PING = 45.0   # 空闲多久后发一帧 ping 促对端应答
_KEEPALIVE_TIMEOUT = 60.0            # 空闲多久后判死
_KEEPALIVE_CHECK_INTERVAL = 5.0      # 检查周期


class _ConnectionLiveness:
    """单条连接的应用层保活状态：只维护「最近一次收到对端消息的时刻」。"""

    def __init__(self) -> None:
        self.touch()

    def touch(self) -> None:
        """收到任意帧（含文件数据块）时调用。"""
        self.last_msg_ts = asyncio.get_running_loop().time()

    def idle_seconds(self) -> float:
        return asyncio.get_running_loop().time() - self.last_msg_ts


async def _app_keepalive(websocket, liveness: _ConnectionLiveness) -> None:
    """后台保活：空闲达阈值时探活，长时间无消息则判死并关闭连接。

    由 _serve_messages 按连接创建、在其结束时取消，因此天然按连接隔离，
    不需要全局连接表。异常一律在此收口（只把 CancelledError 交回去），
    调用方的清理路径就只剩 cancel 一种情况。

    _KEEPALIVE_TIMEOUT 必须小于 Cloudflare 的 WebSocket idle 超时（约 100s
    无数据即掐断），否则会出现「CF 已掐断、服务端还以为连着」的窗口期。
    """
    try:
        while True:
            await asyncio.sleep(_KEEPALIVE_CHECK_INTERVAL)
            idle = liveness.idle_seconds()
            if idle >= _KEEPALIVE_TIMEOUT:
                logger.info(
                    "Application keepalive timeout: no message for %.1fs, "
                    "closing (code=1011)",
                    idle,
                )
                await websocket.close(code=1011, reason="keepalive ping timeout")
                return
            if idle >= _KEEPALIVE_IDLE_BEFORE_PING:
                # 只在「真的没有业务数据」时才探活，因此不会与传输争抢通道
                logger.debug(
                    "Application keepalive: idle %.1fs, sending ping", idle)
                await _send_frame(websocket, {"type": "ping"})
    except asyncio.CancelledError:
        # 连接结束时的正常取消路径，不记日志
        raise
    except Exception as e:
        # 发送/关闭失败通常意味着连接已不可写，交给主循环的 receive 去感知
        logger.warning(f"Application keepalive stopped: {e}")


async def _serve_messages(websocket, session) -> None:
    """S1：循环接收并处理消息，直到连接关闭或出错。

    关闭码（close code）是判断「谁断的、为什么断」的唯一可靠线索，必须留痕：
      1000 = 正常关闭（本端主动 close 或对端带码关闭）
      1005 = 对端未带状态码（浏览器 close() 无参）
      1006 = 连接异常中断，没收到关闭帧（网络层掉线，也可能是本端心跳掐断）
      1011 = 本端内部错误，含应用层保活超时（见 _app_keepalive）
      1012 = 服务端重启
    仅凭 1006 无法区分「对端掉线」与「本端心跳超时」，需配合本端是否有
    keepalive 相关日志、以及对端 onclose 的 code/reason 一起看。
    """
    liveness = _ConnectionLiveness()
    keepalive_task = asyncio.create_task(_app_keepalive(websocket, liveness))
    try:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect as e:
                logger.info(
                    "WebSocket closed by client: code=%s reason=%s",
                    getattr(e, "code", None), getattr(e, "reason", "") or "-",
                )
                break
            except Exception as e:
                logger.error(f"WebSocket receive error: {e}")
                break

            # 任意一帧都算「对端还活着」。文件数据块同样经过这里，因此
            # 传输期间保活任务不会发 ping（wire-protocol.md §7）。
            liveness.touch()

            if message["type"] == "websocket.disconnect":
                logger.info(
                    "WebSocket disconnected: code=%s reason=%s",
                    message.get("code"), message.get("reason") or "-",
                )
                break

            text = message.get("bytes")
            if text is None:
                # 文本消息：协议已全 binary，忽略（旧页面未刷新）
                logger.debug("Ignoring text WebSocket message")
                continue

            if not await _handle_client_message(websocket, session, text):
                break
    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


# ---------- HTTP 资源处理 ----------

# 手机端「开发模式」标记的注入点（对应 mobile.html 的 <head>）。
# 打包版整个日志通道都不启动——不接管 console、不缓冲、不转发、不建入口按钮，
# 所以这个标记必须在**页面返回时**就定下来；等 WebSocket 的 config 帧就太晚了，
# 那时页面脚本早已执行完。占位符缺失时客户端按 false 降级（安全的一侧）。
_DEV_MODE_MARK = "<!--PHONEMIC_DEV_MODE-->"


def _serve_mobile() -> Response:
    """返回手机端聊天页面（mobile.html）。

    页面不含任何翻译文本，语言包由手机端自行请求 /api/lang.json 获取。
    返回前注入开发模式标记，供页面决定是否启动日志模块。
    """
    html_path = get_res_path("mobile.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        dev = "true" if not is_frozen() else "false"
        html = html.replace(
            _DEV_MODE_MARK, f"<script>window.__PHONEMIC_DEV__ = {dev};</script>"
        )
        return HTMLResponse(content=html)
    except Exception as e:
        logger.error(f"Failed to load mobile.html: {e}")
        return HTMLResponse(
            content='<h3>Error: mobile.html not found. Please check resources/ directory.</h3>',
            status_code=404,
        )


def _serve_lang_json() -> Response:
    """返回当前 PC 端语言下的手机端翻译段（locales/{lang}.json 的 mobile 部分）。

    仅包含界面文本，无敏感信息，可置于公开路径。禁用缓存，保证 PC 端
    切换语言后手机端刷新即可拿到最新语言包。
    """
    try:
        i18n = I18n.instance()
        i18n.reload()   # 每次请求重读当前语言文件：改 locale 后手机端刷新即生效，无需重启
        mobile_data = i18n.get_section("mobile")
        return JSONResponse(
            content=mobile_data,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        logger.error(f"Failed to get language data: {e}")
        return JSONResponse(content={}, status_code=500)


# 手机端日志回传的体积上限：入口挂在 HTTP 上，必须限制单次体积，
# 避免被滥用（或客户端 bug）打爆服务端日志。
_CLIENT_LOG_MAX_BODY = 256 * 1024
_CLIENT_LOG_MAX_ENTRIES = 200
_CLIENT_LOG_MAX_TEXT = 400

# 未读请求体的排空上限。
#
# 只服务于「不消费 body 就返回响应」的分支（405、打包版的 404）：urllib 这类客户端
# 会发 Connection: close，uvicorn 收到就在响应写完的那一刻立刻 close；此时内核接收
# 缓冲里若还压着没被读走的请求体，Windows 发的是 RST 而不是 FIN，而 RST 会丢弃客户端
# 「已到达但应用还没读走」的响应 ⇒ 客户端拿到 WinError 10053，看到的不是 405 而是
# 连接被中止（概率复现，机器越忙越容易撞，见
# tests/test_backend.py::test_post_to_readonly_path_is_405）。
#
# 排空只做「把字节读掉」：不解析、不留存，超限或超时就放弃。它的目的是让关闭干净，
# 不是为了收数据，所以宁可放弃，也不让一个只声明 Content-Length 却不发 body 的
# 客户端把 handler 吊住。
_DRAIN_MAX_BODY = 64 * 1024
_DRAIN_TIMEOUT = 2.0

_last_client_ua: str = ""


async def _drain_request_body(request: Request) -> None:
    """把请求体读掉并丢弃，保证响应写出后服务端能干净地关闭连接。

    只在「不消费 body 就返回响应」的分支里调用。已经读过 body 的路径
    （_receive_client_log）不需要也不该再读——重复读会撞 Starlette 的
    RuntimeError("Stream consumed")。
    路由层直接给出的 405（非 GET/HEAD/POST 方法）不经过这里：那条路径只有扫描器
    会走，客户端读不读得到响应都无所谓。
    """
    remaining = _DRAIN_MAX_BODY
    deadline = time.monotonic() + _DRAIN_TIMEOUT
    stream = request.stream()
    while remaining > 0:
        budget = deadline - time.monotonic()
        if budget <= 0:
            return
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=budget)
        except StopAsyncIteration:
            return  # 读完了：正常出口
        except (asyncio.TimeoutError, ClientDisconnect):
            return  # 对端不发或已经走了：照样把响应发出去
        remaining -= len(chunk)
    logger.debug(f"请求体超过 {_DRAIN_MAX_BODY} 字节，放弃继续排空")


async def _receive_client_log(request: Request) -> Response:
    """接收手机端转发的日志（POST /api/client-log），原样打到服务端日志。

    手机上没有 devtools（尤其微信内置浏览器），alert 又会盖住状态栏，把日志
    回传到 PC 的 cmd 是唯一能「实时 + 可对时间」看清手机端现场的办法。
    只做展示：不落盘、不回帧、不参与加密会话——能拿到 secret_path 就已具备
    访问资格，日志内容本身也不比同链路上的聊天内容更敏感。
    """
    global _last_client_ua

    # 打包版不接收手机端日志：页面侧根本不会启动日志模块（见 _serve_mobile），
    # 这里同时兜住旧缓存页面与伪造请求。返回 404，与「路径不存在」一致。
    if is_frozen():
        # body 也得读掉再返回，否则和 405 同一个坑：Connection: close 下未读数据
        # 会让关闭变成 RST，客户端读不到这个 404（见 _drain_request_body）。
        await _drain_request_body(request)
        return Response(status_code=404)

    raw = await request.body()
    if len(raw) > _CLIENT_LOG_MAX_BODY:
        return Response(status_code=413)
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return Response(status_code=400)
    # 顶层必须是对象：JSON 可以是任意类型，日志入口不能因形状不对而抛 500
    if not isinstance(data, dict):
        return Response(status_code=400)
    entries = data.get("entries")
    if not isinstance(entries, list):
        return Response(status_code=400)

    ua = str(data.get("ua", ""))
    if ua and ua != _last_client_ua:
        _last_client_ua = ua
        logger.info(f"手机端 UA: {ua}")

    for item in entries[:_CLIENT_LOG_MAX_ENTRIES]:
        # 线上格式 [毫秒时间戳, 级别, 文本]；脏数据直接跳过，日志入口不该抛错
        try:
            ms = int(item[0])
            level = str(item[1]).upper()[:5]
            text = str(item[2])[:_CLIENT_LOG_MAX_TEXT]
        except (TypeError, ValueError, IndexError):
            continue
        stamp = time.strftime("%H:%M:%S", time.localtime(ms / 1000)) + f".{ms % 1000:03d}"
        logger.info(f"[手机 {stamp}] {level:<5} {text}")

    return Response(status_code=204)


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


def _serve_msgpack() -> Response:
    """返回 msgpack.min.js（浏览器端 MessagePack 编解码库）。"""
    path = get_res_path("msgpack.min.js")
    return FileResponse(path, media_type="application/javascript")


def _clean_nonce(raw: Optional[str]) -> Optional[str]:
    """校验保活请求的防重放 nonce；不合法返回 None。

    回显客户端的 t，是为了让保活器能证明「这个响应是为我这一次请求生成的」：
    任何被 Cloudflare 边缘或中间层缓存/复用的响应都带着上一轮的 t，一比对就会
    露馅——从而把「保活静默失效」变成「保活明确报错」。

    回显的是请求方自带的字符串（反射型数据），因此必须限死格式：纯 ASCII 数字、
    不超过 20 位。既杜绝注入面，也与保活器实际发送的毫秒时间戳一致。
    """
    if not raw or len(raw) > 20:
        return None
    if not (raw.isascii() and raw.isdigit()):
        return None
    return raw


def _serve_keepalive(request: Request) -> Response:
    """隧道保活探测端点（供本机保活器周期请求，见 phonemic/tunnel/keepalive.py）。

    响应体只有 {"status": "ok", "t": <回显请求里的 t>}，不含版本/运行时长/连接数
    等服务信息——它是一个公开路径，不提供任何可供扫描者利用的情报。

    必须禁用缓存：一旦被 Cloudflare 边缘或中间层缓存命中，请求就不会穿透到
    源站，隧道依然会因缺少真实流量被回收，而保活器却拿到 200 以为一切正常。
    响应里的 t 正是为这种情况准备的——缓存复用的响应带着上一轮的 t，保活器比对
    不上就会报错（见 tunnel/keepalive.py::_verify_body）。
    """
    return JSONResponse(
        content={"status": "ok", "t": _clean_nonce(request.query_params.get("t"))},
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# 明文模式放行的白名单（根路径入口）

_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/sodium.js",
    "/crypto_providers.js",
    "/msgpack.min.js",
    "/ws",
    "/api/lang.json",
    # 隧道保活探测（GET）：仅返回 {"status":"ok"}，供本机保活器周期请求，
    # 让快隧道持续有真实 HTTP 流量、不被 Cloudflare 判为空闲回收
    "/api/keepalive",
    # 手机端日志回传（POST）：与 lang.json 同级，仅诊断用途，无敏感信息
    "/api/client-log",
}


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

    # 写方法只有手机端日志回传一个入口，其余 POST 一律 405。
    # 判定必须放在路径校验之前：路径非法时也返回 405 而不是 404，否则
    # 「路径对不对」会从状态码差异里漏出去。这也与 POST 由路由层直接
    # 返回 405 时的既有行为保持一致（见 tests/test_dispatcher.py）。
    if request.method == "POST":
        if normalized == "/api/client-log":
            return await _receive_client_log(request)
        # 405 是「不消费 body 就返回」的分支，必须先排空再返回，否则带 body 的 POST
        # 会以 RST 收场（原因见 _drain_request_body）。
        await _drain_request_body(request)
        return Response(status_code=405)

    if normalized is None:
        return Response(status_code=404)

    if normalized == "/":
        return _serve_mobile()
    if normalized == "/api/lang.json":
        return _serve_lang_json()
    if normalized == "/api/keepalive":
        return _serve_keepalive(request)
    if normalized == "/sodium.js":
        return _serve_sodium(request)
    if normalized == "/crypto_providers.js":
        return _serve_crypto_providers()
    if normalized == "/msgpack.min.js":
        return _serve_msgpack()
    if normalized == "/favicon.ico":
        return _serve_favicon()
    if normalized == "/test":
        return _serve_test()
    return Response(status_code=404)


# ---------- Starlette 应用与路由 ----------


class SecurityHeadersMiddleware:
    """给所有 HTTP 响应统一补安全响应头。

    纯 ASGI 实现：只改 ``http.response.start`` 那一帧，不包装 Request/Response、
    不碰响应正文，因此不会干扰文件上传的 ``request.stream()`` 与静态资源响应；
    websocket 作用域直接放行，不参与握手。

    目前只补一个头 —— ``Referrer-Policy: no-referrer``。加密模式下入口路径里带着
    secret_path，而 URL 一旦作为 Referer 发往别的源，等于把入口地址送出去。现在
    mobile.html 不含任何第三方资源，这条属性靠「不引外部资源」维持着，太脆弱：
    哪天加个 CDN 或统计脚本就悄悄破了。（现代浏览器默认
    strict-origin-when-cross-origin，跨源只发 origin 不含路径，但那是浏览器的
    默认行为，不是我们能保证的东西。）
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


app = Starlette()
# 加密模式下入口路径含 secret_path，禁止它随 Referer 外流。
# 必须在应用启动前注册（Starlette 启动后再 add_middleware 会抛 RuntimeError）。
app.add_middleware(SecurityHeadersMiddleware)


async def root(request: Request) -> Response:
    """根路径入口。"""
    return await _dispatch_http(request, "/")


async def http_catchall(request: Request) -> Response:
    """catch-all HTTP 路由：处理带 /{secret_path} 前缀的所有静态资源。"""
    full_path = request.path_params["full_path"]
    return await _dispatch_http(request, "/" + full_path)


# 允许 POST：手机端日志回传走同一套路由（加密模式下路径带 secret 前缀，
# 无法单独注册一条固定路径，只能在分发处按方法分流）
app.add_route("/", root, methods=["GET", "HEAD", "POST"])
app.add_route("/{full_path:path}", http_catchall, methods=["GET", "HEAD", "POST"])


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

    # 打印客户端 UA，便于排查不同浏览器的兼容性问题
    user_agent = websocket.headers.get("user-agent", "unknown")
    logger.info(f"WebSocket client UA: {user_agent}")

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


def _resolve_log_level() -> str:
    """把环境变量解析成日志档位：info（默认）/ debug / trace。

    - debug：把 phonemic.* 降到 DEBUG，看得到收到的 mouse/key/send 明细
    - trace：再叠加 uvicorn 与 websockets 协议层（WS 帧 hex）。
      应用层保活超时（wire-protocol.md §7）会以 1011 + 明确日志暴露，而网络层
      掉线只能看到 1006；两者靠这里的帧级日志区分。

    旧开关 PHONEMIC_WS_DEBUG=1 等价于 trace，保留以兼容既有用法。
    """
    level = os.environ.get("PHONEMIC_LOG", "").strip().lower()
    if level not in ("debug", "trace"):
        level = "info"
    if os.environ.get("PHONEMIC_WS_DEBUG") == "1":
        level = "trace"
    return level


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
        # 日志档位（PHONEMIC_LOG=debug|trace，默认 info）。
        # phonemic.* 的级别必须显式设置：root 已被 PhoneMic.py 的 basicConfig(INFO)
        # 定死，而 uvicorn 的 log_level 只管 uvicorn.* 那三个 logger——不设置的话
        # phonemic.* 里的 logger.debug 永远不输出，等于死代码。
        log_level = _resolve_log_level()
        if log_level != "info":
            logging.getLogger("phonemic").setLevel(logging.DEBUG)
            logger.info(f"PHONEMIC_LOG={log_level}：应用日志已切到 DEBUG")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=log_config,
            log_level="debug" if log_level == "trace" else "info",
            loop="asyncio",
            # 原生 WebSocket 心跳已关闭，改由应用层判活（wire-protocol.md §7）。
            # 原因：原生心跳只等「本次 ping 匹配的那一个 pong」，而 pong 与文件
            # 数据同向、共用一条有序 TCP 通道 ⇒ 大文件上传时 pong 排在手机端
            # 发送缓冲（mobile.html HIGH_WATER）之后，慢链路上必然超时，
            # 把一条正在正常传输的连接判死（close 1011）。
            # 应用层判活看的是「有没有收到任何消息」，文件块本身就在刷时间戳，
            # 因此天然免疫，且阈值集中在一个地方、随时可调。
            ws_ping_interval=None,
            ws_ping_timeout=None,
            ws_max_size=WS_MAX_FRAME_SIZE,
            # WS 协议栈显式选新实现。默认的 ws="auto" 会优先挑
            # uvicorn.protocols.websockets.websockets_impl，那条路 import 的是
            # websockets.legacy.*：在 websockets 16 下启动即抛三个 DeprecationWarning
            # （websockets.legacy / WebSocketServerProtocol / remove second argument of
            # ws_handler），且 legacy 栈终将被移除。新实现（websockets 14+ 的 sansio 栈）
            # 能力对齐：ws_max_size 同样生效，1008/1011/1012 关闭码照常送达，帧级日志的
            # logger 仍挂在 uvicorn.error 上 ⇒ PHONEMIC_LOG=trace 不受影响。
            ws="websockets-sansio",
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
    global _event_loop, _server, _server_thread, _transfer_queue
    if _server is not None:
        _server.should_exit = True
    if _transfer_queue is not None:
        _transfer_queue.close()          # 取消后台写盘消费者
    if _server_thread is not None:
        _server_thread.join(timeout=5.0)
    _transfer_queue = None
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
