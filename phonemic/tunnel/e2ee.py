"""安全通道：基于 CryptoProvider 的可插拔加密。

信任模型：
  - URL fragment 认证：PC 公钥通过 QR 码（物理带外）传递给手机；手机用 SealedBox
    把 ``{"algo", "pk"}`` 密封发送给 PC。PC 端由 KeyExchange 解封、读 algo、做
    ECDH + KDF 得到会话密钥，再据此实例化对应 CryptoProvider。algo 只出现在
    密文内部（非明文），公钥在此部署中等价于带外 token。
  - TOFU（Trust On First Use）：首次连接时手机明文发 auth（只含公钥），PC 指派
    4 位识别码并用 SealedBox(phone_public) 密封下发给该连接方（**识别码不再由手机
    自选、不再出现在明文链路上**），用户核对手机与 PC 审批通知上的识别码后 PC 才
    审批；审批通过后 PC 通过 SealedBox(phone_public) 加密回传 PC 公钥。手机存储
    PC 公钥到 localStorage，重连时走 SealedBox 认证路径（与 URL fragment 一致）。

连接模型：
  SecureChannel 持有跨连接的长期配置（认证方式、模式、PC 密钥对）与
  KeyExchange 实例；PC 密钥对在进程内稳定持有、只随 SecureChannel 重建而
  更换（因此二维码不会因新连接失效）。每个 WebSocket 连接通过 new_session()
  取得一个独立的 SecureSession，握手状态按连接隔离，互不干扰。

状态机（每个 session 独立）：
  URL fragment / TOFU 重连:  S0 --auth(SealedBox)--> S1(密钥就绪) --auth_proof--> S2(已认证)
  TOFU 首次:                S0 --auth(明文pk)--> S0_pin(识别码已下发) --审批通过--> S1 --auth_proof--> S2
                            S0_pin --审批拒绝/超时--> close
  needs_auth 恒为 True——不存在"连上即就绪"的路径。

TOFU 首次的五步（识别码由 PC 指派并密封下发，见 docs/e2ee-always-on-design.md §5.5.1）：
  1. 手机明文 auth{algo, pk}          —— 手机上没有任何秘密
  2. PC 密封下发 sealed{nonce, pin}   —— 只有持有 sk_手机的方能解封
  3. 手机解封显示 pin，一个字节都不发 —— 明文链路上没有可抄走、可重放的东西
  4. 用户核对 → 审批 → auth_challenge{pc_public, nonce}（SealedBox 加密）
  5. 手机 auth_proof（会话密钥加密）
  第 2 步的 nonce 与第 4 步复用同一个：识别码与挑战必须来自同一个对端。

协议格式（全部帧统一 msgpack 编码，WS 走 binary 帧，密文用 bin 类型、无 base64）：
  URL fragment:  #k=<pubkey>&a=<algo1,algo2,...>
  TOFU:          无 fragment，裸 URL
  auth（URL fragment / TOFU 重连）:
    {"type":"auth", "data":<bin SealedBox( {"algo":..., "pk":<手机公钥 b64>} )>}
  auth（TOFU 首次）:
    {"type":"auth", "algo":"xchacha20", "pk":<bin 32B>}          ← 无 pin
  sealed（TOFU 首次，第 2 步）:
    {"type":"sealed", "data":<bin SealedBox(phone_public): {"nonce":<b64>, "pin":"3847"}>}
  auth_challenge（URL fragment / TOFU 重连）:
    Provider.encrypt({"type":"auth_challenge", "nonce":<bin 16B>})
  auth_challenge（TOFU 首次）:
    msgpack({"type":"auth_challenge", "data":<bin SealedBox(phone_public): {pc_public, nonce}>})
  auth_proof（加密）:
    Provider.encrypt({"type":"auth_proof", "nonce":<bin>})
  消息:
    整帧对称加密：WS binary 帧 = AEAD(msgpack(消息))，无外层信封。
"""

import base64
import hmac
import json
import secrets
import time
from typing import Optional, Tuple

from nacl.public import PrivateKey, PublicKey, SealedBox

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS, create_provider
from phonemic.tunnel.crypto import KeyExchange
from phonemic.tunnel.crypto.errors import CryptoError
from phonemic.tunnel.frame import decode as decode_frame
from phonemic.tunnel.frame import encode as encode_frame

AUTH_TIMEOUT = 10  # 秒：**单次**握手等待的上限（auth / auth_proof 各自计时）
APPROVAL_TIMEOUT = 30  # 秒：TOFU 审批等待时间
CHALLENGE_NONCE_BYTES = 16  # auth_challenge 的 nonce 长度
PIN_DIGITS = 4  # TOFU 识别码位数（10**4 种取值）


def _to_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _generate_pin() -> str:
    """TOFU 识别码：**由 PC 随机指派**，与手机的任何输入都无关。

    对称性决定了这一点：此时 PC 已从明文 auth 里拿到 phone_public（能密封），
    而手机还没有 pc_public（不能密封）。指派而非自选，同时消掉两个攻击面——
    **抄不走**（识别码从不上明文链路）、**刷不出**（与公钥无关，没有 10^4 的
    枚举空间可撞），详见 docs/e2ee-always-on-design.md §5.5.1。
    """
    return str(secrets.randbelow(10 ** PIN_DIGITS)).zfill(PIN_DIGITS)


class SecureSession:
    """单个 WebSocket 连接的握手状态与加解密上下文。

    每个连接持有一个实例。会话密钥由 ECDH 按连接派生（手机端在同一页面内
    复用同一对临时密钥，故同一页面的多次重连会派生出相同的会话密钥）；
    跨连接的新鲜度由握手 nonce 提供，不依赖会话密钥。
    """

    def __init__(self, channel: "SecureChannel"):
        self._channel = channel
        self._provider = None
        self._authenticated = False
        self._connected_at = time.monotonic()
        # TOFU 首次连接状态
        self._tofu_first = False
        self._pin: Optional[str] = None
        self._phone_public: Optional[bytes] = None
        # 本轮 challenge 的 nonce（供 verify_auth_proof 使用）
        self._challenge_nonce: Optional[bytes] = None

    # ---- 属性 ----

    @property
    def needs_auth(self) -> bool:
        """是否需要 auth 握手——恒为 True。"""
        return True

    @property
    def is_encrypted(self) -> bool:
        """是否需要 data 信封加密——恒为 True。"""
        return True

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def auth_timed_out(self) -> bool:
        return not self._authenticated and (time.monotonic() - self._connected_at) > AUTH_TIMEOUT

    @property
    def is_tofu_first(self) -> bool:
        """是否处于 TOFU 首次连接（待审批）状态。"""
        return self._tofu_first

    @property
    def pin(self) -> Optional[str]:
        """TOFU 首次连接的识别码（审批用）。

        注意它是 **PC 指派**的（``receive_auth`` 生成），不是取自 auth 帧——
        手机在整条握手里不发任何与识别码有关的帧。
        """
        return self._pin

    @property
    def phone_public(self) -> Optional[bytes]:
        """TOFU 首次连接的手机公钥原始字节（审批后用于 ECDH）。"""
        return self._phone_public

    @property
    def negotiated_algorithm(self) -> str:
        """本次握手实际协商出的算法。

        未握手返回 "none"，握手后返回 provider 的算法名。
        """
        if self._provider is None:
            return "none"
        return self._provider.algorithm_name()

    # ---- 握手 ----

    def receive_auth(self, auth_msg: dict) -> Tuple[str, Optional[bytes], Optional[str], Optional[bytes]]:
        """处理握手第一步 ``auth``，返回 ``(algo, session_key, pin, phone_pk)``。

        - 认证模式 / TOFU 重连（auth 含 ``data`` 字段）：SealedBox 解封 →
          ECDH → KDF → 返回 ``(algo, session_key, None, None)``。
          **不创建 Provider**——由调用方在 ``create_provider()`` 中完成。
        - TOFU 首次（auth 含 ``algo``/``pk`` 明文字段，无 ``pin``）：
          返回 ``(algo, None, pin, phone_pk)``——密钥待审批后建。``pin`` 是
          **本机会话生成的**识别码（auth 帧里没有这个字段），随机且每连接独立；
          与之同源的 nonce 在本方法里一并生成，交给随后的 ``make_sealed_pin()``
          与 ``make_auth_challenge()`` 复用。

        Raises:
            CryptoError: 解封失败、algo 不在允许列表、字段缺失等。
        """
        data = auth_msg.get("data")

        if data is not None:
            # SealedBox 路径（URL fragment 认证 / TOFU 重连）
            if not isinstance(data, (bytes, bytearray)):
                raise CryptoError("invalid auth data encoding")
            sealed = bytes(data)
            algo, session_key = self._channel.key_exchange.handle_auth(sealed)
            return algo, session_key, None, None
        else:
            # TOFU 首次：明文路径，只解析字段，不做 ECDH
            algo = auth_msg.get("algo")
            phone_pk = auth_msg.get("pk")
            if algo is None or phone_pk is None:
                raise CryptoError("missing algo or pk in TOFU auth")
            if not isinstance(phone_pk, (bytes, bytearray)):
                raise CryptoError("invalid phone public key encoding")
            self._tofu_first = True
            self._phone_public = bytes(phone_pk)
            # 识别码与挑战 nonce 在此同源生成：后面两条下行帧必须是同一个对端的
            self._pin = _generate_pin()
            self._challenge_nonce = secrets.token_bytes(CHALLENGE_NONCE_BYTES)
            return algo, None, self._pin, self._phone_public

    def create_provider(self, algo: str, session_key: bytes) -> None:
        """认证模式 / TOFU 重连：用已就绪的会话密钥创建 Provider。"""
        self._provider = create_provider(algo, session_key)

    def complete_tofu_auth(self, algo: str, phone_pk: bytes) -> None:
        """TOFU 审批通过后调用：做 ECDH → KDF → 创建 Provider。"""
        algo, session_key = self._channel.key_exchange.handle_tofu_auth(algo, phone_pk)
        self._provider = create_provider(algo, session_key)

    def make_sealed_pin(self) -> bytes:
        """TOFU 首次第 2 步：把 PC 指派的识别码连同 nonce 密封给当前连接方。

        线上形态是 ``{"type":"sealed", "data":<bin SealedBox(phone_public)>}``
        的**明文帧**（内容为密文）——此时双方还没有会话密钥，与 auth_challenge
        的处境相同：整帧无法对称加密，只能靠 SealedBox 保证「只有持有 sk_手机
        的人读得到」。手机解封后**只用于显示**，不回任何帧。

        对称性是使用这条通道的全部理由：PC 已从明文 auth 拿到 phone_public，
        能密封；手机还没有 pc_public，不能。这是无信任阶段唯一能把一个共享
        秘密送进手机的通道。
        """
        if not self._tofu_first:
            raise CryptoError("sealed pin is only available in TOFU first-use handshake")
        inner = json.dumps({
            "pin": self._pin,
            "nonce": _to_b64(self._challenge_nonce),
        }).encode("utf-8")
        sealed = SealedBox(PublicKey(self._phone_public)).encrypt(inner)
        return encode_frame({"type": "sealed", "data": sealed})

    def make_auth_challenge(self) -> bytes:
        """生成握手第二步 ``auth_challenge`` 的线上字节并返回。

        - URL fragment / TOFU 重连：Provider 加密整帧。
        - TOFU 首次：SealedBox(phone_public) 加密 ``{pc_public, nonce}``，
          msgpack 编码后返回（不用 Provider 加密——手机还没有 Provider）。
          这里的 nonce **复用** ``make_sealed_pin()`` 那一条（同源要求）。

        nonce 存入 ``self._challenge_nonce``，供 ``verify_auth_proof`` 校验。
        """
        if self._tofu_first:
            nonce = self._challenge_nonce
            inner = json.dumps({
                "pk": _to_b64(self._channel.key_exchange.public_key_bytes),
                "nonce": _to_b64(nonce),
            }).encode("utf-8")
            sealed = SealedBox(PublicKey(self._phone_public)).encrypt(inner)
            frame = {"type": "auth_challenge", "data": sealed}
            return encode_frame(frame)
        else:
            nonce = secrets.token_bytes(CHALLENGE_NONCE_BYTES)
            self._challenge_nonce = nonce
            frame = {"type": "auth_challenge", "nonce": nonce}
            return self._provider.encrypt(encode_frame(frame))

    def verify_auth_proof(self, proof_msg: Optional[dict]) -> bool:
        """校验握手第三步 ``auth_proof``：回显的 nonce 是否与本连接挑战一致。

        Args:
            proof_msg: 解密后的 auth_proof 帧；无法解密/类型不符时传 None。

        Returns:
            True 表示认证通过（并置 ``is_authenticated``），False 表示拒绝。
        """
        if not isinstance(proof_msg, dict) or proof_msg.get("type") != "auth_proof":
            return False
        got = proof_msg.get("nonce")
        if not isinstance(got, (bytes, bytearray)):
            return False
        if not hmac.compare_digest(bytes(got), self._challenge_nonce):
            return False
        self._authenticated = True
        return True

    def proof_rejection_reason(self, proof_msg: Optional[dict]) -> str:
        """给日志用的拒因：区分「解不开」与「nonce 不符」两种完全不同的问题。

        auth_proof 解不开几乎总是**会话密钥不一致**（典型触发：对端换过 PC
        密钥对、却仍拿着上一任公钥派生的会话密钥），把它记成 nonce mismatch
        会把排查带偏——nonce 只是跟着一起对不上而已。
        """
        if proof_msg is None:
            return "undecryptable (session key mismatch?)"
        if proof_msg.get("type") != "auth_proof":
            return f"unexpected frame: {proof_msg.get('type')!r}"
        got = proof_msg.get("nonce")
        if not isinstance(got, (bytes, bytearray)):
            return "missing nonce"
        return "nonce mismatch"

    # ---- 数据加解密 ----

    def wrap(self, message: dict) -> bytes:
        """把应用层帧编成线上字节：Provider 存在时整帧加密，否则直接编码。"""
        plaintext = encode_frame(message)
        if self._provider is None:
            return plaintext
        return self._provider.encrypt(plaintext)

    def unwrap(self, raw: bytes) -> Optional[dict]:
        """把线上字节还原成应用层帧。

        解密失败（密钥错 / 篡改 / 重放）由 CryptoProvider 以异常表达，
        SecureSession 统一归为 None（视为不可用帧丢弃）。
        """
        try:
            plaintext = self._provider.decrypt(raw) if self._provider is not None else raw
            return decode_frame(plaintext)
        except Exception:
            return None


class SecureChannel:
    """跨连接共享的长期配置与密钥材料。

    持有认证方式、模式以及 PC 密钥对（封装在 KeyExchange 内），这些信息
    在整个进程生命周期内保持稳定，二维码不会因新连接而失效。每连接的握手
    状态与会话密钥由 new_session() 产出的 SecureSession 承载。
    """

    def __init__(self, auth_method: str = "tofu", mode: str = "lan"):
        self._auth_method = auth_method
        self._mode = mode
        self._key_exchange = KeyExchange(PrivateKey.generate(), OFFERED_ALGORITHMS)
        # 随机入口路径：url_fragment 模式为 32 位随机串（防扫描），TOFU 为空串（裸 URL）
        self._secret_path = ""

        if auth_method == "url_fragment":
            self._secret_path = secrets.token_urlsafe(24)

    # ---- 属性 ----

    @property
    def auth_method(self) -> str:
        """当前认证方式："tofu"（手动审批）或 "url_fragment"（扫码认证）。"""
        return self._auth_method

    @property
    def algorithm(self) -> str:
        """向后兼容：加密永远开启，等价于 "auto"。"""
        return "auto"

    @property
    def offered_algorithms(self) -> list:
        """下发给客户端的算法列表（按优先级排序）。"""
        return list(OFFERED_ALGORITHMS)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def key_exchange(self) -> KeyExchange:
        """密钥交换实例（PC 身份私钥持有者）。所有模式均存在。"""
        return self._key_exchange

    @property
    def pc_private(self) -> PrivateKey:
        """PC 私钥（归 KeyExchange 持有）。"""
        return self._key_exchange.private_key

    @property
    def needs_auth(self) -> bool:
        """是否需要 auth 握手——恒为 True。"""
        return True

    @property
    def is_encrypted(self) -> bool:
        """是否需要 data 信封加密——恒为 True。"""
        return True

    @property
    def secret_path(self) -> str:
        """随机入口路径：url_fragment 模式为非空随机串，TOFU 为空串（裸 URL）。

        服务端 dispatcher 据此决定放行规则，URL 拼接时非空则插入 /{secret}/ 前缀。
        """
        return self._secret_path

    # ---- 公钥与 URL ----

    def get_public_key_b64(self) -> str:
        """返回 PC 公钥（base64url 无 padding）。"""
        return self._key_exchange.public_key_b64

    def append_to_url(self, url: str) -> str:
        """在 URL 末尾追加加密参数 fragment。

        TOFU 模式：返回裸 URL（无 fragment）——门禁是审批机制，不需要 URL 级访问控制。
        URL fragment 模式：插入 /{secret_path}/ 前缀 + #k=<pubkey>&a=<algos>。
        """
        if self._auth_method == "tofu":
            return url
        if not url.endswith("/"):
            url += "/"
        if self._secret_path:
            url += f"{self._secret_path}/"
        return f"{url}#k={self.get_public_key_b64()}&a={','.join(self.offered_algorithms)}"

    # ---- 连接生命周期 ----

    def new_session(self) -> SecureSession:
        """为该连接创建独立的握手与加解密上下文。"""
        return SecureSession(self)
