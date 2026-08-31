"""不加密提供者：明文传输，用于信任网络下的最低开销场景。"""

from typing import Optional

from phonemic.tunnel.crypto.base import CryptoProvider


class PlainProvider(CryptoProvider):
    """不加密提供者。

    所有数据原样传输，不进行任何加密或认证。
    仅适用于完全信任的局域网环境。
    """

    @staticmethod
    def algorithm_name() -> str:
        return "none"

    def get_public_key_b64(self) -> Optional[str]:
        return None

    def receive_auth(self, auth_data: Optional[str]) -> bool:
        return True

    def make_auth_ack_data(self) -> Optional[str]:
        return None

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, ciphertext: bytes) -> bytes:
        return ciphertext
