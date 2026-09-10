"""加密算法提供者接口。

所有加密算法（包括"不加密"）实现此接口。
接口只负责"对称 AEAD 加解密 + 防重放"，不碰密钥交换（见 KeyExchange）——
这样加解密与具体算法、握手流程彻底解耦。
"""

from abc import ABC, abstractmethod
from typing import Optional


class CryptoProvider(ABC):
    """对称加密提供者接口（纯 AEAD 封装）。

    职责：
    - 用会话密钥加密 / 解密数据消息
    - 在加密层承载防重放 seq（AAD 优先 / 8 字节前缀兜底）
    - 解密失败时以异常表达状态（调用方据此映射 error.code）

    数据格式约定：
    - encrypt 返回 nonce + ciphertext 拼接的 bytes（seq 已焊入密文）
    - decrypt 接收同样的 bytes，自行拆分 nonce 和 ciphertext，并校验 seq
    - 所有 base64 均为 URL-safe 无 padding（由上层信封负责）
    """

    @staticmethod
    @abstractmethod
    def algorithm_name() -> str:
        """算法标识符，如 'xsalsa20', 'xchacha20', 'none'。"""
        ...

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """加密明文，返回 nonce + ciphertext 拼接的 bytes（seq 已内置）。"""
        ...

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes:
        """解密 nonce + ciphertext 拼接的 bytes，返回明文。

        Raises:
            DecryptError: MAC 校验失败（密钥错 / 密文被篡改）。
                          AAD 路径（XChaCha20 / AES-GCM）下 seq 不递增
                          也表现为 MAC 失败，一并归此类。
            ReplayError: seq 不递增（仅前缀路径 XSalsa20 能明确区分；
                         外部按统一策略处理："重放当解密失败"）。
        """
        ...

    def reset(self) -> None:
        """重置会话内 seq 计数器（rekey 时调用）。默认实现为空操作。

        派生类（对称 AEAD 提供者）应在此把内部 ``_tx_seq`` / ``_rx_seq`` 归零。
        """
        return None

