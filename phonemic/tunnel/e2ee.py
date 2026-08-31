"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - 加密模式：PC 公钥通过 QR 码（物理带外）传递给手机，手机用 crypto_box_seal 将自己的公钥密封发送给 PC
  - none+LAN：无认证，明文 JSON，兼容原始协议（不影响 nginx 反代 HTTPS 的用户）
  - none+Cloudflare：token 认证（随机 token 通过 QR 码传递），明文 JSON

连接模型：
  SecureChannel 持有跨连接的长期配置（算法、模式、PC 密钥对或 token），
  每个 WebSocket 连接通过 new_session() 取得一个独立的 SecureSession，
  握手状态与会话密钥按连接隔离，互不干扰。

  只有握手成功的连接才会抢占当前活动连接；仍在握手中或握手失败的连接
  不影响已有连接，避免新连接把活动连接降级为明文。

状态机（每个 session 独立）：
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
           明文中携带递增 seq 字段，接收方校验单调递增以防重放
"""

import base64
import hmac
import json
import os
import time
from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto import create_provider

_AUTH_TIMEOUT = 10  # 秒


class SecureSession:
    """单个 WebSocket 连接的握手状态与加解密上下文。

    每个连接持有一个实例。手机端每次握手生成临时密钥对，
    因此会话密钥必须按连接隔离，不能挂在共享对象上。
    """

    def __init__(self, channel: "SecureChannel"):
        self._channel = channel
        self._provider = None
        self._authenticated = not channel.needs_auth
        self._connected_at = time.monotonic()
        self._rejected = False
        self._reject_reason = ""
        # 防重放：每方向独立递增的序列号，初始 -1 表示尚未收到任何消息
        self._send_seq = 0
        self._recv_seq = -1

    # ---- 属性 ----

    @property
    def needs_auth(self) -> bool:
        """是否需要 auth 握手。none+LAN 不需要，其余需要。"""
        return self._channel.needs_auth

    @property
    def is_encrypted(self) -> bool:
        """是否需要 data 信封加密。none 模式不需要。"""
        return self._channel.is_encrypted

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
        algorithm = self._channel.algorithm
        algo = auth_msg.get("algo", "xsalsa20")

        if algo != algorithm:
            self._rejected = True
            self._reject_reason = f"algorithm '{algo}' not allowed"
            return False

        if algorithm == "none":
            received = auth_msg.get("data") or ""
            expected = self._channel.token or ""
            # 常数时间比较，避免逐字节短路造成的时序侧信道
            if hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
                self._authenticated = True
                return True
            self._rejected = True
            self._reject_reason = "token mismatch"
            return False

        # 加密模式：创建 Provider，处理密钥交换
        self._provider = create_provider(algo, self._channel.pc_private)
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

        if not self.is_encrypted:
            return {"type": "auth_ack", "status": "OK"}

        algo = self._provider.algorithm_name()
        data = self._provider.make_auth_ack_data()
        result = {"type": "auth_ack", "algo": algo}
        if data:
            result["data"] = data
        return result

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> dict:
        """加密并包装消息。none 模式直接返回明文 JSON。

        加密模式在明文中注入递增 seq，供对端做防重放校验。
        """
        if not self.is_encrypted:
            return message
        payload = dict(message)
        payload["seq"] = self._send_seq
        self._send_seq += 1
        plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        encrypted = self._provider.encrypt(plaintext)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(encrypted).decode().rstrip("="),
        }

    def unwrap(self, envelope: dict) -> Optional[dict]:
        """解密消息。none 模式直接返回明文 JSON。

        加密模式下校验明文中的 seq：必须严格递增，
        重放或乱序的消息返回 None（视为解密失败）。
        seq 是传输层字段，返回前从消息中剥离。
        """
        if not self.is_encrypted:
            return envelope
        try:
            raw = base64.urlsafe_b64decode(envelope["data"] + "==")
            plaintext = self._provider.decrypt(raw)
            inner = json.loads(plaintext)
            seq = inner.pop("seq", None)
            if not isinstance(seq, int) or seq <= self._recv_seq:
                return None
            self._recv_seq = seq
            return inner
        except Exception:
            return None


class SecureChannel:
    """跨连接共享的长期配置与密钥材料。

    持有算法、模式以及 PC 密钥对（加密模式）或 token（none+Cloudflare），
    这些信息在整个进程生命周期内保持稳定，二维码不会因新连接而失效。
    每连接的握手状态与会话密钥由 new_session() 产出的 SecureSession 承载。
    """

    def __init__(self, algorithm: str = "none", mode: str = "lan"):
        self._algorithm = algorithm
        self._mode = mode
        self._pc_private: Optional[PrivateKey] = None
        self._token: Optional[str] = None

        if algorithm != "none":
            self._pc_private = PrivateKey.generate()
        elif mode == "cloudflare":
            self._token = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")

    # ---- 属性 ----

    @property
    def algorithm(self) -> str:
        return self._algorithm

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def pc_private(self) -> Optional[PrivateKey]:
        """PC 私钥，仅加密模式下存在。"""
        return self._pc_private

    @property
    def token(self) -> Optional[str]:
        """Cloudflare 明文模式的认证 token，仅 none+cloudflare 下存在。"""
        return self._token

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

    def new_session(self) -> SecureSession:
        """为该连接创建独立的握手与加解密上下文。"""
        return SecureSession(self)
