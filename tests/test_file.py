"""
tests/test_file.py — phonemic/gui/file.py 单元测试（FileSink）

覆盖 docs/http-upload-design.md §7 / wire-protocol.md §9.9 的**落盘**约定：

- ``sanitize_name`` 剥离路径分隔符（防目录穿越）
- ``_allocate_path`` 重名改号，且 ``.part`` 也算占用
- ``FileSink``：构造即建 ``.part`` → ``write*`` → ``finish`` 改名 / ``abort`` 清理

⚠️ 旧版这里是「分块喂字节的状态机」（``FileReceiver`` / ``validate_file_action``），
那些职责已搬到 ``server/upload.py`` 的会话状态机（见 ``test_upload.py``）；
本文件现在只管**磁盘**那一半，且所有方法都是同步的（调用方负责 to_thread）。
"""
from pathlib import Path

import pytest

from phonemic.gui.file import (
    DEFAULT_DEST_DIR,
    FileSink,
    _allocate_path,
    sanitize_name,
)


# ---------- sanitize_name ----------

class TestSanitizeName:
    def test_plain_name_untouched(self):
        assert sanitize_name("a.pdf") == "a.pdf"

    def test_strips_directory_components(self):
        assert sanitize_name("/tmp/x/a.pdf") == "a.pdf"
        assert sanitize_name("..\\..\\evil.txt") == "evil.txt"
        assert sanitize_name("dir/sub/a.pdf") == "a.pdf"

    def test_strips_surrounding_whitespace(self):
        assert sanitize_name("  a.pdf  ") == "a.pdf"

    @pytest.mark.parametrize("bad", ["", "   ", ".", "..", "./", "../"])
    def test_rejects_empty_and_dots(self, bad):
        with pytest.raises(ValueError):
            sanitize_name(bad)

    def test_non_string_is_coerced(self):
        assert sanitize_name(123) == "123"


# ---------- _allocate_path 重名改号 ----------

class TestAllocatePath:
    def test_no_conflict(self, tmp_path):
        assert _allocate_path(tmp_path, "a.pdf") == tmp_path / "a.pdf"

    def test_simple_conflict(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"x")
        assert _allocate_path(tmp_path, "a.pdf") == tmp_path / "a(1).pdf"

    def test_existing_seq_increments(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"x")
        (tmp_path / "a(3).pdf").write_bytes(b"x")
        # 已有序号取最大+1，而不是线性探测
        assert _allocate_path(tmp_path, "a.pdf") == tmp_path / "a(4).pdf"

    def test_conflict_on_seq_name(self, tmp_path):
        (tmp_path / "a(1).pdf").write_bytes(b"x")
        assert _allocate_path(tmp_path, "a(1).pdf") == tmp_path / "a(2).pdf"

    def test_no_extension(self, tmp_path):
        (tmp_path / "Makefile").write_bytes(b"x")
        assert _allocate_path(tmp_path, "Makefile") == tmp_path / "Makefile(1)"

    def test_multi_dot_keeps_last_suffix(self, tmp_path):
        (tmp_path / "a.tar.gz").write_bytes(b"x")
        assert _allocate_path(tmp_path, "a.tar.gz") == tmp_path / "a.tar(1).gz"

    def test_inflight_part_file_counts_as_taken(self, tmp_path):
        """⚠️ 在途的 ``.part`` 也是占用：否则同名两条会话会选到同一个最终名，
        然后一起写同一个 ``.part``（后开的那个 open('wb') 直接截断前一个）。"""
        (tmp_path / "a.pdf.part").touch()
        assert _allocate_path(tmp_path, "a.pdf") == tmp_path / "a(1).pdf"

    def test_seq_step_skips_part_taken_number(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"x")
        (tmp_path / "a(1).pdf.part").touch()
        assert _allocate_path(tmp_path, "a.pdf") == tmp_path / "a(2).pdf"


# ---------- FileSink ----------

class TestFileSink:
    def test_default_dest_dir_is_downloads_phonemic(self):
        # 常量在 import 期就算好了，改 Path.home 也改不动它 ⇒ 只钉形状
        assert DEFAULT_DEST_DIR.name == "PhoneMic"
        assert DEFAULT_DEST_DIR.parent.name == "Downloads"

    def test_writes_into_part_then_renames_on_finish(self, tmp_path):
        done = []
        sink = FileSink("a.pdf", dest_dir=tmp_path,
                        on_done=lambda p, n, s: done.append((p, n, s)))
        assert (tmp_path / "a.pdf.part").exists(), "构造即建 .part"
        assert not (tmp_path / "a.pdf").exists()
        assert sink.saved == "a.pdf"

        sink.write(b"hello ")
        sink.write(b"world")
        assert sink.written == 11
        # ⚠️ 这里**不能**读 .part 的内容：write() 走的是 Python 的文件缓冲，
        # 只有 finish() 才 flush。中途读盘会看到空文件，那是缓冲、不是丢数据。
        assert not (tmp_path / "a.pdf").exists(), "收尾前不占最终名"

        final = sink.finish()
        assert Path(final) == tmp_path / "a.pdf"
        assert (tmp_path / "a.pdf").read_bytes() == b"hello world"
        assert not (tmp_path / "a.pdf.part").exists()
        assert done == [(str(tmp_path / "a.pdf"), "a.pdf", 11)]

    def test_duplicate_rename(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"old")
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        assert sink.saved == "a(1).pdf"
        sink.write(b"new")
        sink.finish()
        assert (tmp_path / "a(1).pdf").read_bytes() == b"new"
        assert (tmp_path / "a.pdf").read_bytes() == b"old"

    def test_rename_recheck_at_finish(self, tmp_path):
        """分配号码之后、落盘之前目标名被占用 ⇒ finish 时再查一次。"""
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        (tmp_path / "a.pdf").write_bytes(b"stolen")
        sink.write(b"new")
        sink.finish()
        assert (tmp_path / "a(1).pdf").read_bytes() == b"new"
        assert sink.saved == "a(1).pdf", "saved 要跟着改（它会回给客户端）"

    def test_empty_file_finishes(self, tmp_path):
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        sink.finish()
        assert (tmp_path / "a.pdf").read_bytes() == b""

    def test_dest_dir_created_if_missing(self, tmp_path):
        target = tmp_path / "deep" / "nested"
        FileSink("a.pdf", dest_dir=target).abort()
        assert target.is_dir()

    def test_callback_exception_is_swallowed(self, tmp_path):
        def boom(p, n, s):
            raise RuntimeError("tray failed")
        sink = FileSink("a.pdf", dest_dir=tmp_path, on_done=boom)
        sink.write(b"x")
        assert Path(sink.finish()).exists(), "回调炸了不能影响落盘"

    # ---- 失败路径 ----

    def test_abort_removes_part_and_is_idempotent(self, tmp_path):
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        sink.write(b"half")
        assert (tmp_path / "a.pdf.part").exists()
        sink.abort()
        assert not (tmp_path / "a.pdf.part").exists()
        assert not (tmp_path / "a.pdf").exists()
        sink.abort()          # 幂等：取消与异常清理会重复调用
        assert not (tmp_path / "a.pdf.part").exists()

    def test_write_after_finish_raises(self, tmp_path):
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        sink.finish()
        with pytest.raises(RuntimeError):
            sink.write(b"late")

    def test_finish_after_abort_raises(self, tmp_path):
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        sink.abort()
        with pytest.raises(RuntimeError):
            sink.finish()

    def test_abort_after_finish_keeps_the_file(self, tmp_path):
        """收尾后再 abort（作废一条已完成的会话）不能把落好的文件删掉。"""
        sink = FileSink("a.pdf", dest_dir=tmp_path)
        sink.write(b"x")
        sink.finish()
        sink.abort()
        assert (tmp_path / "a.pdf").read_bytes() == b"x"

    def test_path_separator_in_name_is_stripped(self, tmp_path):
        sink = FileSink("..\\..\\evil.txt", dest_dir=tmp_path)
        assert sink.saved == "evil.txt"
        sink.write(b"x")
        sink.finish()
        assert (tmp_path / "evil.txt").exists()
        assert not (tmp_path.parent / "evil.txt").exists()
