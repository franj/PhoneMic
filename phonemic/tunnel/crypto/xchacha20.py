"""XChaCha20-Poly1305 对称加密提供者。

构造时只接收会话密钥（由 KeyExchange 协商得到），不碰密钥交换。
防重放 seq 通过 AEAD 的 aad 承载：解密时 AEAD 先整体校验 aad，明文暴露前
即拒绝重放（seq 不对表现为 MAC 失败，归 DecryptError）。
"""

from nacl.secret import Aead

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.errors import DecryptError

_SEQ_LEN = 8


class XChaCha20Provider(CryptoProvider):
    """XChaCha20-Poly1305 AEAD 提供者（纯对称，会话密钥由外部注入）。"""

    def __init__(self, session_key: bytes):
        self._aead = Aead(session_key)
        self._tx_seq = 0
        self._rx_seq = 0

    @staticmethod
    def algorithm_name() -> str:
        return "xchacha20"

    def encrypt(self, plaintext: bytes) -> bytes:
        aad = self._tx_seq.to_bytes(_SEQ_LEN, "big")
        ct = self._aead.encrypt(plaintext, aad)
        self._tx_seq += 1
        return bytes(ct)

    def decrypt(self, ciphertext: bytes) -> bytes:
        aad = self._rx_seq.to_bytes(_SEQ_LEN, "big")
        try:
            pt = self._aead.decrypt(ciphertext, aad)
        except Exception as e:
            raise DecryptError(f"decrypt failed: {e}") from e
        self._rx_seq += 1
        return bytes(pt)

    def reset(self) -> None:
        self._tx_seq = 0
        self._rx_seq = 0
