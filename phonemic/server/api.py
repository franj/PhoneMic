# server/api.py
"""
PhoneMic 后端服务模块

提供 HTTP 静态页面托管和 WebSocket 实时通信服务。
使用 Starlette + Uvicorn 作为 HTTP 服务器框架。
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocket, WebSocketDisconnect

import uvicorn

from phonemic.bridge_interface import EventBridge
from phonemic.server.upload import (
    CHUNK_OVERHEAD,
    PHOTO_MAX_SIZE,
    UPLOAD_CHUNK_SIZE,
    ChunkProgressThrottle,
    UploadManager,
)
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

        # 发送串行化（docs/http-upload-design.md §5.13）：`_send_frame` 靠它把
        # 「取 seq + 编码 + send」包成原子段。
        # ⚠️ 引入第二个发送方（HTTP handler 推 `upload_progress`）之后，这把锁
        # **不是可选优化**：`wrap()` 是同步的、不会拿到重复 seq，但 `await
        # send_bytes` 是个让出点 ⇒ 两个任务的帧可能以**相反顺序**落到线上，
        # 接收端看到的就是重放／解密失败。
        # 每连接一把即可（本管理器是单活动连接模型）；生命周期随管理器。
        # ⚠️ 因此**别让 `_manager` 跨事件循环复用**：asyncio.Lock 在首次 await 时绑定
        # loop，之后换 loop 会抛 RuntimeError。生产只有一个 loop，且 `set_bridge()`
        # 每次都重建管理器（测试里各 fixture 都靠这条），这条约束自然成立。
        self.send_lock = asyncio.Lock()

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
            # ⚠️ 不再下发 `max_frame_size`（09-23 退役）：它唯一的用途是让手机端收紧
            # **文件分块**上限，而片大小现在由 `upload_ready` 的 `chunk` 字段下发
            # （wire-protocol.md §9.1）、客户端不再自算 ⇒ 这个字段没有任何读取方。
            # `ws_max_size` 本身仍然生效，但 WS 上只剩控制帧，撞不到它。
            await _send_frame(websocket, {
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

        ⚠️ 上传会话的作废**不在这里**：它是异步的，由 ``_websocket_endpoint``
        的 finally 显式 await（见 ``UploadManager.abort_for_conn``）。这里只做
        同步的连接状态清理。
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


# ---------- 分片上传：会话表（docs/http-upload-design.md） ----------
# 一次上传 = 一条 UploadSession，数据全走 HTTP PUT /api/upload/<sid>，
# WS 只在两头出现：协商（upload_begin / upload_ready）与取消（upload_cancel）。
# 会话表本身（含分片状态机与验签）在 server/upload.py，这里只管接线与 HTTP 边界。
#
# WS_MAX_FRAME_SIZE 是 WebSocket 单帧上限，同时通过 config 帧下发给手机端。
WS_MAX_FRAME_SIZE = 16 * 1024 * 1024

# 惰性实例：首次用到才创建（生命周期跟随服务，stop_server 里拆除）
_upload_manager: Optional[UploadManager] = None

# TTL 扫描周期（秒）。连接级作废是主力，这只是兜底，不必扫得太勤。
_UPLOAD_SWEEP_INTERVAL = 30.0


def _on_file_saved(path: str, name: str, size: int) -> None:
    """落盘完成 → 托盘通知（经事件桥交给主进程）。"""
    if _manager is not None:
        _manager.bridge.emit("file_saved", {"path": path, "name": name, "size": size})


def _on_photo_received(data: bytes, name, size: int) -> None:
    """图片收齐 → 主进程写系统剪贴板（Qt 必须在 GUI 线程执行）。"""
    if _manager is not None:
        _manager.bridge.emit("photo_received", {"data": data, "name": name, "size": size})


def _get_upload_manager() -> UploadManager:
    """返回上传会话表单例（首次调用时实例化）。"""
    global _upload_manager
    if _upload_manager is None:
        _upload_manager = UploadManager(
            on_file_done=_on_file_saved,
            on_photo_done=_on_photo_received,
        )
    return _upload_manager


async def _upload_sweeper() -> None:
    """TTL 兜底扫描：周期性作废过期会话（删 .part、摘密钥）。

    连接级作废（WS 关闭）才是主力，这里只兜两种边角：连接还活着但谁也不动了、
    以及服务端自己重启。挂在**服务线程的事件循环**上——会话表是进程级的，每条
    连接开一份纯属浪费。

    单轮扫描出错只记日志、继续下一轮：TTL 是最后一道兜底，不该因为某一次
    「磁盘忙 / 文件被占」就整体消失。取消（stop_server）时干净退出。
    """
    while True:
        try:
            await asyncio.sleep(_UPLOAD_SWEEP_INTERVAL)
        except asyncio.CancelledError:
            return
        try:
            await _get_upload_manager().sweep_expired()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("上传会话 TTL 扫描失败")


def set_bridge(bridge: EventBridge) -> None:
    """设置进程通信队列（需在启动服务前调用）。"""
    global _manager
    _manager = ConnectionManager(bridge)
    logger.info("Message bridge set for backend service")


def set_secure_channel(sc: SecureChannel) -> None:
    """设置安全通道引用。"""
    global _secure_channel
    _secure_channel = sc


# ---------- TOFU 审批机制 ----------
#
# 一条待审批连接 = 一个 ApprovalRequest 实例。实例自带 id、识别码、来源 IP、
# deadline 与「握手协程正在等的那一个 future」，因此「界面上显示的那条」与
# 「服务端正在等的那条」是同一个身份，不需要靠"当前全局变量"去猜。
#
# 旧做法是一个模块级 `_pending_approval` 加四个围着它转的自由函数，三条真实故障：
#   · 两条连接并发时 A 的请求被 B 覆盖——点「允许」同时落在 B 的 future 和 A 的
#     等待上，看着像"对上了"，实际是靠时序巧合；
#   · A 先超时 → `cancel_pending_approval()` 打在"此刻的全局"（已经是 B）上 ⇒
#     B 被误拒，而界面上还显示着 B 的识别码、用户一个按钮都没点；
#   · 用户手动点「拒绝」被记成超时（`reason = "timeout" if not approved ...` 恒取
#     timeout），日志与 close reason 都是错的。
# 现在状态只有一份（注册表）、结算入口只有一个（幂等），界面只看快照。


@dataclass(frozen=True)
class ApprovalDecision:
    """一条审批请求的终局。``reason`` 同时用作 WS close reason，必须如实。"""
    approved: bool
    reason: str          # accepted / rejected / timeout / superseded / disconnected


@dataclass
class ApprovalRequest:
    """一条待审批连接。

    ``id`` 是给界面用的身份：按钮回调带回来的就是它，而不是"当前那条"。
    ``future`` 的结果是 ApprovalDecision，由结算方（用户点击 / 超时 / 对端断开）
    写入；只有仍在注册表里的请求才允许写入，因此结算天然幂等。
    """

    id: str
    pin: str
    ip: str
    websocket: Any
    created: float
    deadline: float
    future: asyncio.Future
    state: str = "pending"

    def snapshot(self) -> dict:
        """给界面的不可变投影：界面不需要、也拿不到可变状态。

        ``remaining`` 给的是**剩余秒数**而不是绝对时刻：界面可能不在同一个时钟域
        里（bridge 换成跨进程实现后 monotonic 就不可比了），只有相对时长才成立。
        它只服务于显示——超时的权威始终是本实例自己的 deadline（见
        ``_await_approval``），界面拿它画倒计时，画到 0 也不代表已经超时。
        """
        return {
            "id": self.id,
            "pin": self.pin,
            "ip": self.ip,
            "remaining": max(0, round(self.deadline - time.monotonic())),
        }


class ApprovalRegistry:
    """待审批请求表——审批状态的唯一真源。

    只被 asyncio 事件循环线程触碰；Qt 线程唯一的入口是 ``resolve_approval()``
    的 ``call_soon_threadsafe``。每完成一次状态变更就推一份**全量快照**给界面，
    界面因此可以完全无状态（漏事件、重绘都能靠下一次快照自愈）。
    """

    def __init__(self) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}

    # ---- 查询 ----

    def items(self) -> List[ApprovalRequest]:
        """待审批请求，**新的在前**——界面只显示第一条（见 dashboard）。"""
        return sorted(self._requests.values(), key=lambda r: r.created, reverse=True)

    def pending_count(self) -> int:
        return len(self._requests)

    def head(self) -> Optional[ApprovalRequest]:
        items = self.items()
        return items[0] if items else None

    # ---- 变更 ----

    def request(self, pin: str, ip: str, websocket, timeout: float) -> ApprovalRequest:
        """登记一条新请求，并让同 IP 的旧请求让位。

        同 IP 取代：手机刷新页面 / 网络抖动重连时，旧那条已经没人在等（屏幕上
        显示的是新页面），留着只会占着队列、把并发数抬过风险提示的阈值制造误报。
        只让 **pending** 让位——已认证的连接不在这张表里，绝不能提前踢：新连接
        还没认证，提前踢掉旧的就成了一段时间内谁都连不上（抢占由
        ``ConnectionManager.connect()`` 在认证成功后完成）。
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._supersede(
            lambda other: other.ip == ip and other.websocket is not websocket,
            f"same-ip:{ip}",
        )
        req = ApprovalRequest(
            id=secrets.token_hex(8),
            pin=pin,
            ip=ip,
            websocket=websocket,
            created=now,
            deadline=now + timeout,
            future=loop.create_future(),
        )
        # 每连接独立的 deadline：等待时长出自实例自己，而不是一个裸常量，
        # 「超时」于是和「用户点了」「对端走了」一样，只是结算的一种。
        self._requests[req.id] = req
        logger.info(
            "TOFU approval requested: id=%s pin=%s ip=%s (pending=%d)",
            req.id, pin, ip, len(self._requests),
        )
        self._emit()
        return req

    def resolve(self, request_id: str, approved: bool) -> bool:
        """用户对**这一条**（id 指定）的决定。返回是否真的结算了它。

        批准时顺带清场：用户已经认定真机是哪一条，其余在等的都是来源不确定的
        连接，立刻作废（各自的 handler 会以 4032/superseded 关掉自己的连接）。
        """
        req = self._requests.get(request_id)
        if req is None:
            logger.debug(
                "Approval %s 已不在队列（已结算或被同 IP 的新连接取代），忽略本次点击",
                request_id,
            )
            return False
        if approved:
            self._supersede(lambda other: other.id != request_id, "approved-elsewhere")
        self._settle(req, approved, "accepted" if approved else "rejected")
        self._emit()
        return True

    def deny_all(self) -> int:
        """「全部拒绝」：一次性拒掉队列里所有待审批请求，返回条数。"""
        n = 0
        for req in list(self._requests.values()):
            if self._settle(req, False, "rejected"):
                n += 1
        if n:
            logger.info("TOFU approval: 全部拒绝，共 %d 条", n)
            self._emit()
        return n

    def expire(self, request_id: str, reason: str) -> bool:
        """等待方发现超时 / 对端已走时注销自己那条。

        reason 只能是 ``timeout`` 或 ``disconnected``——它会被写进 close reason，
        是排查"为什么这条没连上"的唯一线索，不能和"用户拒绝了"混为一谈。
        """
        req = self._requests.get(request_id)
        if req is None:
            return False
        self._settle(req, False, reason)
        self._emit()
        return True

    def cancel_for(self, websocket) -> bool:
        """连接断开：它那条请求不该再挂在界面上（否则点「允许」落在空处）。"""
        req = next(
            (r for r in self._requests.values() if r.websocket is websocket), None)
        if req is None:
            return False
        self._settle(req, False, "disconnected")
        self._emit()
        return True

    def reset(self) -> None:
        """服务停止时清空（连接随事件循环一起消失，留着就是幽灵请求）。"""
        if not self._requests:
            return
        logger.info("Clearing %d pending approval(s) on shutdown", len(self._requests))
        self._requests.clear()
        self._emit()

    # ---- 内部 ----

    def _settle(self, req: ApprovalRequest, approved: bool, reason: str) -> bool:
        """摘掉一条请求并写入终局（幂等：先从表里 pop 成功的才算数）。"""
        if self._requests.pop(req.id, None) is None:
            return False
        req.state = "approved" if approved else reason
        if not req.future.done():
            req.future.set_result(ApprovalDecision(approved, reason))
        logger.info(
            "TOFU approval settled: id=%s pin=%s ip=%s → %s",
            req.id, req.pin, req.ip, reason,
        )
        return True

    def _supersede(self, predicate, why: str) -> int:
        """让所有命中 predicate 的待审批请求作废（不推快照，由调用方统一推）。"""
        n = 0
        for req in list(self._requests.values()):
            if predicate(req) and self._settle(req, False, "superseded"):
                logger.info(
                    "TOFU approval superseded: id=%s pin=%s ip=%s (%s)",
                    req.id, req.pin, req.ip, why,
                )
                n += 1
        return n

    def _emit(self) -> None:
        """推全量快照。空队列也要推——「收起面板」由空快照表达，不另发隐藏事件。"""
        if _manager is None:
            return
        items = [r.snapshot() for r in self.items()]
        _manager.bridge.emit(
            "approval_snapshot", {"items": items, "pending": len(items)})


# 惰性单例（与 _transfer_queue 同规矩：不在模块级堆实例）
_approval_registry: Optional[ApprovalRegistry] = None


def _get_approval_registry() -> ApprovalRegistry:
    """返回审批注册表单例（首次调用时实例化）。"""
    global _approval_registry
    if _approval_registry is None:
        _approval_registry = ApprovalRegistry()
    return _approval_registry


def resolve_approval(request_id: Optional[str], approved: bool) -> None:
    """Dashboard（Qt 线程）调用：结算待审批请求。

    ``request_id`` 必须是界面当前显示那条请求的 id（队列第一条），服务端据此结算
    **那一条**——于是「用户看到的识别码」与「被结算的请求」必然出自同一份快照。
    传 ``None`` 且 ``approved=False`` 表示「全部拒绝」（界面仅在并发 ≥3 时给入口）。

    通过 ``loop.call_soon_threadsafe`` 投递到 asyncio 事件循环：注册表只被事件
    循环线程触碰，这是唯一允许的跨线程入口。
    """
    if request_id is None and approved:
        logger.warning("resolve_approval: 不支持「全部允许」，已忽略")
        return
    if _event_loop is None:
        logger.debug("resolve_approval: 事件循环未运行，忽略")
        return
    _event_loop.call_soon_threadsafe(_apply_approval_decision, request_id, approved)


def _apply_approval_decision(request_id: Optional[str], approved: bool) -> None:
    """在事件循环线程里执行结算（``resolve_approval`` 的唯一落点）。"""
    registry = _get_approval_registry()
    if request_id is None:
        registry.deny_all()
        return
    registry.resolve(request_id, approved)


async def _watch_peer_gone(websocket) -> None:
    """审批等待期间的「对端还在吗」探测器：返回即代表对端已经走了。

    审批等待期间协议规定手机不发任何帧（它只该显示识别码、然后等挑战），所以这里
    读到什么都无关紧要，只关心 disconnect。它的价值在于让「手机掉线」立刻从队列里
    消失，而不是让界面挂着一个已经没人在等的请求等满 30s。
    """
    while True:
        try:
            message = await websocket.receive()
        except asyncio.CancelledError:
            raise
        except Exception:
            return                      # 连接已经坏了，等价于对端已走
        if message.get("type") == "websocket.disconnect":
            return


async def _await_approval(req: ApprovalRequest, websocket) -> ApprovalDecision:
    """等这一条请求出结果：用户点了、超时了、还是对端已经走了。

    三种结局都从这里汇进注册表（状态只有一份、快照自动同步），调用方只拿到一个
    ApprovalDecision。刻意不用轮询：等的是「future 有结果」与「对端断开」两个
    事件里的先到者；超时预算取自**这条请求自己的 deadline**。
    """
    loop = asyncio.get_running_loop()
    watcher = loop.create_task(_watch_peer_gone(websocket))
    try:
        done, _ = await asyncio.wait(
            {req.future, watcher},
            timeout=max(0.0, req.deadline - loop.time()),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    if req.future in done:
        return req.future.result()
    _get_approval_registry().expire(
        req.id, "disconnected" if watcher in done else "timeout")
    # expire 幂等地写入了结果；若它发现已被别人结算，future 也已经是 done
    return await req.future


async def _close_quietly(websocket, code: int, reason: str) -> None:
    """尽力关闭连接：对端先走时 close 会抛（starlette 把断连转成 WebSocketDisconnect）。"""
    try:
        await websocket.close(code=code, reason=_close_reason(reason))
    except Exception as e:
        logger.debug(f"Failed to close connection ({code}/{reason}): {e}")



def get_secret_path() -> str:
    """返回当前安全通道的 secret_path（未设置时为空串）。

    算法/模式切换会重建 SecureChannel 并更新全局 _secure_channel，
    调用方应始终通过本函数读取最新值，避免持有过期引用。
    """
    return _secure_channel.secret_path if _secure_channel else ""


async def _send_frame(websocket, message: dict) -> None:
    """发送一帧消息，按该连接自身的会话状态决定是否加密。

    线上字节一律由 session.wrap() 产出：整帧加密，没有外层信封。加解密上下文取自
    连接自己的 SecureSession，而非共享对象，因此处于握手中的新连接不会改变活动
    连接的加密状态。未进入活动状态的连接（握手早期）没有 session，那一档走
    frame_encode 发明文帧——`auth_challenge` / `sealed` 正是这种情况（它们本身就
    是「明文帧装着密文」）。

    ⚠️ ``wrap()`` **必须留在锁内**：它内部会给 provider 的 ``_tx_seq`` 加一，而
    紧随其后的 ``send_bytes`` 是一个让出点。若在锁外取号，两个任务就可能出现
    「A 取到 5、B 取到 6，但 B 先发出去」⇒ 线上顺序颠倒、接收端判重放（§5.13）。
    第二个发送方是 HTTP handler 里的进度帧推送（`_read_upload_body`）。
    """
    manager = _manager
    if manager is None:
        await websocket.send_bytes(frame_encode(message))
        return
    async with manager.send_lock:
        session = manager.session_for(websocket)
        payload = session.wrap(message) if session is not None else frame_encode(message)
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


async def _try_send_bytes(websocket, payload: bytes, what: str) -> bool:
    """尽力发一帧；对端已断开时不让异常冒泡成 ASGI 层的一屏 traceback。

    握手失败的收尾常常发生在「对端已经关掉连接」之后（多数失败正是对端先关
    的），此时 send 会抛 ``WebSocketDisconnect`` / ``ClientDisconnected``。握手
    本身已经判定失败，这类异常唯一合理的归宿是日志里的一行，而不是 uvicorn
    打出来的整条栈——那会让人以为服务端崩了。

    Returns:
        True 表示发出去了，False 表示连接已不可用（调用方按失败处理即可）。
    """
    try:
        await websocket.send_bytes(payload)
        return True
    except Exception as e:
        logger.debug(f"Failed to send {what}: {e}")
        return False


async def _recv_handshake_frame(websocket, deadline: float):
    """在给定截止时刻前读取一条 binary 帧。

    deadline 按「单次等待」计算：auth 与 auth_proof 各有一份（见 _handle_auth），
    TOFU 首次连接的审批等待夹在两者之间、不占用任何一方的时间预算。

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
      TOFU 首次：auth(明文pk) → sealed(nonce+PIN, SealedBox) → 审批 →
        complete_tofu_auth → auth_challenge(SealedBox加密) → auth_proof

    TOFU 首次的识别码**由 PC 指派并密封下发**（e2ee-always-on-design.md §5.5.1）：
    auth 帧里没有它，手机端也不具备决定它的能力（它还没有 pc_public、无从密封），
    因此它不可能被同网段的窃听者抄走复用，也不存在"用可控输入撞同一个码"的枚举
    空间。下发顺序必须是「先 sealed 后审批」——用户开始核对之前，手机上必须已经
    有了一个只属于这条连接的识别码。

    所有失败都发生在 `_manager.connect()` 之前（调用方在返回 False 时短路），
    这正是「重放 auth 帧顶掉真机连接」那条 DoS 被顺带消掉的原因。

    审批阶段的每一次请求都是一个 ApprovalRequest 实例（见 ApprovalRegistry）：
    并发连接各自排队、界面只显示队首、用户按 id 结算，因此不再存在"A 的超时把 B
    拒掉"这类靠全局状态错位的故障。批准会顺带作废其余待审批请求，同 IP 的新连接
    会取代该 IP 的旧请求。

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
        # 注意：第三个返回值（识别码）在这里被故意丢弃——审批用的是 session.pin，
        # 那才是唯一的真源。虽然此刻二者相等，但从帧里取值会暗示「它由对端提供」。
        algo, session_key, _, phone_pk = session.receive_auth(data)
    except CryptoError as e:
        reason = str(e) or "auth data processing failed"
        logger.warning(f"Auth rejected: {reason}, closing 4001")
        await websocket.close(code=4001, reason=_close_reason(reason))
        return False

    if session.is_tofu_first:
        # TOFU 首次：先把 PC 指派的识别码密封下发给这一方，再进入人工审批。
        # 识别码本身由 session 生成（不由对端提供），可与后续挑战共用同一个 nonce。
        if not await _try_send_bytes(websocket, session.make_sealed_pin(), "sealed pin"):
            logger.warning("Failed to send sealed pin, closing")
            return False

        client_ip = websocket.client.host if websocket.client else "unknown"
        # 申请本身是一个实例：它带着自己的 id 进队列、出现在界面快照里，界面点
        # 「允许」时按 id 指回来，于是"显示的那条"与"结算的那条"必然同源。
        # APPROVAL_TIMEOUT 是这一条请求的自身的预算（同 IP 的旧请求此时已被取代）。
        registry = _get_approval_registry()
        req = registry.request(
            session.pin, client_ip, websocket, timeout=APPROVAL_TIMEOUT)
        decision = await _await_approval(req, websocket)
        if not decision.approved:
            # 拒因如实记录：用户拒绝 / 超时 / 被同 IP 的新连接取代 / 对端断开是
            # 四种不同的故障，混成一个字符串就没法排查了。
            logger.warning(
                "TOFU approval %s: id=%s pin=%s ip=%s, closing 4032",
                decision.reason, req.id, req.pin, client_ip,
            )
            await _close_quietly(websocket, 4032, decision.reason)
            return False
        # 审批通过：现在才做 ECDH + 创建 Provider
        session.complete_tofu_auth(algo, phone_pk)
        # 审批等待动辄数十秒，握手 deadline 早已过期——auth_proof 必须拿到
        # 一份全新的预算，否则「用户点了接受、握手却立刻超时」。
        # AUTH_TIMEOUT 的语义是「单次等待的上限」，不是「整轮握手的墙钟预算」。
        deadline = time.monotonic() + AUTH_TIMEOUT
    else:
        # URL fragment / TOFU 重连：session_key 已就绪
        session.create_provider(algo, session_key)

    # ---- 第 2 步：auth_challenge ----
    challenge_bytes = session.make_auth_challenge()
    if not await _try_send_bytes(websocket, challenge_bytes, "auth_challenge"):
        logger.warning("Failed to send auth_challenge, closing")
        return False

    # ---- 第 3 步：auth_proof ----
    raw, err = await _recv_handshake_frame(websocket, deadline)
    proof = session.unwrap(raw) if raw is not None else None
    if not session.verify_auth_proof(proof):
        # 拒因按「解不开 / nonce 不符」分开记：解不开几乎都是会话密钥不一致，
        # 笼统报 nonce mismatch 会把排查带偏（见 SecureSession.proof_rejection_reason）
        reason = err or session.proof_rejection_reason(proof)
        logger.warning(f"Auth proof rejected: {reason}, closing")
        await _try_send_bytes(
            websocket, session.wrap({"type": "error", "code": "auth"}), "auth error")
        await websocket.close(code=1000)
        return False

    logger.info(f"Auth succeeded, algorithm={session.negotiated_algorithm}")
    return True


# ---------- 分片上传：WS 协商与取消（docs/http-upload-design.md §5.1） ----------


async def _upload_error(websocket, tid, code: str, msg: str) -> None:
    """下行 ``upload_error``（① 阶段的拒绝）。

    ``msg`` **只进日志**：界面文案由客户端按 ``code`` 走自己的语言包，否则文案语言
    跟着电脑走、与手机界面不一致。单开一个 type 而不复用通用的 ``error``，是因为
    ``error`` 是**连接级**通道、上传的失败面是**流程内**的（要跟 id 对号、要跟那条
    进度气泡联动）。
    """
    logger.info("upload_begin 被拒: id=%r code=%s (%s)", tid, code, msg)
    await _send_frame(websocket, {
        "type": "upload_error",
        "id": tid,
        "code": code,
        "msg": msg,
    })


async def _handle_upload_begin(websocket, session, inner: dict, liveness) -> None:
    """① 阶段：校验 → 建会话（生成 sid 与两把一次性钥匙）→ 下发 ``upload_ready``。

    这一步顺带把三件事一次做完，所以省不掉：**校验大小**（避免白传）、
    **分配最终文件名**（重名编号只算一次）、**建立密钥绑定**（HTTP 请求才能验签与
    解密）。代价在局域网是几毫秒，在 Cloudflare 上是几十到几百毫秒——相对传输本身
    可忽略。
    """
    tid = inner.get("id")
    ref = inner.get("ref")
    name = inner.get("name")
    size = inner.get("size")

    err = UploadManager.validate_begin(ref, size, name)
    if err is not None:
        await _upload_error(websocket, tid, err,
                            f"ref={ref!r} size={size!r} name={name!r}")
        return

    if ref == "photo" and size > PHOTO_MAX_SIZE:
        # 全程在内存，必须防内存炸弹 ⇒ 在①阶段就拒，一个字节都还没传（§5.7）
        await _upload_error(websocket, tid, "too_large",
                            f"photo {size} > {PHOTO_MAX_SIZE}")
        return

    mgr = _get_upload_manager()
    try:
        upload = await mgr.begin(
            kind=ref,
            name=name,
            size=size,
            conn_id=id(websocket),
            websocket=websocket,
            algorithm=session.negotiated_algorithm,
            liveness=liveness,
        )
    except ValueError as e:
        await _upload_error(websocket, tid, "bad_args", str(e))
        return

    # k_mac / k_body 以明文放在帧里 —— 整帧已被 WS 会话密钥加密（session.wrap），
    # 所以线上字节是密文。⚠️ 这里刻意不打印这一帧的**内容**
    # （PHONEMIC_LOG=trace 下 uvicorn 也只能看到密文）。
    await _send_frame(websocket, {
        "type": "upload_ready",
        "id": tid,
        "sid": upload.sid,
        "chunk": mgr.chunk_size,
        "saved": upload.saved,
        "expires": int(mgr.session_ttl),
        "k_mac": upload.k_mac,
        "k_body": upload.k_body,
    })


async def _handle_upload_cancel(inner: dict) -> None:
    """上行 ``upload_cancel``：作废会话。**单向、幂等、无回帧。**

    取消必须走 WS 而不是靠客户端 ``xhr.abort()``：实测 abort 停不下服务端
    （CF 是缓冲代理，它手里已收下的字节仍会吐给 origin）。而剩下能通的两条路里
    WS 帧每条维度都更优——它已经在跑、已经认证过、不用另开连接、不需要回执。

    准入按连接做（一个连接只服务一台手机，连 ``sid`` 都不必校），但**状态按 sid 存**：
    HTTP 请求与 WS 是两个独立 handler，它们之间唯一的共享物就是会话表。
    """
    sid = inner.get("sid")
    if not isinstance(sid, str) or not sid:
        logger.warning("upload_cancel 缺少 sid，忽略")
        return
    await _get_upload_manager().abort_session(sid, "client")


async def _handle_client_message(websocket, session, raw: bytes, liveness=None) -> bool:
    """
    S1：处理认证后的单条 binary 消息。

    加密与否由会话状态机决定（session.unwrap 内部按 Provider 是否存在判断），
    不靠帧内容判别——因此加密帧没有外层信封，无法也不需要在解密前识别类型。
    解密后拒绝重复 auth，其余转发到事件桥。

    ``liveness``：本连接的应用层判活状态。上传会话要拿它——HTTP 活动也必须刷新
    心跳时间戳，否则一条健康的、跑在 CF 上的大文件上传会在 60 秒时因为「WS 判死 ⇒
    会话作废」把自己杀掉（§6.5，必做）。

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
        # 老的 WS 分块传输已退役（docs/http-upload-design.md §8）：文件与图片改走
        # HTTP 分片 PUT。这里回一帧明确的错误而不是静默丢弃——旧页面（未刷新的
        # 缓存）打到这条路径时，至少能在手机上看到一句原因，而不是"卡住不动"。
        logger.warning("收到已退役的 %s 帧（旧页面？），请刷新页面", msg_type)
        await _send_frame(websocket, {
            "type": "error",
            "code": "malformed",
            "msg": f"{msg_type} is retired, use upload_begin",
        })
    elif msg_type == "upload_begin":
        await _handle_upload_begin(websocket, session, inner, liveness)
    elif msg_type == "upload_cancel":
        await _handle_upload_cancel(inner)
    elif msg_type == "pong":
        # 应用层心跳应答（wire-protocol.md §7）。存活时间戳已在 _serve_messages
        # 的收帧处统一刷新，这里只需认下类型，避免落到 else 回一帧 malformed。
        logger.debug("Received pong")
    else:
        # ⚠️ `hello` 也落在这里（09-23 退役，见 wire-protocol.md §9.11）：它的唯一用途是
        # 给「取消」的三级等待补凭证，而新设计里取消是**单向**的（发 `upload_cancel`
        # 即本地 abort，不等回执、不做探活）⇒ 三级等待整条链不存在，这帧再无调用方。
        # 落到这里正是期望行为：回一帧 `error(malformed)`，让旧页面立刻看见明确原因。
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


async def _drain_request_body(request: Request, limit: Optional[int] = None,
                              timeout: Optional[float] = None) -> None:
    """把请求体读掉并丢弃，保证响应写出后服务端能干净地关闭连接。

    只在「不消费 body 就返回响应」的分支里调用。已经读过 body 的路径
    （_receive_client_log）不需要也不该再读——重复读会撞 Starlette 的
    RuntimeError("Stream consumed")。
    路由层直接给出的 405（非 GET/HEAD/POST 方法）不经过这里：那条路径只有扫描器
    会走，客户端读不读得到响应都无所谓。

    上传端点要传 ``limit``／``timeout``：默认那 64KB 是给扫描器用的量级，
    上传的 body 有 15MB，只读 64KB 再关连接照样 RST。
    """
    remaining = _DRAIN_MAX_BODY if limit is None else int(limit)
    deadline = time.monotonic() + (_DRAIN_TIMEOUT if timeout is None else float(timeout))
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
    logger.debug(f"请求体超过排空上限（{limit}），放弃继续排空")


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

    try:
        raw = await request.body()
    except ClientDisconnect:
        # 同族口子：`body()` 内部也是 `stream()`，对端半路走了照样抛。日志回传本来就是
        # 尽力而为，对端收不到响应也无所谓，别让它冒泡成 traceback。
        return Response(status_code=400)
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


# ---------- 分片上传：HTTP 端点（docs/http-upload-design.md §5.2） ----------
#
# 端点**不带页面前缀**（09-23 决定）：URL 固定是 /api/upload/<sid>，客户端写根绝对
# 路径。理由是「页面前缀只在页面加载那一刻有意义」——上传 URL 会出现在**每一片**的
# 请求行与访问日志里，带上它等于把入口前缀**持续**广播出去。`sid` 已是 24 字节真
# 随机，做选择器足够。⇒ 代价是下面 _normalize_path 的**两条**分支都要给它放行。
_UPLOAD_PATH_PREFIX = "/api/upload/"

# 排空上限按「片大小 + 余量」定，不能留一个小预算：只读 64KB 然后关连接，
# 剩下的十几 MB 仍然留在接收缓冲里 ⇒ 照样 RST。直觉上「读一点就关能省带宽」在
# 这里是错的：「关连接时接收缓冲还有未读数据」本身就是 RST 的充要条件（§6.1）。
# ⚠️ 这条排空**只花在「自己人但状态不对」的请求上**（验签已过、状态码要送达）；
# 「拿不出凭证」的请求一个字节都不读、直接关（§5.10「09-20 决定」）。
_UPLOAD_DRAIN_LIMIT = UPLOAD_CHUNK_SIZE + 64 * 1024
_UPLOAD_DRAIN_TIMEOUT = 20.0


def _parse_upload_headers(request: Request):
    """解析 ``X-Pm-Offset`` / ``X-Pm-Len`` / ``X-Pm-Mac``。

    任一缺失或格式不合法都返回 None ⇒ 调用方按「拿不出凭证」处理（401、
    **不读 body**）。验签只需要头，所以这一支能在**零 body 字节**时判出来——
    这正是「拒绝时按能不能拿出凭证分两类」这条规则可行的前提（§5.10）。
    """
    try:
        offset = int(request.headers["X-Pm-Offset"])
        length = int(request.headers["X-Pm-Len"])
    except (KeyError, ValueError, TypeError):
        return None
    mac = request.headers.get("X-Pm-Mac")
    if not mac:
        return None
    return offset, length, mac


def _make_progress_sender(session):
    """把「往这条连接的 WS 推一帧 `upload_progress`」包成 ``(sid, received)`` 回调。

    节流器在 ``server/upload.py``，它不 import 本模块（会成环）⇒ 发送动作由这里注入。
    """
    async def _send(sid: str, received: int) -> None:
        await _send_frame(session.websocket, {
            "type": "upload_progress",
            "sid": sid,
            "received": received,
        })
    return _send


async def _read_upload_body(request: Request, session, expected: int) -> Optional[bytes]:
    """把整片密文收进内存（供解密）；``None`` = 对端在收完之前把连接关掉了。

    ⚠️ 必须流式读、**不能** ``await request.body()``（§6.3）：那会把 15MB 一次读进
    内存且没有读超时，客户端只声明 Content-Length 却不发数据就能把 handler 吊住。

    ⚠️ 会话被取消时**继续读、只是不再累积**——一跳出读循环就返回，会让 CF 正在吐的
    十几 MB 留在接收缓冲里，照样触发 §6.1 的 RST（§5.6.3 约束 4）。多花约一秒换
    连接干净收尾，这一秒里它已经不再产出任何文件了。

    ⚠️ 但「对端走了」与「会话被取消」是两件不同的事，收尾也相反：取消时连接还在、
    数据照样会来，所以能读完；对端一关连接，``request.stream()`` 立刻抛
    ``ClientDisconnect``，**没有字节可等了**（``_drain_request_body`` 接住的也是
    同一个异常，口径一致）。这一档返回 ``None``，而不是把半截密文交出去——那只会
    在 ``commit_chunk`` 里变成一条 ``reason=decrypt:`` 的作废日志，把「客户端走了」
    伪装成「解密失败」，是最容易带偏排查方向的一类假信号。谁走的就让谁去清理：
    取消走 WS 的 ``upload_cancel``、断连走 ``abort_for_conn``、都没有就等 TTL；
    HTTP 层只管别把异常漏给 uvicorn 打出一整屏栈。

    ⚠️ **片内进度也在这里推**（§5.13）：必须在读循环里推，不能等 ``commit_chunk``
    —— 那是「整片收完、一次性 ``received += 15MB``」的阶梯，拿它当进度就只在整片
    落地时跳一格，而 CF 上一片要两三分钟。节流器**每片新建**：跨片会重复发一帧
    （值等于上一片响应里的 ``received``，客户端取 max 后无变化），代价一帧，换来
    它不必挂在会话上、生命周期与本次 PUT 严格一致。
    """
    throttle = ChunkProgressThrottle(
        session.sid, _make_progress_sender(session),
    )
    buf = bytearray()
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if session.abort_event.is_set():
                continue                      # 已取消：读完丢弃
            if len(buf) + len(chunk) > expected:
                continue                      # 超出声明长度（防御，正常不会发生）
            buf += chunk
            # ⚠️ 单位要换算：`len(buf)` 是**密文**字节数，而 `received` 的语义是**明文**
            # 字节数（HTTP 响应里那个值、会话累计、文件大小，三者同单位）。密文 =
            # nonce(24) ‖ AEAD(seq(8) ‖ 明文) ‖ tag(16)，所以这里减掉整份固定开销是
            # **保守**估计（尾部 tag 那 16 字节本来就不对应明文），误差 ≤ 48 字节 ——
            # 对 15MB 的片、乃至对百分比都无意义，但方向必须是「不超报」。
            await throttle.note(session.received + max(0, len(buf) - CHUNK_OVERHEAD))
    except ClientDisconnect:
        return None
    return bytes(buf)


def _upload_response(status: int, received: int) -> Response:
    """按 §5.2 那张响应表构造响应体：200 / 409 带 ``received``，其余空体。"""
    if status == 200:
        return JSONResponse({"received": received})
    if status == 409:
        return JSONResponse({"received": received}, status_code=409)
    return Response(status_code=status)


async def _serve_upload(request: Request, sid: str) -> Response:
    """``PUT /api/upload/<sid>``：收一片（§5.2 / §5.5 / §6.1）。

    判定顺序固定为 **验签 → offset 判定 → 解密**，不能颠倒（§5.8 前提 1）：
    只有 ``offset == received`` 的片才允许进解密，重复片与错位片在前两步就已处理掉
    ⇒ provider 那句「seq 必须严格相等」在重复片上不会被触发。
    """
    mgr = _get_upload_manager()

    parsed = _parse_upload_headers(request)
    if parsed is None:
        return Response(status_code=401)
    offset, length, mac = parsed

    session = mgr.get(sid)
    if session is None or not mgr.verify(session, offset, length, mac):
        # ⚠️ 401 绝不能顺手作废会话：LAN 下 sid 在 URL 里明文可见，一个只知道 sid 的
        # 人随便发个签名错的 PUT，如果它就能把会话作废，那**一个未认证的包就能毁掉
        # 别人正在传的文件**（纯 DoS）。作废只发生在验签通过之后的分支。
        return Response(status_code=401)

    # ---- 从这里起验签已过：每个拒绝分支都必须把 body 读完再回（§5.10 分类） ----

    # 零成本一致性检查：Content-Length 恒等于 X-Pm-Len + 48（两算法开销一致，§5.4）
    declared = request.headers.get("content-length")
    try:
        clen = int(declared) if declared is not None else -1
    except (ValueError, TypeError):
        clen = -1
    if clen != length + CHUNK_OVERHEAD:
        await _drain_request_body(request, limit=_UPLOAD_DRAIN_LIMIT,
                                  timeout=_UPLOAD_DRAIN_TIMEOUT)
        await mgr.abort_session(sid, "content-length mismatch")
        return Response(status_code=400)

    verdict = mgr.judge(session, offset, length)

    if verdict.action == "write":
        body = await _read_upload_body(request, session, length + CHUNK_OVERHEAD)
        if body is None:
            # 对端在半路关了连接：客户端取消（`_cancel` 里的 xhr.abort）与页面被切走
            # 都会走到这里。不解密、不 commit、**也不作废会话**（理由见
            # _read_upload_body 的第三段）。响应写出去没人收，只为让协议流程有个收尾。
            logger.info("上传分片时客户端断开: sid=%s offset=%d", sid, offset)
            return Response(status_code=400)
        result = await mgr.commit_chunk(session, offset, length, body)
    else:
        # 重复片 / 错位 / 超限：都要先把 body 读完再回，否则客户端拿不到状态码
        await _drain_request_body(request, limit=_UPLOAD_DRAIN_LIMIT,
                                  timeout=_UPLOAD_DRAIN_TIMEOUT)
        result = verdict

    if result.abort:
        # 先记下 received 再作废（作废之后会话就取不到了）；abort_session 幂等，
        # 已被取消的会话再调一次是空操作。
        received = result.received
        await mgr.abort_session(sid, result.reason)
        return _upload_response(result.status, received)

    if result.done:
        return JSONResponse({
            "received": result.received, "done": True, "saved": result.saved,
        })
    return _upload_response(result.status, result.received)


def _normalize_path(path: str) -> Optional[str]:
    """校验入口路径并归一化为内部路径，非法路径返回 None。

    加密模式：请求路径必须带 /{secret_path} 前缀（防扫描，根路由 404）；
    裸 URL（TOFU，secret_path 为空）：只放行白名单内的已知路径。
    secret 每次现读，算法切换时改值即时生效，无需重启服务器。

    ⚠️ ``/api/upload/`` 在**两条分支里都要放行**（它按 §5.2 不带页面前缀），
    只改一条会在另一种认证方式下静默 404。放行的含义只是「路径能到达 dispatcher」，
    **不是「谁都能上传」**——门禁是 ``X-Pm-Mac``。
    """
    secret = _secure_channel.secret_path if _secure_channel else ""

    if secret:
        if path == "/" + secret or path == "/" + secret + "/":
            return "/"
        if path.startswith("/" + secret + "/"):
            return path[len(secret) + 1:]
        if path.startswith(_UPLOAD_PATH_PREFIX):
            return path
        return None
    else:
        if path in _PUBLIC_PATHS:
            return path
        # 精确集合里放不下带 <sid> 的路径 ⇒ 单独给一条前缀匹配
        if path.startswith(_UPLOAD_PATH_PREFIX):
            return path
        if not is_frozen() and path == "/test":
            return path
        return None


async def _dispatch_http(request: Request, path: str) -> Response:
    """HTTP 统一入口：校验路径合法性后分发到具体资源。"""
    normalized = _normalize_path(path)

    # 写方法只有两个入口：手机端日志回传（POST）与上传分片（PUT），其余一律 405。
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

    if request.method == "PUT":
        if normalized is not None and normalized.startswith(_UPLOAD_PATH_PREFIX):
            # sid 只当字典键用、从不拼进文件路径（落盘名在 ① 阶段已由服务端分配），
            # 所以带 `/` 或 `..` 的伪 sid 只会查不到会话 ⇒ 401。
            return await _serve_upload(request, normalized[len(_UPLOAD_PATH_PREFIX):])
        # 打错的上传 URL 也会落到这里，body 同样是片大小量级 ⇒ 用上传的排空预算
        await _drain_request_body(request, limit=_UPLOAD_DRAIN_LIMIT,
                                  timeout=_UPLOAD_DRAIN_TIMEOUT)
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


async def _on_client_disconnect(request: Request, exc: ClientDisconnect) -> Response:
    """``ClientDisconnect`` 的兜底出口：留一行日志，不冒泡成整屏 traceback。

    每条读 body 的路径都该自己接住它（``_drain_request_body`` / ``_read_upload_body``
    / ``_receive_client_log`` 都接了），但这是一张网：将来漏掉一处只会让日志难看，
    不该让 uvicorn 打出一整屏栈——那看起来像服务端崩了（跟握手收尾同一条理由，
    见 ``_try_send_bytes``）。异常语义单一，不存在掩盖真实缺陷的风险。
    """
    logger.debug("客户端在响应前断开: %s", request.url.path)
    return Response(status_code=400)


app.add_exception_handler(ClientDisconnect, _on_client_disconnect)


async def root(request: Request) -> Response:
    """根路径入口。"""
    return await _dispatch_http(request, "/")


async def http_catchall(request: Request) -> Response:
    """catch-all HTTP 路由：处理带 /{secret_path} 前缀的所有静态资源。"""
    full_path = request.path_params["full_path"]
    return await _dispatch_http(request, "/" + full_path)


# 允许 POST：手机端日志回传走同一套路由（加密模式下路径带 secret 前缀，
# 无法单独注册一条固定路径，只能在分发处按方法分流）。
# 允许 PUT：上传分片 PUT /api/upload/<sid>（不带页面前缀，§5.2/§6.2）。⚠️ 不放行
# 的话请求会落在**路由层**直接 405、根本走不到 _dispatch_http，而且 PUT 是带 body
# 的 405 ⇒ 又回到 §6.1 那个 RST 坑。
# 不必放行 DELETE：取消已改走 WS 帧 upload_cancel（§5.6.1）。
app.add_route("/", root, methods=["GET", "HEAD", "POST", "PUT"])
app.add_route("/{full_path:path}", http_catchall, methods=["GET", "HEAD", "POST", "PUT"])


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
        # 兜底：连接没能走到审批结算就结束了（异常/提前返回），队列里不该留幽灵
        _get_approval_registry().cancel_for(websocket)
        # WS 一断，该连接名下的上传会话**全部立即作废**（§5.6.4）。必须在这里
        # await 而不是塞进 ConnectionManager.disconnect：作废要删 .part、关句柄，
        # 是异步的，而那个方法在若干同步路径上被调用。
        # ⚠️ 按**连接**作废而不是按 sid —— 一个连接只服务一台手机，
        # 别误伤别的连接名下正在跑的传输。
        if _upload_manager is not None:
            try:
                await _upload_manager.abort_for_conn(id(websocket), "ws_closed")
            except Exception:
                logger.exception("清理连接名下的上传会话失败")


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
        sweeper = None
        try:
            # TTL 扫描挂在服务线程自己的循环上（每条连接都开一份是浪费，
            # 而会话表是进程级的）
            sweeper = loop.create_task(_upload_sweeper())
            loop.run_until_complete(_server.serve())
        finally:
            if sweeper is not None:
                sweeper.cancel()
                try:
                    loop.run_until_complete(asyncio.gather(sweeper, return_exceptions=True))
                except Exception:
                    pass
            try:
                loop.close()
            except Exception:
                pass

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()
    logger.info(f"Starting PhoneMic backend server on {host}:{port}")


def stop_server() -> None:
    """停止后台服务。"""
    global _event_loop, _server, _server_thread, _upload_manager
    if _server is not None:
        _server.should_exit = True
    if _upload_manager is not None:
        # 调度一次全量作废（删 .part、摘密钥），并立刻把密钥从会话记录里摘掉；
        # 下面的 join 给它时间跑完。见 UploadManager.close。
        _upload_manager.close()
    # 连接随事件循环一起消失，队列里若还留着请求，界面就会挂着一个永远不会被
    # 结算的识别码（点「允许」落在空处）。推一份空快照让面板收起。
    _get_approval_registry().reset()
    if _server_thread is not None:
        _server_thread.join(timeout=5.0)
    _upload_manager = None
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
