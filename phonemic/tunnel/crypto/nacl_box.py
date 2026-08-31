"""XSalsa20-Poly1305 加密提供者（基于 NaCl crypto_box）。

使用 X25519 密钥对 + XSalsa20-Poly1305 认证加密。
密钥交换：PC 公钥通过 QR 码带外传输，手机公钥通过 sealed box 密封发送。
"""

import base64
import json
import time
from typing import Optional

from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from phonemic.tunnel.crypto.base import CryptoProvider


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _from_b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


class NaClBoxProvider(CryptoProvider):
    """XSalsa20-Poly1305 提供者（crypto_box）。

    PC 端在启动时生成 X25519 密钥对。
    手机端生成自己的 X25519 密钥对，
    用 sealed box（crypto_box_seal）将公钥密封发送给 PC。
    之后双向通信使用 crypto_box 加密。
    """

    def __init__(self):
        self._pc_private = PrivateKey.generate()
        self._pc_public = self._pc_private.public_key
        self._phone_public: Optional[PublicKey] = None
        self._box: Optional[Box] = None

    @staticmethod
    def algorithm_name() -> str:
        return "xsalsa20"

    def get_public_key_b64(self) -> Optional[str]:
        return _to_b64(bytes(self._pc_public))

    def receive_auth(self, auth_data: Optional[str]) -> bool:
        if not auth_data:
            return False
        try:
            sealed = _from_b64(auth_data)
            sb = SealedBox(self._pc_private)
            phone_pub_bytes = sb.decrypt(sealed)
            self._phone_public = PublicKey(phone_pub_bytes)
            self._box = Box(self._pc_private, self._phone_public)
            return True
        except Exception:
            return False

    def make_auth_ack_data(self) -> Optional[str]:
        payload = json.dumps(
            {"status": "OK", "ts": int(time.time() * 1000)},
            ensure_ascii=False,
        ).encode("utf-8")
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(payload, nonce)
        return _to_b64(bytes(encrypted))

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(plaintext, nonce)
        return bytes(encrypted)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._box.decrypt(ciphertext)
