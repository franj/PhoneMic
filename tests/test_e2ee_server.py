"""
SecureChannel 服务器端集成测试。
验证 auth 握手、加密消息处理、状态机在真实 WebSocket 连接中的行为。

新架构（e2ee-always-on-design.md）：
- 加密永远开启，不存在明文模式
- 认证方式：url_fragment（扫码）或 tofu（手动审批）
- SecureChannel 构造参数为 auth_method= 而非 algorithm=
"""

import base64
import json
import multiprocessing
import time
import urllib.request
import urllib.error

import pytest
from queue import Empty
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect
from hashlib import blake2b
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.bridge_queue import QueueEventBridge
from phonemic.server.api import (
    set_bridge, start_server, stop_server,
    set_secure_channel, send_to_phone,
    resolve_approval,
)
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.crypto import create_provider
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

from conftest import get_test_port

ALGO = "xchacha20"


# ---------- 辅助函数 ----------


def wait_for_server_ready(host, port, secret_path="", timeout=5.0):
    """探测服务器就绪：请求入口路径（url_fragment 带 /{secret} 前缀），200/404 均视为已响应。"""
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
    """WebSocket 地址：url_fragment 模式带 /{secret} 前缀，TOFU 为裸 URL。"""
    prefix = f"/{sc.secret_path}" if sc.secret_path else ""
    return f"ws://{host}:{port}{prefix}/ws"


class PhoneSimulator:
    """模拟手机端加密操作（使用 PyNaCl 代替 libsodium.js）。

    与 JS 端 SecureClient / CryptoProvider 行为一致：
    - url_fragment / TOFU 重连 auth：密封 {"algo","pk"} JSON（algo 不明文传输）
    - TOFU 首次 auth：明文 {"algo","pk","pin"}
    - 会话密钥：ECDH + blake2b(32) 派生，交给 CryptoProvider
    - 防重放 seq 由 Provider 在加密层承载，不进应用层 JSON
    """

    def __init__(self, pc_public_key_b64: str = None, algo: str = ALGO):
        self._algo = algo
        self._phone_private = PrivateKey.generate()
        self._phone_public = self._phone_private.public_key
        self._provider = None
        self._pc_public = None
        if pc_public_key_b64 is not None:
            self._setup_with_pc_key(pc_public_key_b64)

    def _setup_with_pc_key(self, pc_public_key_b64: str):
        """url_fragment / TOFU 重连：已知 PC 公钥，预派生会话密钥。"""
        pc_pub_bytes = base64.urlsafe_b64decode(pc_public_key_b64 + "==")
        self._pc_public = PublicKey(pc_pub_bytes)
        shared = crypto_scalarmult(bytes(self._phone_private), pc_pub_bytes)
        session_key = blake2b(shared, digest_size=32).digest()
        self._provider = create_provider(self._algo, session_key)

    # ---- URL fragment / TOCU 重连 ----

    def make_auth(self, algo: str = None) -> dict:
        """密封 {"algo","pk"} JSON——url_fragment / TOFU 重连路径。

        每次握手（= 新连接/新会话）开始时归零 seq 计数器，与服务端对齐。
        """
        algo = algo or self._algo
        if self._provider is not None and hasattr(self._provider, "reset"):
            self._provider.reset()
        inner = json.dumps(
            {
                "algo": algo,
                "pk": base64.urlsafe_b64encode(
                    bytes(self._phone_public)
                ).decode().rstrip("="),
            }
        ).encode("utf-8")
        sealed = SealedBox(self._pc_public).encrypt(inner)
        return {"type": "auth", "data": sealed}

    def answer_challenge(self, frame: bytes) -> bytes:
        """解密 auth_challenge 并回 auth_proof（Provider 加密路径）。"""
        msg = self.decrypt(frame)
        assert msg.get("type") == "auth_challenge", msg
        return self.encrypt({"type": "auth_proof", "nonce": msg["nonce"]})

    def handshake(self, ws) -> None:
        """url_fragment / TOFU 重连三步握手。"""
        ws.send(frame_encode(self.make_auth()))
        ws.send(self.answer_challenge(ws.recv(timeout=5)))

    # ---- TOFU 首次 ----

    def make_tofu_first_auth(self, pin: str = "1234") -> dict:
        """明文 auth——TOFU 首次连接路径。"""
        if self._provider is not None and hasattr(self._provider, "reset"):
            self._provider.reset()
        return {
            "type": "auth",
            "algo": self._algo,
            "pk": bytes(self._phone_public),
            "pin": pin,
        }

    def answer_tofu_challenge(self, frame: bytes) -> bytes:
        """解封 SealedBox 挑战，做 ECDH 创建 Provider，回 auth_proof。"""
        msg = frame_decode(frame)
        assert msg.get("type") == "auth_challenge", msg
        sealed = bytes(msg["data"])
        inner = SealedBox(self._phone_private).decrypt(sealed)
        data = json.loads(inner)
        pc_public = base64.urlsafe_b64decode(data["pk"] + "==")
        nonce = base64.urlsafe_b64decode(data["nonce"] + "==")
        shared = crypto_scalarmult(bytes(self._phone_private), pc_public)
        session_key = blake2b(shared, digest_size=32).digest()
        self._provider = create_provider(self._algo, session_key)
        return self.encrypt({"type": "auth_proof", "nonce": nonce})

    def tofu_handshake(self, ws, queue) -> None:
        """TOFU 首次三步握手（含审批）。"""
        ws.send(frame_encode(self.make_tofu_first_auth()))
        msg_type, text = queue.get(timeout=5)
        assert msg_type == "approval_request"
        resolve_approval(True)
        ws.send(self.answer_tofu_challenge(ws.recv(timeout=5)))

    # ---- 加解密 ----

    def encrypt(self, msg: dict) -> bytes:
        pt = frame_encode(msg)
        return bytes(self._provider.encrypt(pt))

    def decrypt(self, raw: bytes) -> dict:
        pt = self._provider.decrypt(raw)
        return frame_decode(pt)


# ---------- Fixtures ----------

@pytest.fixture
def secure_server():
    """url_fragment 认证模式服务器。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(auth_method="url_fragment")
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
def tofu_server():
    """TOFU 认证模式服务器。"""
    bridge = QueueEventBridge(multiprocessing.Queue())
    set_bridge(bridge)
    queue = bridge.queue
    host = "127.0.0.1"
    port = get_test_port()

    sc = SecureChannel(auth_method="tofu", mode="lan")
    set_secure_channel(sc)

    start_server(host, port, bridge)
    if not wait_for_server_ready(host, port, sc.secret_path):
        stop_server()
        pytest.fail("Server did not start within timeout")

    yield host, port, queue, sc

    stop_server()
    set_secure_channel(None)
    time.sleep(0.3)


# ---------- URL fragment 认证握手 ----------

class TestAuthHandshake:
    """测试 url_fragment auth 握手流程。"""

    def test_valid_handshake_completes(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"

    def test_invalid_auth_closes_with_4001(self, secure_server):
        """认证前失败：不回消息层帧、直接 close 4001。"""
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode({"type": "auth", "data": b"garbage!!!"}))
            with pytest.raises(ConnectionClosed) as ei:
                ws.recv(timeout=3)
            assert ei.value.rcvd is not None
            assert ei.value.rcvd.code == 4001

    def test_replayed_auth_cannot_steal_the_connection(self, secure_server):
        """重放真机的 auth 帧：解封成功，但过不了 auth_proof，连接不会被注册。"""
        host, port, queue, sc = secure_server
        victim = PhoneSimulator(sc.get_public_key_b64())
        recorded = frame_encode(victim.make_auth())

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(recorded)
            challenge = victim.decrypt(ws.recv(timeout=5))
            assert challenge["type"] == "auth_challenge"

            ws.send(victim.encrypt({"type": "auth_proof", "nonce": b"\x00" * 16}))
            reject = victim.decrypt(ws.recv(timeout=3))
            assert reject["type"] == "error"
            assert reject["code"] == "auth"

            with pytest.raises(ConnectionClosed) as ei:
                ws.recv(timeout=3)
            assert ei.value.rcvd is not None
            assert ei.value.rcvd.code == 1000

        with pytest.raises(Empty):
            queue.get(timeout=0.5)

    def test_non_auth_first_message_closes(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode({"type": "data", "data": b"anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_connect_event_after_handshake(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"


class TestEncryptedMessageFlow:
    """认证后，服务器应解密客户端发来的加密消息。"""

    def test_server_decrypts_encrypted_preview(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)

            ws.send(phone.encrypt({"type": "preview", "text": "secret hello"}))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == "secret hello"

    def test_server_decrypts_encrypted_send(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)

            ws.send(phone.encrypt({"type": "send", "text": "encrypted send"}))

            msg_type, text = queue.get(timeout=2)
            assert msg_type == "send"
            assert text == "encrypted send"


class TestServerSendsEncrypted:
    """认证后，服务器发往客户端的消息应被加密。"""

    def test_config_is_encrypted(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)

            config = phone.decrypt(ws.recv(timeout=3))
            assert config["type"] == "config"
            assert "mobile_max_records" in config

    def test_push_config_encrypted(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)
            initial = phone.decrypt(ws.recv(timeout=2))
            assert initial["type"] == "config"

            send_to_phone({"type": "config", "test_key": "test_val"})

            pushed = phone.decrypt(ws.recv(timeout=3))
            assert pushed["type"] == "config"
            assert pushed["test_key"] == "test_val"


class TestStateMachineSecurity:
    """状态机安全规则测试。"""

    def test_data_before_auth_closes(self, secure_server):
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode({"type": "data", "data": b"anything"}))
            with pytest.raises(Exception):
                ws.recv(timeout=3)

    def test_replay_auth_after_authenticated_closes(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)
            ws.recv(timeout=2)

            ws.send(frame_encode(phone.make_auth()))
            with pytest.raises(Exception):
                ws.recv(timeout=3)


# ---------- TOFU 认证握手 ----------

class TestTofuHandshake:
    """TOFU 首次连接握手测试。"""

    def test_tofu_first_handshake_completes(self, tofu_server):
        """TOFU 首次连接：auth → 审批 → challenge(SealedBox) → auth_proof。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()  # TOCU 首次：不知道 PC 公钥

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.tofu_handshake(ws, queue)
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"

    def test_tofu_first_approval_request_has_pin(self, tofu_server):
        """审批事件携带 4 位 PIN 和客户端 IP。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode(phone.make_tofu_first_auth(pin="5678")))
            msg_type, text = queue.get(timeout=5)
            assert msg_type == "approval_request"
            assert text["pin"] == "5678"
            assert "ip" in text
            resolve_approval(True)
            ws.send(phone.answer_tofu_challenge(ws.recv(timeout=5)))
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"

    def test_tofu_first_rejected_closes_with_4032(self, tofu_server):
        """TOFU 审批被拒绝：close 4032。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode(phone.make_tofu_first_auth()))
            msg_type, text = queue.get(timeout=5)
            assert msg_type == "approval_request"
            resolve_approval(False)

            with pytest.raises(ConnectionClosed) as ei:
                ws.recv(timeout=5)
            assert ei.value.rcvd is not None
            assert ei.value.rcvd.code == 4032

        with pytest.raises(Empty):
            queue.get(timeout=0.5)

    def test_tofu_first_then_encrypted_message(self, tofu_server):
        """TOFU 握手后可正常收发加密消息。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.tofu_handshake(ws, queue)
            queue.get(timeout=2)  # connect

            ws.send(phone.encrypt({"type": "preview", "text": "tofu hello"}))
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "preview"
            assert text == "tofu hello"

    def test_tofu_reconnect_uses_sealed_box(self, tofu_server):
        """TOFU 重连：手机用已知 PC 公钥走 SealedBox 路径，无需审批。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"
