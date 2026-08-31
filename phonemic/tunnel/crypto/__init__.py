"""加密提供者包。

通过 CryptoProvider 接口抽象不同的加密算法，
实现算法的可插拔切换。
"""

from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.nacl_box import NaClBoxProvider
from phonemic.tunnel.crypto.plain import PlainProvider
from phonemic.tunnel.crypto.xchacha20 import XChaCha20Provider

_PROVIDER_CLASSES = {
    "none": PlainProvider,
    "xsalsa20": NaClBoxProvider,
    "xchacha20": XChaCha20Provider,
}


def create_provider(algo: str, pc_private: Optional[PrivateKey] = None) -> CryptoProvider:
    """根据算法名创建 Provider 实例。

    Args:
        algo: 算法标识符（'none', 'xsalsa20', 'xchacha20'）
        pc_private: 可选的 X25519 私钥，用于共享密钥对（加密算法）

    Returns:
        CryptoProvider 实例

    Raises:
        ValueError: 未知算法名
    """
    cls = _PROVIDER_CLASSES.get(algo)
    if cls is None:
        raise ValueError(f"Unknown algorithm: {algo}")
    if cls is PlainProvider:
        return cls()
    return cls(pc_private)


def get_available_algorithms() -> list:
    """返回所有已注册的算法名。"""
    return list(_PROVIDER_CLASSES.keys())


__all__ = [
    "CryptoProvider",
    "PlainProvider",
    "NaClBoxProvider",
    "XChaCha20Provider",
    "create_provider",
    "get_available_algorithms",
]
