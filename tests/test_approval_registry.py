"""审批注册表的并发语义（``phonemic/server/api.py::ApprovalRegistry``）。

只测注册表本身：它决定「谁被批准、谁被换掉、谁超时」——审批状态的唯一真源。
不依赖真实 WS 连接，因此可以构造"两条不同来源的请求同时在等"这种在局域网里
（同 IP 才会互相取代）没法用真连接复现的场景。

界面按 id 结算、连接断开即消失这两条端到端行为，分别由
``test_dashboard_mode.py`` 与 ``test_e2ee_server.py`` 守住。
"""

import asyncio
import time

import pytest

from phonemic.server import api as api_mod


class FakeBridge:
    """只记录事件，不跨线程。"""

    def __init__(self):
        self.events = []

    def emit(self, event_type, payload=None):
        self.events.append((event_type, payload))


class FakeWS:
    """仅当身份用的假连接：注册表从不向对端发东西，连接的去留由 handler 负责。"""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<FakeWS {self.name}>"


@pytest.fixture
def env():
    """干净的注册表 + 记录桥（注册表通过模块级 _manager.bridge 推快照）。"""
    bridge = FakeBridge()
    api_mod.set_bridge(bridge)
    registry = api_mod._get_approval_registry()
    registry.reset()
    bridge.events.clear()
    yield bridge, registry
    registry.reset()
    bridge.events.clear()


def _run(coro):
    """在独立事件循环里跑场景：registry.request() 需要运行中的 loop。

    刻意不用 pytest-asyncio：本仓没有配 asyncio_mode，sync 测试里 asyncio.run()
    更直接，也不会把 loop 的生死交给插件。
    """
    return asyncio.run(coro)


def _snapshots(bridge):
    """取出所有快照事件（注册表每次状态变更都推一份全量快照）。"""
    return [p for t, p in bridge.events if t == "approval_snapshot"]


class TestConcurrency:
    """并发排队与结算。"""

    def test_two_ips_coexist_newest_first(self, env):
        """不同来源的请求同时在队列里，新的在前（界面只显示队首）。"""
        bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            b = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            return a, b

        a, b = _run(scenario())

        assert registry.pending_count() == 2
        assert [r.id for r in registry.items()] == [b.id, a.id]
        assert [r.pin for r in registry.items()] == ["2222", "1111"]
        snap = _snapshots(bridge)[-1]
        assert snap["pending"] == 2
        assert [i["id"] for i in snap["items"]] == [b.id, a.id]
        assert [i["pin"] for i in snap["items"]] == ["2222", "1111"]
        assert [i["ip"] for i in snap["items"]] == ["10.0.0.2", "10.0.0.1"]

    def test_approving_one_supersedes_the_rest(self, env):
        """批准一条 ⇒ 其余待审批立刻作废。

        用户按下「允许」等于宣布"真机就是这条"，队列里剩下的来源都不确定。
        这是旧实现里最危险的那条路径：A 被批准时 B 还挂在全局 future 上。
        """
        _bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            b = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            assert registry.resolve(b.id, True) is True
            return a, b

        a, b = _run(scenario())

        assert b.future.result() == api_mod.ApprovalDecision(True, "accepted")
        assert a.future.result() == api_mod.ApprovalDecision(False, "superseded")
        assert registry.pending_count() == 0

    def test_denying_one_keeps_the_others(self, env):
        """拒绝只是拒绝这一条：其余请求继续等用户决定，不被牵连。"""
        _bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            b = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            assert registry.resolve(a.id, False) is True
            return a, b

        a, b = _run(scenario())

        assert a.future.result() == api_mod.ApprovalDecision(False, "rejected")
        assert not b.future.done(), "B 不该被 A 的拒绝波及（旧实现会误拒 B）"
        assert registry.pending_count() == 1
        assert registry.head().id == b.id

    def test_resolve_unknown_id_is_noop(self, env):
        """对已结算的 id 再点一次：什么都不做。

        这条守住的是"批准即清场"不能被重复触发——否则用户手抖点两下，第二下
        会把队列里新来的请求一起清掉。
        """
        _bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            assert registry.resolve(a.id, True) is True
            b = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            # 界面还拿着 a 的 id（快照尚未刷新）时用户又点了一次
            assert registry.resolve(a.id, True) is False
            return b

        b = _run(scenario())

        assert not b.future.done(), "重复点击不得清掉后到的请求"
        assert registry.pending_count() == 1


class TestSupersedeAndExpiry:
    """取代、超时、掉线——"没连上"的三种不同原因的收尾。"""

    def test_same_ip_new_connection_replaces_old(self, env):
        """同 IP 的新连接取代旧请求。

        手机刷新页面 / 网络抖动重连都会留下一条没人在等的旧请求；不腾位置的话，
        自己重连两次就把并发数抬到风险提示的阈值以上——那是误报。
        """
        _bridge, registry = env

        async def scenario():
            old = registry.request("1111", "10.0.0.50", FakeWS("old"), timeout=30)
            new = registry.request("2222", "10.0.0.50", FakeWS("new"), timeout=30)
            return old, new

        old, new = _run(scenario())

        assert old.future.result() == api_mod.ApprovalDecision(False, "superseded")
        assert not new.future.done()
        assert registry.pending_count() == 1
        assert registry.head().id == new.id

    def test_same_ip_does_not_touch_other_ips(self, env):
        """同 IP 取代只针对同一个来源：别的设备还在等，不该被顺手清掉。"""
        _bridge, registry = env

        async def scenario():
            other = registry.request("1111", "10.0.0.7", FakeWS("other"), timeout=30)
            registry.request("2222", "10.0.0.50", FakeWS("a"), timeout=30)
            registry.request("3333", "10.0.0.50", FakeWS("b"), timeout=30)
            return other

        other = _run(scenario())

        assert not other.future.done()
        assert registry.pending_count() == 2

    def test_exit_reasons_are_recorded_verbatim(self, env):
        """超时 / 对端断开各自如实入账——它们最终会写进 close reason。

        旧实现把用户手动拒绝也记成 timeout（三元表达式恒取 timeout），日志与
        对端看到的原因全是错的，排查时被带偏。
        """
        _bridge, registry = env

        async def scenario():
            timed_out = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=0.01)
            gone = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            assert registry.expire(timed_out.id, "timeout") is True
            assert registry.cancel_for(gone.websocket) is True
            return timed_out, gone

        timed_out, gone = _run(scenario())

        assert timed_out.future.result() == api_mod.ApprovalDecision(False, "timeout")
        assert gone.future.result() == api_mod.ApprovalDecision(False, "disconnected")
        assert registry.pending_count() == 0

    def test_cancel_for_unknown_socket_is_noop(self, env):
        """连接断开时的收尾必须幂等：它会被 handler 的 finally 与探测器各调一次。"""
        _bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            assert registry.cancel_for(a.websocket) is True
            assert registry.cancel_for(a.websocket) is False
            assert registry.expire(a.id, "timeout") is False
            return a

        a = _run(scenario())

        assert a.future.result() == api_mod.ApprovalDecision(False, "disconnected")

    def test_deny_all_clears_the_queue(self, env):
        """「全部拒绝」是单个决定，不是逐条模拟点击：一次性清空。"""
        _bridge, registry = env

        async def scenario():
            reqs = [
                registry.request(str(1000 + i), f"10.0.0.{i}", FakeWS(i), timeout=30)
                for i in range(3)
            ]
            return reqs, registry.deny_all()

        reqs, n = _run(scenario())

        assert n == 3
        assert registry.pending_count() == 0
        for r in reqs:
            assert r.future.result() == api_mod.ApprovalDecision(False, "rejected")


class TestSnapshotProtocol:
    """快照契约：界面是纯函数，全靠它自愈。"""

    def test_empty_snapshot_is_emitted_too(self, env):
        """空队列也要推快照——「收起面板」由空快照表达，不另发隐藏事件。

        否则界面得自己判断"什么时候该撤下"，而那个判断正是旧实现里漏掉的一半
        （通知还在，请求早没了）。
        """
        bridge, registry = env

        async def scenario():
            a = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)
            registry.resolve(a.id, False)

        _run(scenario())

        assert _snapshots(bridge)[-1] == {"items": [], "pending": 0}

    def test_reset_clears_without_leaving_ghosts(self, env):
        """服务停止时清空：界面不该挂着一个永远不会被结算的识别码。"""
        bridge, registry = env

        async def scenario():
            registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)

        _run(scenario())
        assert registry.pending_count() == 1

        registry.reset()

        assert registry.pending_count() == 0
        assert _snapshots(bridge)[-1] == {"items": [], "pending": 0}

    def test_snapshot_never_leaks_the_websocket(self, env):
        """快照只带 id/pin/ip/remaining：界面拿不到可变状态，也维护不了第二份真相。

        ``remaining`` 是**相对时长**而不是 deadline 本身——界面可能与服务端不在
        同一个时钟域（bridge 换成跨进程实现后 monotonic 就不可比了），给绝对时刻
        会直接失效；给时长则界面只能画倒计时，超时的判定仍归服务端。
        """
        bridge, registry = env

        async def scenario():
            registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=30)

        _run(scenario())

        item = _snapshots(bridge)[-1]["items"][0]
        assert set(item) == {"id", "pin", "ip", "remaining"}

    def test_remaining_reflects_each_request_own_deadline(self, env):
        """每条请求的 remaining 出自它**自己**的 deadline，不是统一的常量。"""
        bridge, registry = env

        async def scenario():
            short = registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=2)
            longer = registry.request("2222", "10.0.0.2", FakeWS("b"), timeout=30)
            return short, longer

        short, longer = _run(scenario())

        items = {i["id"]: i for i in _snapshots(bridge)[-1]["items"]}
        assert items[short.id]["remaining"] < items[longer.id]["remaining"]
        assert 0 <= items[short.id]["remaining"] <= 2
        assert 0 <= items[longer.id]["remaining"] <= 30

    def test_remaining_clamped_at_zero(self, env):
        """已过期但还没被结算的请求显示 0，不显示负数。

        这个窗口真实存在：deadline 已过、等待方还没跑到 ``expire``，此时任何一次
        状态变更都会把这条一起画进快照——负数会直接显示在界面标题上。
        """
        bridge, registry = env

        async def scenario():
            return registry.request("1111", "10.0.0.1", FakeWS("a"), timeout=0.01)

        stale = _run(scenario())
        time.sleep(0.05)
        registry._emit()          # 下一次状态变更走的就是这条路径

        assert not stale.future.done(), "没人结算它——所以它确实还在表里"
        assert _snapshots(bridge)[-1]["items"][0]["remaining"] == 0
