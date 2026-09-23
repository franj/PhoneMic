"""加密算法提供者接口。

所有加密算法实现此接口（加密永远开启，不存在"不加密"的提供者）。
接口只负责"对称 AEAD 加解密 + 防重放"，不碰密钥交换（见 KeyExchange）——
这样加解密与具体算法、握手流程彻底解耦。
"""

from abc import ABC, abstractmethod


class CryptoProvider(ABC):
    """对称加密提供者接口（纯 AEAD 封装）。

    职责：
    - 用会话密钥加密 / 解密数据消息
    - 在加密层承载防重放 seq（两种算法统一为"明文前 8 字节"，见 decrypt）
    - 解密失败时以异常表达状态（调用方据此映射 error.code）

    ⚠️ **同一实例不允许被两条流交叉使用**：seq 是**单个**计数器，只在一条有序流上
    成立（"第几条"参与 tag 计算，双方必须一致才解得开）。两条独立的 TCP 通道
    （WS 与 HTTP 上传）到达顺序不由发送方决定，交叉使用必然出现"必有一条解不开"
    的概率性失败。上传因此用**以 k_body 单独创建的另一实例**，见
    docs/http-upload-design.md §5.8。

    数据格式约定：
    - encrypt 返回 nonce + ciphertext 拼接的 bytes（seq 已焊入密文）
    - decrypt 接收同样的 bytes，自行拆分 nonce 和 ciphertext，并校验 seq
    - 所有 base64 均为 URL-safe 无 padding（由上层信封负责）
    """

    @staticmethod
    @abstractmethod
    def algorithm_name() -> str:
        """算法标识符，如 'xsalsa20', 'xchacha20'。"""
        ...

    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """加密明文，返回 nonce + ciphertext 拼接的 bytes（seq 已内置）。"""
        ...

    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes:
        """解密 nonce + ciphertext 拼接的 bytes，返回明文。

        seq 是明文前 8 字节（见 encrypt）。AEAD 的 tag 只覆盖「nonce ‖ 密文」，
        所以两步是分开的：先解密（校验密钥与完整性），**成功之后**才从前 8 字节
        读回 seq 与 ``_rx_seq`` 比对 —— 序号不对 ⇒ `ReplayError`，不是 MAC 失败。
        两种算法（`nacl_box.py` / `xchacha20.py`）在这点上完全一致。

        Raises:
            DecryptError: MAC 校验失败（密钥错 / 密文或 nonce 被篡改）。
            ReplayError: 前缀里的 seq 不等于 ``_rx_seq``（重放 / 乱序 / 跳号）。
        """
        ...

    def reset(self) -> None:
        """重置会话内 seq 计数器（rekey 时调用）。默认实现为空操作。

        派生类（对称 AEAD 提供者）应在此把内部 ``_tx_seq`` / ``_rx_seq`` 归零。
        """
        return None
