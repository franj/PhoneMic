"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - 加密模式：PC 公钥通过 QR 码（物理带外）传递给手机；手机用 SealedBox 把
    ``{"algo", "pk"}`` 密封发送给 PC。PC 端由 KeyExchange 解封、读 algo、做
    ECDH + KDF 得到会话密钥，再据此实例化对应 CryptoProvider。algo 只出现在
    密文内部（非明文），公钥在此部署中等价于带外 token。
  - none+LAN：无认证，明文 JSON，兼容原始协议（不影响 nginx 反代 HTTPS 的用户）
  - none+Cloudflare：token 认证（随机 token 通过 QR 码传递），明文 JSON

连接模型：
  SecureChannel 持有跨连接的长期配置（算法、模式、PC 密钥对或 token）与
  KeyExchange 实例；每个 WebSocket 连接通过 new_session() 取得一个独立的
  SecureSession，握手状态与会话密钥按连接隔离，互不干扰。

  只有握手成功的连接才会抢占当前活动连接；仍在握手中或握手失败的连接
  不影响已有连接，避免新连接把活动连接降级为明文。

状态机（每个 session 独立）：
  needs_auth=True:  S0 (未认证) → 收到 auth → 验证 → S1 (已认证)，S0 超时 10 秒断开
  needs_auth=False: 连接即 S1，直接处理消息

协议格式：
  URL fragment:
    none+LAN:  无
    none+CF:   #k=<token>&a=none
    加密:      #k=<pubkey>&a=<algo1,algo2,...>（服务端支持的算法，按优先级排序）
  auth（加密模式）:
    {"type":"auth", "data":"<b64( SealedBox( {"algo":..., "pk":<手机公钥 b64>} ) )>"}
    仅 type 为明文；algo 与手机公钥均在密封 blob 内。解密失败即认证失败。
  auth（none+CF）:
    {"type":"auth", "algo":"none", "data":"<token>"}
  auth_ack:
    {"type":"auth_ack", "rejected":true, "reason":"..."}（失败，明文）
    或 {"type":"auth_ack", "algo":..., "data":"<b64( 对称加密帧 )>"}（成功，整帧加密）
  消息（加密模式）:
    {"type":"data", "data":"<b64( nonce || AEAD(明文) )>"}
    防重放 seq 由 CryptoProvider 在加密层承载（AAD 优先 / 8 字节前缀兜底），
    不进应用层 JSON，调用方不可见。
"""

import base64
import hmac
import json
import os
import secrets
import time
from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS, create_provider
from phonemic.tunnel.crypto import KeyExchange
from phonemic.tunnel.crypto.errors import CryptoError

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

    @property
    def negotiated_algorithm(self) -> str:
        """本次握手实际协商出的算法（客户端从 a= 列表中回传的选择）。

        未握手或 none 模式返回 "none"，加密模式返回 provider 的算法名。
        """
        if self._provider is None:
            return "none"
        return self._provider.algorithm_name()

    # ---- 握手 ----

    def receive_auth(self, auth_msg: dict) -> bool:
        """处理 auth 消息：验证算法和密钥材料/token。

        Args:
            auth_msg: 完整的 auth 消息 dict。

        Returns:
            True 如果认证成功，False 如果被拒绝。
        """
        if not self.is_encrypted:
            # none 模式：token 认证（明文 JSON，依赖 WSS 保护）
            if auth_msg.get("algo") != "none":
                self._rejected = True
                self._reject_reason = "algorithm not allowed in plaintext mode"
                return False
            received = auth_msg.get("data") or ""
            expected = self._channel.token or ""
            # 常数时间比较，避免逐字节短路造成的时序侧信道
            if hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8")):
                self._authenticated = True
                return True
            self._rejected = True
            self._reject_reason = "token mismatch"
            return False

        # 加密模式：先解 auth 拿 algo + 会话密钥，再实例化 Provider
        data = auth_msg.get("data")
        if not data:
            self._rejected = True
            self._reject_reason = "missing auth data"
            return False
        try:
            sealed = base64.urlsafe_b64decode(data + "==")
        except Exception:
            # base64 编码非法（长度/字符集错误），与解封失败同等对待
            self._rejected = True
            self._reject_reason = "invalid auth data encoding"
            return False
        try:
            algo, session_key = self._channel.key_exchange.handle_auth(sealed)
        except CryptoError as e:
            self._rejected = True
            self._reject_reason = str(e) or "auth data processing failed"
            return False
        self._provider = create_provider(algo, session_key)
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

        # 加密模式：用刚协商出的会话密钥加密整个应答帧
        payload = json.dumps(
            {"status": "OK", "ts": int(time.time() * 1000)},
            ensure_ascii=False,
        ).encode("utf-8")
        data = base64.urlsafe_b64encode(self._provider.encrypt(payload)).decode().rstrip("=")
        # 成功帧不带 algo：手机能解开这一帧即证明会话密钥正确，
        # 且 algo 在 auth.data 密文内早已协商过（不明文回显）
        return {"type": "auth_ack", "data": data}

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> dict:
        """加密并包装消息。none 模式直接返回明文 JSON。

        防重放 seq 由 CryptoProvider 在加密层承载，此处不感知、也不注入。
        """
        if not self.is_encrypted:
            return message
        plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
        encrypted = self._provider.encrypt(plaintext)
        return {
            "type": "data",
            "data": base64.urlsafe_b64encode(encrypted).decode().rstrip("="),
        }

    def unwrap(self, envelope: dict) -> Optional[dict]:
        """解密消息。none 模式直接返回明文 JSON。

        解密失败（密钥错 / 篡改 / 重放）由 CryptoProvider 以异常表达，
        SecureSession 统一归为 None（视为不可用帧丢弃）。
        """
        if not self.is_encrypted:
            return envelope
        try:
            raw = base64.urlsafe_b64decode(envelope["data"] + "==")
            plaintext = self._provider.decrypt(raw)
            return json.loads(plaintext)
        except Exception:
            return None


class SecureChannel:
    """跨连接共享的长期配置与密钥材料。

    持有算法、模式以及 PC 密钥对（加密模式，封装在 KeyExchange 内）或 token
    （none+Cloudflare），这些信息在整个进程生命周期内保持稳定，二维码不会因
    新连接而失效。每连接的握手状态与会话密钥由 new_session() 产出的
    SecureSession 承载。
    """

    def __init__(self, algorithm: str = "none", mode: str = "lan"):
        # 归一化：具体算法名（历史配置值 xsalsa20/xchacha20）统一视为开启加密（auto），
        # 实际算法由客户端从 a= 列表中协商决定
        self._algorithm = "none" if algorithm == "none" else "auto"
        self._mode = mode
        self._key_exchange: Optional[KeyExchange] = None
        self._token: Optional[str] = None
        # 随机入口路径：加密模式下为 32 位随机串（防扫描），明文模式为空串（根路由）
        self._secret_path = ""

        if self._algorithm != "none":
            self._key_exchange = KeyExchange(PrivateKey.generate(), OFFERED_ALGORITHMS)
            self._secret_path = secrets.token_urlsafe(24)
        elif mode == "cloudflare":
            self._token = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")

    # ---- 属性 ----

    @property
    def algorithm(self) -> str:
        """当前加密模式："none"（不加密）或 "auto"（加密，算法协商）。"""
        return self._algorithm

    @property
    def offered_algorithms(self) -> list:
        """下发给客户端的算法列表（按优先级排序）。

        不加密模式为 ["none"]（token 认证），
        加密模式为 OFFERED_ALGORITHMS（全部支持的加密算法）。
        """
        if self._algorithm == "none":
            return ["none"]
        return list(OFFERED_ALGORITHMS)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def key_exchange(self) -> Optional[KeyExchange]:
        """加密模式的密钥交换实例（PC 身份私钥持有者）。"""
        return self._key_exchange

    @property
    def pc_private(self) -> Optional[PrivateKey]:
        """PC 私钥，仅加密模式下存在（归 KeyExchange 持有）。"""
        if self._key_exchange is None:
            return None
        return self._key_exchange.private_key

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

    @property
    def secret_path(self) -> str:
        """随机入口路径：加密模式为非空随机串，明文模式为空串（根路由）。

        服务端 dispatcher 据此决定放行规则，URL 拼接时非空则插入 /{secret}/ 前缀。
        """
        return self._secret_path

    # ---- 公钥与 URL ----

    def get_public_key_b64(self) -> Optional[str]:
        """返回用于 URL fragment 的密钥/token（base64url）。

        none+LAN: None（无 fragment）
        none+CF:  随机 token
        加密:     PC 公钥
        """
        if self._algorithm == "none":
            return self._token
        return self._key_exchange.public_key_b64

    def append_to_url(self, url: str) -> str:
        """在 URL 末尾追加加密参数 fragment。

        加密模式下：URL 插入 /{secret_path}/ 随机入口前缀（防扫描），
        a= 为算法优先级列表（逗号分隔），客户端按序挑选自身支持的算法并在 auth 时回传。
        """
        key = self.get_public_key_b64()
        if key is None:
            return url
        if not url.endswith("/"):
            url += "/"
        if self._secret_path:
            url += f"{self._secret_path}/"
        return f"{url}#k={key}&a={','.join(self.offered_algorithms)}"

    # ---- 连接生命周期 ----

    def new_session(self) -> SecureSession:
        """为该连接创建独立的握手与加解密上下文。"""
        return SecureSession(self)
