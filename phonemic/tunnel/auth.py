"""
认证管理模块。
提供配对码生成/验证和客户端令牌管理，用于 Cloudflare 隧道模式下的设备授权。
"""

import random
import string
import time
from typing import Optional

_PAIRING_CODE_TTL = 60  # 秒
_TOKEN_LENGTH = 32


class PairingCodeManager:
    """
    配对码管理器。
    生成 4 位数字配对码，60 秒过期，同一时间只有一个有效码。
    """

    def __init__(self, ttl: int = _PAIRING_CODE_TTL):
        self._ttl = ttl
        self._code: Optional[str] = None
        self._generated_at: float = 0

    def generate(self) -> str:
        """生成新的配对码，旧码立即失效。"""
        self._code = f"{random.randint(0, 9999):04d}"
        self._generated_at = time.time()
        return self._code

    def validate(self, code: str) -> bool:
        """验证配对码是否有效（未过期且匹配）。"""
        if self._code is None:
            return False
        if time.time() - self._generated_at > self._ttl:
            return False
        return code == self._code

    def current(self) -> Optional[str]:
        """返回当前有效配对码，已过期则返回 None。"""
        if self._code is None:
            return None
        if time.time() - self._generated_at > self._ttl:
            return None
        return self._code

    def is_active(self) -> bool:
        """是否存在有效的配对码。"""
        return self.current() is not None


class TokenManager:
    """
    客户端令牌管理器。
    内存中只保存一个令牌，新配对码生成新令牌后旧令牌立即失效。
    不持久化，程序重启后需重新配对。
    """

    def __init__(self):
        self._token: Optional[str] = None

    def generate_token(self) -> str:
        """生成新令牌并替换旧令牌，旧令牌立即失效。"""
        chars = string.ascii_lowercase + string.digits
        self._token = ''.join(random.choices(chars, k=_TOKEN_LENGTH))
        return self._token

    def validate(self, token: str) -> bool:
        """验证令牌是否匹配当前令牌。"""
        return self._token is not None and token == self._token
