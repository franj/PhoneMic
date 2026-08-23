"""
认证管理模块。
提供配对码生成/验证和客户端令牌管理，用于 Cloudflare 隧道模式下的设备授权。
"""

import json
import random
import string
import time
from pathlib import Path
from typing import Optional

from phonemic.utils.paths import get_config_dir

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
    管理已授权设备的令牌白名单，持久化到 JSON 文件。
    """

    def __init__(self, storage_path: Optional[Path] = None):
        if storage_path is None:
            storage_path = get_config_dir() / "tunnel_tokens.json"
        self._path = storage_path
        self._tokens: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._tokens = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._tokens = {}

    def _save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._tokens, f, indent=2, ensure_ascii=False)

    def generate_token(self) -> str:
        """生成 32 字符随机令牌（小写字母+数字）。"""
        chars = string.ascii_lowercase + string.digits
        return ''.join(random.choices(chars, k=_TOKEN_LENGTH))

    def add(self, token: str, name: str = "") -> None:
        """添加令牌到白名单。"""
        self._tokens[token] = {
            "name": name,
            "created_at": time.time(),
        }
        self._save()

    def validate(self, token: str) -> bool:
        """验证令牌是否在白名单中。"""
        return token in self._tokens

    def revoke(self, token: str) -> bool:
        """吊销令牌，返回是否成功。"""
        if token in self._tokens:
            del self._tokens[token]
            self._save()
            return True
        return False

    def list_all(self) -> dict:
        """返回所有令牌的副本。"""
        return self._tokens.copy()

    def count(self) -> int:
        """返回已授权令牌数量。"""
        return len(self._tokens)
