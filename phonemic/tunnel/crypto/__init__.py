"""加密提供者包。

通过 CryptoProvider 接口抽象不同的加密算法，实现算法的可插拔切换。
密钥交换（算法无关）由 KeyExchange 负责，详见 key_exchange 模块。
加密永远开启，不存在明文模式。
"""

from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.key_exchange import KeyExchange
from phonemic.tunnel.crypto.mac import (
    MAC_SIZE,
    decode_mac,
    encode_mac,
    keyed_blake2b,
    upload_mac,
)
from phonemic.tunnel.crypto.nacl_box import NaClBoxProvider
from phonemic.tunnel.crypto.xchacha20 import XChaCha20Provider

_PROVIDER_CLASSES = {
    "xsalsa20": NaClBoxProvider,
    "xchacha20": XChaCha20Provider,
}

# 加密算法优先级列表（高 → 低）。
# 通过 URL a= 参数整体下发给客户端，客户端按序挑选自身支持的算法。
# 新增算法（如 aes-256-gcm、aegis256）时在此追加；KeyExchange 不感知具体算法，
# 只校验 algo 是否在本列表内。
OFFERED_ALGORITHMS = ["xchacha20", "xsalsa20"]


def create_provider(algo: str, session_key: bytes) -> CryptoProvider:
    """根据算法名与会话密钥创建 Provider 实例。

    Args:
        algo: 算法标识符（'xsalsa20', 'xchacha20'）
        session_key: 32 字节会话密钥（由 KeyExchange 协商得到）。

    Returns:
        CryptoProvider 实例

    Raises:
        ValueError: 未知算法名
    """
    cls = _PROVIDER_CLASSES.get(algo)
    if cls is None:
        raise ValueError(f"Unknown algorithm: {algo}")
    return cls(session_key)


def get_available_algorithms() -> list:
    """返回所有已注册的算法名。"""
    return list(_PROVIDER_CLASSES.keys())


__all__ = [
    "CryptoProvider",
    "KeyExchange",
    "MAC_SIZE",
    "NaClBoxProvider",
    "XChaCha20Provider",
    "create_provider",
    "decode_mac",
    "encode_mac",
    "get_available_algorithms",
    "keyed_blake2b",
    "upload_mac",
    "OFFERED_ALGORITHMS",
]
