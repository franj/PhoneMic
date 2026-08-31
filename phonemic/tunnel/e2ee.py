"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - 加密模式：PC 公钥通过 QR 码（物理带外）传递给手机，手机用 crypto_box_seal 将自己的公钥密封发送给 PC
  - none+LAN：无认证，明文 JSON，兼容原始协议（不影响 nginx 反代 HTTPS 的用户）
  - none+Cloudflare：token 认证（随机 token 通过 QR 码传递），明文 JSON

状态机：
  needs_auth=True:  S0 (未认证) → 收到 auth → 验证 → S1 (已认证)，S0 超时 10 秒断开
  needs_auth=False: 连接即 S1，直接处理消息

协议格式：
  URL fragment:
    none+LAN:  无
    none+CF:   #k=<token>&a=none
    加密:      #k=<pubkey>&a=<algo>
  auth:      { type:"auth", algo:"<algo>", data:"<b64>" }
  auth_ack:  { type:"auth_ack", status:"OK" } 或 { type:"auth_ack", rejected:true, reason:"..." }
  消息:
    none:  明文 JSON { type:"send", text:"..." }
    加密:  { type:"data", data:"<b64(nonce+ciphertext)>" }
"""

import base64
import json
import os
import time
from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto import create_provider

_AUTH_TIMEOUT = 10  # 秒


class SecureChannel:
    """每个 WebSocket 连接的安全通道。

    根据 algorithm 和 mode 参数决定协议行为：
      - algorithm="none", mode="lan": 无认证，明文 JSON
      - algorithm="none", mode="cloudflare": token 认证，明文 JSON
      - algorithm="xsalsa20"/"xchacha20": 密钥交换认证，加密 data 信封
    """

    def __init__(self, algorithm: str = "none", mode: str = "lan"):
        self._algorithm = algorithm
        self._mode = mode
        self._pc_private: Optional[PrivateKey] = None
        self._token: Optional[str] = None
        self._provider = None

        if algorithm != "none":
            self._pc_private = PrivateKey.generate()
        elif mode == "cloudflare":
            self._token = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")

        self._reset_connection()

    def _reset_connection(self):
        self._provider = None
        self._authenticated = not self.needs_auth
        self._connected_at = time.monotonic()
        self._rejected = False
        self._reject_reason = ""

    # ---- 属性 ----

    @property
    def algorithm(self) -> str:
        return self._algorithm

    @property
    def needs_auth(self) -> bool:
        """是否需要 auth 握手。none+LAN 不需要，其余需要。"""
        return not (self._algorithm == "none" and self._mode == "lan")

    @property
    def is_encrypted(self) -> bool:
        """是否需要 data 信封加密。none 模式不需要。"""
        return self._algorithm != "none"

    # ---- 公钥与 URL ----

    def get_public_key_b64(self) -> Optional[str]:
        """返回用于 URL fragment 的密钥/token（base64url）。

        none+LAN: None（无 fragment）
        none+CF:  随机 token
        加密:     PC 公钥
        """
        if self._algorithm == "none":
            return self._token
        pub = self._pc_private.public_key
        return base64.urlsafe_b64encode(bytes(pub)).decode().rstrip("=")

    def append_to_url(self, url: str) -> str:
        """在 URL 末尾追加加密参数 fragment。"""
        key = self.get_public_key_b64()
        if key is None:
            return url
        if not url.endswith("/"):
            url += "/"
        return f"{url}#k={key}&a={self._algorithm}"

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
        """处理 auth 消息：验证算法和密钥材料/token。

        Args:
            auth_msg: 完整的 auth 消息 dict，包含 algo 和 data 字段。

        Returns:
            True 如果认证成功，False 如果被拒绝。
        """
        algo = auth_msg.get("algo", "xsalsa20")

        if algo != self._algorithm:
            self._rejected = True
            self._reject_reason = f"algorithm '{algo}' not allowed"
            return False

        if self._algorithm == "none":
            if auth_msg.get("data") == self._token:
                self._authenticated = True
                return True
            self._rejected = True
            self._reject_reason = "token mismatch"
            return False

        # 加密模式：创建 Provider，处理密钥交换
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

        if self._algorithm == "none":
            return {"type": "auth_ack", "status": "OK"}

        algo = self._provider.algorithm_name()
        data = self._provider.make_auth_ack_data()
        result = {"type": "auth_ack", "algo": algo}
        if data:
            result["data"] = data
        return result

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> dict:
        """加密并包装消息。none 模式直接返回明文 JSON。"""
        if not self.is_encrypted:
            return message
        plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
        encrypted = self._provider.encrypt(plaintext)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(encrypted).decode().rstrip("="),
        }

    def unwrap(self, envelope: dict) -> Optional[dict]:
        """解密消息。none 模式直接返回明文 JSON。"""
        if not self.is_encrypted:
            return envelope
        try:
            raw = base64.urlsafe_b64decode(envelope["data"] + "==")
            plaintext = self._provider.decrypt(raw)
            return json.loads(plaintext)
        except Exception:
            return None
