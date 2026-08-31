"""CryptoProvider 单元测试。

测试每种加密算法提供者的加解密往返和 auth 握手。
使用 PyNaCl 模拟手机端操作。
"""

import base64
import json

import pytest
from nacl.bindings import crypto_scalarmult
from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.secret import Aead
from nacl.utils import random as random_bytes

from phonemic.tunnel.crypto import (
    CryptoProvider,
    NaClBoxProvider,
    PlainProvider,
    XChaCha20Provider,
)


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


# ---------- PlainProvider ----------

class TestPlainProvider:
    def test_algorithm_name(self):
        assert PlainProvider.algorithm_name() == "none"

    def test_no_public_key(self):
        p = PlainProvider()
        assert p.get_public_key_b64() is None

    def test_auth_no_data(self):
        p = PlainProvider()
        assert p.receive_auth(None) is True

    def test_auth_ack_no_data(self):
        p = PlainProvider()
        assert p.make_auth_ack_data() is None

    def test_encrypt_decrypt_roundtrip(self):
        p = PlainProvider()
        data = b"hello world"
        encrypted = p.encrypt(data)
        assert encrypted == data  # 明文不变
        decrypted = p.decrypt(encrypted)
        assert decrypted == data


# ---------- NaClBoxProvider ----------

class TestNaClBoxProvider:
    def test_algorithm_name(self):
        assert NaClBoxProvider.algorithm_name() == "xsalsa20"

    def test_has_public_key(self):
        p = NaClBoxProvider()
        pub = p.get_public_key_b64()
        assert pub is not None
        assert len(_from_b64(pub)) == 32  # X25519 public key

    def test_auth_handshake(self):
        """模拟手机端 sealed box 认证"""
        pc = NaClBoxProvider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        # 手机端：生成密钥对，用 sealed box 密封公钥
        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        sealed = sb.encrypt(bytes(phone_pub))
        auth_data = _to_b64(sealed)

        assert pc.receive_auth(auth_data) is True

    def test_auth_ack_decryption(self):
        """验证 auth_ack 能被手机端解密"""
        pc = NaClBoxProvider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        auth_data = _to_b64(sb.encrypt(bytes(phone_pub)))

        pc.receive_auth(auth_data)
        ack_b64 = pc.make_auth_ack_data()
        assert ack_b64 is not None

        # 手机端解密 auth_ack
        box = Box(phone_priv, pc_pub)
        raw = _from_b64(ack_b64)
        pt = box.decrypt(raw)
        ack_msg = json.loads(pt)
        assert ack_msg["status"] == "OK"
        assert "ts" in ack_msg

    def test_encrypt_decrypt_roundtrip(self):
        """PC 端加密，模拟手机端解密"""
        pc = NaClBoxProvider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        pc.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        # PC 加密
        plaintext = b'{"type":"preview","text":"hello"}'
        encrypted = pc.encrypt(plaintext)

        # 手机端解密
        box = Box(phone_priv, pc_pub)
        decrypted = box.decrypt(encrypted)
        assert decrypted == plaintext

    def test_phone_to_pc_decryption(self):
        """模拟手机端加密，PC 端解密"""
        pc = NaClBoxProvider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        pc.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        # 手机端加密
        box = Box(phone_priv, pc_pub)
        plaintext = b'{"type":"send","text":"world"}'
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = bytes(box.encrypt(plaintext, nonce))

        # PC 端解密
        decrypted = pc.decrypt(encrypted)
        assert decrypted == plaintext

    def test_invalid_auth_data(self):
        pc = NaClBoxProvider()
        assert pc.receive_auth("invalid_base64_data!!!") is False

    def test_auth_without_data(self):
        pc = NaClBoxProvider()
        assert pc.receive_auth(None) is False


# ---------- XChaCha20Provider ----------

class TestXChaCha20Provider:
    def test_algorithm_name(self):
        assert XChaCha20Provider.algorithm_name() == "xchacha20"

    def test_has_public_key(self):
        p = XChaCha20Provider()
        pub = p.get_public_key_b64()
        assert pub is not None
        assert len(_from_b64(pub)) == 32

    def test_auth_handshake(self):
        """模拟手机端 sealed box 认证 + ECDH"""
        pc = XChaCha20Provider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        # 手机端：生成密钥对，用 sealed box 密封公钥
        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        sealed = sb.encrypt(bytes(phone_pub))
        auth_data = _to_b64(sealed)

        assert pc.receive_auth(auth_data) is True

    def test_auth_ack_decryption(self):
        """验证 auth_ack 能被手机端用 XChaCha20 解密"""
        pc = XChaCha20Provider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        pc.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        ack_b64 = pc.make_auth_ack_data()
        assert ack_b64 is not None

        # 手机端：ECDH 得到共享密钥，用 Aead 解密
        shared = crypto_scalarmult(bytes(phone_priv), bytes(pc_pub))
        aead = Aead(shared)
        raw = _from_b64(ack_b64)
        pt = aead.decrypt(raw)
        ack_msg = json.loads(pt)
        assert ack_msg["status"] == "OK"
        assert "ts" in ack_msg

    def test_encrypt_decrypt_roundtrip(self):
        """PC 端加密，模拟手机端解密"""
        pc = XChaCha20Provider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        pc.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        # PC 加密
        plaintext = b'{"type":"preview","text":"hello"}'
        encrypted = pc.encrypt(plaintext)

        # 手机端解密
        shared = crypto_scalarmult(bytes(phone_priv), bytes(pc_pub))
        aead = Aead(shared)
        decrypted = aead.decrypt(encrypted)
        assert decrypted == plaintext

    def test_phone_to_pc_decryption(self):
        """模拟手机端加密，PC 端解密"""
        pc = XChaCha20Provider()
        pc_pub_b64 = pc.get_public_key_b64()
        pc_pub = PublicKey(_from_b64(pc_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(pc_pub)
        pc.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        # 手机端加密
        shared = crypto_scalarmult(bytes(phone_priv), bytes(pc_pub))
        aead = Aead(shared)
        plaintext = b'{"type":"send","text":"world"}'
        encrypted = bytes(aead.encrypt(plaintext))

        # PC 端解密
        decrypted = pc.decrypt(encrypted)
        assert decrypted == plaintext

    def test_invalid_auth_data(self):
        pc = XChaCha20Provider()
        assert pc.receive_auth("invalid_base64_data!!!") is False

    def test_auth_without_data(self):
        pc = XChaCha20Provider()
        assert pc.receive_auth(None) is False


# ---------- 跨算法交叉测试 ----------

class TestCrossAlgorithm:
    """验证不同算法之间无法互通（密钥不兼容）。"""

    def test_xsalsa20_cannot_decrypt_xchacha20(self):
        """XSalsa20 Provider 无法解密 XChaCha20 的密文"""
        # 设置 XChaCha20 Provider
        xchacha = XChaCha20Provider()
        xchacha_pub_b64 = xchacha.get_public_key_b64()
        xchacha_pub = PublicKey(_from_b64(xchacha_pub_b64))

        phone_priv = PrivateKey.generate()
        phone_pub = phone_priv.public_key
        sb = SealedBox(xchacha_pub)
        xchacha.receive_auth(_to_b64(sb.encrypt(bytes(phone_pub))))

        # 用 XChaCha20 加密
        plaintext = b"secret data"
        xchacha_ct = xchacha.encrypt(plaintext)

        # 设置 NaClBox Provider（不同的密钥对）
        nacl = NaClBoxProvider()
        nacl_pub_b64 = nacl.get_public_key_b64()
        nacl_pub = PublicKey(_from_b64(nacl_pub_b64))

        # 用 NaClBox 手机端（不同密钥对）尝试认证
        phone2_priv = PrivateKey.generate()
        phone2_pub = phone2_priv.public_key
        sb2 = SealedBox(nacl_pub)
        nacl.receive_auth(_to_b64(sb2.encrypt(bytes(phone2_pub))))

        # NaClBox Provider 无法解密 XChaCha20 的密文
        with pytest.raises(Exception):
            nacl.decrypt(xchacha_ct)

    def test_all_providers_implement_interface(self):
        """所有 Provider 都实现了 CryptoProvider 接口"""
        providers = [PlainProvider(), NaClBoxProvider(), XChaCha20Provider()]
        for p in providers:
            assert isinstance(p, CryptoProvider)
            assert isinstance(p.algorithm_name(), str)
            assert hasattr(p, "get_public_key_b64")
            assert hasattr(p, "receive_auth")
            assert hasattr(p, "make_auth_ack_data")
            assert hasattr(p, "encrypt")
            assert hasattr(p, "decrypt")
