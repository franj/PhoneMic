"""加密算法提供者接口。

所有加密算法（包括"不加密"）实现此接口，
SecureChannel 通过此接口与具体算法解耦。
"""

from abc import ABC, abstractmethod
from typing import Optional


class CryptoProvider(ABC):
    """加密算法提供者接口。

    职责：
    - 管理 PC 端密钥对（如需要）
    - 处理手机端 auth 消息，提取密钥材料
    - 生成 auth_ack 数据
    - 加密/解密数据消息

    数据格式约定：
    - encrypt 返回 nonce + ciphertext 拼接的 bytes
    - decrypt 接收同样的 bytes，自行拆分 nonce 和 ciphertext
    - 所有 base64 均为 URL-safe 无 padding
    """

    @staticmethod
    @abstractmethod
    def algorithm_name() -> str:
        """算法标识符，如 'xsalsa20', 'xchacha20', 'none'。"""
        ...

    @abstractmethod
    def get_public_key_b64(self) -> Optional[str]:
        """返回 PC 公钥 base64url，'none' 算法返回 None。"""
        ...

    @abstractmethod
    def receive_auth(self, auth_data: Optional[str]) -> bool:
        """处理手机 auth 消息。

        Args:
            auth_data: auth 消息中 data 字段的 base64url 字符串。
                       'none' 算法为 None。

        Returns:
            True 如果认证成功。
        """
        ...

    @abstractmethod
    def make_auth_ack_data(self) -> Optional[str]:
        """生成 auth_ack 的 data 字段。

        Returns:
            base64url 编码的加密确认数据，'none' 算法返回 None。
        """
        ...

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """加密明文，返回 nonce + ciphertext 拼接的 bytes。"""
        ...

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes:
        """解密 nonce + ciphertext 拼接的 bytes，返回明文。"""
        ...
