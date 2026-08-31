"""
SecureChannel / SecureSession 单元测试。
测试密钥对生成、auth 握手、加解密、状态机、none 模式、会话隔离。
"""

import base64
import json
import time

import pytest
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.tunnel.e2ee import SecureChannel

ALGO = "xsalsa20"


def make_session(algorithm=ALGO, mode="lan"):
    """创建一个已完成握手的会话（加密模式）。"""
    sc = SecureChannel(algorithm=algorithm, mode=mode)
    session = sc.new_session()
    session.receive_auth(make_phone_auth(sc, algorithm=algorithm))
    return sc, session


def make_phone_auth(sc, algorithm=ALGO):
    """模拟手机端：用 sealed box 加密手机公钥。"""
    phone_private = PrivateKey.generate()
    phone_public = phone_private.public_key
    sb = SealedBox(sc.pc_private.public_key)
    sealed = sb.encrypt(bytes(phone_public))
    sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
    return {"type": "auth", "algo": algorithm, "data": sealed_b64}


class TestSecureChannelKeys:
    """测试密钥对生成和编码。"""

    def test_generates_different_keys(self):
        sc1 = SecureChannel(algorithm=ALGO)
        sc2 = SecureChannel(algorithm=ALGO)
        assert sc1.get_public_key_b64() != sc2.get_public_key_b64()

    def test_public_key_is_valid_base64url(self):
        sc = SecureChannel(algorithm=ALGO)
        b64 = sc.get_public_key_b64()
        assert "=" not in b64
        decoded = base64.urlsafe_b64decode(b64 + "==")
        assert len(decoded) == 32

    def test_append_to_url_lan(self):
        sc = SecureChannel(algorithm=ALGO)
        url = "http://192.168.1.100:12000"
        result = sc.append_to_url(url)
        assert result.startswith(url + "/#k=")
        assert "a=xsalsa20" in result

    def test_append_to_url_cloudflare(self):
        sc = SecureChannel(algorithm=ALGO)
        url = "https://abc-def.trycloudflare.com"
        result = sc.append_to_url(url)
        assert result.startswith(url + "/#k=")

    def test_append_to_url_no_trailing_slash(self):
        sc = SecureChannel(algorithm=ALGO)
        url = "http://localhost:8080/"
        result = sc.append_to_url(url)
        assert "//#k=" not in result


class TestSecureChannelAuth:
    """测试 auth 握手流程。"""

    def test_receive_auth_succeeds(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        assert session.receive_auth(make_phone_auth(sc)) is True
        assert session.is_authenticated is True

    def test_receive_auth_fails_with_garbage(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        bad = {"type": "auth", "algo": ALGO, "data": "not_valid_base64!!!"}
        assert session.receive_auth(bad) is False
        assert session.is_authenticated is False

    def test_receive_auth_fails_with_wrong_key(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        other_pc = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        sb = SealedBox(other_pc.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        bad = {"type": "auth", "algo": ALGO, "data": sealed_b64}
        assert session.receive_auth(bad) is False
        assert session.is_authenticated is False

    def test_receive_auth_fails_with_wrong_algo(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        auth_msg = {"type": "auth", "algo": "xchacha20", "data": "whatever"}
        assert session.receive_auth(auth_msg) is False
        assert session.is_rejected is True

    def test_make_auth_ack_returns_encrypted(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        session.receive_auth(make_phone_auth(sc))
        ack = session.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert "data" in ack
        assert len(ack["data"]) > 0

    def test_auth_ack_decrypts_correctly(self):
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        sb = SealedBox(sc.pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_public))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        session.receive_auth({"type": "auth", "algo": ALGO, "data": sealed_b64})
        ack = session.make_auth_ack()
        phone_box = Box(phone_private, sc.pc_private.public_key)
        raw = base64.urlsafe_b64decode(ack["data"] + "==")
        nonce = raw[: Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE :]
        pt = phone_box.decrypt(ct, nonce)
        msg = json.loads(pt)
        assert msg["status"] == "OK"
        assert "ts" in msg


class TestSecureChannelEncryptDecrypt:
    """测试数据加解密。"""

    def _setup_authenticated(self):
        """建立已完成握手的会话，返回 (session, phone_box)。"""
        sc = SecureChannel(algorithm=ALGO)
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        sb = SealedBox(sc.pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        session.receive_auth({"type": "auth", "algo": ALGO, "data": sealed_b64})
        phone_box = Box(phone_private, sc.pc_private.public_key)
        return session, phone_box

    def test_wrap_unwrap_roundtrip(self):
        sc, _ = self._setup_authenticated()
        original = {"type": "preview", "text": "你好世界"}
        encrypted = sc.wrap(original)
        assert encrypted["type"] == "data"
        assert "data" in encrypted
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
        assert ct1["data"] != ct2["data"]

    def test_unwrap_returns_none_on_bad_data(self):
        sc, _ = self._setup_authenticated()
        assert sc.unwrap({"type": "data", "data": "garbage!!!"}) is None

    def test_unwrap_returns_none_on_tampered(self):
        sc, _ = self._setup_authenticated()
        encrypted = sc.wrap({"type": "preview", "text": "hello"})
        tampered = encrypted["data"][:-4] + "aaaa"
        assert sc.unwrap({"type": "data", "data": tampered}) is None

    def test_wrap_without_auth_raises(self):
        """未握手的会话没有 provider，wrap 会失败而非静默发送明文。"""
        session = SecureChannel(algorithm=ALGO).new_session()
        with pytest.raises(AttributeError):
            session.wrap({"type": "preview", "text": "test"})

    def test_replay_envelope_rejected(self):
        """同一信封重放：seq 未递增，返回 None。"""
        sc, _ = self._setup_authenticated()
        envelope = sc.wrap({"type": "preview", "text": "hello"})
        assert sc.unwrap(envelope) == {"type": "preview", "text": "hello"}
        # 重放同一密文：防重放拒绝
        assert sc.unwrap(envelope) is None

    def test_out_of_order_rejected(self):
        """乱序消息（先 seq=1 再 seq=0）：拒绝。"""
        sc, _ = self._setup_authenticated()
        e1 = sc.wrap({"type": "preview", "text": "a"})
        e2 = sc.wrap({"type": "preview", "text": "b"})
        assert sc.unwrap(e2) == {"type": "preview", "text": "b"}
        assert sc.unwrap(e1) is None

    def test_seq_stripped_from_inner(self):
        """seq 是传输层字段，unwrap 后不暴露给业务层。"""
        sc, _ = self._setup_authenticated()
        inner = sc.unwrap(sc.wrap({"type": "send", "text": "x"}))
        assert inner == {"type": "send", "text": "x"}
        assert "seq" not in inner

    def test_missing_seq_rejected(self):
        """密文中没有 seq 字段：按防重放失败处理。"""
        sc, _ = self._setup_authenticated()
        # 直接加密不含 seq 的明文
        raw_pt = b'{"type":"send","text":"x"}'
        encrypted = sc._provider.encrypt(raw_pt)
        import base64
        envelope = {"type": "data", "data": base64.urlsafe_b64encode(encrypted).decode().rstrip("=")}
        assert sc.unwrap(envelope) is None


class TestSecureChannelStateMachine:
    """测试状态机行为。"""

    def test_not_authenticated_by_default(self):
        sc = SecureChannel(algorithm=ALGO)
        assert sc.new_session().is_authenticated is False

    def test_none_lan_auto_authenticated(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.new_session().is_authenticated is True

    def test_auth_timed_out_after_timeout(self):
        session = SecureChannel(algorithm=ALGO).new_session()
        session._connected_at = time.monotonic() - 11
        assert session.auth_timed_out is True

    def test_auth_not_timed_out_within_window(self):
        session = SecureChannel(algorithm=ALGO).new_session()
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
        assert session_a.wrap({"type": "send", "text": "x"})["type"] == "data"

    def test_failed_auth_does_not_affect_authenticated_session(self):
        """新连接认证失败，不得让已认证会话退回明文。"""
        sc, session_a = make_session()
        session_b = sc.new_session()
        assert session_b.receive_auth({"type": "auth", "algo": ALGO, "data": "bad"}) is False
        assert session_a.is_authenticated is True
        assert session_a.wrap({"type": "send", "text": "x"})["type"] == "data"

    def test_sessions_have_independent_keys(self):
        """两个会话各自持有独立密钥，互相无法解密对方报文。"""
        sc = SecureChannel(algorithm=ALGO)
        session_a = sc.new_session()
        session_a.receive_auth(make_phone_auth(sc))
        session_b = sc.new_session()
        session_b.receive_auth(make_phone_auth(sc))
        envelope = session_a.wrap({"type": "send", "text": "hello"})
        assert session_b.unwrap(envelope) is None

    def test_channel_keypair_stable_across_sessions(self):
        """多次建会话不会更换 PC 密钥对，二维码保持有效。"""
        sc = SecureChannel(algorithm=ALGO)
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
        assert sc.new_session().wrap(msg) == msg

    def test_none_lan_unwrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        msg = {"type": "send", "text": "hello"}
        assert sc.new_session().unwrap(msg) == msg

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
        assert sc.new_session().wrap(msg) == msg

    def test_none_cf_unwrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        msg = {"type": "send", "text": "hello"}
        assert sc.new_session().unwrap(msg) == msg

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
