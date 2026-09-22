# PhoneMic 加密层设计（v1 · E2EE）

状态：**设计稿（核心已随代码落地）**。本文档从 `wire-protocol.md` 的 §4 / §8 抽取独立成篇，是加密层的唯一对齐依据。

**核心定位：内容无关的加密层。** 它的输入输出只有字节串——上层用 JSON、MessagePack、CBOR 还是裸二进制，加密层一概不感知；反过来上层换编码、换协议，加密层一行不改。两份文档的分工：

| 内容                                                           | 归属                      |
| ------------------------------------------------------------ | ----------------------- |
| 哪些帧明文 / 加密、`auth` / `auth_challenge` / `auth_proof` 握手帧、WS close 4001、error code | `wire-protocol.md`（消息层） |
| 信任模型、密钥交换、对称封装、seq 防重放、算法实现、线上密文布局                           | 本文档（加密层）                |



---

## 1. 分层模型与接口

```
应用消息   type map（{type:...}，任意结构）
   ↓
编码层     map → bytes（JSON / MessagePack / CBOR …… 任选，见 wire-protocol.md）
   ↓
加密层     bytes → bytes（本文档：KeyExchange + CryptoProvider）
   ↓
传输       WS binary 帧
```

### 1.1 对外接口

```python
class CryptoProvider(ABC):
    @staticmethod
    @abstractmethod
    def algorithm_name() -> str: ...
    @abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes: ...   # 内部自动打 seq 并加密
    @abstractmethod
    def decrypt(self, ciphertext: bytes) -> bytes: ...  # 内部校验 seq，成功返回明文；失败抛异常
    @abstractmethod
    def reset(self) -> None: ...                        # 计数器归零（rekey 复用实例时用）
```

- `encrypt` 的输入是**编码层的产物**（任意编码的字节串），`decrypt` 成功时返回**可直接交给编码层解析**的字节串——加密层对内容零解释。
- 防重放 `seq` **完全内化**（见 §5）：不出现在签名里，调用方不传、不收、不验。
- 失败抛两类异常（基类 `CryptoError`，定义于 `crypto/errors.py`）：
  - `DecryptError` —— MAC 校验失败（密钥错 / 篡改）；
  - `ReplayError` —— seq 不递增（重放 / 乱序），仅前缀路径能明确给出（见 §5.1）。

### 1.2 内容无关承诺

| 自由度                         | 改动面                                            |
| --------------------------- | ---------------------------------------------- |
| 上层换编码（msgpack → CBOR 等）     | 加密层**零改动**                                     |
| 加密层新增算法（AES-GCM、AEGIS-256…） | 编码层与 type 表**零改动**                               |
| 认证方式切换（`url_fragment` ↔ `tofu`） | 编码层零改动，只影响握手前两步的帧形态；Provider 与数据帧完全一致（§3.5） |

这套正交性正是 §3 把 `KeyExchange` 从 `Provider` 拆出来、§5 把 `seq` 内化进 `Provider` 的统一动机：**每一层只认字节与密钥，不认彼此的内部格式。**

---

## 2. 威胁模型与信任模型

### 场景

加密与认证已解耦（设计依据见 `e2ee-always-on-design.md`）：**加密永远开启**，认证方式可选。

| 模式 | 加密 | 认证 | 原因 |
| --------------- | ---- | -------- | ----------------------------------------------------------------------------------------------------------------------------- |
| LAN + TOFU | 强制 | 手动审批（首次）/ PC 公钥 token（重连） | 局域网被动嗅探门槛低，明文是最大短板 ⇒ 加密不可关；首次连接无信任锚，用识别码核对 + 人工审批补门禁 |
| LAN + URL fragment | 强制 | QR fragment 中的 PC 公钥（bearer token） | — |
| Cloudflare（任意配置） | 强制 | URL fragment（强制） | 流量出公网，TLS 在 CF 边缘终结，隧道段裸奔；`mode.py` 的 `effective_auth_method` 在 Cloudflare 下强制 `url_fragment`。**不存在明文 CF**，也不提供 TOFU+CF（公网可达 ⇒ 攻击者可抢先骗取审批） |

`none` 算法与 `PlainProvider` 已完全移除——所有通信都经过 AEAD，不存在"不加密"这一档。

### 两条认证路径

| 路径 | 信任锚 | 首次需审批 | 握手帧 |
|---|---|---|---|
| URL fragment（QR） | QR fragment 中的 PC 公钥（带外分发） | 否 | `auth`(SealedBox) → `auth_challenge`(Provider) → `auth_proof` |
| TOFU | 首次无 ⇒ 人工审批 + 识别码核对；审批后 PC 公钥存 localStorage | 是（仅首次） | `auth`(明文 `algo`/`pk`) → `sealed`(**SealedBox**，PC 指派识别码) → `auth_challenge`(**SealedBox**) → `auth_proof`(Provider) |

TOFU 重连与 URL fragment 完全同构（手机已持有 PC 公钥，走 SealedBox），差异只在首次连接。

### bearer 认证：能密封即认证

PC 公钥在此部署中不是公开密钥，而是**带外分发的 bearer 能力（等价于 token）**：只出现在二维码 fragment、从不发布；在 `SecureChannel`（进程生命周期）内稳定持有，切换模式 / 重启服务时由 `PrivateKey.generate()` 重新生成（`e2ee.py` 的 `SecureChannel`）。因此"能成功解封 `SealedBox`"本身就构成对发送方的认证——只有拿到过该二维码的一方持有此公钥，能密封出 PC 私钥解得开的密文。

这属于**匿名 / bearer 认证**：它证明的是"你持有此二维码"，而**不是**"你是那台手机"。这条边界值得说清楚，因为它决定了下一步推论的方向：**任何拿到二维码的人都能用自己的临时密钥对完成一次合法握手，并在这条连接上发送任意应用帧**——这是 bearer 凭据的固有代价，不是实现缺陷。1:1（一台手机 ↔ 一台 PC）场景下可接受。

在此之上有更强一层，作用是**隔离会话**：会话密钥由 ECDH 派生（PC 身份私钥 × 手机**临时**私钥，临时私钥从不出手机），会话内所有帧均为 AEAD。因此拿到二维码的一方虽然能建立**属于它自己**的那条会话，却**读不到、也伪造不了**真机那条会话的帧，**也无法重放**录下的帧——重放由握手的 `nonce` 挑战挡掉（`wire-protocol.md` §5 / §7）。

随机 `secret_path`（32 位 `token_urlsafe`）是**端点门禁**而非认证主体：让未授权客户端连 WS 入口都够不着（根路由 404）。它与 PC 公钥同处二维码、各司其职——`path` 挡"到达"，`pubkey` 认证"发送方"。

> corollary：公钥按 token 对待即有 token 级敏感性。泄露二维码意味着他人可发起 `auth`，但 (a) 公钥在进程内稳定、切换模式 / 重启服务即更换（届时必须重新扫码）；(b) 那是一条**属于它自己**的会话，读不到也伪造不了真机会话的帧，更无法重放。风险可控，不应视作"无所谓公开"。

---

### 浏览器端 E2EE 的天花板：明文页面下，能改包的人可以偷走 fragment

（本节由 `docs/http-upload-design.md` §5.11.4 移入。）

`#fragment`（PC 公钥 / token）不会进任何请求，所以**能挡住被动嗅探**。但页面 `mobile.html` 本身是明文 HTTP 下发的 ⇒ **能改包的人可以在页面响应里插一段 JS，把 `location.hash` 送到自己那里** ⇒ 公钥泄漏 ⇒ 之后他能完整冒充手机（握手、打字、上传）。

这不是某个功能引入的问题，是「**在明文 HTTP 上做浏览器端 E2EE**」本身的边界：E2EE 的可信根是「页面脚本没被篡改」，而这份脚本自己就是明文送来的。

- **CF 模式**：链路有 TLS，同链路的旁观者进不来。CF 作为可信基础设施，其 TLS 终结点是整个互联网 TLS 体系的常规信任边界——与网银、电商等场景一致。E2EE 在此基础上进一步保证：即使 CF 内部人员理论上能看到 HTTP 层明文，也**读不到** WebSocket 载荷的实际内容（ECDH + AEAD）。这不是因为 CF 不可信，而是因为 E2EE 的设计目标就是「端到端，中间节点无需信任」。
- **LAN 加密模式**：连 TLS 都没有 ⇒ 防的是**被动的旁观者**（同一 WiFi 上顺手抓包的人），不是能改包的主动攻击者。自签证书也挡不住 MITM（攻击者可生成自己的自签证书，手机不预装 CA 无法区分），除非在手机上预先安装信任证书——不考虑此场景。

## 3. 密钥交换：KeyExchange（算法无关）

### 3.1 为什么必须独立成类（死锁论证）

- 不解密 `auth.data` 就不知道 `algo`；不知道 `algo` 就无法实例化对应 `CryptoProvider`；
- 而 `algo` 封在 `auth.data` 的密文里，解密它要先用 PC 身份私钥做 `SealedBox` 解封——这一步**与对称算法无关**，任何 Provider 都还没出生就能做。

所以"解 auth"天然属于一个算法无关的 `KeyExchange` 类，不属于任何 Provider。把 `SealedBox`/ECDH/KDF 塞进 Provider，等于要求 Provider 在构造前先自行解开自己的构造参数，逻辑上不成立。


### 3.2 接口与实现

```python
class KeyExchange:
    """算法无关的密钥交换。持有 PC 长期 X25519 身份私钥（SealedBox 密钥对）。

    只负责"从 auth 报文解出对称会话密钥"，不认识任何具体加密算法。
    """

    def __init__(self, pc_private: PrivateKey, allowed_algos: List[str]):
        self._pc_private = pc_private          # 长期身份私钥，QR 码下发其公钥
        self._allowed = set(allowed_algos)     # 下发算法列表（a=），用于校验

    @property
    def public_key_b64(self) -> str:
        """二维码 fragment 用的 PC 公钥（base64url 无 padding）。"""
        return _to_b64(bytes(self._pc_private.public_key))

    def handle_auth(self, sealed_data: bytes) -> tuple[str, bytes]:
        """解 auth.data（bytes），返回 (algo, session_key)。失败抛 CryptoError。"""
        inner = SealedBox(self._pc_private).decrypt(sealed_data)   # ① 非对称解封（与算法无关）
        d = json.loads(inner)                                      # ② 解内层结构（见 3.3）
        algo = d["algo"]
        if algo not in self._allowed:
            raise ValueError(f"algorithm {algo!r} not allowed")
        phone_public = PublicKey(_from_b64(d["pk"]))
        shared = crypto_scalarmult(bytes(self._pc_private), bytes(phone_public))  # ③ ECDH
        session_key = blake2b(shared, digest_size=32).digest()     # ④ 统一 KDF，32B
        return algo, session_key

    def handle_tofu_auth(self, algo: str, phone_public_bytes: bytes) -> tuple[str, bytes]:
        """TOFU 首次：参数是明文字段（非密封 blob），做同样的 ECDH + KDF。

        与 handle_auth 共用 ③④ 两步，区别只在参数来源：这里不经过 SealedBox
        ——首次连接手机还没有 PC 公钥，无从密封（见 3.5）。
        """
        if algo not in self._allowed:
            raise ValueError(f"algorithm {algo!r} not allowed")
        shared = crypto_scalarmult(bytes(self._pc_private), phone_public_bytes)
        session_key = blake2b(shared, digest_size=32).digest()
        return algo, session_key

    @property
    def public_key_bytes(self) -> bytes:
        """PC 公钥原始字节。TOFU 首次审批通过后据此密封回传（见 3.5）。"""
        return bytes(self._pc_private.public_key)
```

链条：**非对称只用于交换一次临时公钥，之后全程对称。**

为什么不把两个 `handle_*` 合并：`handle_auth` 的输入是一个不透明 blob（消息层不解释内容），`handle_tofu_auth` 的输入是 `algo`(str) + `pk`(bytes) 两个已拆开的明文字段。签名不同，强行合并会让消息层的分支判断渗进密钥交换层。

### 3.3 auth 内层结构（加密层自带的最小编码）

解封后的内层是固定结构：

```json
{"algo": "xchacha20", "pk": "<手机临时 X25519 公钥 base64url>"}
```

- **内层编码刻意选 JSON**（两端 `string ↔ bytes` 各三行即可），**不依赖上层编解码器**——否则加密层反向依赖编码层，鸡生蛋：密钥交换发生时上层编码器还没轮到出场。上层将来用 msgpack 还是 CBOR，与这里无关。
- `algo` 只存在于密文内部，**从不明文传输**——暴露服务端支持的算法列表等于泄露能力面，没有必要。
- **`handle_auth` 的失败路径只有两条**：解封失败（`bad sealed box`）、`algo` 不在下发列表（`algo not offered`）。两者都在密钥建立之前，对应 WS close 4001（帧层行为见 `wire-protocol.md` §5/§7）。
- 握手还有**第三种**失败：第三步 `auth_proof` 的 `nonce` 校验不通过（或超时）。它**不属于 `handle_auth`**——此时 `session_key` 已派生成功，失败由消息层在加密帧里表达（`error(code:"auth")`，见 `wire-protocol.md` §7）。`handle_auth` 的签名与职责不因此改变。

### 3.4 调用侧：两段式，顺序不可逆

```python
algo, session_key = self._channel.key_exchange.handle_auth(auth_data)  # 先解 auth
self._provider = create_provider(algo, session_key)                    # 才知道建哪个 Provider
```

`KeyExchange` 在 `SecureChannel`（长期配置）层持有一个实例，与 `pc_private` 生命周期一致；每个 `SecureSession` 握手时各调用一次 `handle_auth`。

> **注意"每连接"的边界**：`session_key` 只活在这条连接上、不会跨连接复用，但它**不等于"每连接一个新的随机值"**——同一对密钥对（PC 身份私钥 × 手机页面密钥对）重连时会派生出**完全相同**的 `session_key`。跨连接的新鲜度不由 ECDH 提供，而由握手的 `nonce` 挑战提供（`wire-protocol.md` §5 / §7）。任何"密钥每连接不同、所以旧帧必然解不开"的推论都是错的。

**收益**：新增算法只需写一个几十行的 `CryptoProvider` 子类并在 `create_provider` 注册，`KeyExchange` 一行不用改；X25519 交换代码从两份收敛为一处。老的 `algo == "none"`（token）分支随明文 CF 模式一并删除。

### 3.5 TOFU 首次：明文 auth 是唯一例外

TOFU 首次连接时手机没有 PC 公钥，**无法**密封 `auth`——这是"信任锚尚未建立"的直接后果，不是可以绕过的实现细节。因此该帧走明文（`{"type":"auth","algo":...,"pk":<bin>}`），密钥材料只有公钥，保密性无损失：

- `pk` 是手机临时公钥，本身即公开值；被动窃听者拿到它也算不出 `shared`（Curve25519 DLP）。
- `algo` 明文暴露服务端算法列表——仅在"无信任锚"的首次连接上发生，且服务端的算法列表不是秘密（可枚举）。
- **帧里没有识别码**：识别码**由 PC 指派**并密封下发给这一方（`sealed` 帧，`SealedBox(phone_public)`），
  手机解封后只用于显示、不回任何帧。理由见 `e2ee-always-on-design.md` §5.5.1——自选自报 + 明文
  传输的识别码可被同网段窃听者抄走复用，那是一次被动窃听即可确定性得手的攻击。

同一条对称性是这条通道存在与否的全部依据：**PC 已从明文 auth 拿到 `phone_pub`，所以 PC 能密封；
手机还没有 `pc_pub`，所以手机不能密封。** 因此 `sealed` 帧与 `auth_challenge` 一样是"明文帧 + 内容为密文"。

关键设计：**审批在 ECDH 之前、`auth_challenge` 必须等审批通过才发**。`auth_challenge` 里含 PC 公钥（= token），只有审批通过才允许暴露给对端——否则未授权的连接方可以借此拿到 token，绕过审批直接走认证路径重连。`sealed` 不含 PC 公钥，故可以先于审批下发（用户开始核对之前，手机上必须已经有一个码）。所以：

```
receive_auth(明文) → 只解析字段，指派识别码 + 生成挑战 nonce，不做 ECDH，不建 Provider
下发 sealed（SealedBox(phone_public)：{pin, nonce}）
审批通过 → complete_tofu_auth()：handle_tofu_auth() → create_provider()
```

`SecureSession` 因此有四个协作点：`receive_auth()` 返回 `(algo, session_key_or_none, pin_or_none, phone_pk_or_none)`（TOFU 首次下 `pin` 是**本机指派**的）；`make_sealed_pin()` 产出第 2 步的密封帧；`complete_tofu_auth()` 在审批通过后补上 ECDH + Provider；`make_auth_challenge()` 在 TOFU 首次下改发 `SealedBox(phone_public)` 包裹的 `{pc_public, nonce}`，且 **nonce 复用第 2 步那一个**（手机据此校验两条下行帧同源；手机此刻还没有 Provider，无法用对称加密收挑战）。

---

## 4. 对称封装：CryptoProvider

`CryptoProvider` 是**纯对称 AEAD 封装**：构造时直接拿 `session_key`，不碰 `pc_private`、不做 `SealedBox` / ECDH / KDF（那些归 §3 的 `KeyExchange`）。


### 4.1 两个算法（+ 一个预留）

```python
# XChaCha20：nacl.secret.Aead，支持 aad → seq 进 aad（§5.1 AAD 路径）
class XChaCha20Provider(CryptoProvider):
    def __init__(self, session_key: bytes):
        self._aead = Aead(session_key)
        self._tx_seq = self._rx_seq = 0
    def encrypt(self, plaintext):
        out = self._aead.encrypt(plaintext, aad=self._tx_seq.to_bytes(8, "big"))
        self._tx_seq += 1
        return out
    def decrypt(self, ciphertext):
        plain = self._aead.decrypt(ciphertext, aad=self._rx_seq.to_bytes(8, "big"))
        self._rx_seq += 1                    # aad 不符 → DecryptError（明文暴露前即拒绝）
        return plain
    def reset(self):
        self._tx_seq = self._rx_seq = 0

# XSalsa20：nacl.secret.SecretBox，无 aad → seq 前置 8 字节到明文（§5.1 前缀路径）
# 注：PyNaCl 无 Box.from_shared_key；SecretBox 即 crypto_secretbox ≡ crypto_box_afternm，
# 派生密钥直接使用。JS 端对应 crypto_box_easy_afternm（密钥用 scalarmult + generichash 派生）。
class XSalsa20Provider(CryptoProvider):
    def __init__(self, session_key: bytes):
        self._box = SecretBox(session_key)
        self._tx_seq = self._rx_seq = 0
    def encrypt(self, plaintext):
        out = self._box.encrypt(self._tx_seq.to_bytes(8, "big") + plaintext)
        self._tx_seq += 1
        return out
    def decrypt(self, ciphertext):
        plain = self._box.decrypt(ciphertext)
        seq = int.from_bytes(plain[:8], "big")
        if seq != self._rx_seq:
            raise ReplayError(f"seq {seq} != expected {self._rx_seq}")
        self._rx_seq += 1
        return plain[8:]                     # 内部剥掉前缀，返回纯上层编码字节
    def reset(self):
        self._tx_seq = self._rx_seq = 0

# AES-GCM：预留。支持 aad，走 AAD 路径，实现骨架同 XChaCha20（cryptography 库 AESGCM）
class AESGCMProvider(CryptoProvider):
    ...
```

### 4.2 新增算法流程

写一个 `CryptoProvider` 子类（决定走 AAD 路径还是前缀路径）→ 在 `create_provider(algo, session_key)` 注册 → 二维码 `a=` 列表加上算法名。`KeyExchange`、编码层、type 表零改动。

---

## 5. seq 与防重放

`seq` 是**加密层防护**，不是消息内容——它不进上层编码的 map，而且外部（帧编解码层 / `SecureSession`）**完全感知不到 `seq` 的存在**：计数、承载、校验全部在 `CryptoProvider` 内部完成，调用方只看到 `encrypt(plaintext) -> bytes` / `decrypt(ciphertext) -> bytes`。理由：

- 它是防重放 / 防重排的机密性设施，与应用层字段无关；放进上层编码等于把"线格式"和"加密防护"耦合，将来换编码（→ CBOR 等）还得跟着改 `seq` 的承载。
- 把防重放做成加密层的内建属性，与 §4"Provider 只管对称 AEAD"定位一致。

### 5.1 承载方式：AAD 优先，前缀兜底

`seq` 为每方向单调计数器（首帧 = 0，之后严格 +1），由 Provider 内部维护 `_tx_seq` / `_rx_seq`，不出现在 `encrypt` / `decrypt` 签名里：

- **AAD 优先**（XChaCha20 / AES-GCM 这类支持 `aad` 的 AEAD）：`seq` 作为关联数据传入。解密时 AEAD 先整体校验 aad，seq 不对则**在明文暴露前就拒绝**——既防篡改又防重放。
- **前缀兜底**（XSalsa20 的 SecretBox 无 aad 参数）：加密前把 `seq` 编码成 **8 字节大端**前置到明文（`seq(8B) || 上层编码字节`）再整体加密；解密后先读前 8 字节校验、剥掉，再把纯上层编码字节交回调用方。同样在解析上层编码前完成 seq 校验。

两条路径对外接口完全一致，上层无感；区别只在 Provider 内部怎么把 `seq` 焊进密文。无论哪种路径，`seq` 都在 AEAD/Box 的 MAC 认证范围内，攻击者无法篡改 seq 而不被察觉。

### 5.2 校验与状态

- **单调策略**：`seq` 必须等于当前 `_rx_seq`（初始 0，每成功收一帧 +1），即首帧 seq=0、每帧严格 +1。`encrypt` 自动打当前 `_tx_seq` 并 +1；`decrypt` 校验失败即抛异常，不返回任何明文。
- **外部只收状态，不收 seq**：`decrypt` 成功返回明文；失败抛 `DecryptError` / `ReplayError`（§1.1），供 `SecureSession` 映射成 error code（异常类型即状态）。**注意**：AAD 路径下重放也表现为 MAC 失败（aad 不符），一并归 `DecryptError`；前缀路径能区分，重放单独归 `ReplayError`。两者对客户端行为一致（丢弃该帧），区分只在日志粒度；安全上"重放当解密失败处理"更稳妥——不向攻击者区分是重放还是篡改。
- **`SecureSession` 不碰 seq**：只调用 `provider.decrypt()`，按异常类型归类 error code（见 §8）。
- **重置**：seq 状态随 Provider 实例生命周期。每个连接握手时新建 Provider，`seq` 天然从 0 开始，通常无需显式 reset。若同一实例复用（rekey 沿用 Provider），`reset()` 把 `_tx_seq` / `_rx_seq` 归零，等价于重新初始化。**TOFU 首次连接是唯一需要显式 `reset()` 的场景**：Provider 在 `init()` 时就建好（此时还没有密钥），审批通过后拿到 PC 公钥才派生会话密钥，两端计数器必须在这一刻对齐归零。
- **上下行各自独立计数器**：服务端校验上行、手机端校验下行，规则相同（严格 +1）。CF 隧道在边缘终结 TLS，下行重放需靠手机端 `seq` 校验挡掉。
- **边际价值，以及它的边界**：同连接内 WS 可靠有序，`seq` 主要防御 (a) 连接内的帧重放 / 乱序，(b) CF 边缘节点这类"TLS 在别处终结"的中间节点重放。**跨连接的重放不是它的职责**——`seq` 每个连接都从 0 重来（见上），所以从会话开头录下的整段密文，在新连接上按序重放是**解得开**的；挡住它的是握手的新鲜值 `nonce`（`wire-protocol.md` §5 / §7）。成本仅 8 字节/帧 + 约 5 行，保留。

---

## 6. 线上密文布局（内容无关视角）

加密模式下，**WS binary 帧 = `provider.encrypt(上层编码字节)` 的原始输出**，帧内布局由算法实现决定，上层协议不需要知道：

| 路径                       | 线上帧布局                                       | `decrypt` 返回给上层 |
| ------------------------ | ------------------------------------------- | --------------- |
| AAD（XChaCha20 / AES-GCM） | `nonce(24B) ‖ AEAD(上层编码字节)`，aad = `seq(8B)` | 上层编码字节          |
| 前缀（XSalsa20）             | `nonce(24B) ‖ AEAD(seq(8B) ‖ 上层编码字节)`       | （内部剥 seq）上层编码字节 |

- PyNaCl 的 `Aead` / `SecretBox` `.encrypt()` 自带随机 nonce 前缀，无需手动拼帧。
- 两种路径线上字节不同，但**上层编码的 map 内容完全一致**；换算法（或 Provider 优化布局）都不影响编码层与 type 表。
- 加密覆盖**整个上层消息，包括 `type`**——元数据不泄露（攻击端看不出你在发 mouse 还是 file）。
- 例外：**`auth` 帧不走此布局**——它是密钥交换本身的载体，见 §3.3 与 `wire-protocol.md` §7。

---

## 7. 会话状态与安全原则

- **会话状态是权威**：`SecureSession.is_encrypted` **恒为 `True`**（加密与认证解耦后没有"不加密"这一档，见 §2）。接收端永远知道该解还是不该解，不需要"试一试"。
- **原则：绝不"解密失败就当明文"。** 解密失败的可能原因（对端版本不同、中间人篡改、重放）处理动作完全一致——丢弃 + 回 `error`。区分它们没有价值。
- **明文帧只有两种，且都是握手的必然产物**：
  - `auth`(SealedBox) —— 密钥尚未建立，无密钥可用。
  - `auth`(TOFU 首次明文) —— 手机还没有 PC 公钥，无从密封（§3.5）。
  除此之外，握手**成功**时从 `auth_challenge` 起（含）全部整帧对称加密（含 `type`）；握手在**认证前**失败时服务端不回消息层帧、直接关闭 WS（close 4001）。实现上绝不能把 `auth` 帧塞进 `wrap()`。
- **唯一的加密失败例外是握手第三步**：`auth_proof` 校验不通过（或超时）时密钥已经存在、对端也读得懂加密帧，所以这一路失败用**加密的 `error(code:"auth")`** 表达而非 close（`wire-protocol.md` §7）。这不破坏上一条——那条约束的是"无密钥可用的时刻"。
- **`auth_challenge` 只带一个 `nonce`**（TOFU 首次例外：它额外携带 PC 公钥，见 §3.5）：它不重复"我已经承认你的材料"这件事——手机能把它解开，本身就是"PC 持有正确会话密钥"的证明；`nonce` 的唯一职责是**给本次连接一个新鲜值**，使录下的整段旧握手（`auth` + `auth_challenge` + `auth_proof`）无法按序重放。它既不是 `algo` 的回显（`algo` 早在 `auth` 里协商完），也不是"你通过了"的最终回执（那是随后 `config` 的事）。
  - 威胁前提要说清楚：这套重放防护对**"能看见隧道明文帧、但拿不到二维码"**的对手才有意义（CF 内部路径、或 TLS 在别处被终结）。对**持有二维码 / 持有 PC 公钥**的一方，重放本来就是多余手段——它直接自己完成一次握手即可（见 §2）。
- **判别不靠帧类型，靠会话状态机**：`needs_auth` 恒为 `True`，消息层永远先期待 `auth`（不存在"连上即进数据帧"的路径）；`is_encrypted` 恒为 `True`，认证后的每一帧都解密。（状态机表与 text 帧拒绝策略见 `wire-protocol.md` §10。）
- **握手三步不带任何跨连接状态**：`nonce` 由服务端现场生成、只活这一次握手，是 `_handle_auth` 协程里的局部变量（不入 `SecureSession` 字段）。这正是它相对「轮换密钥对 / 盐 / 续期票据」那类方案的关键差别——那些方案要把新鲜度存进长期凭据，而长期凭据唯一的分发通道是二维码（带外、只在页面加载时读一次），于是要么每次重连都重新扫码，要么把真机锁死。

---

## 8. 与 wire-protocol.md 的接口

消息层与加密层的全部接触面，收敛为四条：

1. **握手判定**：`needs_auth` **恒为 `True`**（`e2ee.py` 的 `SecureChannel`/`SecureSession`）——所有模式都需要握手，不存在跳过握手的路径。消息层期待 `auth` 消息帧：
   - 帧含 `data`（bytes）⇒ 交 `KeyExchange.handle_auth` 解封，返回 `(algo, session_key)`，经 `create_provider` 建 Provider，此后连 `auth_challenge` 在内整帧走 Provider。
   - 帧含明文 `algo`/`pk` ⇒ TOFU 首次：`receive_auth` 只解析字段并**指派识别码 + 生成挑战 nonce**（**不建 Provider**），消息层先发 `sealed`（SealedBox 加密，见 §3.5）再等待审批；通过后调 `complete_tofu_auth()` 走 `handle_tofu_auth` 补上 ECDH + Provider，再发 `auth_challenge`（SealedBox 加密，nonce 与 `sealed` 同源）；拒绝 / 超时（30s）→ WS close 4032。
   握手第三步（`auth_proof` 的 `nonce` 校验）属于消息层，见 `wire-protocol.md` §7：认证前失败 → WS close 4001 + reason；`nonce` 不符 / 超时 → 加密 `error(code:"auth")`。
2. **数据帧**：消息层把编码后的字节交给 `provider.encrypt()`，密文原样上 WS binary 帧；收到 binary 帧交给 `provider.decrypt()`，把返回的字节交给编码层解析。
3. **错误映射**：`CryptoError` → `error(code:"decrypt")`；`ReplayError` → `error(code:"replay")`。统一丢弃 + 回 error，连续 N 次断连（code 表见 `wire-protocol.md` §7 的 error 小节）。
4. **失败路径与关闭码**：认证前失败 → `4001`（`auth` 解不开、`algo` 不在列表）；TOFU 审批被拒 / 超时 → `4032`。**不存在"连上即进数据帧"的路径**，因此也没有"无握手帧"的模式（`wire-protocol.md` §5 / §10）。

---

## 9. 实现落点

| 文件                                       | 职责                                                   |
| ---------------------------------------- | ---------------------------------------------------- |
| `phonemic/tunnel/crypto/key_exchange.py` | `KeyExchange`：算法无关密钥交换（§3；含 TOFU 首次的 `handle_tofu_auth` / `public_key_bytes`） |
| `phonemic/tunnel/crypto/errors.py`       | `CryptoError` / `DecryptError` / `ReplayError`       |
| `phonemic/tunnel/crypto/base.py`         | `CryptoProvider` ABC（§1.1 / §4）                      |
| `phonemic/tunnel/crypto/xchacha20.py`    | `XChaCha20Provider`（AAD 路径）                          |
| `phonemic/tunnel/crypto/nacl_box.py`     | `XSalsa20Provider`（前缀路径）                             |
| `phonemic/tunnel/e2ee.py`                | `SecureChannel` / `SecureSession`：装配、握手分叉、TOFU 审批后建密钥（§3.5）、`needs_auth` / `is_encrypted` 恒真 |
| `phonemic/resources/crypto_providers.js` | JS 端同构实现（密封 auth、明文 auth、seq 内化、TOFU 挑战解封）             |
| `phonemic/resources/mobile.html`         | `SecureClient`：JS 调用侧（三种认证模式分叉、识别码、localStorage）      |

`phonemic/tunnel/crypto/plain.py`（`PlainProvider`）已删除——加密永远开启，不再有明文 Provider，`PROVIDER_CLASSES` 中也没有 `"none"` 条目。

依赖：Python `pynacl`（已有）；JS `sodium.js`（已 vendor）。新增算法时：AAD 路径优先选支持 aad 的库（`cryptography` 的 AESGCM）；无 aad 的库走前缀路径。
