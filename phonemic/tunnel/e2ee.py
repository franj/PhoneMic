"""端到端加密管理器，插件式，可启用/禁用。

E2EE 启用时，所有 WebSocket 消息整体打包加密：
    {"type": "encrypted", "data": "<base64url(nonce + ciphertext)>"}
E2EE 关闭时，消息原样传输，行为与现有逻辑一致。

密钥通过 QR 码 URL fragment 传递（#k=<base64url>），不经过网络。
"""

import base64
import json
import os

from Crypto.Cipher import AES

_KEY_SIZE = 32  # AES-256
_NONCE_SIZE = 12  # 96-bit nonce for GCM
_TAG_SIZE = 16  # 128-bit GCM authentication tag


class E2EEManager:
    """端到端加密管理器"""

    def __init__(self):
        self._key: bytes | None = None
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        """生成新密钥并启用 E2EE"""
        self._key = os.urandom(_KEY_SIZE)
        self._enabled = True

    def disable(self) -> None:
        """禁用 E2EE，清除密钥"""
        self._key = None
        self._enabled = False

    def get_key_b64(self) -> str:
        """返回 base64url 编码的密钥（无 padding）"""
        if not self._enabled:
            return ""
        return base64.urlsafe_b64encode(self._key).decode().rstrip("=")

    def append_to_url(self, url: str) -> str:
        """在 URL 后追加 #k=<key> fragment"""
        if not self._enabled:
            return url
        if not url.endswith("/"):
            url += "/"
        return f"{url}#k={self.get_key_b64()}"

    def wrap(self, message: dict) -> dict:
        """加密整个 JSON 消息，返回信封格式

        Returns:
            {"type": "encrypted", "data": "<base64url(nonce + ciphertext + tag)>"}
        """
        plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
        nonce = os.urandom(_NONCE_SIZE)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        return {
            "type": "encrypted",
            "data": base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("="),
        }

    def unwrap(self, message: dict) -> dict:
        """解密信封格式消息，返回原始 JSON dict

        Args:
            message: {"type": "encrypted", "data": "..."}

        Returns:
            解密后的原始 dict

        Raises:
            ValueError: 消息格式不正确
            Exception: 解密失败（密钥不匹配或数据损坏）
        """
        data = message.get("data")
        if not data:
            raise ValueError("Encrypted message missing 'data' field")
        raw = base64.urlsafe_b64decode(data + "==")
        nonce = raw[:_NONCE_SIZE]
        ct = raw[_NONCE_SIZE:-_TAG_SIZE]
        tag = raw[-_TAG_SIZE:]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ct, tag).decode("utf-8")
        return json.loads(plaintext)

    def is_encrypted(self, message: dict) -> bool:
        """判断消息是否为加密信封格式"""
        return message.get("type") == "encrypted"
