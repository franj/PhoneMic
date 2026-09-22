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
import re
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
        # TOFU 首次：第 2 步下发的 nonce（随后的 auth_challenge 必须同源）
        self._tofu_nonce = None
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

    def make_tofu_first_auth(self) -> dict:
        """明文 auth——TOFU 首次连接路径。

        不再携带识别码：识别码由 **PC 指派**并密封下发（design §5.5.1），
        手机上没有任何「可被抄走」的东西。
        """
        if self._provider is not None and hasattr(self._provider, "reset"):
            self._provider.reset()
        return {
            "type": "auth",
            "algo": self._algo,
            "pk": bytes(self._phone_public),
        }

    def read_sealed_pin(self, frame: bytes) -> str:
        """第 2 步：解封 PC 密封下发的识别码，返回 4 位识别码（同时记住 nonce）。"""
        msg = frame_decode(frame)
        assert msg.get("type") == "sealed", msg
        inner = SealedBox(self._phone_private).decrypt(bytes(msg["data"]))
        data = json.loads(inner)
        self._tofu_nonce = base64.urlsafe_b64decode(data["nonce"] + "==")
        return data["pin"]

    def answer_tofu_challenge(self, frame: bytes) -> bytes:
        """解封 SealedBox 挑战，做 ECDH 创建 Provider，回 auth_proof。

        挑战里的 nonce 必须与第 2 步的识别码帧同源——同一条连
        接、同一个对端（design §5.5.1 要点第 2 条）。
        """
        msg = frame_decode(frame)
        assert msg.get("type") == "auth_challenge", msg
        sealed = bytes(msg["data"])
        inner = SealedBox(self._phone_private).decrypt(sealed)
        data = json.loads(inner)
        pc_public = base64.urlsafe_b64decode(data["pk"] + "==")
        nonce = base64.urlsafe_b64decode(data["nonce"] + "==")
        assert nonce == self._tofu_nonce, "挑战 nonce 与识别码帧不同源"
        shared = crypto_scalarmult(bytes(self._phone_private), pc_public)
        session_key = blake2b(shared, digest_size=32).digest()
        self._provider = create_provider(self._algo, session_key)
        return self.encrypt({"type": "auth_proof", "nonce": nonce})

    def tofu_handshake(self, ws, queue) -> str:
        """TOFU 首次五步握手（含识别码下发与审批），返回 PC 指派的识别码。"""
        ws.send(frame_encode(self.make_tofu_first_auth()))
        # 第 2 步先到：PC 密封下发的识别码（手机上显示、供用户核对）
        pin = self.read_sealed_pin(ws.recv(timeout=5))
        msg_type, text = queue.get(timeout=5)
        assert msg_type == "approval_request"
        assert text["pin"] == pin, "PC 审批通知显示的必须是同一个识别码"
        resolve_approval(True)
        ws.send(self.answer_tofu_challenge(ws.recv(timeout=5)))
        return pin

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

    def test_tofu_first_sealed_pin_arrives_before_approval(self, tofu_server):
        """识别码先到、审批后到：用户开始核对之前，手机上必须已经有码了。"""
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode(phone.make_tofu_first_auth()))
            # 尚未审批就已收到第 2 步的密封帧（审批请求此刻还排在队里或还没发）
            pin = phone.read_sealed_pin(ws.recv(timeout=5))
            assert re.fullmatch(r"\d{4}", pin), f"识别码应为 4 位数字: {pin}"

            msg_type, text = queue.get(timeout=5)
            assert msg_type == "approval_request"
            assert text["pin"] == pin
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
            phone.read_sealed_pin(ws.recv(timeout=5))
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

    def test_tofu_approval_slower_than_auth_timeout_still_succeeds(
            self, tofu_server, monkeypatch):
        """审批耗时超过 AUTH_TIMEOUT，握手仍须完成。

        回归：deadline 曾在握手开始时一次算好并给两次等待共享，TOFU 审批动辄
        数十秒、早就把它耗光，于是「用户点了允许、auth_proof 却立刻超时」。
        现在 auth 与 auth_proof 各有一份预算，审批不占用任何一方。
        """
        import phonemic.server.api as api_mod
        monkeypatch.setattr(api_mod, "AUTH_TIMEOUT", 1.0)

        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            ws.send(frame_encode(phone.make_tofu_first_auth()))
            phone.read_sealed_pin(ws.recv(timeout=5))
            msg_type, text = queue.get(timeout=5)
            assert msg_type == "approval_request"

            time.sleep(1.5)          # 躺过原 deadline（1s）
            resolve_approval(True)

            ws.send(phone.answer_tofu_challenge(ws.recv(timeout=5)))
            msg_type, text = queue.get(timeout=2)
            assert msg_type == "connect"


class TestTofuEavesdropper:
    """design §5.5.1：窃听者拿走 auth 也没用——两边的识别码对不上。

    旧实现里识别码由手机自选且明文传输：窃听到 ``pin=3847`` 后用自己的密钥对
    连 PC，PC 审批通知上显示的与手机屏幕完全一致 ⇒ 一次被动窃听 + 一次主动
    连接即确定性得手。现在识别码由 PC 指派、每连接独立，且从不上明文链路。
    """

    def _first_leg(self, host, port, queue, sc):
        """跑一次「真机」连接，返回（真机模拟器，PC 指派的识别码）。"""
        phone = PhoneSimulator()
        ws = ws_connect(ws_url(host, port, sc))
        ws.send(frame_encode(phone.make_tofu_first_auth()))
        pin = phone.read_sealed_pin(ws.recv(timeout=5))
        # 前一条连接断开留下的 "disconnect" 事件先出队，跳过等-event
        while True:
            msg_type, text = queue.get(timeout=5)
            if msg_type == "approval_request":
                break
        assert text["pin"] == pin
        resolve_approval(True)
        ws.send(phone.answer_tofu_challenge(ws.recv(timeout=5)))
        assert queue.get(timeout=2)[0] == "connect"
        ws.close()
        return phone, pin

    def test_two_connections_get_different_pins(self, tofu_server):
        """两条独立连接拿到两个独立随机的识别码——没有可控输入可以把它对齐。"""
        host, port, queue, sc = tofu_server
        _, pin_real = self._first_leg(host, port, queue, sc)
        _, pin_other = self._first_leg(host, port, queue, sc)

        assert re.fullmatch(r"\d{4}", pin_real)
        assert re.fullmatch(r"\d{4}", pin_other)
        assert pin_real != pin_other, (
            "两条连接撞到同一个识别码（概率 1/10^4，重跑确认）；"
            "若必现则说明识别码不再是 PC 独立随机指派"
        )

    def test_pin_field_in_auth_frame_is_ignored(self, tofu_server):
        """窃听者（或旧式客户端）在 auth 里塞一个自定义识别码：服务端完全不读它。

        这是「抄真机明文识别码、换个密钥对复用」这条攻击在新实现里的落点：
        对端提供的 ``pin`` 字段不是真源，PC 指派的那一个才是。
        """
        host, port, queue, sc = tofu_server
        phone = PhoneSimulator()

        with ws_connect(ws_url(host, port, sc)) as ws:
            auth = phone.make_tofu_first_auth()
            auth["pin"] = "0000"      # 攻击者想让电脑显示这个码
            ws.send(frame_encode(auth))

            pin = phone.read_sealed_pin(ws.recv(timeout=5))
            assert pin != "0000", "识别码必须由 PC 指派，不能采信 auth 帧里的值"

            msg_type, text = queue.get(timeout=5)
            assert msg_type == "approval_request"
            assert text["pin"] == pin
            resolve_approval(True)
            ws.send(phone.answer_tofu_challenge(ws.recv(timeout=5)))
            assert queue.get(timeout=2)[0] == "connect"
