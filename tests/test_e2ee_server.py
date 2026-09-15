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
        # msgpack 的 bin 类型承载密封 blob，不再 base64 包裹
        return {"type": "auth", "data": sealed}

    def answer_challenge(self, frame: bytes) -> bytes:
        """模拟 JS 端 SecureClient.makeAuthProof：解密 auth_challenge 并回 auth_proof。

        挑战是下行的 0 号加密帧，proof 是上行的 0 号加密帧——两端 seq 都在这里
        从 0 走起（make_auth 已把计数器归零）。
        """
        msg = self.decrypt(frame)
        assert msg.get("type") == "auth_challenge", msg
        return self.encrypt({"type": "auth_proof", "nonce": msg["nonce"]})

    def handshake(self, ws) -> None:
        """在一条连接上跑完三步握手（auth → auth_challenge → auth_proof）。"""
        ws.send(frame_encode(self.make_auth()))
        ws.send(self.answer_challenge(ws.recv(timeout=5)))

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

    def test_valid_handshake_completes(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            # 三步走完服务端才注册连接：connect 事件是「真的通过了」的可靠证据
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"

    def test_invalid_auth_closes_with_4001(self, secure_server):
        """认证前失败：不回消息层帧、直接 close 4001（与 wire-protocol §7 一致）。

        这一条覆盖了原先「先发明文 auth_ack(rejected) 再 close(1000)」的漂移。
        """
        host, port, queue, sc = secure_server

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode({"type": "auth", "algo": "xsalsa20", "data": b"garbage!!!"}))
            with pytest.raises(ConnectionClosed) as ei:
                ws.recv(timeout=3)
            assert ei.value.rcvd is not None
            assert ei.value.rcvd.code == 4001

    def test_replayed_auth_cannot_steal_the_connection(self, secure_server):
        """重放真机的 auth 帧：解封成功，但过不了 auth_proof，连接不会被注册。

        这是三步握手买到的直接好处——认证失败发生在 _manager.connect() 之前，
        真机那条活动连接不会被顶掉（旧的两步握手下这里是一条零成本的 DoS）。
        """
        host, port, queue, sc = secure_server
        victim = PhoneSimulator(sc.get_public_key_b64())
        recorded = frame_encode(victim.make_auth())  # 攻击者录下真机那一帧

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(recorded)
            # 能拿到 challenge：说明解封确实成功了，重放卡不住第一步
            challenge = victim.decrypt(ws.recv(timeout=5))
            assert challenge["type"] == "auth_challenge"

            # 认证后失败：已有会话密钥 → 回加密的 error(code="auth")，随后关闭
            ws.send(victim.encrypt({"type": "auth_proof", "nonce": b"\x00" * 16}))
            reject = victim.decrypt(ws.recv(timeout=3))
            assert reject["type"] == "error"
            assert reject["code"] == "auth"

            with pytest.raises(ConnectionClosed) as ei:
                ws.recv(timeout=3)
            assert ei.value.rcvd is not None
            assert ei.value.rcvd.code == 1000

        # 全程没有 connect 事件：真机连接毫发无损
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
            queue.get(timeout=2)  # connect event

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
            queue.get(timeout=2)  # connect

            # config 应为整帧加密：能整帧解密即证明无明文泄漏、无外层信封
            config = phone.decrypt(ws.recv(timeout=3))
            assert config["type"] == "config"
            assert "mobile_max_records" in config

    def test_push_config_encrypted(self, secure_server):
        host, port, queue, sc = secure_server
        phone = PhoneSimulator(sc.get_public_key_b64())

        with ws_connect(ws_url(host, port, sc)) as ws:
            phone.handshake(ws)
            queue.get(timeout=2)  # connect
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
            queue.get(timeout=2)  # connect
            ws.recv(timeout=2)  # config (encrypted)

            # 再次发送 auth → 应断开
            ws.send(frame_encode(phone.make_auth()))
            with pytest.raises(Exception):
                ws.recv(timeout=3)
