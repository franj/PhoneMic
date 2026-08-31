"""XChaCha20-Poly1305 加密提供者。

使用 X25519 密钥对进行 ECDH 密钥交换，
然后用共享密钥通过 XChaCha20-Poly1305 (AEAD) 进行认证加密。
"""

import base64
import json
import time
from typing import Optional

from nacl.bindings import crypto_scalarmult
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import Aead
from nacl.utils import random as random_bytes

from phonemic.tunnel.crypto.base import CryptoProvider


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


class XChaCha20Provider(CryptoProvider):
    """XChaCha20-Poly1305 AEAD 提供者。

    密钥交换流程：
    1. PC 生成 X25519 密钥对，公钥通过 QR 码带外传输
    2. 手机生成 X25519 密钥对，用 sealed box 密封公钥发送给 PC
    3. 双方通过 crypto_scalarmult 计算 ECDH 共享密钥
    4. 使用共享密钥通过 XChaCha20-Poly1305 加密通信
    """

    def __init__(self, pc_private: Optional[PrivateKey] = None):
        self._pc_private = pc_private or PrivateKey.generate()
        self._pc_public = self._pc_private.public_key
        self._aead: Optional[Aead] = None

    @staticmethod
    def algorithm_name() -> str:
        return "xchacha20"

    def get_public_key_b64(self) -> Optional[str]:
        return _to_b64(bytes(self._pc_public))

    def receive_auth(self, auth_data: Optional[str]) -> bool:
        if not auth_data:
            return False
        try:
            sealed = _from_b64(auth_data)
            sb = SealedBox(self._pc_private)
            phone_pub_bytes = sb.decrypt(sealed)
            phone_public = PublicKey(phone_pub_bytes)
            shared = crypto_scalarmult(bytes(self._pc_private), bytes(phone_public))
            self._aead = Aead(shared)
            return True
        except Exception:
            return False

    def make_auth_ack_data(self) -> Optional[str]:
        payload = json.dumps(
            {"status": "OK", "ts": int(time.time() * 1000)},
            ensure_ascii=False,
        ).encode("utf-8")
        em = self._aead.encrypt(payload)
        return _to_b64(bytes(em))

    def encrypt(self, plaintext: bytes) -> bytes:
        em = self._aead.encrypt(plaintext)
        return bytes(em)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._aead.decrypt(ciphertext)
