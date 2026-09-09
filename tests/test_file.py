"""
tests/test_file.py — phonemic/gui/file.py 单元测试

覆盖 wire-protocol.md §9 / §9.2 的接收端约定：
validate 校验、状态机（start/data/end/cancel）、重名改号、
.tmp 清理、断连 abort_all、文件名路径剥离。
纯单测不碰网络，dest_dir 注入 tmp_path。
"""
import pytest

from phonemic.gui.file import (
    FileReceiver,
    validate_file_action,
    _allocate_path,
)


# ---------- validate_file_action ----------

class TestValidate:
    def test_valid_frames(self):
        assert validate_file_action({"a": "start", "id": 1, "name": "a.pdf", "size": 10, "chunks": 1})[0]
        assert validate_file_action({"a": "data", "id": 1, "n": 0, "chunk": b"xx"})[0]
        assert validate_file_action({"a": "end", "id": 1})[0]
        assert validate_file_action({"a": "cancel", "id": 1})[0]

    def test_not_a_dict(self):
        assert not validate_file_action("file")[0]
        assert not validate_file_action(None)[0]

    def test_unknown_action(self):
        assert not validate_file_action({"a": "open", "id": 1})[0]

    def test_missing_fields(self):
        assert not validate_file_action({"a": "start", "id": 1, "name": "a.pdf"})[0]
        assert not validate_file_action({"a": "data", "id": 1})[0]

    def test_negative_or_bool_int(self):
        # bool 是 int 子类，显式拒绝；负数同样拒绝
        assert not validate_file_action({"a": "start", "id": True, "name": "a", "size": 1, "chunks": 1})[0]
        assert not validate_file_action({"a": "data", "id": 1, "n": -1, "chunk": b""})[0]
        assert not validate_file_action({"a": "start", "id": 1, "name": "a", "size": -5, "chunks": 1})[0]

    def test_chunk_must_be_binary(self):
        assert not validate_file_action({"a": "data", "id": 1, "n": 0, "chunk": "text"})[0]

    def test_name_must_be_nonempty_string(self):
        assert not validate_file_action({"a": "start", "id": 1, "name": "", "size": 1, "chunks": 1})[0]
        assert not validate_file_action({"a": "start", "id": 1, "name": 123, "size": 1, "chunks": 1})[0]


# ---------- _allocate_path 重名改号 ----------

class TestAllocatePath:
    def test_no_conflict(self, tmp_path):
        p = _allocate_path(tmp_path, "a.pdf")
        assert p == tmp_path / "a.pdf"

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


# ---------- FileReceiver 状态机 ----------

def _start(fid=7, name="a.pdf", size=None, chunks=None):
    frame = {"a": "start", "id": fid, "name": name,
             "size": size if size is not None else 6, "chunks": chunks or 1}
    return frame


class TestReceiverFlow:
    def test_full_transfer(self, tmp_path):
        done = []
        rx = FileReceiver(dest_dir=tmp_path, on_done=lambda p, n, s: done.append((p, n, s)))

        ack, err = rx.handle(_start())
        assert ack is None and err is None

        ack, err = rx.handle({"a": "data", "id": 7, "n": 0, "chunk": b"hello "})
        assert err is None
        assert ack == {"type": "ack", "ref": "file", "id": 7, "n": 0, "received": 6}

        ack, err = rx.handle({"a": "data", "id": 7, "n": 1, "chunk": b"world"})
        assert ack["received"] == 11

        ack, err = rx.handle({"a": "end", "id": 7})
        assert ack is None and err is None

        assert (tmp_path / "a.pdf").read_bytes() == b"hello world"
        assert not (tmp_path / "a.pdf.part").exists()
        assert done == [(str(tmp_path / "a.pdf"), "a.pdf", 11)]

    def test_duplicate_rename(self, tmp_path):
        (tmp_path / "a.pdf").write_bytes(b"old")
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start())
        rx.handle({"a": "data", "id": 7, "n": 0, "chunk": b"new"})
        rx.handle({"a": "end", "id": 7})
        assert (tmp_path / "a(1).pdf").read_bytes() == b"new"
        assert (tmp_path / "a.pdf").read_bytes() == b"old"

    def test_rename_recheck_after_start(self, tmp_path):
        # start 之后、end 之前目标名被占用 → end 时再查重名
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start())
        (tmp_path / "a.pdf").write_bytes(b"stolen")
        rx.handle({"a": "data", "id": 7, "n": 0, "chunk": b"new"})
        rx.handle({"a": "end", "id": 7})
        assert (tmp_path / "a(1).pdf").read_bytes() == b"new"

    def test_cancel_discards_part_file(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start())
        rx.handle({"a": "data", "id": 7, "n": 0, "chunk": b"half"})
        assert (tmp_path / "a.pdf.part").exists()

        ack, err = rx.handle({"a": "cancel", "id": 7})
        assert ack is None and err is None
        assert not (tmp_path / "a.pdf.part").exists()
        assert not (tmp_path / "a.pdf").exists()

    def test_cancel_idempotent(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start())
        rx.handle({"a": "cancel", "id": 7})
        # 再 cancel / end 已无会话：幂等不报错
        assert rx.handle({"a": "cancel", "id": 7}) == (None, None)
        _, err = rx.handle({"a": "end", "id": 7})
        assert err is not None   # end 无会话算协议错误

    def test_data_without_start(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        _, err = rx.handle({"a": "data", "id": 9, "n": 0, "chunk": b"x"})
        assert err is not None

    def test_second_start_rejected(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start(fid=1))
        _, err = rx.handle(_start(fid=2))
        assert err is not None
        _, err = rx.handle(_start(fid=1))  # 同 id 重复 start 同样拒绝
        assert err is not None

    def test_invalid_frame_rejected(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        _, err = rx.handle({"a": "start", "id": 1})   # 缺字段
        assert err is not None
        assert list(rx._sessions) == []               # 不产生半初始化会话

    def test_name_strips_path_separators(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start(name="..\\..\\evil.txt"))
        rx.handle({"a": "end", "id": 7})
        assert (tmp_path / "evil.txt").exists()
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_abort_all_on_disconnect(self, tmp_path):
        rx = FileReceiver(dest_dir=tmp_path)
        rx.handle(_start())
        rx.handle({"a": "data", "id": 7, "n": 0, "chunk": b"partial"})
        rx.abort_all()
        assert not (tmp_path / "a.pdf.part").exists()
        assert list(rx._sessions) == []
