"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - 加密模式：PC 公钥通过 QR 码（物理带外）传递给手机；手机用 SealedBox 把
    ``{"algo", "pk"}`` 密封发送给 PC。PC 端由 KeyExchange 解封、读 algo、做
    ECDH + KDF 得到会话密钥，再据此实例化对应 CryptoProvider。algo 只出现在
    密文内部（非明文），公钥在此部署中等价于带外 token。
  - none+LAN：无认证，明文 msgpack，兼容原始协议（不影响 nginx 反代 HTTPS 的用户）
  - none+Cloudflare：token 认证（随机 token 通过 QR 码传递），明文 msgpack

连接模型：
  SecureChannel 持有跨连接的长期配置（算法、模式、PC 密钥对或 token）与
  KeyExchange 实例；PC 密钥对在进程内稳定持有、只随 SecureChannel 重建而
  更换（因此二维码不会因新连接失效）。每个 WebSocket 连接通过 new_session()
  取得一个独立的 SecureSession，握手状态按连接隔离，互不干扰。

  「按连接隔离」的边界要说清：手机端在同一页面内复用同一对临时密钥，因此
  同一页面的多次重连会 ECDH 出**相同**的 session_key。跨连接的新鲜度由握手
  nonce（auth_challenge）提供，不由会话密钥提供。

  只有握手成功的连接才会抢占当前活动连接；仍在握手中或握手失败的连接
  不影响已有连接，避免新连接把活动连接降级为明文。

状态机（每个 session 独立）：
  加密模式:  S0 (未认证) --auth--> S1 (密钥就绪) --auth_proof--> S2 (已认证)
  none+CF:   S0 --auth(token)--> S2（无会话密钥，做不了密钥确认，auth 一过即认证）
  none+LAN:  连接即 S2，直接处理消息
  整轮握手受 AUTH_TIMEOUT 约束：两次等待共享同一**绝对截止时刻**，不是各 10 秒。

协议格式（全部帧统一 msgpack 编码，WS 走 binary 帧，密文用 bin 类型、无 base64）：
  URL fragment:
    none+LAN:  无
    none+CF:   #k=<token>&a=none
    加密:      #k=<pubkey>&a=<algo1,algo2,...>（服务端支持的算法，按优先级排序）
  auth（加密模式，唯一明文帧）:
    {"type":"auth", "data":<bin SealedBox( {"algo":..., "pk":<手机公钥 b64>} )>}
    仅 type 为明文；algo 与手机公钥均在密封 blob 内。解封成功只建立会话密钥，
    不等于认证通过——录下来的 auth 帧原样重放同样解得开。
  auth（none+CF）:
    {"type":"auth", "algo":"none", "data":"<token>"}
  auth_challenge（加密）:
    {"type":"auth_challenge", "nonce":<bin 16B>}
    解封成功后立即下发，是本连接现场生成的新鲜值。只要求「每连接不同」，
    不要求「不可预测」：重放者没有会话密钥，连它都读不到。
  auth_proof（加密）:
    {"type":"auth_proof", "nonce":<bin>}
    回显该值。能把它正确加密回来，就是持有会话密钥的证明；握手到此完成，
    不再另发一条「成功」帧（随后的 config 即确认）。
  消息（加密模式）:
    整帧对称加密：WS binary 帧 = AEAD(msgpack(消息))，无外层信封——
    加密状态由会话状态机决定，不靠帧内容判别。
    防重放 seq 由 CryptoProvider 在加密层承载（AAD 优先 / 8 字节前缀兜底），
    不进应用层报文，调用方不可见。
"""

import base64
import hmac
import os
import secrets
import time
from typing import Optional

from nacl.public import PrivateKey

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS, create_provider
from phonemic.tunnel.crypto import KeyExchange
from phonemic.tunnel.crypto.errors import CryptoError
from phonemic.tunnel.frame import decode as decode_frame
from phonemic.tunnel.frame import encode as encode_frame

AUTH_TIMEOUT = 10  # 秒：整轮握手的**绝对预算**，两次等待共享（见 api._handle_auth）
CHALLENGE_NONCE_BYTES = 16  # auth_challenge 的 nonce 长度


class SecureSession:
    """单个 WebSocket 连接的握手状态与加解密上下文。

    每个连接持有一个实例。会话密钥由 ECDH 按连接派生（手机端在同一页面内
    复用同一对临时密钥，故同一页面的多次重连会派生出相同的会话密钥）；
    跨连接的新鲜度由握手 nonce 提供，不依赖会话密钥。
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
        return not self._authenticated and (time.monotonic() - self._connected_at) > AUTH_TIMEOUT

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
        """处理握手第一步 ``auth``：校验 token / 解封密钥材料。

        加密模式下**只建立会话密钥，不算认证通过**——认证要等
        ``verify_auth_proof()``。理由是解封成功仅证明「发送方持有 PC 公钥」，
        而录下来的 auth 帧原样重放同样解得开：帧本身无法自证「它是刚发出来的」。
        none+CF 没有会话密钥、做不了密钥确认，故 token 一过即认证。

        Args:
            auth_msg: 完整的 auth 消息 dict。

        Returns:
            True 表示材料可用（明文 token 模式下即认证通过），False 表示被拒绝。
        """
        if not self.is_encrypted:
            # none 模式：token 认证（明文，依赖 WSS 保护）
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
        if not isinstance(data, (bytes, bytearray)):
            # msgpack 的 bin 类型是密封 blob 的唯一合法承载；str 属协议错误
            self._rejected = True
            self._reject_reason = "invalid auth data encoding"
            return False
        sealed = bytes(data)
        try:
            algo, session_key = self._channel.key_exchange.handle_auth(sealed)
        except CryptoError as e:
            self._rejected = True
            self._reject_reason = str(e) or "auth data processing failed"
            return False
        self._provider = create_provider(algo, session_key)
        # 不置 _authenticated：认证由 verify_auth_proof() 完成（见该方法说明）
        return True

    def make_auth_challenge(self) -> dict:
        """生成握手第二步 ``auth_challenge`` 帧（不含加密，加密由 wrap() 完成）。

        nonce 现场生成、生命周期只有这一次握手，因此**不需要任何跨连接状态**
        （不像「轮换密钥对 / 盐 / 续期票据」那样要把新鲜度存进长期凭据，也就
        不需要客户端持久化任何东西）。它由调用方从返回的帧里取回、作为局部
        变量持有，用完即弃，不进本对象的字段。

        Returns:
            ``{"type":"auth_challenge","nonce":<bytes>}``
        """
        return {"type": "auth_challenge", "nonce": secrets.token_bytes(CHALLENGE_NONCE_BYTES)}

    def verify_auth_proof(self, proof_msg: Optional[dict], nonce: bytes) -> bool:
        """校验握手第三步 ``auth_proof``：回显的 nonce 是否与本连接挑战一致。

        这是整套握手里唯一能挡住重放的地方：重放者没有会话密钥，既读不到
        nonce、也无法把任意值正确地加密回来。

        Args:
            proof_msg: 解密后的 auth_proof 帧；无法解密/类型不符时传 None。
            nonce: ``make_auth_challenge()`` 本轮下发、由调用方持有的值。

        Returns:
            True 表示认证通过（并置 ``is_authenticated``），False 表示拒绝。
        """
        if not isinstance(proof_msg, dict) or proof_msg.get("type") != "auth_proof":
            return False
        got = proof_msg.get("nonce")
        if not isinstance(got, (bytes, bytearray)):
            return False
        # 常数时间比较：失败即关闭连接，理论上无可利用的时序差，但成本为零
        if not hmac.compare_digest(bytes(got), nonce):
            return False
        self._authenticated = True
        return True

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> bytes:
        """把应用层帧编成线上字节：加密模式整帧加密，明文模式直接编码。

        是否加密以 Provider 是否已建立为唯一判据——握手成功才有 Provider，
        握手失败（rejected）时按明文发送，避免"该不该加密"出现第二种真相。
        调用方只拿到字节，不再感知信封与编码。

        防重放 seq 由 CryptoProvider 在加密层承载，此处不感知、也不注入。
        """
        plaintext = encode_frame(message)
        if self._provider is None:
            return plaintext
        return self._provider.encrypt(plaintext)

    def unwrap(self, raw: bytes) -> Optional[dict]:
        """把线上字节还原成应用层帧：加密模式先解密，明文模式直接解码。

        解密失败（密钥错 / 篡改 / 重放）由 CryptoProvider 以异常表达，
        SecureSession 统一归为 None（视为不可用帧丢弃）。
        """
        try:
            plaintext = self._provider.decrypt(raw) if self._provider is not None else raw
            return decode_frame(plaintext)
        except Exception:
            # TODO: wire-protocol 需区分两类异常DecryptError, ReplayError映射 error.code 
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
