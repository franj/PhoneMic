"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - PC 公钥通过 QR 码（物理带外）传递给手机，不经过网络
  - 手机用 crypto_box_seal 将自己的公钥密封发送给 PC
  - 此后双向通信使用协商选定的加密算法

状态机：
  S0 (未认证) → 收到 auth → 协商算法 → 解密成功 → S1 (已认证)
  S0 状态下 10 秒未完成认证 → 超时断开

协议格式：
  URL fragment: #k=<pc_pubkey>&a=xsalsa20,xchacha20&p=xsalsa20
  auth:   { type:"auth", algo:"xsalsa20", data:"<sealed_pubkey_b64>" }
  auth_ack: { type:"auth_ack", algo:"xsalsa20", data:"<encrypted_ack_b64>" }
  data:   { type:"data", data:"<b64(nonce+ciphertext)>" }
"""

import base64
import json
import time
from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto import CryptoProvider, create_provider, get_available_algorithms

_AUTH_TIMEOUT = 10  # 秒

# PC 端允许的算法白名单（策略控制）
# 'none' 不在默认白名单中——LAN 下仍要求最低限度加密
_ALLOWED_ALGORITHMS = {"xsalsa20", "xchacha20"}

# PC 端优先算法（用于 URL fragment 中的 p= 字段）
_PREFERRED_ALGORITHM = "xsalsa20"


class SecureChannel:
    """每个 WebSocket 连接的安全通道。

    PC 密钥对在启动时生成一次（持久），连接状态按连接重置。
    """

    def __init__(self):
        self._pc_private = PrivateKey.generate()
        self._provider: Optional[CryptoProvider] = None
        self._reset_connection()

    def _reset_connection(self):
        self._provider = None
        self._authenticated = False
        self._connected_at = time.monotonic()
        self._rejected = False
        self._reject_reason = ""

    # ---- 公钥与 URL ----

    def get_public_key_b64(self) -> Optional[str]:
        """返回 PC 公钥 base64url，用于 URL fragment。"""
        if _PREFERRED_ALGORITHM == "none":
            return None
        pub = self._pc_private.public_key
        return base64.urlsafe_b64encode(bytes(pub)).decode().rstrip("=")

    def append_to_url(self, url: str) -> str:
        """在 URL 末尾追加加密参数 fragment。"""
        pubkey = self.get_public_key_b64()
        algos = ",".join(_ALLOWED_ALGORITHMS)
        params = f"a={algos}&p={_PREFERRED_ALGORITHM}"
        if pubkey:
            params = f"k={pubkey}&{params}"
        if not url.endswith("/"):
            url += "/"
        return f"{url}#{params}"

    # ---- 连接生命周期 ----

    def on_new_connection(self) -> None:
        self._reset_connection()

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def auth_timed_out(self) -> bool:
        return not self._authenticated and (time.monotonic() - self._connected_at) > _AUTH_TIMEOUT

    @property
    def is_rejected(self) -> bool:
        return self._rejected

    @property
    def reject_reason(self) -> str:
        return self._reject_reason

    # ---- 握手 ----

    def receive_auth(self, auth_msg: dict) -> bool:
        """处理 auth 消息：协商算法，提取密钥材料。

        Args:
            auth_msg: 完整的 auth 消息 dict，包含 algo 和 data 字段。

        Returns:
            True 如果认证成功，False 如果被拒绝。
        """
        algo = auth_msg.get("algo", "xsalsa20")  # 向后兼容：无 algo 字段时默认 xsalsa20

        if algo not in _ALLOWED_ALGORITHMS:
            self._rejected = True
            self._reject_reason = f"algorithm '{algo}' not allowed"
            return False

        # 创建对应算法的 Provider（共享 PC 密钥对）
        self._provider = create_provider(algo, self._pc_private)

        auth_data = auth_msg.get("data")
        if not self._provider.receive_auth(auth_data):
            self._rejected = True
            self._reject_reason = "auth data processing failed"
            return False

        self._authenticated = True
        return True

    def make_auth_ack(self) -> dict:
        """生成 auth_ack 消息。"""
        if self._rejected:
            return {
                "type": "auth_ack",
                "rejected": True,
                "reason": self._reject_reason,
            }

        algo = self._provider.algorithm_name() if self._provider else _PREFERRED_ALGORITHM
        data = self._provider.make_auth_ack_data() if self._provider else None
        result = {"type": "auth_ack", "algo": algo}
        if data:
            result["data"] = data
        return result

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> dict:
        plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
        encrypted = self._provider.encrypt(plaintext)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(encrypted).decode().rstrip("="),
        }

    def unwrap(self, envelope: dict) -> Optional[dict]:
        try:
            raw = base64.urlsafe_b64decode(envelope["data"] + "==")
            plaintext = self._provider.decrypt(raw)
            return json.loads(plaintext)
        except Exception:
            return None
