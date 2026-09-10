"""分块传输（file / photo）的写盘队列（wire-protocol.md §9）。

背景
----
早期实现里 file / photo 帧直接在收帧循环中 ``await to_thread(receiver.handle)``，
必须等这一块落盘才收下一帧。传文件期间同一条连接上的 mouse / key / preview
会一起排在落盘后面，表现为"传文件时鼠标卡"。

现在接收侧只负责入队，落盘与 ack 交给后台消费者串行执行，收帧循环立刻回去
处理控制帧。

背压
----
队列按**字节**限流（不是按块数）：分块大小是动态的（256KB ~ 15MB，见
``FilePanel.pickChunkSize``），按块计数在大块下会失控（32 块 × 15MB ≈ 480MB）。
``enqueue()`` 在驻留字节超过 ``max_bytes`` 时 await，此时不再 ``receive()``，
TCP 窗口关闭，手机端自然降速——不丢帧、不爆内存。

ack 语义
--------
保持"落盘才回"：ack 由消费者在写盘成功后发出，因此 ack 回来的速率恰好等于
真实落盘速率，背压信号不会失真。若改成"入队即回"，数据只是从网络挪进内存，
进度条会跑到实际落盘前面。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 队列驻留上限（字节）。约等于 2 个最大块，足以吸收写盘抖动，又不占内存。
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

# data 帧携带二进制块的字段名（file / photo 一致，见 wire-protocol.md §9）
CHUNK_FIELD = "chunk"


class TransferQueue:
    """file / photo 帧的串行写盘队列（惰性创建后台消费者）。

    :param file_receiver:  ``FileReceiver`` 实例（落盘状态机）
    :param photo_receiver:``PhotoReceiver`` 实例（内存重组状态机）
    :param sender:         异步回帧函数，签名 ``async (websocket, message) -> None``
    :param max_bytes:      队列驻留字节上限，超过则入队方 await（背压）
    """

    def __init__(
        self,
        file_receiver: Any,
        photo_receiver: Any,
        sender: Callable[[Any, dict], Awaitable[None]],
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._file_receiver = file_receiver
        self._photo_receiver = photo_receiver
        self._sender = sender
        self.max_bytes = int(max_bytes)

        # 以下状态在首次 enqueue 时按当前事件循环惰性创建（服务重启换 loop 会重建）
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._drain: Optional[asyncio.Event] = None
        self._queued_bytes = 0
        # 已请求取消的 (kind, id)：队列中残留的同 id 数据块直接丢弃，不必等排空
        self._cancelled_ids: set = set()

    # ---------- 生命周期 ----------

    def _ensure_worker(self) -> asyncio.Queue:
        """惰性创建队列与后台消费者：服务可能重启换事件循环，按 loop 判定重建。"""
        # 只允许在事件循环内调用（enqueue 是协程），用 get_running_loop 而非
        # get_event_loop：后者在无运行中循环时行为依赖全局状态，易受其它测试污染
        loop = asyncio.get_running_loop()
        if (self._loop is not loop or self._worker is None
                or self._worker.done()):
            self._loop = loop
            self._queue = asyncio.Queue()
            self._drain = asyncio.Event()
            self._drain.set()
            self._queued_bytes = 0
            self._worker = loop.create_task(self._consume_loop())
        return self._queue

    def close(self) -> None:
        """关闭后台消费者（线程安全：cancel 调度到所属事件循环执行）。

        用于跨线程场景（``stop_server``）。在事件循环内请用 ``aclose()``，
        它会把取消真正跑完，不留悬空任务。
        """
        self._cancelled_ids.clear()
        if self._worker is not None and not self._worker.done():
            try:
                self._loop.call_soon_threadsafe(self._worker.cancel)
            except (RuntimeError, AttributeError):
                pass
        self._worker = None
        self._loop = None
        self._queue = None
        self._drain = None
        self._queued_bytes = 0

    async def aclose(self) -> None:
        """关闭后台消费者并等待其真正结束（需在事件循环内调用）。"""
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._worker = None
        self._loop = None
        self._queue = None
        self._drain = None
        self._queued_bytes = 0
        self._cancelled_ids.clear()

    def abort_all(self) -> None:
        """断连清理：丢弃所有未完成会话，并清掉取消标记。

        队列里残留的块会在消费时因 ``_ws_gone`` 被跳过，不做无用功。
        """
        self._file_receiver.abort_all()
        self._photo_receiver.abort_all()
        self._cancelled_ids.clear()

    # ---------- 入队 ----------

    async def enqueue(self, kind: str, inner: dict, websocket) -> None:
        """把一条 file / photo 帧放进写盘队列；驻留字节超限时 await（背压）。

        cancel 帧立即记入取消集合（且不占字节额度），后续同 id 的残留数据块
        会被直接跳过，因此取消不等排空。标记在新会话 start 时才清除——
        若改为 cancel 处理完就清，晚到的残留块会漏网（取消的本质是"丢弃这一
        轮剩下的所有块"，与块何时到达无关）。
        """
        action = inner.get("a")
        tid = inner.get("id")
        if action == "cancel":
            self._cancelled_ids.add((kind, tid))
        elif action == "start":
            self._cancelled_ids.discard((kind, tid))

        chunk = inner.get(CHUNK_FIELD) or b""
        n = len(chunk)
        q = self._ensure_worker()

        if n and self.max_bytes > 0:
            # 单块就超过上限时直接放行，否则会永久等待（防御，正常不该发生）
            while n <= self.max_bytes and self._queued_bytes + n > self.max_bytes:
                self._drain.clear()
                if self._queued_bytes + n <= self.max_bytes:
                    break                       # clear 后立即复查，避免错过唤醒
                await self._drain.wait()
            self._queued_bytes += n

        await q.put((kind, inner, websocket, n))

    # ---------- 后台消费 ----------

    @staticmethod
    def _ws_gone(websocket) -> bool:
        """连接是否已断开（队列中残留任务直接丢弃，不做无用功）。"""
        state = getattr(websocket, "client_state", None)
        return state is not None and getattr(state, "name", "") != "CONNECTED"

    async def _consume_loop(self) -> None:
        """串行消费传输队列：写盘放线程，ack / error 回给对应连接。"""
        q = self._queue
        while True:
            kind, inner, websocket, n = await q.get()
            key = (kind, inner.get("id"))
            try:
                if inner.get("a") == "data" and key in self._cancelled_ids:
                    continue                      # 已取消，丢弃残留块
                if self._ws_gone(websocket):
                    continue
                if kind == "file":
                    ack, err = await asyncio.to_thread(
                        self._file_receiver.handle, inner)
                else:
                    ack, err = await asyncio.to_thread(
                        self._photo_receiver.handle, inner)
                if self._ws_gone(websocket):
                    continue
                if err is not None:
                    await self._sender(websocket, {
                        "type": "error",
                        "code": "malformed",
                        "msg": err,
                    })
                elif ack is not None:
                    await self._sender(websocket, ack)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("传输后台任务失败")
            finally:
                q.task_done()
                if n:
                    self._queued_bytes -= n
                    if self._drain is not None:   # close() 可能已清空（取消竞态）
                        self._drain.set()         # 同步唤醒等待中的入队方
