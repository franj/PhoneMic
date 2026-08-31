"""安全通道：基于 NaCl crypto_box 的双向认证加密。

信任模型：
  - PC 公钥通过 QR 码（物理带外）传递给手机，不经过网络
  - 手机用 crypto_box_seal 将自己的公钥密封发送给 PC
  - 此后双向通信均使用 crypto_box 加密

状态机：
  S0 (未认证) → 收到 auth → 解密成功 → S1 (已认证)
  S0 状态下 10 秒未完成认证 → 超时断开
"""

import base64
import json
import time

from nacl.public import Box, PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

_AUTH_TIMEOUT = 10  # 秒


class SecureChannel:
    """每个 WebSocket 连接的安全通道。

    PC 密钥对在启动时生成一次（持久），连接状态按连接重置。
    """

    def __init__(self):
        self._pc_private = PrivateKey.generate()
        self._pc_public = self._pc_private.public_key
        self._reset_connection()

    def _reset_connection(self):
        self._phone_public: PublicKey | None = None
        self._box: Box | None = None
        self._authenticated = False
        self._connected_at = time.monotonic()

    # ---- 公钥与 QR 码 ----

    def get_public_key_b64(self) -> str:
        return base64.urlsafe_b64encode(bytes(self._pc_public)).decode().rstrip("=")

    def append_to_url(self, url: str) -> str:
        if not url.endswith("/"):
            url += "/"
        return f"{url}#k={self.get_public_key_b64()}"

    # ---- 连接生命周期 ----

    def on_new_connection(self) -> None:
        self._reset_connection()

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def auth_timed_out(self) -> bool:
        return not self._authenticated and (time.monotonic() - self._connected_at) > _AUTH_TIMEOUT

    # ---- 握手 ----

    def receive_auth(self, sealed_b64: str) -> bool:
        """处理 auth 消息：解密封装盒，提取手机公钥。"""
        try:
            sealed = base64.urlsafe_b64decode(sealed_b64 + "==")
            sb = SealedBox(self._pc_private)
            phone_pub_bytes = sb.decrypt(sealed)
            self._phone_public = PublicKey(phone_pub_bytes)
            self._box = Box(self._pc_private, self._phone_public)
            self._authenticated = True
            return True
        except Exception:
            return False

    def make_auth_ack(self) -> dict:
        """生成 auth_ack：用 Box 加密确认消息。"""
        payload = json.dumps(
            {"status": "OK", "ts": int(time.time() * 1000)},
            ensure_ascii=False,
        ).encode("utf-8")
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(payload, nonce)
        return {
            "type": "auth_ack",
            "data": base64.urlsafe_b64encode(bytes(encrypted)).decode().rstrip("="),
        }

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> dict:
        plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
        nonce = random_bytes(Box.NONCE_SIZE)
        encrypted = self._box.encrypt(plaintext, nonce)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(bytes(encrypted)).decode().rstrip("="),
        }

    def unwrap(self, envelope: dict) -> dict | None:
        try:
            raw = base64.urlsafe_b64decode(envelope["data"] + "==")
            plaintext = self._box.decrypt(raw)
            return json.loads(plaintext)
        except Exception:
            return None
