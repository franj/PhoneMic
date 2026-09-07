"""XSalsa20-Poly1305 对称加密提供者（crypto_secretbox ≡ crypto_box_afternm）。

Box 的 afternm 原语等价于 SecretBox：密钥（KeyExchange 派生的会话密钥）直接
作为对称密钥使用，不再做二次 ECDH。SecretBox 不支持 aad，故 seq 以 8 字节
大端前缀焊入明文后再整体加密；解密后先读前缀校验（在解析应用层数据之前
完成校验），不匹配即抛 ReplayError。
"""

from nacl.secret import SecretBox

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.errors import DecryptError, ReplayError

_SEQ_LEN = 8


class NaClBoxProvider(CryptoProvider):
    """XSalsa20-Poly1305 提供者（crypto_secretbox，会话密钥由外部注入）。"""

    def __init__(self, session_key: bytes):
        self._box = SecretBox(session_key)
        self._tx_seq = 0
        self._rx_seq = 0

    @staticmethod
    def algorithm_name() -> str:
        return "xsalsa20"

    def encrypt(self, plaintext: bytes) -> bytes:
        body = self._tx_seq.to_bytes(_SEQ_LEN, "big") + plaintext
        ct = self._box.encrypt(body)
        self._tx_seq += 1
        return bytes(ct)

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            body = self._box.decrypt(ciphertext)
        except Exception as e:
            raise DecryptError(f"decrypt failed: {e}") from e
        seq = int.from_bytes(body[:_SEQ_LEN], "big")
        if seq != self._rx_seq:
            raise ReplayError(f"seq {seq} != expected {self._rx_seq}")
        self._rx_seq += 1
        return body[_SEQ_LEN:]

    def reset(self) -> None:
        self._tx_seq = 0
        self._rx_seq = 0
