"""
tests/test_upload.py — 分片上传（docs/http-upload-design.md）

两层：

  1. **会话状态机**（``UploadManager``，不碰网络）：建会话、验签、offset 三行判定、
     解密写盘、收尾、作废、TTL。这一层是 §5.5 / §5.8 那些「写反了不报错」的规则
     的实际落点，所以覆盖得最细。
  2. **HTTP 端点**（真起服务）：``PUT /api/upload/<sid>`` 的头校验、两类排空规则、
     状态码，以及 §6.2 那条「两种认证模式下都要放行」的前缀规则。

手机端用一个 ``FakePhone`` 模拟：它自己做一遍「每片独立加密 + 头签名」，
与服务端 upload.py 用同一套原语（keyed BLAKE2b / 8 字节片序号前缀）。

⚠️ 三条会让测试**假失败**的坑，用例里都刻意绕开了：

  · 401 那一档服务端**不读 body 直接关连接**（§5.10 分类）⇒ 带 body 去探它会被
    RST 掉响应，测出来的是套接字故障而不是状态码。所以 401 一律用 ``probe()``
    发空 body。
  · 手机端 provider 的 seq 是个**计数器**：想造出「第 2 片的密文」必须先真的
    加密过第 1 片（见 ``test_out_of_order_ciphertext_is_rejected``）。
  · 「对端半路断开」只能靠 ``shutdown(SHUT_WR)`` 半关闭来造（``_raw_truncated``）：
    直接让 socket 析构会在接收缓冲还有未读数据时发 RST，服务端读到的就不是
    ``ClientDisconnect`` 而是套接字故障。而且那一档**可能根本读不到响应**（uvicorn
    收到 EOF 自己也会关传输）⇒ 别断言状态码，断言会话状态。
"""
import asyncio
import json
import multiprocessing
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from starlette.requests import ClientDisconnect
from websockets.sync.client import connect as ws_connect

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server import api as api_mod
from phonemic.server.api import (
    _UPLOAD_PATH_PREFIX,
    _normalize_path,
    set_bridge,
    set_secure_channel,
    start_server,
    stop_server,
)
from phonemic.server.upload import (
    CHUNK_OVERHEAD,
    PHOTO_MAX_SIZE,
    PROGRESS_TICK,
    UPLOAD_CHUNK_SIZE,
    ChunkProgressThrottle,
    UploadManager,
)
from phonemic.tunnel.crypto import (
    create_provider,
    decode_mac,
    encode_mac,
    upload_mac,
)
from phonemic.tunnel.e2ee import SecureChannel

from conftest import PhoneSimulator, get_test_port


# ---------- 辅助 ----------


def _run(coro):
    """在独立线程里跑一个新事件循环。

    理由：Playwright 的同步 API 会在主线程留下一个**运行中**的 loop，
    ``asyncio.run`` 会直接拒绝执行。
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

    t = threading.Thread(target=_worker, name="upload-test-loop")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class FakePhone:
    """模拟手机端的上传侧：一个独立 provider 实例 + 逐片签名。

    ⚠️ 与服务端跑 WS 的那个 provider **不是同一个实例**——seq 是单个计数器，
    两条独立 TCP 的到达顺序不由发送方决定（§5.8）。这里刻意分开建，正好也把
    这条约束测出来。
    """

    def __init__(self, algo: str, k_mac: bytes, k_body: bytes):
        self.algo = algo
        self.k_mac = k_mac
        self.provider = create_provider(algo, k_body)

    def chunk(self, sid: str, offset: int, data: bytes):
        """返回 ``(offset, length, mac_text, body_bytes)``。"""
        body = bytes(self.provider.encrypt(data))
        mac = upload_mac(self.k_mac, sid, offset, len(data))
        return offset, len(data), encode_mac(mac), body


async def _begin(mgr, kind="file", name="a.bin", size=12, conn_id=1, algo="xchacha20"):
    return await mgr.begin(
        kind=kind, name=name, size=size, conn_id=conn_id,
        websocket=object(), algorithm=algo, liveness=None,
    )


def _manager(tmp_path, **kw):
    kw.setdefault("dest_dir", tmp_path)
    kw.setdefault("chunk_size", 1024)
    return UploadManager(**kw)


# ========== 1. 会话状态机 ==========


class TestBegin:
    def test_validate_begin(self):
        assert UploadManager.validate_begin("file", 10, "a.txt") is None
        assert UploadManager.validate_begin("file", 10, None) is None
        assert UploadManager.validate_begin("photo", 10, None) is None
        assert UploadManager.validate_begin("weird", 10, "a") == "bad_ref"
        assert UploadManager.validate_begin("file", -1, "a") == "bad_args"
        assert UploadManager.validate_begin("file", True, "a") == "bad_args", "bool 是 int 的子类"
        assert UploadManager.validate_begin("file", "10", "a") == "bad_args"
        assert UploadManager.validate_begin("file", 10, 123) == "bad_args"

    def test_keys_are_fresh_and_split(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            a = await _begin(mgr)
            b = await _begin(mgr, name="b.bin")
            return a, b

        a, b = _run(main())
        assert len(a.k_mac) == 32 and len(a.k_body) == 32
        assert a.k_mac != a.k_body, "两把钥匙不能相同（各管一段）"
        assert a.sid != b.sid and a.k_body != b.k_body, "每次上传必须是新的一把"
        assert decode_mac(encode_mac(a.k_mac)) == a.k_mac

    def test_sink_and_saved_name(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            (tmp_path / "a.bin").write_bytes(b"x")      # 制造重名
            return await _begin(mgr)

        s = _run(main())
        assert s.saved == "a(1).bin", "重名要自动改号"
        assert (tmp_path / "a(1).bin.part").exists(), "建会话就该建好 .part"

    def test_part_file_blocks_same_name(self, tmp_path):
        """同名在途上传不能再选到同一个最终名（否则两边写同一个 .part）。"""
        async def main():
            mgr = _manager(tmp_path)
            return await _begin(mgr), await _begin(mgr)

        a, b = _run(main())
        assert a.saved != b.saved
        assert (tmp_path / "a.bin.part").exists()
        assert (tmp_path / "a(1).bin.part").exists()

    def test_photo_over_limit_is_rejected(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            with pytest.raises(ValueError):
                await _begin(mgr, kind="photo", name=None, size=PHOTO_MAX_SIZE + 1)

        _run(main())


class TestVerify:
    def test_accepts_correct_mac_rejects_everything_else(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            s = await _begin(mgr)
            good = encode_mac(upload_mac(s.k_mac, s.sid, 0, 4))
            return (
                mgr.verify(s, 0, 4, good),
                mgr.verify(s, 0, 4, encode_mac(upload_mac(s.k_mac, s.sid, 0, 5))),
                mgr.verify(s, 0, 4, encode_mac(upload_mac(s.k_mac, "other-sid", 0, 4))),
                mgr.verify(s, 8, 4, good),
                mgr.verify(s, 0, 4, "not-base64!!"),
                mgr.verify(s, 0, 4, None),
                mgr.verify(s, 0, 4, ""),
            )

        ok, wrong_len, wrong_sid, wrong_off, malformed, missing, empty = _run(main())
        assert ok is True
        assert not any([wrong_len, wrong_sid, wrong_off, malformed, missing, empty])


class TestJudge:
    """§5.5 的三行判定 —— 写反了不报错，所以每条都单独钉住。"""

    def test_write_when_offset_matches(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=12)
            return mgr.judge(s, 0, 4)

        v = _run(main())
        assert (v.action, v.status) == ("write", 200)

    def test_ignore_on_repeat_chunk(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=12)
            s.received = 8
            return mgr.judge(s, 0, 4), mgr.judge(s, 4, 4)

        v0, v1 = _run(main())
        assert (v0.action, v0.status) == ("ignore", 200)
        assert v1.action == "ignore", "整段已收的片同样只是重复"
        assert v0.abort is False, "⚠️ 重复片绝不能作废会话（否则录一片重放就能 DoS）"

    def test_abort_on_gap(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=12)
            s.received = 4
            return mgr.judge(s, 8, 4)

        v = _run(main())
        assert (v.action, v.status, v.abort) == ("abort", 409, True)

    def test_abort_on_partial_overlap(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=8)
            s = await _begin(mgr, size=24)
            s.received = 8
            return mgr.judge(s, 4, 8)

        v = _run(main())
        assert (v.action, v.status) == ("abort", 409)

    def test_chunk_larger_than_limit_is_413(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=100)
            return mgr.judge(s, 0, 5)

        v = _run(main())
        assert (v.action, v.status, v.abort) == ("abort", 413, True)

    def test_beyond_declared_size_is_413(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=16)
            s = await _begin(mgr, size=6)
            return mgr.judge(s, 4, 4)

        v = _run(main())
        assert (v.action, v.status, v.abort) == ("abort", 413, True)

    def test_negative_offset_is_rejected(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=8)
            return mgr.judge(s, -1, 4), mgr.judge(s, 0, -4)

        a, b = _run(main())
        assert a.status == 400 and b.status == 400

    def test_done_session_only_serves_idempotent_done(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=4)
            s.done = True
            s.received = 4
            return mgr.judge(s, 4, 0), mgr.judge(s, 0, 4)

        a, b = _run(main())
        assert (a.action, a.done, a.saved) == ("ignore", True, "a.bin"), \
            "已收尾的会话：重发最后一片要拿到幂等的 done"
        assert (b.action, b.done) == ("ignore", True)


class TestCommitChunk:
    def test_full_roundtrip_writes_file(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=10)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            results = []
            for off, data in ((0, b"0123"), (4, b"4567"), (8, b"89")):
                o, ln, _mac, body = phone.chunk(s.sid, off, data)
                results.append(await mgr.commit_chunk(s, o, ln, body))
            return results

        results = _run(main())
        assert [r.status for r in results] == [200, 200, 200]
        assert [r.received for r in results] == [4, 8, 10]
        assert results[-1].done is True
        assert results[-1].saved == "a.bin"
        assert (tmp_path / "a.bin").read_bytes() == b"0123456789"
        assert not list(tmp_path.glob("*.part"))

    def test_seq_prefix_never_reaches_the_file(self, tmp_path):
        """片序号是 provider 内部的 8 字节明文前缀，**不能**出现在文件里。

        ⚠️ 上传侧**不能**自己再剥一次（provider 交回的已经是应用明文）——
        剥两次会把正文头 8 字节吃掉，而片小于 9 字节时看起来「只是少了一点」。
        """
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=4)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.commit_chunk(s, o, ln, body)

        _run(main())
        assert (tmp_path / "a.bin").read_bytes() == b"abcd"

    def test_out_of_order_ciphertext_is_rejected(self, tmp_path):
        """片序号接不上 ⇒ 解密失败（另一条通道的片搬不过来）。

        ⚠️ 想得到「第 2 片」的密文，必须先真的加密过第 1 片 —— seq 是发送侧的
        计数器，不加密它就不会前进。
        """
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=8)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            phone.chunk(s.sid, 0, b"0123")               # tx_seq 前进到 1
            _o, _ln, _mac, body2 = phone.chunk(s.sid, 4, b"4567")
            return await mgr.commit_chunk(s, 0, 4, body2)

        v = _run(main())
        assert (v.action, v.status, v.abort) == ("abort", 409, True)

    def test_other_session_key_cannot_decrypt(self, tmp_path):
        """每次上传一把新 k_body ⇒ 别的会话的片根本解不开（§5.8 前提 2）。"""
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            victim = await _begin(mgr, size=8)
            other = await _begin(mgr, name="other.bin", size=8)
            phone = FakePhone(other.algorithm, other.k_mac, other.k_body)
            _o, _ln, _mac, body = phone.chunk(victim.sid, 0, b"abcd")
            return await mgr.commit_chunk(victim, 0, 4, body)

        v = _run(main())
        assert (v.action, v.status, v.abort) == ("abort", 409, True)

    def test_aborted_session_does_not_write(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=8)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.abort_session(s.sid, "client")
            return await mgr.commit_chunk(s, o, ln, body)

        v = _run(main())
        assert v.action == "abort"
        assert not (tmp_path / "a.bin").exists()
        assert not list(tmp_path.glob("*.part"))


class TestLifecycle:
    def test_abort_removes_part_and_keys(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            s = await _begin(mgr)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.commit_chunk(s, o, ln, body)
            existed = (tmp_path / "a.bin.part").exists()
            first = await mgr.abort_session(s.sid, "client")
            second = await mgr.abort_session(s.sid, "client")
            return existed, first, second, s

        existed, first, second, s = _run(main())
        assert existed
        assert first is True
        assert second is False, "作废必须幂等"
        assert not list(tmp_path.glob("*.part")), "取消要删掉半成品"
        assert s.k_mac is None and s.k_body is None

    def test_abort_all(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            await _begin(mgr, name="a.bin", conn_id=1)
            await _begin(mgr, name="b.bin", conn_id=2)
            n = await mgr.abort_all("test")
            return n, len(mgr)

        n, left = _run(main())
        assert n == 2 and left == 0
        assert not list(tmp_path.glob("*.part"))

    def test_finish_drops_body_key_but_keeps_mac(self, tmp_path):
        """收尾后 body 密钥摘掉，k_mac 留着——幂等 done 那条路要靠它验签。"""
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=4)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.commit_chunk(s, o, ln, body)
            return s, mgr.verify(s, 0, 4, mac), mgr.get(s.sid)

        s, again, still = _run(main())
        assert s.k_body is None and s.provider is None
        assert s.k_mac is not None
        assert again is True, "重发最后一片仍要能验签"
        assert still is s, "收尾后会话留到过期（幂等）"

    def test_repeat_last_chunk_gives_idempotent_done(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=4)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.commit_chunk(s, o, ln, body)
            return mgr.judge(s, o, ln)

        v = _run(main())
        assert (v.action, v.done, v.saved) == ("ignore", True, "a.bin")

    def test_zero_length_file(self, tmp_path):
        """空文件 = 一片空片（客户端把片数夹到至少 1），服务端不需要特判。"""
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            s = await _begin(mgr, size=0)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"")
            v = mgr.judge(s, o, ln)
            assert v.action == "write"
            return await mgr.commit_chunk(s, o, ln, body)

        v = _run(main())
        assert v.done is True
        assert (tmp_path / "a.bin").read_bytes() == b""

    def test_abort_for_conn_only_touches_that_conn(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path)
            a = await _begin(mgr, conn_id=1, name="a.bin")
            b = await _begin(mgr, conn_id=2, name="b.bin")
            indexed = mgr.sessions_for_conn(1)
            n = await mgr.abort_for_conn(1, "ws_closed")
            return indexed, n, mgr.get(a.sid), mgr.get(b.sid)

        indexed, n, a, b = _run(main())
        assert len(indexed) == 1, "按连接索引应只查到本连接的那一条"
        assert n == 1
        assert a is None, "本连接的会话必须全作废"
        assert b is not None, "⚠️ 一个连接只服务一台手机，别误伤别人的上传"

    def test_ttl_expiry_and_sweep(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, session_ttl=10.0)
            s = await _begin(mgr, size=8)
            s.expires_at = time.monotonic() - 1       # 直接让它过期
            gone = mgr.get(s.sid)
            n = await mgr.sweep_expired()
            return gone, n, mgr.get(s.sid)

        gone, n, after = _run(main())
        assert gone is None, "过期即视为「拿不出凭证」"
        assert n == 1 and after is None
        assert not list(tmp_path.glob("*.part"))

    def test_valid_chunk_extends_ttl(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4, session_ttl=100.0)
            s = await _begin(mgr, size=8)
            before = s.expires_at
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            o, ln, _mac, body = phone.chunk(s.sid, 0, b"abcd")
            await mgr.commit_chunk(s, o, ln, body)
            return before, s.expires_at

        before, after = _run(main())
        assert after > before, "每次收到合法分片都要向后顺延"


class TestPhoto:
    def test_photo_goes_to_clipboard_not_disk(self, tmp_path):
        async def main():
            mgr = _manager(tmp_path, chunk_size=4)
            got = {}
            mgr.on_photo_done = lambda data, name, size: got.update(
                data=data, name=name, size=size)
            s = await _begin(mgr, kind="photo", name="pic.png", size=8)
            phone = FakePhone(s.algorithm, s.k_mac, s.k_body)
            for off, data in ((0, b"AAAA"), (4, b"BBBB")):
                o, ln, _mac, body = phone.chunk(s.sid, off, data)
                await mgr.commit_chunk(s, o, ln, body)
            return got

        got = _run(main())
        assert got["data"] == b"AAAABBBB"
        assert got["name"] == "pic.png"
        assert list(tmp_path.iterdir()) == [], "photo 不落盘"


# ========== 2. HTTP 端点 ==========


def _wait_ready(host, port, secret_path="", timeout=8.0):
    base = f"http://{host}:{port}/{secret_path}/" if secret_path else f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(base, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


def _raw(host, port, method, path, headers, body=b"", timeout=8.0):
    """裸 socket 发一个请求，返回 ``(状态码, 原始响应文本)``。

    用裸 socket 而不是 urllib：要精确控制 Content-Length 与 X-Pm-Len 的不一致
    （那条 400 分支），urllib 会把两者算成一样。
    """
    head = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close"]
    head += [f"{k}: {v}" for k, v in headers.items()]
    head.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body

    buf = b""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(raw)
        sock.settimeout(timeout)
        while True:
            try:
                part = sock.recv(65536)
            except (socket.timeout, ConnectionResetError, OSError):
                break
            if not part:
                break
            buf += part
            if b"\r\n\r\n" in buf:
                head_bytes, rest = buf.split(b"\r\n\r\n", 1)
                clen = 0
                for line in head_bytes.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        try:
                            clen = int(value.strip())
                        except ValueError:
                            clen = 0
                if len(rest) >= clen:
                    break

    if not buf:
        return None, ""
    first = buf.split(b"\r\n", 1)[0].decode("latin-1")
    try:
        status = int(first.split(" ")[1])
    except (IndexError, ValueError):
        status = None
    return status, buf.decode("latin-1", "replace")


def _upload_headers(offset, length, mac_text):
    return {"X-Pm-Offset": str(offset), "X-Pm-Len": str(length), "X-Pm-Mac": mac_text}


def _raw_truncated(host, port, path, headers, declared_len, partial=b"", *,
                   method="PUT", timeout=8.0):
    """声明完整 Content-Length，却只发一部分就半关闭（FIN）。

    复刻真机现场：客户端点「取消」（``xhr.abort()``）或页面被切走时，服务端在
    ``request.stream()`` 里读到的是 ``ClientDisconnect``。

    ⚠️ 必须 ``shutdown(SHUT_WR)`` 半关闭，不能直接让 socket 析构——接收缓冲里还有未读
    数据时 close 会发 RST，服务端看到的就不是「对端走了」而是套接字故障（与 §6.1 的
    RST 是同一件事）。半关闭之后**也可能读不到任何响应**：uvicorn 收到 EOF 自己也会关
    传输，那时它写不出字节。⇒ 返回 ``(None, "")`` 是合法结果，用例别断言状态码。
    """
    head = [f"{method} {path} HTTP/1.1", f"Host: {host}:{port}", "Connection: close"]
    head += [f"{k}: {v}" for k, v in headers.items()]
    head.append(f"Content-Length: {declared_len}")
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + partial

    buf = b""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(raw)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.settimeout(timeout)
        while True:
            try:
                part = sock.recv(65536)
            except (socket.timeout, ConnectionResetError, OSError):
                break
            if not part:
                break
            buf += part

    if not buf:
        return None, ""
    first = buf.split(b"\r\n", 1)[0].decode("latin-1")
    try:
        status = int(first.split(" ")[1])
    except (IndexError, ValueError):
        status = None
    return status, buf.decode("latin-1", "replace")


def _body_json(text):
    """从原始响应里取出 JSON 体（断言别去抠字符间距）。"""
    _, _, body = text.partition("\r\n\r\n")
    return json.loads(body) if body.strip() else {}


class _UploadHarness:
    """一台真服务 + 一条已协商好的上传会话，外加「按片 PUT」的便捷方法。"""

    def __init__(self, host, port, ws, sim, dest_dir, ready, algo):
        self.host, self.port = host, port
        self.ws, self.sim = ws, sim
        self.dest_dir = dest_dir
        self.ready = ready
        self.sid = ready["sid"]
        self.phone = FakePhone(algo, bytes(ready["k_mac"]), bytes(ready["k_body"]))
        self._sent = {}          # (offset, data) → 已发出的那一片（见 _encode）

    @property
    def path(self):
        return f"{_UPLOAD_PATH_PREFIX}{self.sid}"

    def _encode(self, offset, data):
        """把一片编码出来，**同样的 (offset, data) 只编码一次**。

        ⚠️ 这条缓存是「重复片」用例成立的前提，不是优化。真实的重传是**原样重发
        同一段密文**（seq 与其它字节都不变），服务端靠 ``offset + length <= received``
        一眼认出是重复片、回 200 忽略掉。若每次都重新加密，tx_seq 会前进——那在
        语义上已经是「另一片」，服务端解密时 seq 对不上，只能按作废处理。
        """
        key = (offset, bytes(data))
        if key not in self._sent:
            self._sent[key] = self.phone.chunk(self.sid, offset, data)
        return self._sent[key]

    def put(self, offset, data, *, mac=None, length=None, body=None):
        """发一片（body 默认是该片的真实密文）。"""
        o, ln, mac_text, enc = self._encode(offset, data)
        if mac is not None:
            mac_text = mac
        if length is not None:
            ln = length
        return _raw(self.host, self.port, "PUT", self.path,
                    _upload_headers(o, ln, mac_text), enc if body is None else body)

    def probe(self, offset, length, mac_text, body=b""):
        """不带货的探针，专打「服务端不读 body」的 401 分支。

        ⚠️ 那一档不排空 body ⇒ 带着 body 去探会被 RST 掉响应，测出来的是
        套接字故障而不是状态码。空 body 才测得到状态码。
        """
        return _raw(self.host, self.port, "PUT", self.path,
                    _upload_headers(offset, length, mac_text), body)

    def drain_frames(self, timeout=0.4):
        """把 WS 上**已经到达**的下行帧全读出来（读空即返回）。

        进度帧是服务端在读 body 的过程中推的，与 PUT 的响应不同步 ⇒ 不能只 ``recv``
        一次就断言。读空表现为 ``recv`` 超时；解帧失败则原样抛出去（那是真问题，
        别被这个 helper 吞掉）。
        """
        out = []
        while True:
            try:
                raw = self.ws.recv(timeout=timeout)
            except Exception:
                return out
            out.append(self.sim.decrypt(raw))

    def saved_path(self):
        return self.dest_dir / self.ready["saved"]


@pytest.fixture
def upload_server(tmp_path, monkeypatch):
    """真服务 + 一条已协商好的上传会话（file，10 字节 = 4 + 4 + 2）。

    ⚠️ 声明的 size 必须与用例实际要发的字节数一致：不一致的话最后那一片不会
    ``done``，文件一直停在 ``.part``（看起来像"落盘丢了"）。

    落盘目录指到 tmp_path：默认是 ``~/Downloads/PhoneMic``，测试不该往用户目录写。
    """
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    host, port = "127.0.0.1", get_test_port()
    sc = SecureChannel(auth_method="url_fragment")
    set_secure_channel(sc)

    mgr = UploadManager(dest_dir=tmp_path)
    monkeypatch.setattr(api_mod, "_upload_manager", mgr)

    start_server(host, port, bridge)
    assert _wait_ready(host, port, sc.secret_path), "服务未在超时内就绪"

    sim = PhoneSimulator(sc.get_public_key_b64(), algo="xchacha20")
    ws = ws_connect(f"ws://{host}:{port}/{sc.secret_path}/ws", max_size=None)
    sim.handshake(ws)
    # 握手后服务端会推 config 帧，先消费掉，保持两端 seq 对齐
    cfg = sim.decrypt(ws.recv(timeout=5))
    assert cfg["type"] == "config"

    ws.send(sim.encrypt({"type": "upload_begin", "id": 1,
                         "ref": "file", "name": "a.bin", "size": 10}))
    ready = sim.decrypt(ws.recv(timeout=5))
    assert ready["type"] == "upload_ready", ready

    session = mgr.get(ready["sid"])
    assert session is not None, "服务端没建出会话"
    h = _UploadHarness(host, port, ws, sim, tmp_path, ready, session.algorithm)
    try:
        yield h
    finally:
        try:
            ws.close()
        except Exception:
            pass
        stop_server()
        set_secure_channel(None)
        time.sleep(0.3)


class TestUploadEndpoint:
    def test_ready_carries_chunk_and_keys(self, upload_server):
        ready = upload_server.ready
        assert ready["chunk"] == UPLOAD_CHUNK_SIZE
        assert ready["saved"] == "a.bin"
        assert len(bytes(ready["k_mac"])) == 32
        assert len(bytes(ready["k_body"])) == 32
        assert bytes(ready["k_mac"]) != bytes(ready["k_body"]), "两把钥匙必须分开"
        assert ready["expires"] > 0

    def test_chunk_overhead_is_48(self, upload_server):
        """Content-Length 可精确预计算的依据 = nonce(24) + tag(16)（§5.4）。"""
        _o, _ln, _mac, body = upload_server.phone.chunk(upload_server.sid, 0, b"0123")
        assert len(body) == 4 + CHUNK_OVERHEAD

    def test_full_upload_roundtrip(self, upload_server):
        for i, (off, data) in enumerate(((0, b"0123"), (4, b"4567"), (8, b"89"))):
            status, text = upload_server.put(off, data)
            assert status == 200, text
            body = _body_json(text)
            assert body["received"] == off + len(data)
            assert bool(body.get("done")) == (i == 2), "只有最后一片才带 done"
        assert upload_server.saved_path().read_bytes() == b"0123456789"
        assert not list(upload_server.dest_dir.glob("*.part"))

    def test_missing_mac_is_401(self, upload_server):
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         upload_server.path, {"X-Pm-Offset": "0", "X-Pm-Len": "4"})
        assert status == 401

    def test_bad_offset_header_is_401(self, upload_server):
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         upload_server.path,
                         _upload_headers("abc", 4, "AAAA"))
        assert status == 401, "头格式不合法 ⇒ 拿不出凭证"

    def test_unknown_sid_is_401(self, upload_server):
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         f"{_UPLOAD_PATH_PREFIX}NOPE_NOT_A_SESSION",
                         _upload_headers(0, 4, "AAAA"))
        assert status == 401

    def test_wrong_mac_does_not_kill_session(self, upload_server):
        """§5.2 红线：一个未认证的包绝不能毁掉别人正在传的上传。"""
        bogus = encode_mac(upload_mac(b"x" * 32, upload_server.sid, 0, 4))
        status, _ = upload_server.probe(0, 4, bogus)
        assert status == 401
        # 会话必须**还活着**：随后的合法片照常推进
        status, text = upload_server.put(0, b"0123")
        assert status == 200 and _body_json(text)["received"] == 4

    def test_repeat_chunk_is_ignored_and_session_alive(self, upload_server):
        status, text = upload_server.put(0, b"0123")
        assert status == 200 and _body_json(text)["received"] == 4
        status, text = upload_server.put(0, b"0123")      # 重放同一片
        assert status == 200 and _body_json(text)["received"] == 4, "重复片要忽略而不是作废"
        status, text = upload_server.put(4, b"4567")      # 会话还活着
        assert status == 200 and _body_json(text)["received"] == 8

    def test_gap_aborts_session(self, upload_server):
        status, text = upload_server.put(8, b"89")
        assert status == 409, text
        # 会话已作废 ⇒ 下一片 401
        status, _ = upload_server.probe(0, 4, "AAAA")
        assert status == 401

    def test_content_length_mismatch_is_400(self, upload_server):
        o, ln, mac_text, body = upload_server.phone.chunk(upload_server.sid, 0, b"0123")
        # Content-Length 与 X-Pm-Len + 48 不符（多塞 7 字节）
        status, text = _raw(upload_server.host, upload_server.port, "PUT",
                            upload_server.path, _upload_headers(o, ln, mac_text),
                            body + b"0" * 7)
        assert status == 400, text
        # 会话被作废 ⇒ 后续片 401
        status, _ = upload_server.probe(0, 4, "AAAA")
        assert status == 401

    def test_upload_path_allowed_without_secret_prefix(self, upload_server):
        """§6.2：不带页面前缀也要能到达 dispatcher —— 未知 sid 应为 401 而不是 404。"""
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         f"{_UPLOAD_PATH_PREFIX}bogus",
                         _upload_headers(0, 0, "AAAA"))
        assert status == 401, "404 说明前缀放行漏了"

    def test_put_to_other_path_is_405(self, upload_server):
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         "/api/lang.json", {})
        assert status == 405

    def test_client_disconnect_mid_chunk_keeps_session_alive(self, upload_server, caplog):
        """对端在片收完之前关连接：既不冒 traceback，也不拿半截密文去 commit。

        两条同时守：漏了前者，uvicorn 会打出一整屏 ``Exception in ASGI application``
        （看着像服务端崩了）；漏了后者，半截密文会在 ``commit_chunk`` 里变成一条
        ``reason=decrypt:`` 的作废日志——把「客户端走了」伪装成「解密失败」，是最容易
        带偏排查方向的假信号。会话生死归 WS 那条链管（``upload_cancel`` /
        ``abort_for_conn`` / TTL），HTTP 层不插手。
        """
        caplog.set_level("INFO", logger="phonemic.server.upload")
        # ⚠️ 必须走 harness 的 `_encode`（而不是直接 `phone.chunk`）：片序号是**计数器**，
        # 这里若为拿密文多加密一次，后面那次 `put(0, …)` 就会带着 seq=1 的密文到达一个
        # 仍在等 seq=0 的服务端 ⇒ 被测对象没坏、测试自己先错了。
        o, ln, mac_text, enc = upload_server._encode(0, b"0123")
        status, _ = _raw_truncated(
            upload_server.host, upload_server.port, upload_server.path,
            _upload_headers(o, ln, mac_text), len(enc), enc[:10])
        assert status is None or status < 500, f"漏给 uvicorn 了？状态码 {status}"

        session = api_mod._get_upload_manager().get(upload_server.sid)
        assert session is not None, "客户端走了不该由 HTTP 层作废会话"
        assert session.received == 0, "半截密文绝不能推进 received"
        assert not [r for r in caplog.records if "decrypt" in r.getMessage()], \
            "半截密文不该冒充解密失败"

        # 会话照旧可用：把第一片完整发上去仍然推进（证明没被误作废、abort_event 没被置位）
        status, text = upload_server.put(0, b"0123")
        assert status == 200 and _body_json(text)["received"] == 4

    def test_client_disconnect_before_body_is_harmless(self, upload_server):
        """声明了长度却一个字节都没发就 FIN（切走页面）：同样不动会话。"""
        o, ln, mac_text, enc = upload_server._encode(0, b"0123")
        status, _ = _raw_truncated(upload_server.host, upload_server.port,
                                   upload_server.path,
                                   _upload_headers(o, ln, mac_text), len(enc))
        assert status is None or status < 500, f"漏给 uvicorn 了？状态码 {status}"
        assert api_mod._get_upload_manager().get(upload_server.sid) is not None

    def test_client_log_disconnect_is_swallowed(self, upload_server):
        """同族口子：``request.body()`` 内部也是 ``stream()``，对端半路走了照样抛。"""
        sc = api_mod._secure_channel
        path = f"/{sc.secret_path}/api/client-log" if sc.secret_path else "/api/client-log"
        status, _ = _raw_truncated(upload_server.host, upload_server.port, path, {},
                                   200, b'{"entries":')
        assert status is None or status < 500, f"漏给 uvicorn 了？状态码 {status}"

    def test_unhandled_client_disconnect_gets_a_clean_response(self, upload_server,
                                                              monkeypatch):
        """兜底网：将来某条路径漏接了 ``ClientDisconnect``，也只该回 4xx。

        这里刻意让 ``_drain_request_body`` 抛出来，模拟「新代码忘了接」的情形——
        它必须被异常处理器收在应用内，而不是冒到 uvicorn 去打栈。
        """
        async def _boom(request, *a, **kw):
            raise ClientDisconnect()

        monkeypatch.setattr(api_mod, "_drain_request_body", _boom)
        status, _ = _raw(upload_server.host, upload_server.port, "POST",
                         "/api/lang.json", {})
        assert status == 400, "漏接的 ClientDisconnect 必须被兜底处理器收掉"


class TestNormalizePathAllowsUpload:
    """两条分支都要放行（只改一条会在另一种认证方式下静默 404）。"""

    def test_secret_branch(self):
        sc = SecureChannel(auth_method="url_fragment")
        set_secure_channel(sc)
        try:
            assert _normalize_path(f"/{sc.secret_path}/ws") == "/ws"
            assert _normalize_path("/api/upload/abc") == "/api/upload/abc"
            assert _normalize_path("/api/lang.json") is None, "带前缀模式下裸路径应收紧"
        finally:
            set_secure_channel(None)

    def test_bare_branch(self):
        set_secure_channel(SecureChannel(auth_method="tofu", mode="lan"))
        try:
            assert _normalize_path("/api/upload/abc") == "/api/upload/abc"
            assert _normalize_path("/api/lang.json") == "/api/lang.json"
            assert _normalize_path("/api/other") is None
        finally:
            set_secure_channel(None)


class TestFramesRetired:
    """老的 file / photo 帧必须明确回错，而不是被静默丢弃。"""

    def test_old_file_frame_gets_malformed(self, upload_server):
        sim, ws = upload_server.sim, upload_server.ws
        ws.send(sim.encrypt({"type": "file", "a": "start", "id": 9,
                             "name": "x", "size": 1, "chunks": 1}))
        msg = sim.decrypt(ws.recv(timeout=5))
        assert msg["type"] == "error" and msg["code"] == "malformed"
        assert "upload_begin" in msg["msg"]

    def test_old_photo_frame_gets_malformed(self, upload_server):
        sim, ws = upload_server.sim, upload_server.ws
        ws.send(sim.encrypt({"type": "photo", "a": "start", "id": 10,
                             "size": 1, "chunks": 1}))
        msg = sim.decrypt(ws.recv(timeout=5))
        assert msg["type"] == "error" and msg["code"] == "malformed"

    def test_upload_cancel_is_silent_and_idempotent(self, upload_server):
        sim, ws = upload_server.sim, upload_server.ws
        ws.send(sim.encrypt({"type": "upload_cancel", "sid": upload_server.sid}))
        ws.send(sim.encrypt({"type": "upload_cancel", "sid": upload_server.sid}))
        # 幂等、无回帧 ⇒ 下一条能收到的一定是探针自己的回帧。
        # 探针用**未知类型**（回一帧 malformed）：它不依赖任何业务 type。
        ws.send(sim.encrypt({"type": "no_such_type"}))
        msg = sim.decrypt(ws.recv(timeout=5))
        assert msg["type"] == "error" and msg["code"] == "malformed", \
            f"cancel 不该有回帧，实际 {msg}"
        # 会话已作废 ⇒ PUT 401
        status, _ = upload_server.probe(0, 4, "AAAA")
        assert status == 401

    def test_upload_cancel_without_sid_is_ignored(self, upload_server):
        """缺 sid 只记日志、不炸、也不回帧。"""
        sim, ws = upload_server.sim, upload_server.ws
        ws.send(sim.encrypt({"type": "upload_cancel"}))
        ws.send(sim.encrypt({"type": "no_such_type"}))
        msg = sim.decrypt(ws.recv(timeout=5))
        assert msg["type"] == "error" and msg["code"] == "malformed"
        # 会话不受影响
        status, text = upload_server.put(0, b"0123")
        assert status == 200 and _body_json(text)["received"] == 4


class TestUploadBeginRejections:
    """① 阶段的拒绝走 ``upload_error``（独立 type，不复用连接级 error）。"""

    def _begin_and_read(self, upload_server, inner):
        sim, ws = upload_server.sim, upload_server.ws
        ws.send(sim.encrypt(inner))
        return sim.decrypt(ws.recv(timeout=5))

    def test_bad_ref(self, upload_server):
        msg = self._begin_and_read(upload_server, {
            "type": "upload_begin", "id": 2, "ref": "weird", "name": "a", "size": 1})
        assert msg["type"] == "upload_error" and msg["id"] == 2
        assert msg["code"] == "bad_ref"

    def test_bad_size(self, upload_server):
        msg = self._begin_and_read(upload_server, {
            "type": "upload_begin", "id": 3, "ref": "file", "name": "a", "size": -5})
        assert msg["type"] == "upload_error" and msg["code"] == "bad_args"

    def test_photo_too_large(self, upload_server):
        msg = self._begin_and_read(upload_server, {
            "type": "upload_begin", "id": 4, "ref": "photo",
            "size": PHOTO_MAX_SIZE + 1})
        assert msg["type"] == "upload_error" and msg["code"] == "too_large"

    def test_ok_begin_gets_ready(self, upload_server):
        msg = self._begin_and_read(upload_server, {
            "type": "upload_begin", "id": 5, "ref": "file", "name": "b.bin", "size": 3})
        assert msg["type"] == "upload_ready" and msg["id"] == 5
        assert msg["saved"] == "b.bin", "同目录重名要错开"


class TestWsCloseAbortsUpload:
    def test_close_kills_session(self, upload_server):
        upload_server.ws.close()
        time.sleep(0.6)     # 等 finally 里的 abort_for_conn 跑完
        status, _ = _raw(upload_server.host, upload_server.port, "PUT",
                         upload_server.path, _upload_headers(0, 0, "AAAA"))
        assert status == 401, "WS 一断，该连接名下的会话必须立即作废"
        assert not list(upload_server.dest_dir.glob("*.part")), "作废要删掉半成品"


class TestProgressThrottle:
    """片内进度节流器（§5.13）：只按恒定 tick 封帧率、发失败即停发。

    纯逻辑、不碰网络，所以这一层能覆盖集成测试观测不到的时间边界（tick 用的是
    ``time.monotonic``，靠真跑一遍是测不稳的）。
    """

    @staticmethod
    def _mk(*, tick=PROGRESS_TICK, boom=False):
        sent = []

        async def _send(sid, received):
            if boom:
                raise ConnectionResetError("peer is gone")
            sent.append((sid, received))

        th = ChunkProgressThrottle("sid-1", _send, tick=tick)
        return th, sent

    def test_first_frame_has_no_time_gate(self):
        """首帧不设时间门槛，否则一片的开头要白等一整个 tick 才有动静。"""
        th, sent = self._mk()
        assert _run(th.note(10)) is True
        assert sent == [("sid-1", 10)]

    def test_tick_caps_the_frame_rate(self):
        """同一 tick 内只允许一帧 —— LAN 上一片 15MB 会瞬间扫过所有台阶。"""
        th, sent = self._mk(tick=60.0)
        assert _run(th.note(100)) is True
        assert _run(th.note(500)) is False
        assert len(sent) == 1

    def test_tick_alone_decides_no_size_threshold(self):
        """不叠「变化量阈值」：值没变、时间到了**照样发**（§5.13）。

        阈值会把「没进展」这件事一起滤掉 —— 而这正是 10 秒平均速率要的样本：
        有阈值时卡住了就不发帧，速率冻在旧值上，看着像还在传。
        """
        th, sent = self._mk(tick=0.0)
        assert _run(th.note(100)) is True
        assert _run(th.note(100)) is True       # 同一个值也发
        assert _run(th.note(100)) is True
        assert [r for _, r in sent] == [100, 100, 100]

    def test_send_failure_stops_pushing_without_raising(self):
        """发失败（连接已关）就地停发、**绝不冒泡** —— 它是从 HTTP handler 里调的。"""
        th, sent = self._mk(tick=0.0, boom=True)
        assert _run(th.note(100)) is False      # 不抛异常
        assert th.enabled is False
        assert _run(th.note(900)) is False      # 此后不再尝试
        assert sent == []


class TestProgressFrames:
    """②阶段的服务端进度帧（§5.13）：真值、单调，且**不掺和判定**。"""

    def test_put_pushes_progress_frames(self, upload_server):
        """一次 PUT 至少推一帧，值是**明文字节数**（不是密文长度 52）。"""
        h = upload_server
        status, _ = h.put(0, b"0123")
        assert status == 200
        prog = [f for f in h.drain_frames() if f.get("type") == "upload_progress"]
        assert prog, "PUT 之后没收到任何进度帧"
        assert all(f["sid"] == h.sid for f in prog)
        assert [f["received"] for f in prog] == sorted(
            f["received"] for f in prog), "received 必须单调不减"
        assert prog[-1]["received"] == 4, "必须是明文字节数（密文那片是 4 + 48）"

    def test_rejected_chunk_pushes_no_progress(self, upload_server):
        """跳片（409）走排空分支、不进读循环 ⇒ 一个进度帧都不该有。

        给被拒的请求推进度，会让界面显示「收到了 4 字节」而其实一个字节都没落盘。
        """
        h = upload_server
        status, _ = h.put(4, b"4567")          # offset 4 ≠ received 0 ⇒ 跳片
        assert status == 409
        assert not [f for f in h.drain_frames()
                    if f.get("type") == "upload_progress"]

    def test_progress_is_not_the_authority(self, upload_server):
        """完成信号**只在 HTTP 响应里**（`done` 字段）：WS 上不许出现 done 帧。

        满格由最后一片的响应解锁，绝不由进度帧 —— 这是「进度条满了、服务端还在等」
        那道观感的最后防线（§5.13 红线 1）。
        """
        h = upload_server
        h.put(0, b"0123")                      # size=10，远没传完
        types = {f.get("type") for f in h.drain_frames()}
        assert "done" not in types
        assert types <= {"upload_progress"}
