"""
PhoneMic 后端服务单元测试
使用真实 HTTP/WebSocket 连接测试 Starlette 服务器。
所有 WebSocket 测试均通过 SecureChannel 认证后发送加密消息。
"""

import asyncio
import base64
import json
import logging
import multiprocessing
import threading
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox
from hashlib import blake2b

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server import api as api_mod
from phonemic.server.api import (
    set_bridge, start_server, stop_server, restart_server,
    push_config, send_to_phone, set_secure_channel, request_client_rescan,
)
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.crypto import create_provider
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

from conftest import get_test_port


# ---------- 辅助函数 ----------


def wait_for_server_ready(host, port, secret_path="", timeout=5.0):
    """探测服务器就绪：请求入口路径（加密模式带 /{secret} 前缀），200/404 均视为已响应。"""
    prefix = f"/{secret_path}" if secret_path else ""
    url = f"http://{host}:{port}{prefix}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=0.5)
            if resp.status in (200, 404):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


def _path_prefix(sc):
    """加密模式的随机路径前缀（明文模式为空串 → 根路径）。"""
    return f"/{sc.secret_path}" if sc.secret_path else ""


def ws_url(host, port, sc):
    """WebSocket 地址：加密模式带 /{secret} 前缀。"""
    return f"ws://{host}:{port}{_path_prefix(sc)}/ws"


def http_url(host, port, sc, path="/"):
    """HTTP 地址：加密模式带 /{secret} 前缀。"""
    return f"http://{host}:{port}{_path_prefix(sc)}{path}"


class PhoneSimulator:
    """模拟手机端加密操作（使用 PyNaCl 代替 libsodium.js）。

    与 JS 端 SecureClient / CryptoProvider 行为一致：
    - auth：密封 {"algo","pk"} JSON（algo 不明文传输）
    - 会话密钥：ECDH + blake2b(32) 派生，交给 CryptoProvider
    - 防重放 seq 由 Provider 在加密层承载，不进应用层 JSON
    """

    def __init__(self, pc_public_key_b64: str, algo: str = "xsalsa20"):
        self._algo = algo
        if algo == "none":
            # none+CF：pc_public_key_b64 实为 token
            self._token = pc_public_key_b64
            self._provider = None
            return
        pc_pub_bytes = base64.urlsafe_b64decode(pc_public_key_b64 + "==")
        self._pc_public = PublicKey(pc_pub_bytes)
        self._phone_private = PrivateKey.generate()
        self._phone_public = self._phone_private.public_key
        shared = crypto_scalarmult(bytes(self._phone_private), pc_pub_bytes)
        session_key = blake2b(shared, digest_size=32).digest()
        self._provider = create_provider(algo, session_key)

    def make_auth(self, algo: str = None) -> dict:
        """模拟手机端 auth：加密模式密封 {"algo","pk"}；none+CF 明文 token。

        每次握手（= 新连接/新会话）开始时归零 seq 计数器，与服务端对齐。
        """
        algo = algo or self._algo
        if self._provider is not None and hasattr(self._provider, "reset"):
            self._provider.reset()
        if algo == "none":
            return {"type": "auth", "algo": "none", "data": self._token}
        inner = json.dumps(
            {
                "algo": algo,
                "pk": base64.urlsafe_b64encode(
                    bytes(self._phone_public)
                ).decode().rstrip("="),
            }
        ).encode("utf-8")
        sealed = SealedBox(self._pc_public).encrypt(inner)
        # msgpack 的 bin 类型承载密封 blob，不再 base64 包裹
        return {"type": "auth", "data": sealed}

    def encrypt(self, msg: dict) -> bytes:
        # 产出线上字节：none 模式直接 msgpack 编码，加密模式整帧加密；无信封
        pt = frame_encode(msg)
        if self._provider is None:
            return pt
        # seq 由 Provider 在加密层自动打上
        return bytes(self._provider.encrypt(pt))

    def decrypt(self, raw: bytes) -> dict:
        # 还原应用层报文：none 模式直接解码，加密模式整帧解密
        if self._provider is None:
            return frame_decode(raw)
        # 重放/乱序/篡改由 Provider 校验并抛错
        pt = self._provider.decrypt(raw)
        return frame_decode(pt)


def authenticate(ws, phone):
    """完成 auth 握手，返回解密后的 auth_ack 内容。

    ack 是下行首帧（服务端 tx_seq=0，整帧加密）：此处必须解密消费，
    手机端 rx 计数才能与服务端后续下行帧（config 等）对齐。
    """
    ws.send(frame_encode(phone.make_auth()))
    msg = ws.recv(timeout=5)
    inner = phone.decrypt(msg)
    assert inner["type"] == "auth_ack"
    assert inner["status"] == "OK"
    return inner


def authenticate_and_verify(ws, phone):
    """完成 auth 握手并验证 auth_ack 可被客户端解密，返回解密后的内容。"""
    ws.send(frame_encode(phone.make_auth()))
    msg = ws.recv(timeout=5)
    inner = phone.decrypt(msg)
    assert inner["type"] == "auth_ack"
    assert inner["status"] == "OK"
    assert "ts" in inner
    return inner


def consume_connect(queue, algo="xsalsa20"):
    """消费 connect 事件，并断言事件携带协商出的算法。"""
    msg_type, payload = queue.get(timeout=2)
    assert msg_type == "connect"
    assert payload == algo


def consume_initial_config(ws, phone):
    """认证后服务器发送的加密 config，消费并整帧解密验证。"""
    msg = ws.recv(timeout=2)
    inner = phone.decrypt(msg)
    assert inner["type"] == "config"
    assert "mobile_max_records" in inner


# ---------- Fixtures ----------
@pytest.fixture
def secure_server():
    """启动带安全通道的服务器并返回 (host, port, queue, sc)"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port, sc.secret_path):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, queue, sc

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


@pytest.fixture
def server_no_sc():
    """启动不带安全通道的服务器（用于纯 HTTP 测试和 server lifecycle 测试）。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port, sc.secret_path):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, bridge, sc

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- 测试 WebSocket 消息处理 ----------
def test_websocket_message_parsing(secure_server):
    """验证加密 WebSocket 消息能正确解析并推送到队列"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        ws.send(phone.encrypt({"type": "preview", "text": "hello"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "hello"

        ws.send(phone.encrypt({"type": "send", "text": "world"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "send"
        assert text == "world"


def test_websocket_invalid_frame(secure_server):
    """加密模式收到无法解密的帧 → 服务端关闭连接。

    解密失败意味着两端密钥/seq 计数失步，留在同一连接上只会持续错位；
    主动关闭让客户端走重连（新握手新建 Provider、seq 归零）恢复。
    """
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        # 发送无法解密的字节（明文 msgpack 也会被当密文解密失败）
        ws.send(b"this is not msgpack")
        with pytest.raises(Exception):
            ws.recv(timeout=3)  # 服务端主动关闭，而非静默丢弃


def test_connection_lifecycle(secure_server):
    """测试连接/断开事件是否正确推送"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        event, _ = queue.get(timeout=2)
        assert event == "connect"

    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_only_one_active_connection(secure_server):
    """新连接应自动替换旧连接"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    ws1 = ws_connect(ws_url(host, port, sc))
    ws1.send(frame_encode(phone.make_auth()))
    ws1.recv(timeout=5)  # auth_ack
    event, _ = queue.get(timeout=2)
    assert event == "connect"
    ws1.recv(timeout=2)  # config

    ws2 = ws_connect(ws_url(host, port, sc))
    # 新连接会替换旧连接
    ws2.send(frame_encode(phone.make_auth()))
    ws2.recv(timeout=5)  # auth_ack

    events = []
    for _ in range(2):
        event, _ = queue.get(timeout=2)
        events.append(event)
    assert "disconnect" in events
    assert "connect" in events

    ws1.close()
    ws2.close()


def test_config_message_on_connect(secure_server):
    """连接后客户端应收到加密的 config 消息"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)

        # 客户端应收到加密的 config 消息
        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["type"] == "config"
        assert "mobile_max_records" in inner
        assert isinstance(inner["mobile_max_records"], int)


def test_push_config_to_connected_phone(secure_server):
    """push_config 应实时推送加密的配置更新到已连接的手机端"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        result = push_config("mobile_max_records", 25)
        assert result is True

        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["type"] == "config"
        assert inner["mobile_max_records"] == 25


def test_send_to_phone_custom_message(secure_server):
    """send_to_phone 应能推送加密的自定义消息"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        result = send_to_phone({"type": "notice", "text": "hello from server"})
        assert result is True

        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["type"] == "notice"
        assert inner["text"] == "hello from server"


def test_request_client_rescan_notifies_and_closes(secure_server):
    """request_client_rescan：通知已连接手机重新扫码，随后关闭连接。"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        # 触发重新扫码通知（如用户切换加密开关）
        assert request_client_rescan() is True

        # 手机端收到 reconnect 消息（加密信封，解密后为明文指令）
        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["type"] == "reconnect"
        assert inner["reason"] == "config_changed"

        # 连接随后被服务端关闭
        with pytest.raises(Exception):
            ws.recv(timeout=3)

    # 断开事件已推送
    msg_type, _ = queue.get(timeout=2)
    assert msg_type == "disconnect"


def test_request_client_rescan_when_disconnected():
    """没有活动连接时 request_client_rescan 应返回 False。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    assert request_client_rescan() is False

    set_secure_channel(None)


def test_send_to_phone_when_disconnected():
    """没有连接时 send_to_phone 应返回 False"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    result = send_to_phone({"type": "test"})
    assert result is False

    set_secure_channel(None)


def test_unknown_inner_type_tolerance(secure_server):
    """未知内部消息类型不应崩溃"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        # 发送加密的未知类型消息（服务器会记录警告但不断开）
        ws.send(phone.encrypt({"type": "unknown_type", "text": "???"}))
        time.sleep(0.3)

        # 后续合法消息应正常工作
        ws.send(phone.encrypt({"type": "preview", "text": "still working"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "still working"


# ---------- 端到端完整流程测试 ----------
def test_full_e2e_flow(secure_server):
    """完整端到端流程：认证→auth_ack解密→config解密→双向消息→断连"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        # 1. 发送 auth
        ws.send(frame_encode(phone.make_auth()))

        # 2. 接收并解密 auth_ack（之前未覆盖的关键步骤）
        msg = ws.recv(timeout=5)
        ack_inner = phone.decrypt(msg)
        assert ack_inner["type"] == "auth_ack"
        assert ack_inner["status"] == "OK"
        assert "ts" in ack_inner

        # 3. 消费 connect 事件
        event, _ = queue.get(timeout=2)
        assert event == "connect"

        # 4. 接收并解密 config
        msg = ws.recv(timeout=2)
        config_inner = phone.decrypt(msg)
        assert config_inner["type"] == "config"
        assert "mobile_max_records" in config_inner

        # 5. 发送加密消息 → 服务器接收
        ws.send(phone.encrypt({"type": "preview", "text": "hello e2e"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "hello e2e"

        # 6. 服务器推送 → 客户端解密
        send_to_phone({"type": "notice", "text": "server push"})
        msg = ws.recv(timeout=2)
        notice_inner = phone.decrypt(msg)
        assert notice_inner["type"] == "notice"
        assert notice_inner["text"] == "server push"

    # 7. 断连后应收到 disconnect 事件
    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_auth_ack_decryption(secure_server):
    """验证客户端能解密 auth_ack 消息（模拟 JS 端 handleAuthAck）"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port, sc)) as ws:
        ws.send(frame_encode(phone.make_auth()))
        msg = ws.recv(timeout=5)

        # 模拟 JS handleAuthAck：整帧解密——seq 由 Provider 内部校验（首帧 seq=0）
        inner = phone.decrypt(msg)
        assert inner["type"] == "auth_ack"
        assert inner["status"] == "OK"
        assert isinstance(inner["ts"], int)


def test_reconnection_cycle(secure_server):
    """模拟客户端断连后重连的完整流程（复现重连场景）"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    # ---- 第一次连接 ----
    with ws_connect(ws_url(host, port, sc)) as ws1:
        authenticate_and_verify(ws1, phone)
        consume_connect(queue)
        consume_initial_config(ws1, phone)

        ws1.send(phone.encrypt({"type": "preview", "text": "first"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "first"

    # 断连事件
    event, _ = queue.get(timeout=2)
    assert event == "disconnect"

    # ---- 第二次连接（重连）----
    with ws_connect(ws_url(host, port, sc)) as ws2:
        authenticate_and_verify(ws2, phone)
        consume_connect(queue)
        consume_initial_config(ws2, phone)

        ws2.send(phone.encrypt({"type": "send", "text": "second"}))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "send"
        assert text == "second"

    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_rapid_reconnect(secure_server):
    """快速重连：连接→认证→立即断开→重连（模拟日志中的重连循环）"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    for i in range(3):
        with ws_connect(ws_url(host, port, sc)) as ws:
            authenticate_and_verify(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            ws.send(phone.encrypt({
                "type": "preview", "text": f"cycle {i}"
            }))
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == f"cycle {i}"

        event, _ = queue.get(timeout=2)
        assert event == "disconnect"


# ---------- 测试 HTTP 路由（加密模式带 /{secret} 前缀）----------
def test_get_root_returns_html(server_no_sc):
    """GET /{secret}/ 应返回 HTML 响应"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/")
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.read().decode("utf-8")
    assert len(body) > 0


def test_lang_json_route(server_no_sc):
    """GET /{secret}/api/lang.json 返回当前语言的手机端翻译段，且禁用缓存"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/api/lang.json")
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    assert "application/json" in resp.headers.get("content-type", "")

    cache_control = resp.headers.get("cache-control", "")
    assert "no-cache" in cache_control, f"Expected no-cache, got: {cache_control}"

    data = json.loads(resp.read().decode("utf-8"))
    assert isinstance(data, dict)
    assert len(data) > 0, "language data should not be empty"


def test_favicon_route(server_no_sc):
    """GET /{secret}/favicon.ico 应返回图标文件"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/favicon.ico")
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    content_type = resp.headers.get("content-type", "")
    assert "image" in content_type, f"Expected image content-type, got: {content_type}"
    body = resp.read()
    assert len(body) > 0, "favicon body should not be empty"


def test_sodium_js_route(server_no_sc):
    """GET /{secret}/sodium.js 应返回完整的 JS 文件（非 gzip）"""
    host, port, _, sc = server_no_sc
    req = urllib.request.Request(
        http_url(host, port, sc, "/sodium.js"),
        headers={"Accept-Encoding": "identity"},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    assert "javascript" in resp.headers.get("content-type", "")
    body = resp.read()
    assert len(body) > 100000, f"sodium.js too small: {len(body)} bytes"


def test_sodium_js_gzip_route(server_no_sc):
    """GET /{secret}/sodium.js 带 gzip 应返回压缩文件"""
    host, port, _, sc = server_no_sc
    req = urllib.request.Request(
        http_url(host, port, sc, "/sodium.js"),
        headers={"Accept-Encoding": "gzip"},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    body = resp.read()
    assert len(body) > 100000, f"sodium.js.gz too small: {len(body)} bytes"


# ---------- 测试手机端日志回传（POST /{secret}/api/client-log）----------
def _post(url, payload: bytes):
    """POST 原始字节并返回响应（异常由调用方断言状态码）。"""
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=2)


def test_client_log_route(server_no_sc, caplog):
    """手机端日志应原样打进服务端日志，并返回 204（不回帧、不落盘）"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/api/client-log")
    now = int(time.time() * 1000)
    entries = [
        [now, "warn", "[WS] closed code=1006 clean=false"],
        [now + 1, "error", "auth_ack decrypt failed"],
    ]
    body = json.dumps({"ua": "pytest-mobile-ua", "entries": entries}).encode("utf-8")

    caplog.set_level(logging.INFO)
    resp = _post(url, body)
    assert resp.status == 204

    # 手机端原样回传的现场必须能在服务端日志里看到（含时间戳与级别）
    assert "[WS] closed code=1006 clean=false" in caplog.text
    assert "auth_ack decrypt failed" in caplog.text
    assert "pytest-mobile-ua" in caplog.text


def test_client_log_rejects_bad_payload(server_no_sc):
    """非法 JSON 或缺 entries 一律 400：日志入口不能变成异常源"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/api/client-log")

    for payload in (b"{not json", json.dumps({"ua": "x"}).encode("utf-8"),
                    json.dumps([1, 2, 3]).encode("utf-8")):
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(url, payload)
        assert ei.value.code == 400


def test_client_log_rejects_oversize(server_no_sc):
    """超过体积上限直接 413，避免日志入口被滥用打爆服务端日志"""
    host, port, _, sc = server_no_sc
    url = http_url(host, port, sc, "/api/client-log")
    entry = [int(time.time() * 1000), "info", "x" * 500]
    payload = json.dumps({"ua": "x", "entries": [entry] * 600}).encode("utf-8")
    assert len(payload) > 256 * 1024

    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(url, payload)
    assert ei.value.code == 413


def test_post_to_readonly_path_is_405(server_no_sc):
    """只读资源不接受 POST：分发处按方法分流，其余路径一律 405"""
    host, port, _, sc = server_no_sc
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(http_url(host, port, sc, "/api/lang.json"), b"{}")
    assert ei.value.code == 405


# ---------- 集成测试 ----------
def test_real_server_with_websocket_client():
    """启动真实服务，使用同步 WebSocket 客户端测试多种加密消息"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port, sc.secret_path):
        stop_server()
        pytest.fail("Server did not start within timeout")

    phone = PhoneSimulator(sc.get_public_key_b64())

    test_msgs = [
        ("preview", "integration test"),
        ("send", "你好世界 🌍😊 日本語 漢字"),
        ("preview", "Hello, PhoneMic!"),
        ("preview", ""),
        ("preview", "A" * 11000),
        ("preview", "🐍✨ 混合符号 ¥€$ 测试"),
        ("preview", "  前后空格  "),
        ("preview", "\n\t多行文本\n第二行"),
        ("send", "普通发送"),
        ("send", "超长发送" + "B" * 15000),
        ("send", "表情包 😀😂😍"),
        ("send", ""),
        ("send", "  修剪测试  "),
    ]

    try:
        with ws_connect(ws_url(host, port, sc)) as ws:
            authenticate(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            for orig_type, orig_text in test_msgs:
                ws.send(phone.encrypt({"type": orig_type, "text": orig_text}))
                msg_type, text = queue.get(timeout=2)
                assert msg_type == orig_type
                assert text == orig_text
    finally:
        stop_server()
        set_secure_channel(None)

    msg_type, _ = queue.get(timeout=2)
    assert msg_type == "disconnect"


# ---------- 服务器生命周期测试 ----------
def test_server_start_stop():
    """验证 start_server / stop_server 能正常启停且释放端口"""
    host = "127.0.0.1"
    port = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)

    assert wait_for_server_ready(host, port, sc.secret_path), "Server did not start"

    stop_server()
    set_secure_channel(None)
    time.sleep(0.5)

    with pytest.raises((urllib.error.URLError, OSError, ConnectionRefusedError)):
        urllib.request.urlopen(f"http://{host}:{port}/", timeout=1.0)


def test_server_restart_cycle():
    """验证服务器 stop 后可以重新 start（模拟网络切换场景）"""
    host = "127.0.0.1"
    port = get_test_port()
    port2 = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port, sc.secret_path), "First start failed"

    stop_server()
    time.sleep(0.5)

    start_server(host, port2, bridge)
    assert wait_for_server_ready(host, port2, sc.secret_path), "Restart failed"

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url(host, port2, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        ws.send(phone.encrypt({"type": "preview", "text": "after restart"}))
        msg_type, text = bridge.queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "after restart"

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- 测试 restart_server（模式切换）----------
def test_restart_server_with_different_host():
    """restart_server 应能切换绑定地址（模拟 LAN→Cloudflare 模式切换）"""
    port = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server("0.0.0.0", port, bridge)
    assert wait_for_server_ready("127.0.0.1", port, sc.secret_path), "LAN mode start failed"

    restart_server("127.0.0.1", port, bridge)
    assert wait_for_server_ready("127.0.0.1", port, sc.secret_path), "Cloudflare mode restart failed"

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url("127.0.0.1", port, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        ws.send(phone.encrypt({"type": "preview", "text": "after mode switch"}))
        msg_type, text = bridge.queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "after mode switch"

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


def test_restart_server_preserves_bridge():
    """restart_server 后 bridge 仍能正常推送加密消息"""
    port = get_test_port()
    port2 = get_test_port()
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server("127.0.0.1", port, bridge)
    assert wait_for_server_ready("127.0.0.1", port, sc.secret_path)

    stop_server()
    time.sleep(0.5)
    start_server("127.0.0.1", port2, bridge)
    assert wait_for_server_ready("127.0.0.1", port2, sc.secret_path)

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(ws_url("127.0.0.1", port2, sc)) as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        result = send_to_phone({"type": "notice", "text": "post-restart"})
        assert result is True

        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["text"] == "post-restart"

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- 并发连接：认证后才抢占 ----------
class TestConnectionPreemption:
    """新连接必须先通过握手才能抢占活动连接。

    握手中的连接不得改变活动连接的加密状态，否则攻击者可用一次
    未认证的连接把通信降级为明文。
    """

    def _assert_still_encrypted(self, ws, phone, value):
        """推送配置并断言活动连接收到的仍是整帧加密的下行帧。"""
        assert push_config("mobile_max_records", value) is True
        msg = ws.recv(timeout=2)
        inner = phone.decrypt(msg)
        assert inner["type"] == "config", f"活动连接被降级为明文: {inner}"
        assert inner["mobile_max_records"] == value

    def test_silent_connection_does_not_downgrade_active(self, secure_server):
        """新连接建连后不发 auth，活动连接保持加密。"""
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            authenticate_and_verify(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            with ws_connect(ws_url(host, port, sc)):
                self._assert_still_encrypted(ws, phone, 42)

            time.sleep(0.3)
            self._assert_still_encrypted(ws, phone, 7)

    def test_rejected_auth_does_not_downgrade_active(self, secure_server):
        """新连接认证失败，活动连接保持加密且不被抢占。"""
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            authenticate_and_verify(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            with ws_connect(ws_url(host, port, sc)) as intruder:
                bogus = {"type": "auth", "algo": "xsalsa20", "data": b"bogus"}
                intruder.send(frame_encode(bogus))
                ack = frame_decode(intruder.recv(timeout=2))
                assert ack.get("rejected") is True

            time.sleep(0.3)
            self._assert_still_encrypted(ws, phone, 13)

    def test_authenticated_connection_replaces_active(self, secure_server):
        """认证成功的新连接正常抢占，旧连接被关闭。"""
        host, port, queue, sc = secure_server
        phone_a = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws_a:
            authenticate_and_verify(ws_a, phone_a)
            consume_connect(queue)
            consume_initial_config(ws_a, phone_a)

            phone_b = PhoneSimulator(sc.get_public_key_b64())
            with ws_connect(ws_url(host, port, sc)) as ws_b:
                authenticate_and_verify(ws_b, phone_b)

                # 旧连接被抢占，先收到 disconnect 再收到新连接的 connect
                msg_type, _ = queue.get(timeout=2)
                assert msg_type == "disconnect"
                consume_connect(queue)
                consume_initial_config(ws_b, phone_b)

                assert push_config("mobile_max_records", 99) is True
                msg = ws_b.recv(timeout=2)
                inner = phone_b.decrypt(msg)
                assert inner["type"] == "config"
                assert inner["mobile_max_records"] == 99

            with pytest.raises(Exception):
                ws_a.recv(timeout=2)
# ---------- 日志级别与开发模式标记 ----------


class TestLogLevelResolution:
    """PHONEMIC_LOG 的三档解析：info 默认 / debug / trace，旧开关等价 trace。"""

    @pytest.mark.parametrize("env,expected", [
        ({}, "info"),
        ({"PHONEMIC_LOG": "debug"}, "debug"),
        ({"PHONEMIC_LOG": "trace"}, "trace"),
        ({"PHONEMIC_LOG": "DEBUG"}, "debug"),
        ({"PHONEMIC_LOG": "nonsense"}, "info"),
        ({"PHONEMIC_WS_DEBUG": "1"}, "trace"),
        ({"PHONEMIC_WS_DEBUG": "0"}, "info"),
        ({"PHONEMIC_LOG": "debug", "PHONEMIC_WS_DEBUG": "1"}, "trace"),
    ])
    def test_resolve(self, monkeypatch, env, expected):
        monkeypatch.delenv("PHONEMIC_LOG", raising=False)
        monkeypatch.delenv("PHONEMIC_WS_DEBUG", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert api_mod._resolve_log_level() == expected


class TestDevModeMarkInjection:
    """注入给手机页面的开发模式标记：源码运行为 true，打包版为 false。"""

    def _body(self, monkeypatch, frozen):
        monkeypatch.setattr(api_mod, "is_frozen", lambda: frozen)
        return api_mod._serve_mobile().body.decode("utf-8")

    def test_source_run_injects_true(self, monkeypatch):
        body = self._body(monkeypatch, False)
        assert "window.__PHONEMIC_DEV__ = true" in body
        # 占位符必须被替换掉，否则客户端拿不到标记
        assert api_mod._DEV_MODE_MARK not in body

    def test_packaged_injects_false(self, monkeypatch):
        body = self._body(monkeypatch, True)
        assert "window.__PHONEMIC_DEV__ = false" in body
        assert api_mod._DEV_MODE_MARK not in body


class TestClientLogEndpoint:
    """手机端日志回传入口：打包版整个关掉（手机端也不会发）。"""

    def test_404_when_packaged(self, monkeypatch):
        monkeypatch.setattr(api_mod, "is_frozen", lambda: True)

        class _FakeRequest:
            async def body(self):
                return b'{"entries": [[0, "info", "x"]]}'

        # 必须在独立线程里跑：pytest-playwright 会让当前线程处于 running
        # event loop 中，直接 asyncio.run() 会抛 RuntimeError，协程永不被 await
        box = {}

        def worker():
            box["resp"] = asyncio.run(api_mod._receive_client_log(_FakeRequest()))

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert box["resp"].status_code == 404
