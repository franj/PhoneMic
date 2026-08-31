"""
SecureChannel 单元测试。
测试密钥对生成、auth 握手、加解密、状态机、none 模式。
"""

import base64
import json
import time

import pytest
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.tunnel.e2ee import SecureChannel

ALGO = "xsalsa20"


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

    def _make_phone_auth(self, sc: SecureChannel) -> dict:
        """模拟手机端：用 sealed box 加密手机公钥。"""
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_public))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        return {"type": "auth", "algo": ALGO, "data": sealed_b64}

    def test_receive_auth_succeeds(self):
        sc = SecureChannel(algorithm=ALGO)
        auth_msg = self._make_phone_auth(sc)
        assert sc.receive_auth(auth_msg) is True
        assert sc.is_authenticated is True

    def test_receive_auth_fails_with_garbage(self):
        sc = SecureChannel(algorithm=ALGO)
        assert sc.receive_auth({"type": "auth", "algo": ALGO, "data": "not_valid_base64!!!"}) is False
        assert sc.is_authenticated is False

    def test_receive_auth_fails_with_wrong_key(self):
        sc = SecureChannel(algorithm=ALGO)
        other_pc = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        sb = SealedBox(other_pc.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        assert sc.receive_auth({"type": "auth", "algo": ALGO, "data": sealed_b64}) is False
        assert sc.is_authenticated is False

    def test_receive_auth_fails_with_wrong_algo(self):
        sc = SecureChannel(algorithm=ALGO)
        auth_msg = {"type": "auth", "algo": "xchacha20", "data": "whatever"}
        assert sc.receive_auth(auth_msg) is False
        assert sc.is_rejected is True

    def test_make_auth_ack_returns_encrypted(self):
        sc = SecureChannel(algorithm=ALGO)
        auth_msg = self._make_phone_auth(sc)
        sc.receive_auth(auth_msg)
        ack = sc.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert "data" in ack
        assert len(ack["data"]) > 0

    def test_auth_ack_decrypts_correctly(self):
        sc = SecureChannel(algorithm=ALGO)
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_public))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        sc.receive_auth({"type": "auth", "algo": ALGO, "data": sealed_b64})
        ack = sc.make_auth_ack()
        phone_box = Box(phone_private, sc._pc_private.public_key)
        raw = base64.urlsafe_b64decode(ack["data"] + "==")
        nonce = raw[: Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE :]
        pt = phone_box.decrypt(ct, nonce)
        msg = json.loads(pt)
        assert msg["status"] == "OK"
        assert "ts" in msg


class TestSecureChannelEncryptDecrypt:
    """测试数据加解密。"""

    def _setup_authenticated(self) -> tuple[SecureChannel, Box]:
        sc = SecureChannel(algorithm=ALGO)
        phone_private = PrivateKey.generate()
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        sc.receive_auth({"type": "auth", "algo": ALGO, "data": sealed_b64})
        phone_box = Box(phone_private, sc._pc_private.public_key)
        return sc, phone_box

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
        sc = SecureChannel(algorithm=ALGO)
        with pytest.raises(AttributeError):
            sc.wrap({"type": "preview", "text": "test"})


class TestSecureChannelStateMachine:
    """测试状态机行为。"""

    def test_not_authenticated_by_default(self):
        sc = SecureChannel(algorithm=ALGO)
        assert sc.is_authenticated is False

    def test_none_lan_auto_authenticated(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        assert sc.is_authenticated is True

    def test_on_new_connection_resets_state(self):
        sc = SecureChannel(algorithm=ALGO)
        sc._authenticated = True
        sc.on_new_connection()
        assert sc.is_authenticated is False
        assert sc._provider is None

    def test_auth_timed_out_after_timeout(self):
        sc = SecureChannel(algorithm=ALGO)
        sc._connected_at = time.monotonic() - 11
        assert sc.auth_timed_out is True

    def test_auth_not_timed_out_within_window(self):
        sc = SecureChannel(algorithm=ALGO)
        assert sc.auth_timed_out is False

    def test_auth_not_timed_out_after_auth(self):
        sc = SecureChannel(algorithm=ALGO)
        sc._authenticated = True
        sc._connected_at = time.monotonic() - 20
        assert sc.auth_timed_out is False


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
        assert sc.wrap(msg) == msg

    def test_none_lan_unwrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="lan")
        msg = {"type": "send", "text": "hello"}
        assert sc.unwrap(msg) == msg

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
        token = sc.get_public_key_b64()
        auth_msg = {"type": "auth", "algo": "none", "data": token}
        assert sc.receive_auth(auth_msg) is True
        assert sc.is_authenticated is True

    def test_none_cf_auth_wrong_token(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        auth_msg = {"type": "auth", "algo": "none", "data": "wrong_token"}
        assert sc.receive_auth(auth_msg) is False
        assert sc.is_rejected is True
        assert "token" in sc.reject_reason

    def test_none_cf_make_auth_ack(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        token = sc.get_public_key_b64()
        sc.receive_auth({"type": "auth", "algo": "none", "data": token})
        ack = sc.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert ack["status"] == "OK"
        assert "data" not in ack

    def test_none_cf_wrap_passthrough(self):
        sc = SecureChannel(algorithm="none", mode="cloudflare")
        msg = {"type": "send", "text": "hello"}
        assert sc.wrap(msg) == msg

    def test_none_default_is_lan(self):
        sc = SecureChannel()
        assert sc.algorithm == "none"
        assert sc.needs_auth is False
        assert sc.is_encrypted is False
