"""
PhoneMic 后端服务单元测试
使用真实 HTTP/WebSocket 连接测试 aiohttp 服务器。
所有 WebSocket 测试均通过 SecureChannel 认证后发送加密消息。
"""

import base64
import json
import multiprocessing
import re
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import (
    set_bridge, start_server, stop_server, restart_server,
    push_config, send_to_phone, set_secure_channel,
)
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.mode import TunnelMode, get_bind_address


# ---------- 辅助函数 ----------
_test_port_counter = 8880


def get_test_port():
    global _test_port_counter
    _test_port_counter += 1
    return _test_port_counter


def wait_for_server_ready(host, port, timeout=5.0):
    url = f"http://{host}:{port}/"
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=0.5)
            if resp.status in (200, 404):
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.1)
    return False


class PhoneSimulator:
    """模拟手机端加密操作（使用 PyNaCl 代替 libsodium.js）。"""

    def __init__(self, pc_public_key_b64: str):
        pc_pub_bytes = base64.urlsafe_b64decode(pc_public_key_b64 + "==")
        self._pc_public = PublicKey(pc_pub_bytes)
        self._phone_private = PrivateKey.generate()
        self._phone_public = self._phone_private.public_key
        self._box = Box(self._phone_private, self._pc_public)

    def make_auth(self, algo: str = "xsalsa20") -> dict:
        sb = SealedBox(self._pc_public)
        sealed = sb.encrypt(bytes(self._phone_public))
        return {
            "type": "auth",
            "algo": algo,
            "data": base64.urlsafe_b64encode(sealed).decode().rstrip("="),
        }

    def encrypt(self, msg: dict) -> dict:
        pt = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(pt, nonce)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(bytes(encrypted)).decode().rstrip("="),
        }

    def decrypt(self, envelope: dict) -> dict:
        raw = base64.urlsafe_b64decode(envelope["data"] + "==")
        nonce = raw[:Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE:]
        pt = self._box.decrypt(ct, nonce)
        return json.loads(pt)


def authenticate(ws, phone):
    """完成 auth 握手，返回 auth_ack 原始 dict。"""
    ws.send(json.dumps(phone.make_auth()))
    msg = ws.recv(timeout=5)
    data = json.loads(msg)
    assert data["type"] == "auth_ack"
    return data


def authenticate_and_verify(ws, phone):
    """完成 auth 握手并验证 auth_ack 可被客户端解密，返回解密后的内容。"""
    ack = authenticate(ws, phone)
    inner = phone.decrypt(ack)
    assert inner["status"] == "OK"
    assert "ts" in inner
    return inner


def consume_connect(queue):
    """消费 connect 事件。"""
    msg_type, _ = queue.get(timeout=2)
    assert msg_type == "connect"


def consume_initial_config(ws, phone):
    """认证后服务器发送的加密 config，消费并解密验证。"""
    msg = ws.recv(timeout=2)
    data = json.loads(msg)
    assert data["type"] == "data"
    inner = phone.decrypt(data)
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

    sc = SecureChannel()
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port):
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

    sc = SecureChannel()
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, bridge

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- 测试 WebSocket 消息处理 ----------
def test_websocket_message_parsing(secure_server):
    """验证加密 WebSocket 消息能正确解析并推送到队列"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "hello"})))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "hello"

        ws.send(json.dumps(phone.encrypt({"type": "send", "text": "world"})))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "send"
        assert text == "world"


def test_websocket_invalid_json(secure_server):
    """无效 JSON 不应崩溃，但新协议下非 data 消息会断开连接"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        # 发送无效 JSON → 服务器应断开连接
        ws.send("this is not json")
        with pytest.raises(Exception):
            ws.recv(timeout=3)


def test_connection_lifecycle(secure_server):
    """测试连接/断开事件是否正确推送"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        event, _ = queue.get(timeout=2)
        assert event == "connect"

    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_only_one_active_connection(secure_server):
    """新连接应自动替换旧连接"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    ws1 = ws_connect(f"ws://{host}:{port}/ws")
    ws1.send(json.dumps(phone.make_auth()))
    ws1.recv(timeout=5)  # auth_ack
    event, _ = queue.get(timeout=2)
    assert event == "connect"
    ws1.recv(timeout=2)  # config

    ws2 = ws_connect(f"ws://{host}:{port}/ws")
    # 新连接会替换旧连接
    ws2.send(json.dumps(phone.make_auth()))
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

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)

        # 客户端应收到加密的 config 消息
        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        inner = phone.decrypt(data)
        assert inner["type"] == "config"
        assert "mobile_max_records" in inner
        assert isinstance(inner["mobile_max_records"], int)


def test_push_config_to_connected_phone(secure_server):
    """push_config 应实时推送加密的配置更新到已连接的手机端"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        result = push_config("mobile_max_records", 25)
        assert result is True

        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        inner = phone.decrypt(data)
        assert inner["type"] == "config"
        assert inner["mobile_max_records"] == 25


def test_send_to_phone_custom_message(secure_server):
    """send_to_phone 应能推送加密的自定义消息"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        result = send_to_phone({"type": "notice", "text": "hello from server"})
        assert result is True

        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        inner = phone.decrypt(data)
        assert inner["type"] == "notice"
        assert inner["text"] == "hello from server"


def test_send_to_phone_when_disconnected():
    """没有连接时 send_to_phone 应返回 False"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    sc = SecureChannel()
    set_secure_channel(sc)

    result = send_to_phone({"type": "test"})
    assert result is False

    set_secure_channel(None)


def test_unknown_inner_type_tolerance(secure_server):
    """未知内部消息类型不应崩溃"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(queue)
        consume_initial_config(ws, phone)

        # 发送加密的未知类型消息（服务器会记录警告但不断开）
        ws.send(json.dumps(phone.encrypt({"type": "unknown_type", "text": "???"})))
        time.sleep(0.3)

        # 后续合法消息应正常工作
        ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "still working"})))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "still working"


# ---------- 端到端完整流程测试 ----------
def test_full_e2e_flow(secure_server):
    """完整端到端流程：认证→auth_ack解密→config解密→双向消息→断连"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        # 1. 发送 auth
        ws.send(json.dumps(phone.make_auth()))

        # 2. 接收并解密 auth_ack（之前未覆盖的关键步骤）
        msg = ws.recv(timeout=5)
        data = json.loads(msg)
        assert data["type"] == "auth_ack"
        ack_inner = phone.decrypt(data)
        assert ack_inner["status"] == "OK"
        assert "ts" in ack_inner

        # 3. 消费 connect 事件
        event, _ = queue.get(timeout=2)
        assert event == "connect"

        # 4. 接收并解密 config
        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        config_inner = phone.decrypt(data)
        assert config_inner["type"] == "config"
        assert "mobile_max_records" in config_inner

        # 5. 发送加密消息 → 服务器接收
        ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "hello e2e"})))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "hello e2e"

        # 6. 服务器推送 → 客户端解密
        send_to_phone({"type": "notice", "text": "server push"})
        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        notice_inner = phone.decrypt(data)
        assert notice_inner["type"] == "notice"
        assert notice_inner["text"] == "server push"

    # 7. 断连后应收到 disconnect 事件
    event, _ = queue.get(timeout=2)
    assert event == "disconnect"


def test_auth_ack_decryption(secure_server):
    """验证客户端能解密 auth_ack 消息（模拟 JS 端 handleAuthAck）"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port}/ws") as ws:
        ws.send(json.dumps(phone.make_auth()))
        msg = ws.recv(timeout=5)
        data = json.loads(msg)
        assert data["type"] == "auth_ack"

        # 模拟 JS: handleAuthAck(data.data)
        raw = base64.urlsafe_b64decode(data["data"] + "==")
        nonce = raw[:Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE:]
        pt = phone._box.decrypt(ct, nonce)
        inner = json.loads(pt)
        assert inner["status"] == "OK"
        assert isinstance(inner["ts"], int)


def test_reconnection_cycle(secure_server):
    """模拟客户端断连后重连的完整流程（复现重连场景）"""
    host, port, queue, sc = secure_server
    phone = PhoneSimulator(sc.get_public_key_b64())

    # ---- 第一次连接 ----
    with ws_connect(f"ws://{host}:{port}/ws") as ws1:
        authenticate_and_verify(ws1, phone)
        consume_connect(queue)
        consume_initial_config(ws1, phone)

        ws1.send(json.dumps(phone.encrypt({"type": "preview", "text": "first"})))
        msg_type, text = queue.get(timeout=2)
        assert msg_type == "preview"
        assert text == "first"

    # 断连事件
    event, _ = queue.get(timeout=2)
    assert event == "disconnect"

    # ---- 第二次连接（重连）----
    with ws_connect(f"ws://{host}:{port}/ws") as ws2:
        authenticate_and_verify(ws2, phone)
        consume_connect(queue)
        consume_initial_config(ws2, phone)

        ws2.send(json.dumps(phone.encrypt({"type": "send", "text": "second"})))
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
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            authenticate_and_verify(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            ws.send(json.dumps(phone.encrypt({
                "type": "preview", "text": f"cycle {i}"
            })))
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == f"cycle {i}"

        event, _ = queue.get(timeout=2)
        assert event == "disconnect"


# ---------- 测试 HTTP 路由 ----------
def test_get_root_returns_html(server_no_sc):
    """GET / 应返回 HTML 响应"""
    host, port, _ = server_no_sc
    url = f"http://{host}:{port}/"
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.read().decode("utf-8")
    assert len(body) > 0


def test_root_html_i18n_replaced(server_no_sc):
    """GET / 返回的 HTML 中 __I18N_JSON__ 占位符应被替换为有效 JSON"""
    host, port, _ = server_no_sc
    url = f"http://{host}:{port}/"
    resp = urllib.request.urlopen(url, timeout=2)
    body = resp.read().decode("utf-8")

    assert "__I18N_JSON__" not in body, "i18n placeholder was not replaced"

    match = re.search(
        r'<script id="i18n-data" type="application/json">\s*(.*?)\s*</script>',
        body, re.DOTALL
    )
    assert match is not None, "i18n-data script tag not found in HTML"
    i18n_data = json.loads(match.group(1))
    assert isinstance(i18n_data, dict)
    assert len(i18n_data) > 0, "i18n data should not be empty"


def test_favicon_route(server_no_sc):
    """GET /favicon.ico 应返回图标文件"""
    host, port, _ = server_no_sc
    url = f"http://{host}:{port}/favicon.ico"
    resp = urllib.request.urlopen(url, timeout=2)
    assert resp.status == 200
    content_type = resp.headers.get("content-type", "")
    assert "image" in content_type, f"Expected image content-type, got: {content_type}"
    body = resp.read()
    assert len(body) > 0, "favicon body should not be empty"


def test_sodium_js_route(server_no_sc):
    """GET /sodium.js 应返回完整的 JS 文件（非 gzip）"""
    host, port, _ = server_no_sc
    req = urllib.request.Request(
        f"http://{host}:{port}/sodium.js",
        headers={"Accept-Encoding": "identity"},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    assert "javascript" in resp.headers.get("content-type", "")
    body = resp.read()
    assert len(body) > 100000, f"sodium.js too small: {len(body)} bytes"


def test_sodium_js_gzip_route(server_no_sc):
    """GET /sodium.js 带 gzip 应返回压缩文件"""
    host, port, _ = server_no_sc
    req = urllib.request.Request(
        f"http://{host}:{port}/sodium.js",
        headers={"Accept-Encoding": "gzip"},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    assert resp.headers.get("Content-Encoding") == "gzip"
    body = resp.read()
    assert len(body) > 100000, f"sodium.js.gz too small: {len(body)} bytes"


# ---------- 集成测试 ----------
def test_real_server_with_websocket_client():
    """启动真实服务，使用同步 WebSocket 客户端测试多种加密消息"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel()
    set_secure_channel(sc)

    start_server(host, port, bridge)

    if not wait_for_server_ready(host, port):
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
        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            authenticate(ws, phone)
            consume_connect(queue)
            consume_initial_config(ws, phone)

            for orig_type, orig_text in test_msgs:
                ws.send(json.dumps(phone.encrypt({"type": orig_type, "text": orig_text})))
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
    sc = SecureChannel()
    set_secure_channel(sc)

    start_server(host, port, bridge)

    assert wait_for_server_ready(host, port), "Server did not start"

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
    sc = SecureChannel()
    set_secure_channel(sc)

    start_server(host, port, bridge)
    assert wait_for_server_ready(host, port), "First start failed"

    stop_server()
    time.sleep(0.5)

    start_server(host, port2, bridge)
    assert wait_for_server_ready(host, port2), "Restart failed"

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://{host}:{port2}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "after restart"})))
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
    sc = SecureChannel()
    set_secure_channel(sc)

    lan_host = get_bind_address(TunnelMode.LAN)
    start_server(lan_host, port, bridge)
    assert wait_for_server_ready("127.0.0.1", port), "LAN mode start failed"

    cf_host = get_bind_address(TunnelMode.CLOUDFLARE)
    restart_server(cf_host, port, bridge)
    assert wait_for_server_ready("127.0.0.1", port), "Cloudflare mode restart failed"

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "after mode switch"})))
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
    sc = SecureChannel()
    set_secure_channel(sc)

    start_server("127.0.0.1", port, bridge)
    assert wait_for_server_ready("127.0.0.1", port)

    stop_server()
    time.sleep(0.5)
    start_server("127.0.0.1", port2, bridge)
    assert wait_for_server_ready("127.0.0.1", port2)

    phone = PhoneSimulator(sc.get_public_key_b64())

    with ws_connect(f"ws://127.0.0.1:{port2}/ws") as ws:
        authenticate(ws, phone)
        consume_connect(bridge.queue)
        consume_initial_config(ws, phone)

        result = send_to_phone({"type": "notice", "text": "post-restart"})
        assert result is True

        msg = ws.recv(timeout=2)
        data = json.loads(msg)
        assert data["type"] == "data"
        inner = phone.decrypt(data)
        assert inner["text"] == "post-restart"

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)
