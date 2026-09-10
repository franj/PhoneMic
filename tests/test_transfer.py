"""
tests/test_transfer.py — phonemic/server/transfer.py 单元测试

覆盖分块传输写盘队列：
入队/串行消费、按字节背压、单块超限放行、cancel 丢弃晚到块、
start 清除取消标记、断连丢弃、abort_all 与 close。
纯单测不碰网络：websocket 与回帧函数均为替身，dest_dir 注入 tmp_path。
"""
import asyncio
import threading
import time

import pytest

from phonemic.gui.file import FileReceiver
from phonemic.gui.photo import PhotoReceiver
from phonemic.server.transfer import TransferQueue


# ---------- 替身 ----------

class _FakeState:
    def __init__(self, name):
        self.name = name


class _FakeWS:
    """最小 websocket 替身，只提供队列用于判断连接是否存活的 client_state。"""

    def __init__(self, connected=True):
        self.client_state = _FakeState("CONNECTED" if connected else "DISCONNECTED")


class _Recorder:
    """回帧函数替身：收集所有下行帧。"""

    def __init__(self):
        self.frames = []

    async def __call__(self, websocket, message):
        self.frames.append(message)


class _SlowReceiver:
    """阻塞式接收器替身：用于稳定观察背压挂起（不依赖真实 IO 耗时）。"""

    def __init__(self, delay=0.25):
        self.delay = delay
        self.calls = []
        self.aborted = False

    def handle(self, frame):
        self.calls.append(frame.get("a"))
        time.sleep(self.delay)
        return None, None

    def abort_all(self):
        self.aborted = True


def _run(coro):
    """在独立线程里跑一个新事件循环。

    不能直接用 asyncio.run()：Playwright 的同步 API（test_mobile.py）会在主线程
    留下运行中的 loop，asyncio.run 会抛 "cannot be called from a running event
    loop"（且同线程再开 loop 也不行）。放到子线程里跑可完全隔离，不受前置用例
    残留的 loop 状态影响。
    """
    box = {}

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:      # noqa: BLE001 - 原样抛回主线程
            box["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_worker, name="transfer-test-loop")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _make(tmp_path, **kw):
    rec = _Recorder()
    fr = FileReceiver(dest_dir=str(tmp_path))
    pr = PhotoReceiver()
    q = TransferQueue(fr, pr, rec, **kw)
    return q, fr, pr, rec


# ---------- 基本入队与消费 ----------

def test_full_file_transfer_roundtrip(tmp_path):
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        saved = {}
        fr.on_done = lambda path, name, size: saved.update(
            path=path, name=name, size=size)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "start", "id": 1, "name": "a.txt",
                                 "size": 4, "chunks": 1}, ws)
        await q.enqueue("file", {"a": "data", "id": 1, "n": 0,
                                 "chunk": b"abcd"}, ws)
        await q.enqueue("file", {"a": "end", "id": 1}, ws)
        await asyncio.sleep(0.05)          # 让后台消费者跑完
        await q.aclose()
        assert saved.get("size") == 4
        with open(saved["path"], "rb") as fh:
            assert fh.read() == b"abcd"
        assert any(f.get("type") == "ack" and f.get("ref") == "file"
                   and f.get("a") == "end" for f in rec.frames)

    _run(main())


def test_sequential_order_preserved(tmp_path):
    """多块按入队顺序写盘，内容不错乱。"""
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        saved = {}
        fr.on_done = lambda path, name, size: saved.update(path=path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "start", "id": 2, "name": "b.bin",
                                 "size": 6, "chunks": 3}, ws)
        for i, blk in enumerate((b"11", b"22", b"33")):
            await q.enqueue("file", {"a": "data", "id": 2, "n": i,
                                     "chunk": blk}, ws)
        await q.enqueue("file", {"a": "end", "id": 2}, ws)
        await asyncio.sleep(0.05)
        await q.aclose()
        with open(saved["path"], "rb") as fh:
            assert fh.read() == b"112233"

    _run(main())


# ---------- 按字节背压 ----------

def test_backpressure_blocks_when_over_budget():
    """驻留字节超限时 enqueue 挂起，直到消费者归还额度。"""
    async def main():
        slow = _SlowReceiver(delay=0.25)
        rec = _Recorder()
        q = TransferQueue(slow, slow, rec, max_bytes=1024)
        ws = _FakeWS()
        # 先占 512 字节，消费者取走后仍在 slow.handle 中阻塞，额度未归还
        await q.enqueue("file", {"a": "data", "id": 1, "n": 0,
                                 "chunk": b"x" * 512}, ws)
        pending = asyncio.ensure_future(q.enqueue(
            "file", {"a": "data", "id": 1, "n": 1, "chunk": b"y" * 1024}, ws))
        await asyncio.sleep(0.05)          # 远小于 slow.delay
        assert not pending.done(), "额度已满，第二个入队应当挂起"
        await asyncio.wait_for(pending, timeout=3.0)   # 归还后自动放行
        assert q._queued_bytes <= q.max_bytes
        await q.aclose()

    _run(main())


def test_oversized_single_chunk_is_not_blocked():
    """单块就超过额度时直接放行，避免永久等待。"""
    async def main():
        slow = _SlowReceiver(delay=0.01)
        rec = _Recorder()
        q = TransferQueue(slow, slow, rec, max_bytes=10)
        ws = _FakeWS()
        await asyncio.wait_for(q.enqueue(
            "file", {"a": "data", "id": 9, "n": 0, "chunk": b"z" * 4096}, ws),
            timeout=2.0)
        await q.aclose()

    _run(main())


def test_queued_bytes_returns_to_zero(tmp_path):
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "data", "id": 8, "n": 0,
                                 "chunk": b"q" * 100}, ws)
        await asyncio.sleep(0.05)
        assert q._queued_bytes == 0, "消费后字节额度必须归还"
        await q.aclose()

    _run(main())


# ---------- cancel / start 标记语义 ----------

def test_cancel_discards_late_chunks(tmp_path):
    """cancel 之后到达的同 id 数据块被丢弃，不产生'无匹配会话'错误帧。"""
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "start", "id": 3, "name": "c.txt",
                                 "size": 8, "chunks": 2}, ws)
        await q.enqueue("file", {"a": "data", "id": 3, "n": 0,
                                 "chunk": b"aaaa"}, ws)
        await q.enqueue("file", {"a": "cancel", "id": 3}, ws)
        await q.enqueue("file", {"a": "data", "id": 3, "n": 1,
                                 "chunk": b"bbbb"}, ws)   # 晚到的残留块
        await asyncio.sleep(0.05)
        await q.aclose()
        assert not [f for f in rec.frames if f.get("code") == "malformed"], \
            "已取消会话的晚到块应被静默丢弃"
        assert not fr._sessions, "cancel 后会话必须已作废"

    _run(main())


def test_start_clears_cancel_mark(tmp_path):
    """同一 id 重新 start 时清除取消标记，新传输不受上一轮影响。"""
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        saved = {}
        fr.on_done = lambda path, name, size: saved.update(path=path, size=size)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "cancel", "id": 4}, ws)
        await q.enqueue("file", {"a": "start", "id": 4, "name": "d.txt",
                                 "size": 4, "chunks": 1}, ws)
        await q.enqueue("file", {"a": "data", "id": 4, "n": 0,
                                 "chunk": b"dddd"}, ws)
        await q.enqueue("file", {"a": "end", "id": 4}, ws)
        await asyncio.sleep(0.05)
        await q.aclose()
        assert saved.get("size") == 4, "重新 start 后数据不应被当作残留块丢弃"
        assert ("file", 4) not in q._cancelled_ids

    _run(main())


def test_cancel_does_not_consume_byte_budget():
    """cancel 帧不占字节额度，保证取消永远能立即入队。"""
    async def main():
        slow = _SlowReceiver(delay=0.01)
        rec = _Recorder()
        q = TransferQueue(slow, slow, rec, max_bytes=10)
        ws = _FakeWS()
        await asyncio.wait_for(q.enqueue("file", {"a": "cancel", "id": 5}, ws),
                               timeout=2.0)
        await q.aclose()

    _run(main())


# ---------- 断连与错误回帧 ----------

def test_disconnected_websocket_is_skipped(tmp_path):
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS(connected=False)
        await q.enqueue("file", {"a": "data", "id": 6, "n": 0,
                                 "chunk": b"abcd"}, ws)
        await asyncio.sleep(0.05)
        await q.aclose()
        assert rec.frames == [], "连接已断开，不应再回帧"

    _run(main())


def test_error_frame_sent_for_bad_chunk(tmp_path):
    """data 帧没有匹配会话时，回 error 帧（malformed）。"""
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "data", "id": 77, "n": 0,
                                 "chunk": b"abcd"}, ws)
        await asyncio.sleep(0.05)
        await q.aclose()
        assert any(f.get("type") == "error" and f.get("code") == "malformed"
                   for f in rec.frames)

    _run(main())


# ---------- 生命周期 ----------

def test_abort_all_clears_state(tmp_path):
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "start", "id": 4, "name": "c.txt",
                                 "size": 4, "chunks": 1}, ws)
        await asyncio.sleep(0.02)
        assert fr._sessions, "会话应已建立"
        q._cancelled_ids.add(("file", 4))
        q.abort_all()
        assert not fr._sessions
        assert not q._cancelled_ids
        await q.aclose()

    _run(main())


def test_close_resets_and_allows_restart(tmp_path):
    async def main():
        q, fr, pr, rec = _make(tmp_path)
        ws = _FakeWS()
        await q.enqueue("file", {"a": "data", "id": 7, "n": 0,
                                 "chunk": b"a"}, ws)
        await asyncio.sleep(0.02)
        await q.aclose()
        assert q._worker is None and q._queue is None
        # 关闭后可再次使用（模拟服务重启后重新入队）
        await q.enqueue("file", {"a": "data", "id": 7, "n": 0,
                                 "chunk": b"a"}, ws)
        await asyncio.sleep(0.02)
        await q.aclose()

    _run(main())
