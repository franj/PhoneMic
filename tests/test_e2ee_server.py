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
from hashlib import blake2b
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import (
    set_bridge, start_server, stop_server,
    set_secure_channel, send_to_phone,
)
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.crypto import create_provider

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


def ws_url(host, port, sc):
    """WebSocket 地址：加密模式带 /{secret} 前缀。"""
    prefix = f"/{sc.secret_path}" if sc.secret_path else ""
    return f"ws://{host}:{port}{prefix}/ws"


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
        return {
            "type": "auth",
            "data": base64.urlsafe_b64encode(sealed).decode().rstrip("="),
        }

    def verify_auth_ack(self, ack_data: str) -> bool:
        """模拟 JS 端 handleAuthAck：解密 ack（首帧 seq=0）。"""
        try:
            raw = base64.urlsafe_b64decode(ack_data + "==")
            pt = self._provider.decrypt(raw)
            msg = json.loads(pt)
            return msg.get("status") == "OK"
        except Exception:
            return False

    def encrypt(self, msg: dict) -> dict:
        # none 模式：信封即消息本身（与 JS SecureClient 一致）
        if self._provider is None:
            return msg
        pt = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        # seq 由 Provider 在加密层自动打上
        encrypted = self._provider.encrypt(pt)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(bytes(encrypted)).decode().rstrip("="),
        }

    def decrypt(self, envelope: dict) -> dict:
        if self._provider is None:
            return envelope
        raw = base64.urlsafe_b64decode(envelope["data"] + "==")
        # 重放/乱序/篡改由 Provider 校验并抛错
        pt = self._provider.decrypt(raw)
        return json.loads(pt)


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
    if not wait_for_server_ready(host, port, sc.secret_path):
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

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps(phone.make_auth()))
            msg = ws.recv(timeout=5)
            data = json.loads(msg)
            assert data["type"] == "auth_ack"
            assert phone.verify_auth_ack(data["data"]) is True

    def test_invalid_auth_closes_connection(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps({"type": "auth", "algo": "xsalsa20", "data": "garbage!!!"}))
            # 服务端先发送拒绝消息再关闭
            ack = json.loads(ws.recv(timeout=3))
            assert ack["type"] == "auth_ack"
            assert ack.get("rejected") is True
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_non_auth_first_message_closes(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps({"type": "data", "data": "anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_connect_event_after_auth(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"


class TestEncryptedMessageFlow:
    """认证后，服务器应解密客户端发来的加密消息。"""

    def test_server_decrypts_encrypted_preview(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
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

        with ws_connect(ws_url(host, port, sc)) as ws:
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

        with ws_connect(ws_url(host, port, sc)) as ws:
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

        with ws_connect(ws_url(host, port, sc)) as ws:
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

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps({"type": "data", "data": "anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_replay_auth_after_authenticated_closes(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(json.dumps(phone.make_auth()))
            ws.recv(timeout=5)  # auth_ack
            queue.get(timeout=2)  # connect
            ws.recv(timeout=2)  # config (encrypted)

            # 再次发送 auth → 应断开
            ws.send(json.dumps(phone.make_auth()))
            with pytest.raises(Exception):
                ws.recv(timeout=3)
