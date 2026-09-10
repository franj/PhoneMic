"""
tests/test_photo.py — phonemic/gui/photo.py 单元测试

覆盖 wire-protocol.md §9 / §9.1 的 photo（图片到剪贴板）接收端约定：
validate 校验（name 可选）、状态机（start/data/end/cancel 内存重组）、
累计超声明 size 拒绝、cancel/abort_all 清理。纯单测不碰网络与剪贴板。
"""
import pytest

from phonemic.gui.photo import PhotoReceiver, validate_photo_action


# ---------- validate_photo_action ----------

class TestValidate:
    def test_valid_frames(self):
        # photo 的 name 对剪贴板无意义（§9.1），start 允许省略
        assert validate_photo_action({"a": "start", "id": 1, "size": 10, "chunks": 1})[0]
        assert validate_photo_action({"a": "start", "id": 1, "name": "a.png", "size": 10, "chunks": 1})[0]
        assert validate_photo_action({"a": "data", "id": 1, "n": 0, "chunk": b"xx"})[0]
        assert validate_photo_action({"a": "end", "id": 1})[0]
        assert validate_photo_action({"a": "cancel", "id": 1})[0]

    def test_not_a_dict(self):
        assert not validate_photo_action("photo")[0]
        assert not validate_photo_action(None)[0]

    def test_unknown_action(self):
        assert not validate_photo_action({"a": "save", "id": 1})[0]

    def test_missing_fields(self):
        assert not validate_photo_action({"a": "start", "id": 1, "size": 10})[0]
        assert not validate_photo_action({"a": "data", "id": 1})[0]
        assert not validate_photo_action({"a": "end"})[0]

    def test_negative_or_bool_int(self):
        assert not validate_photo_action({"a": "start", "id": True, "size": 1, "chunks": 1})[0]
        assert not validate_photo_action({"a": "data", "id": 1, "n": -1, "chunk": b""})[0]
        assert not validate_photo_action({"a": "start", "id": 1, "size": -5, "chunks": 1})[0]

    def test_chunk_must_be_binary(self):
        assert not validate_photo_action({"a": "data", "id": 1, "n": 0, "chunk": "text"})[0]


# ---------- PhotoReceiver 状态机 ----------

class TestReceiver:
    def test_full_flow_memory_reassembly(self):
        """start → data×3 → end：内存重组出完整字节并触发 on_done。"""
        received = []
        r = PhotoReceiver(on_done=lambda data, name, size: received.append((data, name, size)))
        payload = b"0123456789" * 3
        # 两块 15 字节 + 尾块 15 字节（模拟任意切块，不需要 256KB）
        chunks = [payload[0:15], payload[15:30]]
        assert r.handle({"a": "start", "id": 7, "name": "shot.png", "size": len(payload), "chunks": 2}) == (None, None)
        # data 块不再逐块回 ack（协议 §9）
        assert r.handle({"a": "data", "id": 7, "n": 0, "chunk": chunks[0]}) == (None, None)
        assert r.handle({"a": "data", "id": 7, "n": 1, "chunk": chunks[1]}) == (None, None)
        ack_end, err = r.handle({"a": "end", "id": 7})
        assert err is None and ack_end == {
            "type": "ack", "ref": "photo", "id": 7, "a": "end", "received": 30,
        }
        assert len(received) == 1
        data, name, size = received[0]
        assert data == payload and name == "shot.png" and size == 30
        # end 后会话已清空
        assert r.handle({"a": "end", "id": 7})[1] is not None

    def test_name_optional(self):
        """name 省略时 on_done 拿到 None（协议 §9.1）。"""
        received = []
        r = PhotoReceiver(on_done=lambda data, name, size: received.append((data, name, size)))
        assert r.handle({"a": "start", "id": 1, "size": 3, "chunks": 1}) == (None, None)
        assert r.handle({"a": "data", "id": 1, "n": 0, "chunk": b"abc"})[1] is None
        assert r.handle({"a": "end", "id": 1})[1] is None
        assert received[0][1] is None

    def test_end_rejects_incomplete(self):
        """收不齐就 end → 判失败，残缺字节不能进剪贴板。"""
        received = []
        r = PhotoReceiver(on_done=lambda data, name, size: received.append(data))
        assert r.handle({"a": "start", "id": 1, "size": 30, "chunks": 2})[1] is None
        assert r.handle({"a": "data", "id": 1, "n": 0, "chunk": b"0123456789"})[1] is None
        ack, err = r.handle({"a": "end", "id": 1})
        assert ack is None and "不完整" in err
        assert received == []

    def test_duplicate_start_rejected(self):
        r = PhotoReceiver()
        assert r.handle({"a": "start", "id": 1, "size": 10, "chunks": 1})[1] is None
        ok, err = r.handle({"a": "start", "id": 1, "size": 10, "chunks": 1})
        assert not ok and "重复 start" in err

    def test_second_concurrent_start_rejected(self):
        """v1 单会话：已有传输进行中不允许再开一个（§9 单会话单文件）。"""
        r = PhotoReceiver()
        assert r.handle({"a": "start", "id": 1, "size": 10, "chunks": 1})[1] is None
        ok, err = r.handle({"a": "start", "id": 2, "size": 10, "chunks": 1})
        assert not ok and "仅支持单文件" in err

    def test_data_over_declared_size_rejected(self):
        """累计字节超过 start 声明的 size → 协议错误，会话丢弃。"""
        r = PhotoReceiver()
        assert r.handle({"a": "start", "id": 1, "size": 5, "chunks": 1})[1] is None
        ok, err = r.handle({"a": "data", "id": 1, "n": 0, "chunk": b"123456"})
        assert not ok and "超过声明的 size" in err
        # 会话已丢弃，后续 end 找不到会话
        assert r.handle({"a": "end", "id": 1})[1] is not None

    def test_data_without_session(self):
        r = PhotoReceiver()
        ok, err = r.handle({"a": "data", "id": 9, "n": 0, "chunk": b"xx"})
        assert not ok and "无匹配会话" in err

    def test_cancel_discards_and_idempotent(self):
        r = PhotoReceiver()
        assert r.handle({"a": "start", "id": 1, "size": 10, "chunks": 1})[1] is None
        assert r.handle({"a": "cancel", "id": 1}) == (None, None)
        # 会话已清，重复 cancel 幂等成功；end 也找不到会话
        assert r.handle({"a": "cancel", "id": 1}) == (None, None)
        assert r.handle({"a": "end", "id": 1})[1] is not None

    def test_abort_all_on_disconnect(self):
        r = PhotoReceiver()
        assert r.handle({"a": "start", "id": 1, "size": 10, "chunks": 1})[1] is None
        r.abort_all()
        # 会话已清：end 找不到会话
        assert r.handle({"a": "end", "id": 1})[1] is not None

    def test_on_done_exception_not_raised(self):
        """on_done 抛异常不能冒泡（api.py 事件循环安全）。"""
        def boom(data, name, size):
            raise RuntimeError("clipboard failed")
        r = PhotoReceiver(on_done=boom)
        assert r.handle({"a": "start", "id": 1, "name": "x.png", "size": 3, "chunks": 1})[1] is None
        assert r.handle({"a": "data", "id": 1, "n": 0, "chunk": b"abc"})[1] is None
        assert r.handle({"a": "end", "id": 1})[1] is None   # 异常被吞，只记日志
