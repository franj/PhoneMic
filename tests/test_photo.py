"""
tests/test_photo.py — phonemic/gui/photo.py 单元测试（PhotoSink）

photo 与 file 的唯一区别是**落地方式**：file → 磁盘，photo → 剪贴板
（内存重组，不落盘）。所以这里只钉三件事：

- 内存重组出的字节与原图逐字节相等（收齐才交出去，残缺字节绝不进剪贴板）
- ``name`` 可省略（对剪贴板无意义，仅用于日志与托盘提示）
- 累计超过声明的 ``size`` 立刻拒绝（上限在会话层已把住，这里是纵深防御）

⚠️ 旧版这里是 PhotoReceiver 状态机（start/data/end/cancel + ack），
那些职责已搬到 ``server/upload.py``，见 ``test_upload.py::TestPhoto``。
"""
import pytest

from phonemic.gui.photo import PhotoSink


class TestPhotoSink:
    def test_reassembles_bytes_and_calls_on_done(self):
        got = []
        sink = PhotoSink(30, name="shot.png",
                         on_done=lambda data, name, size: got.append((data, name, size)))
        payload = b"0123456789" * 3
        sink.write(payload[:15])
        sink.write(payload[15:])
        data = sink.finish()
        assert data == payload
        assert got == [(payload, "shot.png", 30)]

    def test_name_optional(self):
        got = []
        sink = PhotoSink(3, on_done=lambda d, n, s: got.append((d, n, s)))
        sink.write(b"abc")
        sink.finish()
        assert got == [(b"abc", None, 3)]
        assert sink.saved == "", "没有文件名时 saved 为空（结果提示用）"

    def test_saved_mirrors_name(self):
        assert PhotoSink(1, name="x.png").saved == "x.png"

    def test_empty_image(self):
        got = []
        sink = PhotoSink(0, on_done=lambda d, n, s: got.append(d))
        assert sink.finish() == b""
        assert got == [b""]

    def test_over_declared_size_rejected(self):
        """拖到 end 才失败就晚了（上限必须在 ① upload_begin 阶段把住）。"""
        sink = PhotoSink(5, name="x.png")
        with pytest.raises(ValueError, match="超过声明的 size"):
            sink.write(b"123456")
        # 超限那一片被拒之后，缓冲里仍是**已经合法收下的**部分
        assert sink.finish() == b""

    def test_exactly_declared_size_is_ok(self):
        sink = PhotoSink(6, name="x.png")
        sink.write(b"123456")
        assert sink.finish() == b"123456"

    def test_abort_discards_buffer_and_is_idempotent(self):
        sink = PhotoSink(10, name="x.png")
        sink.write(b"partial")
        sink.abort()
        assert sink.finish() == b"", "作废后不该再交出任何字节"
        sink.abort()          # 幂等

    def test_callback_exception_is_swallowed(self):
        """on_done 抛异常不能冒泡（api.py 事件循环安全）。"""
        def boom(data, name, size):
            raise RuntimeError("clipboard failed")
        sink = PhotoSink(3, name="x.png", on_done=boom)
        sink.write(b"abc")
        assert sink.finish() == b"abc"      # 异常被吞，只记日志
