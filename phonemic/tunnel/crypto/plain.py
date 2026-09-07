"""不加密提供者：明文传输，用于信任网络下的最低开销场景。"""

from phonemic.tunnel.crypto.base import CryptoProvider


class PlainProvider(CryptoProvider):
    """不加密提供者。

    所有数据原样传输，不进行任何加密或认证（含 seq）。
    仅适用于完全信任的局域网环境。
    """

    def __init__(self, session_key: bytes = None):
        # 明文模式无会话密钥，参数仅为了保持与其他 Provider 的构造签名一致
        self._session_key = session_key

    @staticmethod
    def algorithm_name() -> str:
        return "none"

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext

    def reset(self) -> None:
        return None
