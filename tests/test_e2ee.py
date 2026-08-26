"""
E2EE 管理器单元测试。
测试密钥生成、加解密、URL fragment 拼接、状态切换。
"""

import base64
import json

import pytest

from phonemic.tunnel.e2ee import E2EEManager


class TestE2EEState:
    """测试启用/禁用状态管理。"""

    def test_disabled_by_default(self):
        mgr = E2EEManager()
        assert mgr.enabled is False

    def test_enable_sets_enabled(self):
        mgr = E2EEManager()
        mgr.enable()
        assert mgr.enabled is True

    def test_disable_clears_state(self):
        mgr = E2EEManager()
        mgr.enable()
        mgr.disable()
        assert mgr.enabled is False

    def test_enable_generates_different_keys(self):
        mgr = E2EEManager()
        mgr.enable()
        key1 = mgr.get_key_b64()
        mgr.enable()
        key2 = mgr.get_key_b64()
        assert key1 != key2

    def test_disable_clears_key(self):
        mgr = E2EEManager()
        mgr.enable()
        assert mgr.get_key_b64() != ""
        mgr.disable()
        assert mgr.get_key_b64() == ""


class TestE2EEUrl:
    """测试 URL fragment 拼接。"""

    def test_append_to_url_when_enabled(self):
        mgr = E2EEManager()
        mgr.enable()
        url = "http://192.168.1.100:12000"
        result = mgr.append_to_url(url)
        assert result.startswith(url + "#k=")
        assert len(mgr.get_key_b64()) > 0

    def test_append_to_url_when_disabled(self):
        mgr = E2EEManager()
        url = "http://192.168.1.100:12000"
        result = mgr.append_to_url(url)
        assert result == url

    def test_append_to_cloudflare_url(self):
        mgr = E2EEManager()
        mgr.enable()
        url = "https://abc-def.trycloudflare.com"
        result = mgr.append_to_url(url)
        assert result.startswith(url + "#k=")


class TestE2EEEncryptDecrypt:
    """测试加解密核心功能。"""

    def test_wrap_unwrap_roundtrip(self):
        mgr = E2EEManager()
        mgr.enable()
        original = {"type": "preview", "text": "你好世界"}
        encrypted = mgr.wrap(original)
        assert encrypted["type"] == "encrypted"
        assert "data" in encrypted
        decrypted = mgr.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unroll_with_english(self):
        mgr = E2EEManager()
        mgr.enable()
        original = {"type": "send", "text": "Hello World"}
        encrypted = mgr.wrap(original)
        decrypted = mgr.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unroll_with_empty_text(self):
        mgr = E2EEManager()
        mgr.enable()
        original = {"type": "preview", "text": ""}
        encrypted = mgr.wrap(original)
        decrypted = mgr.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_unroll_with_nested_json(self):
        mgr = E2EEManager()
        mgr.enable()
        original = {"type": "config", "settings": {"max": 10, "lang": "zh"}}
        encrypted = mgr.wrap(original)
        decrypted = mgr.unwrap(encrypted)
        assert decrypted == original

    def test_wrap_produces_different_ciphertexts(self):
        mgr = E2EEManager()
        mgr.enable()
        msg = {"type": "preview", "text": "hello"}
        ct1 = mgr.wrap(msg)
        ct2 = mgr.wrap(msg)
        assert ct1["data"] != ct2["data"]  # nonce 不同

    def test_decrypt_with_wrong_key_fails(self):
        mgr1 = E2EEManager()
        mgr1.enable()
        encrypted = mgr1.wrap({"type": "send", "text": "secret"})

        mgr2 = E2EEManager()
        mgr2.enable()  # 不同的密钥
        with pytest.raises(Exception):
            mgr2.unwrap(encrypted)

    def test_decrypt_tampered_data_fails(self):
        mgr = E2EEManager()
        mgr.enable()
        encrypted = mgr.wrap({"type": "preview", "text": "hello"})
        # 篡改密文
        tampered = encrypted["data"][:-4] + "aaaa"
        with pytest.raises(Exception):
            mgr.unwrap({"type": "encrypted", "data": tampered})

    def test_unwrap_missing_data_field_raises(self):
        mgr = E2EEManager()
        mgr.enable()
        with pytest.raises(ValueError, match="missing 'data'"):
            mgr.unwrap({"type": "encrypted"})


class TestE2EEDisabledPassthrough:
    """测试禁用时的透传行为。"""

    def test_disabled_get_key_b64_returns_empty(self):
        mgr = E2EEManager()
        assert mgr.get_key_b64() == ""

    def test_disabled_append_to_url_unchanged(self):
        mgr = E2EEManager()
        url = "http://localhost:8080"
        assert mgr.append_to_url(url) == url

    def test_is_encrypted_detects_envelope(self):
        mgr = E2EEManager()
        assert mgr.is_encrypted({"type": "encrypted", "data": "abc"}) is True
        assert mgr.is_encrypted({"type": "preview", "text": "abc"}) is False


class TestE2EERebindKey:
    """测试密钥重新生成（切换场景）。"""

    def test_reenable_generates_new_key(self):
        mgr = E2EEManager()
        mgr.enable()
        key1 = mgr.get_key_b64()
        mgr.disable()
        mgr.enable()
        key2 = mgr.get_key_b64()
        assert key1 != key2

    def test_old_key_cannot_decrypt_after_reenable(self):
        mgr = E2EEManager()
        mgr.enable()
        old_encrypted = mgr.wrap({"type": "send", "text": "old message"})
        old_key_b64 = mgr.get_key_b64()

        # 重新生成密钥
        mgr.enable()
        # 用旧密钥加密的消息应该解不了
        with pytest.raises(Exception):
            mgr.unwrap(old_encrypted)
