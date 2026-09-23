"""XChaCha20-Poly1305 对称加密提供者。

构造时只接收会话密钥（由 KeyExchange 协商得到，或上传时由 k_body 注入），
不碰密钥交换。

防重放 seq 以 **8 字节大端前缀**焊入明文后再整体加密 —— 与 XSalsa20 完全同构
（docs/http-upload-design.md §5.8「两种算法现在还差在哪」）。此前它走的是 AEAD 的
aad 槽位，两个算法因此分裂成两条实现路径；统一成前缀之后，上传侧对"这次用的哪个
算法"完全无感，调用方也不需要传任何上下文。

⚠️ 前缀路径的一个性质：**seq 只能解密之后才读得到** —— AEAD 的 tag 覆盖的是
「nonce ‖ 密文」，不含明文，所以序号不对时**解密本身会成功**，是随后的前缀
比对才抛 `ReplayError`（与 `nacl_box.py` 完全一致）。
"""

from nacl.secret import Aead

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.errors import DecryptError, ReplayError

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
        body = self._tx_seq.to_bytes(_SEQ_LEN, "big") + plaintext
        ct = self._aead.encrypt(body)
        self._tx_seq += 1
        return bytes(ct)

    def decrypt(self, ciphertext: bytes) -> bytes:
        try:
            body = self._aead.decrypt(ciphertext)
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
