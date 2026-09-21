"""
SecureChannel / SecureSession 单元测试。

新架构（e2ee-always-on-design.md）：
- 加密永远开启，不存在明文模式
- 认证方式：url_fragment（扫码）或 tofu（手动审批）
- receive_auth() 返回 (algo, session_key, pin, phone_pk) 元组
- make_auth_challenge() 返回线上字节（bytes）
- verify_auth_proof() 不再需要外部传入 nonce
"""

import base64
import json
import time
from hashlib import blake2b

import pytest
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS, create_provider
from phonemic.tunnel.crypto.errors import CryptoError
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.frame import decode as frame_decode
from phonemic.tunnel.frame import encode as frame_encode

ALGO = "xchacha20"


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


def make_phone_auth(sc, algorithm=ALGO):
    """模拟手机端：密封 {"algo","pk"} JSON，返回 (auth_msg, phone_private)。"""
    phone_private = PrivateKey.generate()
    phone_public = phone_private.public_key
    inner = json.dumps({
        "algo": algorithm,
        "pk": _to_b64(bytes(phone_public)),
    }).encode("utf-8")
    sb = SealedBox(sc.pc_private.public_key)
    return {"type": "auth", "data": sb.encrypt(inner)}, phone_private


def make_tofu_first_auth(algorithm=ALGO, pin="3847"):
    """模拟手机端 TOFU 首次连接：明文 auth，返回 (auth_msg, phone_private)。"""
    phone_private = PrivateKey.generate()
    return {
        "type": "auth",
        "algo": algorithm,
        "pk": bytes(phone_private.public_key),
        "pin": pin,
    }, phone_private


def do_url_fragment_handshake(session, auth_msg_and_pk):
    """跑完 url_fragment / TOFU 重连三步握手。

    auth(SealedBox) → create_provider → auth_challenge(Provider加密) → auth_proof
    """
    auth_msg, phone_private = auth_msg_and_pk
    algo, session_key, pin, phone_pk = session.receive_auth(auth_msg)
    assert pin is None  # url_fragment 路径无 pin
    session.create_provider(algo, session_key)

    challenge_bytes = session.make_auth_challenge()
    nonce = session._challenge_nonce

    # 手机端解密 challenge（模拟）
    shared = crypto_scalarmult(bytes(phone_private), bytes(session._channel.pc_private.public_key))
    phone_session_key = blake2b(shared, digest_size=32).digest()
    phone_provider = create_provider(algo, phone_session_key)
    pt = phone_provider.decrypt(challenge_bytes)
    msg = frame_decode(pt)
    assert msg["type"] == "auth_challenge"

    # 手机端发送 auth_proof
    proof = phone_provider.encrypt(frame_encode({"type": "auth_proof", "nonce": msg["nonce"]}))
    assert session.verify_auth_proof(session.unwrap(proof)) is True
    return nonce


def do_tofu_first_handshake(session, auth_msg_and_pk):
    """跑完 TOFU 首次三步握手（含模拟审批）。

    auth(明文) → complete_tofu_auth → auth_challenge(SealedBox) → auth_proof
    """
    auth_msg, phone_private = auth_msg_and_pk
    algo, session_key, pin, phone_pk = session.receive_auth(auth_msg)
    assert pin is not None  # TOFU 首次有 pin
    assert session_key is None  # 尚未做 ECDH

    # 模拟审批通过
    session.complete_tofu_auth(algo, phone_pk)

    challenge_bytes = session.make_auth_challenge()

    # 手机端解封 SealedBox 取 pc_public + nonce
    sealed_data = frame_decode(challenge_bytes)["data"]
    inner = SealedBox(phone_private).decrypt(bytes(sealed_data))
    msg = json.loads(inner)
    pc_public = _from_b64(msg["pk"])
    nonce = _from_b64(msg["nonce"])

    # 手机端做 ECDH + 创建 Provider
    shared = crypto_scalarmult(bytes(phone_private), pc_public)
    phone_session_key = blake2b(shared, digest_size=32).digest()
    phone_provider = create_provider(algo, phone_session_key)

    # 手机端发送 auth_proof
    proof = phone_provider.encrypt(frame_encode({"type": "auth_proof", "nonce": nonce}))
    assert session.verify_auth_proof(session.unwrap(proof)) is True
    return nonce


def make_session(auth_method="url_fragment", mode="lan", phone_algo=ALGO):
    """创建一个已完成握手的会话。"""
    sc = SecureChannel(auth_method=auth_method, mode=mode)
    session = sc.new_session()
    if auth_method == "url_fragment":
        do_url_fragment_handshake(session, make_phone_auth(sc, algorithm=phone_algo))
    return sc, session


# ---------- 密钥对与 URL ----------

class TestSecureChannelKeys:
    def test_generates_different_keys(self):
        sc1 = SecureChannel(auth_method="url_fragment")
        sc2 = SecureChannel(auth_method="url_fragment")
        assert sc1.get_public_key_b64() != sc2.get_public_key_b64()

    def test_public_key_is_valid_base64url(self):
        sc = SecureChannel(auth_method="url_fragment")
        b64 = sc.get_public_key_b64()
        assert "=" not in b64
        decoded = base64.urlsafe_b64decode(b64 + "==")
        assert len(decoded) == 32

    def test_append_to_url_url_fragment(self):
        sc = SecureChannel(auth_method="url_fragment")
        url = "http://192.168.1.100:12000"
        result = sc.append_to_url(url)
        assert result.startswith(url + f"/{sc.secret_path}/#k=")
        assert f"a={','.join(OFFERED_ALGORITHMS)}" in result

    def test_append_to_url_cloudflare(self):
        sc = SecureChannel(auth_method="url_fragment")
        url = "https://abc-def.trycloudflare.com"
        result = sc.append_to_url(url)
        assert result.startswith(url + f"/{sc.secret_path}/#k=")

    def test_append_to_url_no_trailing_slash(self):
        sc = SecureChannel(auth_method="url_fragment")
        url = "http://localhost:8080/"
        result = sc.append_to_url(url)
        assert f"/{sc.secret_path}/#k=" in result
        assert "//#k=" not in result

    def test_tofu_bare_url(self):
        """TOFU 模式：裸 URL，无 fragment、无 secret_path。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        assert sc.secret_path == ""
        url = "http://192.168.1.100:12000"
        assert sc.append_to_url(url) == url

    def test_secret_path_only_in_url_fragment(self):
        """secret_path 仅在 url_fragment 模式生成，TOFU 为空串。"""
        uf = SecureChannel(auth_method="url_fragment")
        tofu = SecureChannel(auth_method="tofu", mode="lan")
        assert len(uf.secret_path) >= 20
        assert tofu.secret_path == ""
        uf2 = SecureChannel(auth_method="url_fragment")
        assert uf.secret_path != uf2.secret_path

    def test_channel_keypair_stable_across_sessions(self):
        sc = SecureChannel(auth_method="url_fragment")
        pub = sc.get_public_key_b64()
        for _ in range(3):
            sc.new_session()
        assert sc.get_public_key_b64() == pub


# ---------- URL fragment 认证握手 ----------

class TestUrlFragmentHandshake:
    def test_receive_auth_establishes_keys_but_not_authenticated(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, _ = make_phone_auth(sc)
        algo, session_key, pin, phone_pk = session.receive_auth(auth_msg)
        assert pin is None
        assert session_key is not None
        assert session.is_authenticated is False
        # 还没 create_provider，negotiated_algorithm 为 none
        assert session.negotiated_algorithm == "none"

    def test_full_handshake_completes(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        do_url_fragment_handshake(session, make_phone_auth(sc))
        assert session.is_authenticated is True

    def test_receive_auth_garbage_raises(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "data": b"not a sealed box!!!"})

    def test_receive_auth_wrong_key_raises(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        other_pc = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        inner = json.dumps({
            "algo": ALGO,
            "pk": _to_b64(bytes(phone_private.public_key)),
        }).encode("utf-8")
        sb = SealedBox(other_pc.public_key)
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "data": sb.encrypt(inner)})

    def test_receive_auth_unsupported_algo_raises(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        phone_private = PrivateKey.generate()
        inner = json.dumps({
            "algo": "aes-256-gcm",
            "pk": _to_b64(bytes(phone_private.public_key)),
        }).encode("utf-8")
        sb = SealedBox(sc.pc_private.public_key)
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "data": sb.encrypt(inner)})

    @pytest.mark.parametrize("phone_algo", OFFERED_ALGORITHMS)
    def test_accepts_any_offered_algo(self, phone_algo):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        do_url_fragment_handshake(session, make_phone_auth(sc, algorithm=phone_algo))
        assert session.is_authenticated is True
        assert session.negotiated_algorithm == phone_algo

    def test_make_auth_challenge_returns_bytes(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, _ = make_phone_auth(sc)
        algo, session_key, _, _ = session.receive_auth(auth_msg)
        session.create_provider(algo, session_key)
        challenge = session.make_auth_challenge()
        assert isinstance(challenge, bytes)

    def test_auth_challenge_phone_can_decrypt(self):
        """手机端独立推导会话密钥后可解开 Provider 加密的 auth_challenge。"""
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, phone_private = make_phone_auth(sc)
        algo, session_key, _, _ = session.receive_auth(auth_msg)
        session.create_provider(algo, session_key)
        challenge_bytes = session.make_auth_challenge()

        shared = crypto_scalarmult(bytes(phone_private), bytes(sc.pc_private.public_key))
        phone_key = blake2b(shared, digest_size=32).digest()
        pt = create_provider(ALGO, phone_key).decrypt(challenge_bytes)
        msg = frame_decode(pt)
        assert msg["type"] == "auth_challenge"
        assert len(msg["nonce"]) == 16


# ---------- 防重放 ----------

class TestAuthReplayProtection:
    def test_replayed_auth_cannot_complete(self):
        """录下真机的 auth 帧重放：解封成功，但过不了 nonce 这一关。"""
        sc = SecureChannel(auth_method="url_fragment")
        recorded = make_phone_auth(sc)

        victim = sc.new_session()
        do_url_fragment_handshake(victim, recorded)

        attacker = sc.new_session()
        algo, session_key, _, _ = attacker.receive_auth(dict(recorded[0]))  # 重放
        attacker.create_provider(algo, session_key)
        attacker.make_auth_challenge()
        attacker_nonce = attacker._challenge_nonce
        victim_nonce = victim._challenge_nonce
        assert attacker_nonce != victim_nonce

        # 重放者用 victim 的 nonce 伪造 auth_proof → 不匹配
        proof = attacker._provider.encrypt(
            frame_encode({"type": "auth_proof", "nonce": victim_nonce})
        )
        assert attacker.verify_auth_proof(attacker.unwrap(proof)) is False
        assert attacker.is_authenticated is False

    def test_nonce_fresh_per_challenge(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, _ = make_phone_auth(sc)
        algo, sk, _, _ = session.receive_auth(auth_msg)
        session.create_provider(algo, sk)
        session.make_auth_challenge()
        first = session._challenge_nonce
        session.make_auth_challenge()
        second = session._challenge_nonce
        assert first != second
        assert len(first) == len(second) == 16

    def test_proof_must_be_valid_dict(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, _ = make_phone_auth(sc)
        algo, sk, _, _ = session.receive_auth(auth_msg)
        session.create_provider(algo, sk)
        session.make_auth_challenge()
        for bad in ({"type": "auth_proof", "nonce": "str_not_bytes"},
                    {"type": "auth_proof"},
                    {"type": "auth_proof", "nonce": None},
                    None,
                    {"type": "auth", "nonce": b"\x00" * 16}):
            assert session.verify_auth_proof(bad) is False
        assert session.is_authenticated is False

    def test_proof_wrong_nonce_rejected(self):
        sc = SecureChannel(auth_method="url_fragment")
        session = sc.new_session()
        auth_msg, _ = make_phone_auth(sc)
        algo, sk, _, _ = session.receive_auth(auth_msg)
        session.create_provider(algo, sk)
        session.make_auth_challenge()
        assert session.verify_auth_proof(
            {"type": "auth_proof", "nonce": b"\x00" * 16}
        ) is False


# ---------- 加解密往返 ----------

class TestEncryptDecrypt:
    def _setup(self):
        sc, session = make_session()
        return session

    def test_wrap_unwrap_roundtrip(self):
        session = self._setup()
        original = {"type": "preview", "text": "你好世界"}
        encrypted = session.wrap(original)
        assert isinstance(encrypted, bytes)
        assert session.unwrap(encrypted) == original

    def test_wrap_unwrap_english(self):
        session = self._setup()
        original = {"type": "send", "text": "Hello World"}
        assert session.unwrap(session.wrap(original)) == original

    def test_wrap_unwrap_empty(self):
        session = self._setup()
        original = {"type": "preview", "text": ""}
        assert session.unwrap(session.wrap(original)) == original

    def test_wrap_unwrap_nested_json(self):
        session = self._setup()
        original = {"type": "config", "settings": {"max": 10, "lang": "zh"}}
        assert session.unwrap(session.wrap(original)) == original

    def test_wrap_produces_different_ciphertexts(self):
        session = self._setup()
        msg = {"type": "preview", "text": "hello"}
        assert session.wrap(msg) != session.wrap(msg)

    def test_unwrap_none_on_bad_data(self):
        session = self._setup()
        assert session.unwrap(b"garbage!!!") is None

    def test_unwrap_none_on_tampered(self):
        session = self._setup()
        encrypted = session.wrap({"type": "preview", "text": "hello"})
        tampered = encrypted[:-4] + b"aaaa"
        assert session.unwrap(tampered) is None

    def test_wrap_without_provider_is_plaintext(self):
        session = SecureChannel(auth_method="url_fragment").new_session()
        msg = {"type": "preview", "text": "test"}
        assert session.wrap(msg) == frame_encode(msg)

    def test_replay_rejected(self):
        session = self._setup()
        envelope = session.wrap({"type": "preview", "text": "hello"})
        assert session.unwrap(envelope) == {"type": "preview", "text": "hello"}
        assert session.unwrap(envelope) is None

    def test_out_of_order_rejected(self):
        session = self._setup()
        e1 = session.wrap({"type": "preview", "text": "a"})
        e2 = session.wrap({"type": "preview", "text": "b"})
        assert session.unwrap(e2) is None
        assert session.unwrap(e1) == {"type": "preview", "text": "a"}

    def test_seq_not_in_plaintext(self):
        session = self._setup()
        inner = session.unwrap(session.wrap({"type": "send", "text": "x"}))
        assert inner == {"type": "send", "text": "x"}
        assert "seq" not in inner


# ---------- 状态机 ----------

class TestStateMachine:
    def test_not_authenticated_by_default(self):
        sc = SecureChannel(auth_method="url_fragment")
        assert sc.new_session().is_authenticated is False

    def test_needs_auth_always_true(self):
        assert SecureChannel(auth_method="url_fragment").needs_auth is True
        assert SecureChannel(auth_method="tofu", mode="lan").needs_auth is True

    def test_is_encrypted_always_true(self):
        assert SecureChannel(auth_method="url_fragment").is_encrypted is True
        assert SecureChannel(auth_method="tofu", mode="lan").is_encrypted is True

    def test_auth_timed_out(self):
        session = SecureChannel(auth_method="url_fragment").new_session()
        session._connected_at = time.monotonic() - 11
        assert session.auth_timed_out is True

    def test_auth_not_timed_out_within_window(self):
        session = SecureChannel(auth_method="url_fragment").new_session()
        assert session.auth_timed_out is False

    def test_auth_not_timed_out_after_handshake(self):
        _, session = make_session()
        session._connected_at = time.monotonic() - 20
        assert session.auth_timed_out is False


# ---------- 会话隔离 ----------

class TestSessionIsolation:
    def test_new_session_does_not_affect_authenticated(self):
        sc, session_a = make_session()
        sc.new_session()
        assert session_a.is_authenticated is True
        payload = session_a.wrap({"type": "send", "text": "x"})
        assert session_a.unwrap(payload) == {"type": "send", "text": "x"}

    def test_failed_auth_does_not_affect_authenticated(self):
        sc, session_a = make_session()
        session_b = sc.new_session()
        with pytest.raises(CryptoError):
            session_b.receive_auth({"type": "auth", "data": b"bad"})
        assert session_a.is_authenticated is True

    def test_sessions_have_independent_keys(self):
        sc = SecureChannel(auth_method="url_fragment")
        session_a = sc.new_session()
        do_url_fragment_handshake(session_a, make_phone_auth(sc))
        session_b = sc.new_session()
        do_url_fragment_handshake(session_b, make_phone_auth(sc))
        envelope = session_a.wrap({"type": "send", "text": "hello"})
        assert session_b.unwrap(envelope) is None


# ---------- TOFU 首次连接 ----------

class TestTofuFirstHandshake:
    def test_receive_tofu_first_auth(self):
        """TOFU 首次：明文 auth，返回 (algo, None, pin, phone_pk)。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        auth_msg, phone_private = make_tofu_first_auth(pin="1234")
        algo, session_key, pin, phone_pk = session.receive_auth(auth_msg)
        assert algo == ALGO
        assert session_key is None  # 尚未做 ECDH
        assert pin == "1234"
        assert phone_pk == bytes(phone_private.public_key)
        assert session.is_tofu_first is True
        assert session.is_authenticated is False

    def test_complete_tofu_auth_creates_provider(self):
        """审批通过后 complete_tofu_auth 做 ECDH + 创建 Provider。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        auth_msg, phone_private = make_tofu_first_auth()
        algo, _, pin, phone_pk = session.receive_auth(auth_msg)
        session.complete_tofu_auth(algo, phone_pk)
        assert session._provider is not None
        assert session.negotiated_algorithm == ALGO

    def test_tofu_first_full_handshake(self):
        """TOFU 首次完整握手：auth → 审批 → challenge(SealedBox) → proof。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        do_tofu_first_handshake(session, make_tofu_first_auth())
        assert session.is_authenticated is True

    def test_tofu_first_challenge_uses_sealed_box(self):
        """TOFU 首次的 auth_challenge 是 SealedBox(phone_public) 加密，非 Provider 加密。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        auth_msg, phone_private = make_tofu_first_auth()
        algo, _, _, phone_pk = session.receive_auth(auth_msg)
        session.complete_tofu_auth(algo, phone_pk)
        challenge_bytes = session.make_auth_challenge()

        # SealedBox 路径：手机端用 phone_private 解封
        msg = frame_decode(challenge_bytes)
        assert msg["type"] == "auth_challenge"
        assert "data" in msg  # SealedBox blob，不是明文 nonce

        inner = SealedBox(phone_private).decrypt(bytes(msg["data"]))
        data = json.loads(inner)
        assert "pk" in data  # PC 公钥
        assert "nonce" in data

    def test_tofu_first_session_key_matches(self):
        """TOFU 首次的 session_key 与手机端独立推导一致。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        auth_msg, phone_private = make_tofu_first_auth()
        algo, _, _, phone_pk = session.receive_auth(auth_msg)
        session.complete_tofu_auth(algo, phone_pk)

        # PC 端 session_key
        pc_session_key = session._provider._aead._key if hasattr(session._provider, '_aead') else None

        # 手机端推导
        shared = crypto_scalarmult(bytes(phone_private), bytes(sc.pc_private.public_key))
        expected = blake2b(shared, digest_size=32).digest()

        # 验证加解密往返（间接验证密钥一致）
        ct = session.wrap({"type": "test"})
        phone_provider = create_provider(ALGO, expected)
        assert phone_provider.decrypt(ct) == frame_encode({"type": "test"})

    def test_tofu_first_missing_fields_raises(self):
        """TOFU 首次 auth 缺少 algo 或 pk 时抛 CryptoError。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "pin": "1234"})
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "algo": ALGO, "pin": "1234"})

    def test_tofu_first_invalid_pk_raises(self):
        """TOFU 首次 auth 的 pk 不是 bin 类型时抛 CryptoError。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        with pytest.raises(CryptoError):
            session.receive_auth({"type": "auth", "algo": ALGO, "pk": "not_bytes", "pin": "1234"})


# ---------- TOFU 重连（等同 url_fragment） ----------

class TestTofuReconnect:
    def test_tofu_reconnect_uses_sealed_box(self):
        """TOFU 重连：手机用 localStorage 里的 PC 公钥做 SealedBox 密封 auth。"""
        sc = SecureChannel(auth_method="tofu", mode="lan")
        session = sc.new_session()
        # 模拟手机端有 PC 公钥（存自首次连接），走 SealedBox 路径
        do_url_fragment_handshake(session, make_phone_auth(sc))
        assert session.is_authenticated is True
        assert session.is_tofu_first is False  # 重连不是 TOFU 首次

    def test_tofu_reconnect_handshake_same_as_url_fragment(self):
        """TOFU 重连与 url_fragment 认证的握手帧格式完全一致。"""
        sc_tofu = SecureChannel(auth_method="tofu", mode="lan")
        session_tofu = sc_tofu.new_session()
        auth_tofu, phone_private = make_phone_auth(sc_tofu)
        algo, session_key, pin, _ = session_tofu.receive_auth(auth_tofu)
        assert pin is None  # SealedBox 路径无 pin
        assert session_key is not None
        session_tofu.create_provider(algo, session_key)
        challenge_bytes = session_tofu.make_auth_challenge()
        # challenge 是 Provider 加密（与 url_fragment 一致），可被手机端解密
        shared = crypto_scalarmult(bytes(phone_private), bytes(sc_tofu.pc_private.public_key))
        phone_key = blake2b(shared, digest_size=32).digest()
        pt = create_provider(ALGO, phone_key).decrypt(challenge_bytes)
        msg = frame_decode(pt)
        assert msg["type"] == "auth_challenge"
        assert len(msg["nonce"]) == 16
