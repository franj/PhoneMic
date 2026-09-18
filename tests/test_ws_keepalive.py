"""
tests/test_ws_keepalive.py — phonemic/server/api.py 应用层保活单元测试

覆盖 wire-protocol.md §7 的判活规则（原生 WebSocket 心跳已关闭）：

- 空闲达阈值时发出 `ping`；
- 持续空闲最终以 1011 关闭连接；
- 有业务消息（含文件数据块）时既不发 ping 也不判死 —— 这是大文件上传
  不再被误杀的**核心保证**；
- `pong` 在白名单内（不回 error），未知类型仍回 error（防回归）。

纯单测不碰网络：websocket 与 session 均为替身，`_manager` 置空使其走明文编码。
"""
import asyncio
import threading

import pytest

from phonemic.server import api
from phonemic.tunnel.frame import decode as frame_decode


def _run(coro):
    """在独立线程里跑一个新事件循环（与 test_transfer.py 同因）。

    不能直接用 asyncio.run()：Playwright 的同步 API（test_mobile.py）会在主线程
    留下运行中的 loop，asyncio.run 会抛 "cannot be called from a running event
    loop"。放到子线程里跑可完全隔离，不受前置用例残留的 loop 状态影响。
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

    t = threading.Thread(target=_worker, name="ws-keepalive-test-loop")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------- 替身 ----------

class _FakeWS:
    """最小 websocket 替身：记录下行帧与关闭码，用队列模拟 receive。"""

    def __init__(self):
        self.frames = []
        self.closed = None
        self._inbox = asyncio.Queue()

    async def receive(self):
        return await self._inbox.get()

    async def send_bytes(self, data):
        self.frames.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        # 真实 Starlette 在 close 之后会让 receive() 收到 disconnect，
        # 主循环靠它退出——替身必须复现这一点，否则用例会挂死。
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    def push(self, raw: bytes):
        """投递一帧「客户端 → 服务端」消息。"""
        self._inbox.put_nowait({"type": "websocket.receive", "bytes": raw})

    def push_disconnect(self, code=1000):
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    def types(self):
        """已发出帧的 type 列表（按顺序）。"""
        return [frame_decode(f).get("type") for f in self.frames]


class _FakeSession:
    """最小 session 替身：unwrap 直接返回预置消息（明文模式，不做加解密）。"""

    is_encrypted = False

    def __init__(self, message):
        self._message = message

    def unwrap(self, raw):
        return dict(self._message)


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """把 `_manager` 置空：_send_frame 走 frame_encode，不依赖连接注册表。"""
    monkeypatch.setattr(api, "_manager", None)


def _fast_thresholds(monkeypatch, idle=0.05, timeout=0.30, interval=0.02):
    """把三个阈值压到毫秒级，避免用例真的要等 45s / 60s。"""
    monkeypatch.setattr(api, "_KEEPALIVE_IDLE_BEFORE_PING", idle)
    monkeypatch.setattr(api, "_KEEPALIVE_TIMEOUT", timeout)
    monkeypatch.setattr(api, "_KEEPALIVE_CHECK_INTERVAL", interval)


# ---------- 判活行为 ----------

def test_idle_sends_ping_then_closes_with_1011(monkeypatch):
    """长时间无任何消息：先发 ping 探活，达超时后以 1011 关闭。

    这是「手机真的掉线」时的路径，关闭码沿用 1011 以兼容手机端既有日志解析。
    """
    _fast_thresholds(monkeypatch)

    async def main():
        ws = _FakeWS()
        await api._serve_messages(ws, None)
        return ws

    ws = _run(main())
    assert "ping" in ws.types(), "空闲时应发出应用层 ping"
    assert ws.closed is not None, "超时后应关闭连接"
    assert ws.closed[0] == 1011, "关闭码应为 1011（keepalive ping timeout）"
    assert "keepalive" in ws.closed[1]


def test_traffic_suppresses_ping_and_never_closes(monkeypatch):
    """持续有消息时既不发 ping 也不判死 —— 大文件上传免疫性的核心保证。

    模拟文件数据块以远小于空闲阈值的间隔到达，总时长已超过超时阈值。
    修复前的原生心跳在这里必然误杀（pong 排在数据之后）；应用层判活不会。
    """
    _fast_thresholds(monkeypatch, idle=0.05, timeout=0.15, interval=0.02)

    async def main():
        ws = _FakeWS()
        session = _FakeSession({"type": "pong"})

        async def feeder():
            for _ in range(12):
                await asyncio.sleep(0.02)
                ws.push(b"block")
            ws.push_disconnect(1000)

        feeder_task = asyncio.create_task(feeder())
        await api._serve_messages(ws, session)
        await feeder_task
        return ws

    ws = _run(main())
    assert ws.types() == [], "传输中不该发出任何心跳帧"
    assert ws.closed is None, "传输中不该被误杀"


def test_ping_is_sent_every_check_once_idle(monkeypatch):
    """空闲窗口内每轮检查都重发 ping：单次丢包不至于直接判死。"""
    _fast_thresholds(monkeypatch, idle=0.02, timeout=0.12, interval=0.02)

    async def main():
        ws = _FakeWS()
        await api._serve_messages(ws, None)
        return ws

    ws = _run(main())
    pings = [t for t in ws.types() if t == "ping"]
    assert len(pings) >= 2, f"空闲窗口内应多次探活，实际 {len(pings)} 次"


# ---------- 协议白名单 ----------

def test_pong_whitelisted_but_unknown_type_still_errors():
    """pong 认下且不回帧；未知类型仍回 malformed（确保白名单没放宽到失控）。"""

    async def main():
        ws = _FakeWS()
        ok_pong = await api._handle_client_message(
            ws, _FakeSession({"type": "pong"}), b"x")
        ok_unknown = await api._handle_client_message(
            ws, _FakeSession({"type": "definitely_not_a_type"}), b"x")
        return ws, ok_pong, ok_unknown

    ws, ok_pong, ok_unknown = _run(main())
    assert ok_pong is True and ok_unknown is True
    assert ws.types() == ["error"], "只有未知类型才该回帧"
    assert frame_decode(ws.frames[0])["code"] == "malformed"


# ---------- 阈值约束（防回归） ----------

def test_keepalive_thresholds_stay_within_cloudflare_idle_timeout():
    """阈值必须小于 Cloudflare 的 WebSocket idle 超时（约 100s）。

    否则会出现「CF 已掐断、服务端还以为连着」的窗口期：连接实际已死，
    服务端却还在等自己的超时，期间任何下行消息都会静默丢失。
    """
    assert api._KEEPALIVE_CHECK_INTERVAL < api._KEEPALIVE_IDLE_BEFORE_PING
    assert api._KEEPALIVE_IDLE_BEFORE_PING < api._KEEPALIVE_TIMEOUT
    assert api._KEEPALIVE_TIMEOUT < 100.0
