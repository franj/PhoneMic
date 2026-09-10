"""
SecureChannel / SecureSession 单元测试。
测试密钥对生成、auth 握手、加解密、状态机、none 模式、会话隔离。
"""

import base64
import json
import time
from hashlib import blake2b

import pytest
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, SealedBox

from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.crypto import OFFERED_ALGORITHMS, create_provider
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

ALGO = "xsalsa20"  # 客户端从 a= 列表中协商选择的算法


def make_session(phone_algo=ALGO, mode="lan"):
    """创建一个已完成握手的会话（加密模式，算法由客户端协商）。"""
    sc = SecureChannel(algorithm="auto", mode=mode)
    session = sc.new_session()
    session.receive_auth(make_phone_auth(sc, algorithm=phone_algo))
    return sc, session


def make_phone_auth(sc, algorithm=ALGO):
    """模拟手机端：密封 {"algo","pk"} JSON——algo 在密文内部，不明文传输。"""
    phone_private = PrivateKey.generate()
    phone_public = phone_private.public_key
    inner = json.dumps(
        {
            "algo": algorithm,
            "pk": base64.urlsafe_b64encode(bytes(phone_public)).decode().rstrip("="),
        }
    ).encode("utf-8")
    sb = SealedBox(sc.pc_private.public_key)
    # msgpack 的 bin 类型承载密封 blob，不再 base64 包裹
    return {"type": "auth", "data": sb.encrypt(inner)}


class TestSecureChannelKeys:
    """测试密钥对生成和编码。"""

    def test_generates_different_keys(self):
        sc1 = SecureChannel(algorithm="auto")
        sc2 = SecureChannel(algorithm="auto")
        assert sc1.get_public_key_b64() != sc2.get_public_key_b64()

    def test_public_key_is_valid_base64url(self):
        sc = SecureChannel(algorithm="auto")
        b64 = sc.get_public_key_b64()
        assert "=" not in b64
        decoded = base64.urlsafe_b64decode(b64 + "==")
        assert len(decoded) == 32

    def test_append_to_url_lan(self):
        sc = SecureChannel(algorithm="auto")
        url = "http://192.168.1.100:12000"
        result = sc.append_to_url(url)
        # 加密模式插入随机入口路径（防扫描），根路径不可见
        assert result.startswith(url + f"/{sc.secret_path}/#k=")
        # a= 为算法优先级列表（逗号分隔），客户端按序协商
        assert f"a={','.join(OFFERED_ALGORITHMS)}" in result

    def test_append_to_url_cloudflare(self):
        sc = SecureChannel(algorithm="auto")
        url = "https://abc-def.trycloudflare.com"
        result = sc.append_to_url(url)
        assert result.startswith(url + f"/{sc.secret_path}/#k=")

    def test_append_to_url_no_trailing_slash(self):
        sc = SecureChannel(algorithm="auto")
        url = "http://localhost:8080/"
        result = sc.append_to_url(url)
        assert f"/{sc.secret_path}/#k=" in result
        assert "//#k=" not in result

    def test_append_to_url_plaintext_no_secret_path(self):
        """明文模式不加密：无随机路径、无 fragment。"""
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.secret_path == ""
        url = "http://192.168.1.100:12000"
        assert sc.append_to_url(url) == url

    def test_secret_path_only_in_encrypted_mode(self):
        """随机入口路径仅在加密模式生成，明文模式为空串。"""
        enc = SecureChannel(algorithm="auto")
        plain = SecureChannel(algorithm="none", mode="lan")
        assert len(enc.secret_path) >= 20
        assert plain.secret_path == ""
        # 两个加密实例的路径不同（每次生成）
        enc2 = SecureChannel(algorithm="auto")
        assert enc.secret_path != enc2.secret_path


class TestSecureChannelAuth:
    """测试 auth 握手流程。"""

    def test_receive_auth_succeeds(self):
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        assert session.receive_auth(make_phone_auth(sc)) is True
        assert session.is_authenticated is True

    def test_receive_auth_fails_with_garbage(self):
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        bad = {"type": "auth", "data": b"not a sealed box!!!"}
        assert session.receive_auth(bad) is False
        assert session.is_authenticated is False

    def test_receive_auth_fails_with_wrong_key(self):
        """用错误公钥密封的 auth：PC 端解封失败。"""
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        other_pc = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        inner = json.dumps(
            {
                "algo": ALGO,
                "pk": base64.urlsafe_b64encode(
                    bytes(phone_private.public_key)
                ).decode().rstrip("="),
            }
        ).encode("utf-8")
        sb = SealedBox(other_pc.public_key)
        bad = {"type": "auth", "data": sb.encrypt(inner)}
        assert session.receive_auth(bad) is False
        assert session.is_authenticated is False

    def test_receive_auth_fails_with_unsupported_algo(self):
        """密封在密文内的算法不在服务端下发列表中：拒绝。"""
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        inner = json.dumps(
            {
                "algo": "aes-256-gcm",
                "pk": base64.urlsafe_b64encode(
                    bytes(phone_private.public_key)
                ).decode().rstrip("="),
            }
        ).encode("utf-8")
        sb = SealedBox(sc.pc_private.public_key)
        auth_msg = {"type": "auth", "data": sb.encrypt(inner)}
        assert session.receive_auth(auth_msg) is False
        assert session.is_rejected is True

    @pytest.mark.parametrize("phone_algo", OFFERED_ALGORITHMS)
    def test_receive_auth_accepts_any_offered_algo(self, phone_algo):
        """客户端可从下发列表中任选一个算法完成握手。"""
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        assert session.receive_auth(make_phone_auth(sc, algorithm=phone_algo)) is True
        assert session.is_authenticated is True
        # 成功的 auth_ack 整帧加密且不带明文 algo（algo 已在 auth 密文内协商）
        ack = session.make_auth_ack()
        assert "algo" not in ack
        # 协商结果可通过属性查询（供状态栏展示）
        assert session.negotiated_algorithm == phone_algo

    def test_negotiated_algorithm_none_before_handshake(self):
        """未握手或 none 模式下协商结果为 none。"""
        sc = SecureChannel(algorithm="auto")
        assert sc.new_session().negotiated_algorithm == "none"
        sc_none = SecureChannel(algorithm="none", mode="lan")
        assert sc_none.new_session().negotiated_algorithm == "none"

    def test_offered_algorithms_priority_order(self):
        """加密模式下 a= 列表按优先级排序，xchacha20 优先。"""
        sc = SecureChannel(algorithm="auto")
        assert sc.offered_algorithms == OFFERED_ALGORITHMS
        assert sc.offered_algorithms[0] == "xchacha20"

    def test_offered_algorithms_none_mode(self):
        """不加密模式下仅提供 none（token 认证）。"""
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        assert sc.offered_algorithms == ["none"]

    def test_legacy_algorithm_normalized_to_auto(self):
        """历史配置值（xsalsa20/xchacha20）归一化为 auto。"""
        assert SecureChannel(algorithm="xsalsa20").algorithm == "auto"
        assert SecureChannel(algorithm="xchacha20").algorithm == "auto"
        assert SecureChannel(algorithm="auto").algorithm == "auto"

    def test_make_auth_ack_returns_encrypted(self):
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        session.receive_auth(make_phone_auth(sc))
        ack = session.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert ack["status"] == "OK"
        # 成功 ack 走 wrap 统一出口：整帧加密成线上字节（无明文泄漏、无信封）
        wrapped = session.wrap(ack)
        assert isinstance(wrapped, bytes)
        assert session.unwrap(wrapped) == ack

    def test_auth_ack_decrypts_correctly(self):
        """手机端独立推导会话密钥后可解开整帧加密的 auth_ack（首帧 seq=0）。"""
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        inner = json.dumps(
            {
                "algo": ALGO,
                "pk": base64.urlsafe_b64encode(bytes(phone_public)).decode().rstrip("="),
            }
        ).encode("utf-8")
        sb = SealedBox(sc.pc_private.public_key)
        session.receive_auth({"type": "auth", "data": sb.encrypt(inner)})
        ack = session.make_auth_ack()
        wrapped = session.wrap(ack)
        # 手机端：ECDH + blake2b 派生同一会话密钥
        shared = crypto_scalarmult(bytes(phone_private), bytes(sc.pc_private.public_key))
        session_key = blake2b(shared, digest_size=32).digest()
        pt = create_provider(ALGO, session_key).decrypt(wrapped)
        msg = frame_decode(pt)
        assert msg["type"] == "auth_ack"
        assert msg["status"] == "OK"
        assert "ts" in msg


class TestSecureChannelEncryptDecrypt:
    """测试数据加解密。"""

    def _setup_authenticated(self):
        """建立已完成握手的会话（手机端密封 {"algo","pk"} JSON）。"""
        sc = SecureChannel(algorithm="auto")
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        inner = json.dumps(
            {
                "algo": ALGO,
                "pk": base64.urlsafe_b64encode(
                    bytes(phone_private.public_key)
                ).decode().rstrip("="),
            }
        ).encode("utf-8")
        sb = SealedBox(sc.pc_private.public_key)
        session.receive_auth({"type": "auth", "data": sb.encrypt(inner)})
        return session, phone_private

    def test_wrap_unwrap_roundtrip(self):
        sc, _ = self._setup_authenticated()
        original = {"type": "preview", "text": "你好世界"}
        encrypted = sc.wrap(original)
        # 无外层信封：wrap 直接产出线上字节（密文），调用方只拿到 bytes
        assert isinstance(encrypted, bytes)
        decrypted = sc.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unwrap_with_english(self):
        sc, _ = self._setup_authenticated()
        original = {"type": "send", "text": "Hello World"}
        encrypted = sc.wrap(original)
        decrypted = sc.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unwrap_with_empty_text(self):
        sc, _ = self._setup_authenticated()
        original = {"type": "preview", "text": ""}
        encrypted = sc.wrap(original)
        decrypted = sc.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unwrap_with_nested_json(self):
        sc, _ = self._setup_authenticated()
        original = {"type": "config", "settings": {"max": 10, "lang": "zh"}}
        encrypted = sc.wrap(original)
        decrypted = sc.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_produces_different_ciphertexts(self):
        sc, _ = self._setup_authenticated()
        msg = {"type": "preview", "text": "hello"}
        ct1 = sc.wrap(msg)
        ct2 = sc.wrap(msg)
        assert ct1 != ct2

    def test_unwrap_returns_none_on_bad_data(self):
        sc, _ = self._setup_authenticated()
        assert sc.unwrap(b"garbage!!!") is None

    def test_unwrap_returns_none_on_tampered(self):
        sc, _ = self._setup_authenticated()
        encrypted = sc.wrap({"type": "preview", "text": "hello"})
        tampered = encrypted[:-4] + b"aaaa"
        assert sc.unwrap(tampered) is None

    def test_wrap_without_auth_is_plaintext(self):
        """未握手的会话没有 provider：wrap 退化为明文编码。

        只用于 rejected ack 这类握手期帧（无会话密钥可加密）；
        api.py 的数据出口只对已认证连接调用 wrap，不会明文发业务数据。
        """
        session = SecureChannel(algorithm="auto").new_session()
        msg = {"type": "preview", "text": "test"}
        assert session.wrap(msg) == frame_encode(msg)

    def test_replay_envelope_rejected(self):
        """同一信封重放：seq 未递增，返回 None。"""
        sc, _ = self._setup_authenticated()
        envelope = sc.wrap({"type": "preview", "text": "hello"})
        assert sc.unwrap(envelope) == {"type": "preview", "text": "hello"}
        # 重放同一密文：防重放拒绝
        assert sc.unwrap(envelope) is None

    def test_out_of_order_rejected(self):
        """乱序消息（先 seq=1 再 seq=0）：第 2 帧拒绝，第 1 帧正常。"""
        sc, _ = self._setup_authenticated()
        e1 = sc.wrap({"type": "preview", "text": "a"})
        e2 = sc.wrap({"type": "preview", "text": "b"})
        # 期望 seq=0 却收到 seq=1：拒绝，计数器不推进
        assert sc.unwrap(e2) is None
        assert sc.unwrap(e1) == {"type": "preview", "text": "a"}

    def test_seq_stripped_from_inner(self):
        """seq 由加密层承载，unwrap 后不暴露给业务层。"""
        sc, _ = self._setup_authenticated()
        inner = sc.unwrap(sc.wrap({"type": "send", "text": "x"}))
        assert inner == {"type": "send", "text": "x"}
        assert "seq" not in inner

    # 注：旧的 test_missing_seq_rejected（密文中无 seq）已删除——
    # seq 内化到 CryptoProvider 后，调用方无法绕过加密层构造"不带 seq 的密文"，
    # 该状态在线上不可达，正是内化的设计目标。


class TestSecureChannelStateMachine:
    """测试状态机行为。"""

    def test_not_authenticated_by_default(self):
        sc = SecureChannel(algorithm="auto")
        assert sc.new_session().is_authenticated is False

    def test_none_lan_auto_authenticated(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.new_session().is_authenticated is True

    def test_auth_timed_out_after_timeout(self):
        session = SecureChannel(algorithm="auto").new_session()
        session._connected_at = time.monotonic() - 11
        assert session.auth_timed_out is True

    def test_auth_not_timed_out_within_window(self):
        session = SecureChannel(algorithm="auto").new_session()
        assert session.auth_timed_out is False

    def test_auth_not_timed_out_after_auth(self):
        _, session = make_session()
        session._connected_at = time.monotonic() - 20
        assert session.auth_timed_out is False


class TestSessionIsolation:
    """测试会话隔离：新连接的握手不得影响已认证的连接。"""

    def test_new_session_does_not_reset_authenticated_session(self):
        """新建会话后，已认证会话仍保持认证态且能正常加解密。"""
        sc, session_a = make_session()
        sc.new_session()  # 攻击者建立新连接，尚未认证
        assert session_a.is_authenticated is True
        payload = session_a.wrap({"type": "send", "text": "x"})
        assert isinstance(payload, bytes)
        assert session_a.unwrap(payload) == {"type": "send", "text": "x"}

    def test_failed_auth_does_not_affect_authenticated_session(self):
        """新连接认证失败，不得让已认证会话退回明文。"""
        sc, session_a = make_session()
        session_b = sc.new_session()
        assert session_b.receive_auth({"type": "auth", "data": "bad"}) is False
        assert session_a.is_authenticated is True
        payload = session_a.wrap({"type": "send", "text": "x"})
        assert isinstance(payload, bytes)
        assert session_a.unwrap(payload) == {"type": "send", "text": "x"}

    def test_sessions_have_independent_keys(self):
        """两个会话各自持有独立密钥，互相无法解密对方报文。"""
        sc = SecureChannel(algorithm="auto")
        session_a = sc.new_session()
        session_a.receive_auth(make_phone_auth(sc))
        session_b = sc.new_session()
        session_b.receive_auth(make_phone_auth(sc))
        envelope = session_a.wrap({"type": "send", "text": "hello"})
        assert session_b.unwrap(envelope) is None

    def test_channel_keypair_stable_across_sessions(self):
        """多次建会话不会更换 PC 密钥对，二维码保持有效。"""
        sc = SecureChannel(algorithm="auto")
        pub = sc.get_public_key_b64()
        for _ in range(3):
            sc.new_session()
        assert sc.get_public_key_b64() == pub


class TestNoneMode:
    """测试 none 模式（不加密）。"""

    def test_none_lan_no_auth_needed(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.needs_auth is False
        assert sc.is_encrypted is False

    def test_none_lan_no_key(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.get_public_key_b64() is None

    def test_none_lan_append_url_unchanged(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        url = "http://192.168.1.100:12000"
        assert sc.append_to_url(url) == url

    def test_none_lan_wrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        msg = {"type": "send", "text": "hello"}
        # 明文模式：wrap 产出的就是 msgpack 编码字节，无加密、无信封
        assert sc.new_session().wrap(msg) == frame_encode(msg)

    def test_none_lan_unwrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        msg = {"type": "send", "text": "hello"}
        assert sc.new_session().unwrap(frame_encode(msg)) == msg

    def test_none_cf_needs_auth(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        assert sc.needs_auth is True
        assert sc.is_encrypted is False

    def test_none_cf_has_token(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        token = sc.get_public_key_b64()
        assert token is not None
        assert len(token) > 0

    def test_none_cf_append_url(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        url = "https://abc.trycloudflare.com"
        result = sc.append_to_url(url)
        token = sc.get_public_key_b64()
        assert f"#k={token}&a=none" in result

    def test_none_cf_auth_correct_token(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        session = sc.new_session()
        token = sc.get_public_key_b64()
        auth_msg = {"type": "auth", "algo": "none", "data": token}
        assert session.receive_auth(auth_msg) is True
        assert session.is_authenticated is True

    def test_none_cf_auth_wrong_token(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        session = sc.new_session()
        auth_msg = {"type": "auth", "algo": "none", "data": "wrong_token"}
        assert session.receive_auth(auth_msg) is False
        assert session.is_rejected is True
        assert "token" in session.reject_reason

    def test_none_cf_make_auth_ack(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        session = sc.new_session()
        token = sc.get_public_key_b64()
        session.receive_auth({"type": "auth", "algo": "none", "data": token})
        ack = session.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert ack["status"] == "OK"
        assert "data" not in ack

    def test_none_cf_wrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        msg = {"type": "send", "text": "hello"}
        # 明文模式：wrap 产出的就是 msgpack 编码字节，无加密、无信封
        assert sc.new_session().wrap(msg) == frame_encode(msg)

    def test_none_cf_unwrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        msg = {"type": "send", "text": "hello"}
        assert sc.new_session().unwrap(frame_encode(msg)) == msg

    def test_none_cf_tokens_unique(self):
        sc1 = SecureChannel(algorithm="none", mode="cloudflare")
        sc2 = SecureChannel(algorithm="none", mode="cloudflare")
        assert sc1.get_public_key_b64() != sc2.get_public_key_b64()

    def test_none_lan_new_session_still_authenticated(self):
        """none+LAN 无需握手，新会话天然处于已认证态。"""
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.new_session().is_authenticated is True

    def test_none_cf_new_session_starts_unauthenticated(self):
        """none+CF 每个新会话都要重新认证，且不影响已有会话。"""
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        token = sc.get_public_key_b64()
        session_a = sc.new_session()
        session_a.receive_auth({"type": "auth", "algo": "none", "data": token})
        assert session_a.is_authenticated is True

        session_b = sc.new_session()
        assert session_b.is_authenticated is False
        assert session_b.is_rejected is False
        assert session_a.is_authenticated is True

    def test_none_cf_auth_wrong_algo_rejected(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        session = sc.new_session()
        token = sc.get_public_key_b64()
        auth_msg = {"type": "auth", "algo": "xsalsa20", "data": token}
        assert session.receive_auth(auth_msg) is False
        assert session.is_rejected is True
        assert "not allowed" in session.reject_reason

    def test_none_default_is_lan(self):
        sc = SecureChannel()
        assert sc.algorithm == "none"
        assert sc.needs_auth is False
        assert sc.is_encrypted is False
