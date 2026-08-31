"""加密提供者包。

通过 CryptoProvider 接口抽象不同的加密算法，
实现算法的可插拔切换。
"""

from phonemic.tunnel.crypto.base import CryptoProvider
from phonemic.tunnel.crypto.nacl_box import NaClBoxProvider
from phonemic.tunnel.crypto.plain import PlainProvider
from phonemic.tunnel.crypto.xchacha20 import XChaCha20Provider

__all__ = [
    "CryptoProvider",
    "PlainProvider",
    "NaClBoxProvider",
    "XChaCha20Provider",
]
