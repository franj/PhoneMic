"""
SecureChannel 单元测试。
测试密钥对生成、auth 握手、加解密、状态机。
"""

import base64
import json
import time

import pytest
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.tunnel.e2ee import SecureChannel


class TestSecureChannelKeys:
    """测试密钥对生成和编码。"""

    def test_generates_different_keys(self):
        sc1 = SecureChannel()
        sc2 = SecureChannel()
        assert sc1.get_public_key_b64() != sc2.get_public_key_b64()

    def test_public_key_is_valid_base64url(self):
        sc = SecureChannel()
        b64 = sc.get_public_key_b64()
        # URL-safe base64 without padding
        assert "=" not in b64
        decoded = base64.urlsafe_b64decode(b64 + "==")
        assert len(decoded) == 32  # X25519 public key

    def test_append_to_url_lan(self):
        sc = SecureChannel()
        url = "http://192.168.1.100:12000"
        result = sc.append_to_url(url)
        assert result.startswith(url + "/#k=")
        assert len(result) > len(url) + 5

    def test_append_to_url_cloudflare(self):
        sc = SecureChannel()
        url = "https://abc-def.trycloudflare.com"
        result = sc.append_to_url(url)
        assert result.startswith(url + "/#k=")

    def test_append_to_url_no_trailing_slash(self):
        sc = SecureChannel()
        url = "http://localhost:8080/"
        result = sc.append_to_url(url)
        # 不应该双斜杠
        assert "//#k=" not in result


class TestSecureChannelAuth:
    """测试 auth 握手流程。"""

    def _make_phone_auth(self, sc: SecureChannel) -> str:
        """模拟手机端：用 sealed box 加密手机公钥。"""
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_public))
        return base64.urlsafe_b64encode(sealed).decode().rstrip("=")

    def test_receive_auth_succeeds(self):
        sc = SecureChannel()
        sealed_b64 = self._make_phone_auth(sc)
        assert sc.receive_auth(sealed_b64) is True
        assert sc.is_authenticated is True

    def test_receive_auth_fails_with_garbage(self):
        sc = SecureChannel()
        assert sc.receive_auth("not_valid_base64!!!") is False
        assert sc.is_authenticated is False

    def test_receive_auth_fails_with_wrong_key(self):
        sc = SecureChannel()
        # 用另一个 PC 的公钥密封
        other_pc = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        sb = SealedBox(other_pc.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sealed_b64 = base64.urlsafe_b64encode(sealed).decode().rstrip("=")
        assert sc.receive_auth(sealed_b64) is False
        assert sc.is_authenticated is False

    def test_make_auth_ack_returns_encrypted(self):
        sc = SecureChannel()
        sealed_b64 = self._make_phone_auth(sc)
        sc.receive_auth(sealed_b64)
        ack = sc.make_auth_ack()
        assert ack["type"] == "auth_ack"
        assert "data" in ack
        assert len(ack["data"]) > 0

    def test_auth_ack_decrypts_correctly(self):
        """验证 auth_ack 可以被手机端解密。"""
        sc = SecureChannel()
        # 手机端密钥对
        phone_private = PrivateKey.generate()
        phone_public = phone_private.public_key
        # 发送 auth
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_public))
        sc.receive_auth(base64.urlsafe_b64encode(sealed).decode().rstrip("="))
        # PC 回复 auth_ack
        ack = sc.make_auth_ack()
        # 手机端解密 auth_ack
        phone_box = Box(phone_private, sc._pc_private.public_key)
        raw = base64.urlsafe_b64decode(ack["data"] + "==")
        nonce = raw[:Box.NONCE_SIZE]
        ct = raw[Box.NONCE_SIZE:]
        pt = phone_box.decrypt(ct, nonce)
        msg = json.loads(pt)
        assert msg["status"] == "OK"
        assert "ts" in msg


class TestSecureChannelEncryptDecrypt:
    """测试数据加解密。"""

    def _setup_authenticated(self) -> tuple[SecureChannel, Box]:
        sc = SecureChannel()
        phone_private = PrivateKey.generate()
        sb = SealedBox(sc._pc_private.public_key)
        sealed = sb.encrypt(bytes(phone_private.public_key))
        sc.receive_auth(base64.urlsafe_b64encode(sealed).decode().rstrip("="))
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
        assert ct1["data"] != ct2["data"]  # nonce 不同

    def test_unwrap_returns_none_on_bad_data(self):
        sc, _ = self._setup_authenticated()
        assert sc.unwrap({"type": "data", "data": "garbage!!!"}) is None

    def test_unwrap_returns_none_on_tampered(self):
        sc, _ = self._setup_authenticated()
        encrypted = sc.wrap({"type": "preview", "text": "hello"})
        tampered = encrypted["data"][:-4] + "aaaa"
        assert sc.unwrap({"type": "data", "data": tampered}) is None

    def test_wrap_without_auth_raises(self):
        sc = SecureChannel()
        with pytest.raises(AttributeError):
            sc.wrap({"type": "preview", "text": "test"})


class TestSecureChannelStateMachine:
    """测试状态机行为。"""

    def test_not_authenticated_by_default(self):
        sc = SecureChannel()
        assert sc.is_authenticated is False

    def test_on_new_connection_resets_state(self):
        sc = SecureChannel()
        # 模拟已认证
        sc._authenticated = True
        sc._box = Box(sc._pc_private, sc._pc_private.public_key)
        # 重置
        sc.on_new_connection()
        assert sc.is_authenticated is False
        assert sc._box is None

    def test_auth_timed_out_after_timeout(self):
        sc = SecureChannel()
        # 模拟连接已建立超过 10 秒
        sc._connected_at = time.monotonic() - 11
        assert sc.auth_timed_out is True

    def test_auth_not_timed_out_within_window(self):
        sc = SecureChannel()
        # 刚刚建立连接
        assert sc.auth_timed_out is False

    def test_auth_not_timed_out_after_auth(self):
        sc = SecureChannel()
        sc._authenticated = True
        sc._connected_at = time.monotonic() - 20  # 即使超过 10 秒
        assert sc.auth_timed_out is False
