"""CryptoProvider / KeyExchange 单元测试。

新架构（与 docs/wire-protocol.md §4.1 一致）：
- KeyExchange：算法无关的密钥交换（SealedBox 解封 -> 读 algo -> ECDH -> KDF）
- CryptoProvider：纯对称 AEAD 封装，构造只收 session_key；
  防重放 seq 在 Provider 内部承载（两种算法统一为「明文前 8 字节」），外部不可见
"""

import base64
import json
from hashlib import blake2b

import pytest
from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.tunnel.crypto import (
    OFFERED_ALGORITHMS,
    KeyExchange,
    NaClBoxProvider,
    XChaCha20Provider,
    create_provider,
)
from phonemic.tunnel.crypto.errors import CryptoError, DecryptError, ReplayError


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


def make_phone_auth_blob(algo: str, pc_public_key, phone_private=None):
    """模拟手机端：密封 {"algo","pk"} JSON，返回 (sealed_bytes, phone_private)。"""
    if phone_private is None:
        phone_private = PrivateKey.generate()
    inner = json.dumps(
        {"algo": algo, "pk": _to_b64(bytes(phone_private.public_key))}
    ).encode("utf-8")
    sealed = SealedBox(pc_public_key).encrypt(inner)
    return sealed, phone_private


# ---------- KeyExchange ----------

class TestKeyExchange:
    def test_handle_auth_succeeds_for_each_algo(self):
        """密封 {"algo","pk"} 后可解出算法名与会话密钥。"""
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        for algo in OFFERED_ALGORITHMS:
            sealed, _ = make_phone_auth_blob(algo, pc_private.public_key)
            got_algo, session_key = kx.handle_auth(sealed)
            assert got_algo == algo
            assert len(session_key) == 32

    def test_session_key_matches_phone_side_derivation(self):
        """会话密钥与手机端独立推导结果一致（ECDH + blake2b）。"""
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        sealed, phone_private = make_phone_auth_blob(
            "xchacha20", pc_private.public_key
        )
        _, session_key = kx.handle_auth(sealed)
        shared = crypto_scalarmult(
            bytes(phone_private), bytes(pc_private.public_key)
        )
        expected = blake2b(shared, digest_size=32).digest()
        assert session_key == expected

    def test_unsupported_algo_rejected(self):
        """密封的 algo 不在下发列表中：拒绝。"""
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        sealed, _ = make_phone_auth_blob("aes-256-gcm", pc_private.public_key)
        with pytest.raises(CryptoError):
            kx.handle_auth(sealed)

    def test_garbage_rejected(self):
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        with pytest.raises(CryptoError):
            kx.handle_auth(b"not a sealed box")
        with pytest.raises(CryptoError):
            kx.handle_auth(b"")

    def test_wrong_pc_key_cannot_decrypt(self):
        """用错误公钥密封的 auth：PC 端解封失败。"""
        pc_private = PrivateKey.generate()
        other_pc = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        sealed, _ = make_phone_auth_blob("xsalsa20", other_pc.public_key)
        with pytest.raises(CryptoError):
            kx.handle_auth(sealed)

    def test_public_key_b64_roundtrip(self):
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        b64 = kx.public_key_b64
        assert "=" not in b64
        assert _from_b64(b64) == bytes(pc_private.public_key)


# ---------- KeyExchange: TOFU 路径 ----------

class TestKeyExchangeTofu:
    """handle_tofu_auth / public_key_bytes 测试。"""

    def test_handle_tofu_auth_succeeds_for_each_algo(self):
        """明文 algo + 手机公钥直接 ECDH，返回正确 algo 和 session_key。"""
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        for algo in OFFERED_ALGORITHMS:
            phone_private = PrivateKey.generate()
            phone_public_bytes = bytes(phone_private.public_key)
            got_algo, session_key = kx.handle_tofu_auth(algo, phone_public_bytes)
            assert got_algo == algo
            assert len(session_key) == 32

    def test_tofu_session_key_matches_phone_side(self):
        """TOFU 路径的 session_key 与手机端独立推导一致。"""
        pc_private = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        _, session_key = kx.handle_tofu_auth("xchacha20", bytes(phone_private.public_key))
        shared = crypto_scalarmult(
            bytes(phone_private), bytes(pc_private.public_key)
        )
        expected = blake2b(shared, digest_size=32).digest()
        assert session_key == expected

    def test_tofu_unsupported_algo_rejected(self):
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        phone_private = PrivateKey.generate()
        with pytest.raises(CryptoError):
            kx.handle_tofu_auth("aes-256-gcm", bytes(phone_private.public_key))

    def test_tofu_invalid_public_key_rejected(self):
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        with pytest.raises(CryptoError):
            kx.handle_tofu_auth("xchacha20", b"not-a-public-key")

    def test_public_key_bytes_matches_public_key(self):
        """public_key_bytes 返回 32B 原始公钥。"""
        pc_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)
        pk_bytes = kx.public_key_bytes
        assert len(pk_bytes) == 32
        assert pk_bytes == bytes(pc_private.public_key)

    def test_tofu_and_sealed_paths_produce_same_key(self):
        """同一对密钥，TOFU 明文路径和 SealedBox 路径推导出相同 session_key。"""
        pc_private = PrivateKey.generate()
        phone_private = PrivateKey.generate()
        kx = KeyExchange(pc_private, OFFERED_ALGORITHMS)

        # TOFU 路径
        _, tofu_key = kx.handle_tofu_auth("xchacha20", bytes(phone_private.public_key))

        # SealedBox 路径
        sealed, _ = make_phone_auth_blob("xchacha20", pc_private.public_key, phone_private)
        _, sealed_key = kx.handle_auth(sealed)

        assert tofu_key == sealed_key


# ---------- 对称 Provider：往返与防篡改 ----------

PROVIDERS = [XChaCha20Provider, NaClBoxProvider]


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.algorithm_name())
class TestProviderRoundtrip:
    def test_algorithm_name(self, cls):
        assert cls.algorithm_name() in OFFERED_ALGORITHMS

    def test_roundtrip(self, cls):
        p = cls(random_bytes(32))
        data = "你好世界 hello".encode("utf-8")
        assert p.decrypt(p.encrypt(data)) == data

    def test_roundtrip_empty(self, cls):
        p = cls(random_bytes(32))
        assert p.decrypt(p.encrypt(b"")) == b""

    def test_random_nonce_produces_different_ciphertexts(self, cls):
        p = cls(random_bytes(32))
        assert p.encrypt(b"same") != p.encrypt(b"same")

    def test_tampered_ciphertext_rejected(self, cls):
        p = cls(random_bytes(32))
        ct = bytearray(p.encrypt(b"hello"))
        ct[-1] ^= 0xFF
        with pytest.raises(DecryptError):
            p.decrypt(bytes(ct))

    def test_wrong_key_rejected(self, cls):
        ct = cls(random_bytes(32)).encrypt(b"hello")
        with pytest.raises(DecryptError):
            cls(random_bytes(32)).decrypt(ct)


# ---------- 对称 Provider：seq 内化（防重放） ----------

@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.algorithm_name())
class TestProviderSeqInternalized:
    def test_replay_rejected(self, cls):
        """同一密文重放：seq 未递增，拒绝。"""
        p = cls(random_bytes(32))
        ct = p.encrypt(b"hello")
        assert p.decrypt(ct) == b"hello"
        with pytest.raises((DecryptError, ReplayError)):
            p.decrypt(ct)

    def test_out_of_order_rejected(self, cls):
        """乱序（先收第 2 帧再收第 1 帧）：第 2 帧拒绝，第 1 帧正常。"""
        p = cls(random_bytes(32))
        c1 = p.encrypt(b"first")
        c2 = p.encrypt(b"second")
        # 期望 seq=0 却收到 seq=1：解密成功、前缀比对失败 ⇒ ReplayError
        with pytest.raises((DecryptError, ReplayError)):
            p.decrypt(c2)
        # 计数器未推进，第 1 帧仍可正常解密
        assert p.decrypt(c1) == b"first"

    def test_seq_not_in_plaintext(self, cls):
        """seq 由加密层承载，不污染应用层明文。"""
        p = cls(random_bytes(32))
        pt = p.decrypt(p.encrypt(b"plain"))
        assert pt == b"plain"  # 明文就是应用层原样字节，无 seq 前缀

    def test_skip_forward_rejected(self, cls):
        """跳号（seq 前进了但中间帧缺失）：拒绝。"""
        p = cls(random_bytes(32))
        c1 = p.encrypt(b"a")
        p.encrypt(b"b")  # c2 丢失
        c3 = p.encrypt(b"c")
        assert p.decrypt(c1) == b"a"
        # 收到 seq=2（期望 1）：解密成功、前缀比对失败 ⇒ ReplayError
        with pytest.raises((DecryptError, ReplayError)):
            p.decrypt(c3)

    def test_reset_allows_resync(self, cls):
        """reset() 归零计数器：seq 从 0 的帧重新可解。"""
        p = cls(random_bytes(32))
        ct = p.encrypt(b"hello")
        assert p.decrypt(ct) == b"hello"
        p.reset()
        # 新会话中同样的 seq=0 帧（不同密钥实例语义下模拟 rekey）
        p2 = cls(random_bytes(32))
        p2._tx_seq = 0
        assert p2.decrypt(p2.encrypt(b"world")) == b"world"
        # reset 后 rx 从 0 重新计数
        assert p._rx_seq == 0

    def test_tx_rx_counters_independent(self, cls):
        """发送计数与接收计数互不干扰。"""
        p = cls(random_bytes(32))
        p.encrypt(b"a")
        p.encrypt(b"b")
        assert p._tx_seq == 2
        assert p._rx_seq == 0  # 未收过任何帧


# ---------- 重放与「解不开」是两类异常（两种算法一致） ----------

@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.algorithm_name())
class TestReplayIsNotFoldedIntoDecryptError:
    def test_replay_is_replay_error_not_decrypt_error(self, cls):
        """seq 焊在明文前 8 字节 ⇒ 解密成功、比对才失败 ⇒ 报 ReplayError。

        09-23 之前 XChaCha20 走 `aad`，重放表现为 MAC 失败因而被折叠进
        DecryptError，所以这条断言必须按算法分开写。两算法统一走前缀后差异
        消失，合成一条参数化用例 —— 顺带守住「别再把重放折回解密失败」。
        """
        p = cls(random_bytes(32))
        ct = p.encrypt(b"hello")
        p.decrypt(ct)
        with pytest.raises(ReplayError) as exc_info:
            p.decrypt(ct)
        assert not isinstance(exc_info.value, DecryptError)


# ---------- create_provider ----------

class TestCreateProvider:
    @pytest.mark.parametrize("algo", OFFERED_ALGORITHMS)
    def test_creates_each_offered_algo(self, algo):
        p = create_provider(algo, random_bytes(32))
        assert p.algorithm_name() == algo

    def test_unknown_algo_raises(self):
        with pytest.raises(ValueError):
            create_provider("aes-256-gcm", random_bytes(32))
