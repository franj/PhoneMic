"""
SecureChannel 服务器端集成测试。
验证 auth 握手、加密消息处理、状态机在真实 WebSocket 连接中的行为。
"""

import base64
import json
import multiprocessing
import time
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import connect as ws_connect
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import (
    set_bridge, start_server, stop_server,
    set_secure_channel, send_to_phone,
)
from phonemic.tunnel.e2ee import SecureChannel


# ---------- 辅助函数 ----------
_test_port_counter = 9900


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
        self._seq = 0

    def make_auth(self) -> dict:
        sb = SealedBox(self._pc_public)
        sealed = sb.encrypt(bytes(self._phone_public))
        return {
            "type": "auth",
            "data": base64.urlsafe_b64encode(sealed).decode().rstrip("="),
        }

    def verify_auth_ack(self, ack_data: str) -> bool:
        raw = base64.urlsafe_b64decode(ack_data + "==")
        nonce = raw[:Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE:]
        try:
            pt = self._box.decrypt(ct, nonce)
            msg = json.loads(pt)
            return msg.get("status") == "OK"
        except Exception:
            return False

    def encrypt(self, msg: dict) -> dict:
        # 与手机端 JS 一致：明文中注入递增 seq 供服务端防重放
        payload = dict(msg)
        payload["seq"] = self._seq
        self._seq += 1
        pt = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(pt, nonce)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(bytes(encrypted)).decode().rstrip("="),
        }


# ---------- Fixtures ----------
@pytest.fixture
def secure_server():
    """启动带安全通道的服务器"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(algorithm="xsalsa20")
    set_secure_channel(sc)

    start_server(host, port, bridge)
    if not wait_for_server_ready(host, port):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, queue, sc

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- 测试 ----------

class TestAuthHandshake:
    """测试 auth 握手流程。"""

    def test_valid_auth_receives_auth_ack(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            msg = ws.recv(timeout=5)
            data = json.loads(msg)
            assert data["type"] == "auth_ack"
            assert phone.verify_auth_ack(data["data"]) is True

    def test_invalid_auth_closes_connection(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps({"type": "auth", "algo": "xsalsa20", "data": "garbage!!!"}))
            # 服务端先发送拒绝消息再关闭
            ack = json.loads(ws.recv(timeout=3))
            assert ack["type"] == "auth_ack"
            assert ack.get("rejected") is True
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_non_auth_first_message_closes(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps({"type": "data", "data": "anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_connect_event_after_auth(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"


class TestEncryptedMessageFlow:
    """认证后，服务器应解密客户端发来的加密消息。"""

    def test_server_decrypts_encrypted_preview(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack
            queue.get(timeout=2)  # connect event

            ws.send(json.dumps(phone.encrypt({"type": "preview", "text": "secret hello"})))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == "secret hello"

    def test_server_decrypts_encrypted_send(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)
            queue.get(timeout=2)

            ws.send(json.dumps(phone.encrypt({"type": "send", "text": "encrypted send"})))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "send"
            assert text == "encrypted send"


class TestServerSendsEncrypted:
    """认证后，服务器发往客户端的消息应被加密。"""

    def test_config_is_encrypted(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack
            queue.get(timeout=2)  # connect

            # 服务器发送的 config 应该是加密的 data 类型
            msg = ws.recv(timeout=3)
            data = json.loads(msg)
            assert data["type"] == "data"
            assert "data" in data

    def test_push_config_encrypted(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack
            queue.get(timeout=2)  # connect
            ws.recv(timeout=2)  # initial config

            send_to_phone({"type": "config", "test_key": "test_val"})

            msg = ws.recv(timeout=3)
            data = json.loads(msg)
            assert data["type"] == "data"
            assert "data" in data


class TestStateMachineSecurity:
    """状态机安全规则测试。"""

    def test_data_before_auth_closes(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps({"type": "data", "data": "anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_replay_auth_after_authenticated_closes(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(f"ws://{host}:{port}/ws") as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack
            queue.get(timeout=2)  # connect
            ws.recv(timeout=2)  # config (encrypted)

            # 再次发送 auth → 应断开
            ws.send(json.dumps(phone.make_auth()))
            with pytest.raises(Exception):
                ws.recv(timeout=3)
