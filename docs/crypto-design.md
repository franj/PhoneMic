# PhoneMic 加密层设计（v1 · E2EE）

状态：**设计稿（核心已随代码落地）**。本文档从 `wire-protocol.md` 的 §4 / §8 抽取独立成篇，是加密层的唯一对齐依据。

**核心定位：内容无关的加密层。** 它的输入输出只有字节串——上层用 JSON、MessagePack、CBOR 还是裸二进制，加密层一概不感知；反过来上层换编码、换协议，加密层一行不改。两份文档的分工：

| 内容                                                           | 归属                      |
| ------------------------------------------------------------ | ----------------------- |
| 哪些帧明文 / 加密、`auth` / `auth_ack` 消息帧、WS close 4001、error code | `wire-protocol.md`（消息层） |
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
| 明文 ↔ 加密切换（LAN `none` ↔ 加密）  | 编码层零改动，由 `SecureSession.is_encrypted` 决定走不走加密层 |

这套正交性正是 §3 把 `KeyExchange` 从 `Provider` 拆出来、§5 把 `seq` 内化进 `Provider` 的统一动机：**每一层只认字节与密钥，不认彼此的内部格式。**

---

## 2. 威胁模型与信任模型

### 场景

| 模式              | 加密   | 原因                                                                                                                            |
| --------------- | ---- | ----------------------------------------------------------------------------------------------------------------------------- |
| `none` + LAN    | 可选关闭 | 局域网内，用户自担风险                                                                                                                   |
| 加密 + LAN        | 强制   | —                                                                                                                             |
| 任何 + Cloudflare | 强制   | 流量出公网，TLS 在 CF 边缘终结，隧道段裸奔；`mode.py` 的 `effective_algorithm` 在 Cloudflare 下把 `none` 归一为 `auto`。**不存在明文 CF**（`none`+CF 历史模式已删除） |

### bearer 认证：能密封即认证

PC 公钥在此部署中不是公开密钥，而是**带外分发的 bearer 能力（等价于 token）**：只出现在二维码 fragment、从不发布，且每次建连由 `PrivateKey.generate()` 重新生成（`e2ee.py` 的 `SecureChannel`）。因此"能成功解封 `SealedBox`"本身就构成对发送方的认证——只有扫过该二维码的一方持有此公钥，能密封出 PC 私钥解得开的密文。

这属于**匿名 / bearer 认证**：证明"你是持有此二维码的一方"，不绑定特定设备身份；1:1（一台手机 ↔ 一台 PC）场景已足够。在此之上有更强一层：会话密钥由 ECDH 派生（PC 身份私钥 × 手机**临时**私钥，临时私钥从不出手机），会话内所有帧均为 AEAD，只有做过密钥交换的两方持有会话密钥——**即便二维码（PC 公钥）泄露，攻击者也无法伪造会话内帧**。

随机 `secret_path`（32 位 `token_urlsafe`）是**端点门禁**而非认证主体：让未授权客户端连 WS 入口都够不着（根路由 404）。它与 PC 公钥同处二维码、各司其职——`path` 挡"到达"，`pubkey` 认证"发送方"。

> corollary：公钥按 token 对待即有 token 级敏感性。泄露二维码意味着他人可发起 `auth`，但 (a) 公钥 per-session 生成，重新扫码即作废；(b) 无法伪造会话帧。风险可控，不应视作"无所谓公开"。

---

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
```

链条：**非对称只用于交换一次临时公钥，之后全程对称。**

### 3.3 auth 内层结构（加密层自带的最小编码）

解封后的内层是固定结构：

```json
{"algo": "xchacha20", "pk": "<手机临时 X25519 公钥 base64url>"}
```

- **内层编码刻意选 JSON**（两端 `string ↔ bytes` 各三行即可），**不依赖上层编解码器**——否则加密层反向依赖编码层，鸡生蛋：密钥交换发生时上层编码器还没轮到出场。上层将来用 msgpack 还是 CBOR，与这里无关。
- `algo` 只存在于密文内部，**从不明文传输**——暴露服务端支持的算法列表等于泄露能力面，没有必要。
- 失败路径只有两条：解封失败（`bad sealed box`）、`algo` 不在下发列表（`algo not offered`）。对应 WS close 4001（帧层行为见 `wire-protocol.md` §5/§7）。

### 3.4 调用侧：两段式，顺序不可逆

```python
algo, session_key = self._channel.key_exchange.handle_auth(auth_data)  # 先解 auth
self._provider = create_provider(algo, session_key)                    # 才知道建哪个 Provider
```

`KeyExchange` 在 `SecureChannel`（长期配置）层持有一个实例，与 `pc_private` 生命周期一致；每个 `SecureSession` 握手时调用 `handle_auth`，产出的 `session_key` 按连接隔离。

**收益**：新增算法只需写一个几十行的 `CryptoProvider` 子类并在 `create_provider` 注册，`KeyExchange` 一行不用改；X25519 交换代码从两份收敛为一处。老的 `algo == "none"`（token）分支随明文 CF 模式一并删除。

---

## 4. 对称封装：CryptoProvider

`CryptoProvider` 是**纯对称 AEAD 封装**：构造时直接拿 `session_key`，不碰 `pc_private`、不做 `SealedBox` / ECDH / KDF（那些归 §3 的 `KeyExchange`）。


### 4.1 三个算法

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
- **重置**：seq 状态随 Provider 实例生命周期。每个连接握手时新建 Provider（session_key 按连接隔离），`seq` 天然从 0 开始，通常无需显式 reset。若同一实例复用（rekey 沿用 Provider），`reset()` 把 `_tx_seq` / `_rx_seq` 归零，等价于重新初始化。
- **明文模式（`none` + LAN）不带 `seq`**：没有密钥就没有 MAC，seq 既无完整性也无机密性，无意义，故省略。
- **上下行各自独立计数器**：服务端校验上行、手机端校验下行，规则相同（严格 +1）。CF 隧道在边缘终结 TLS，下行重放需靠手机端 `seq` 校验挡掉。
- **边际价值**：会话密钥按连接 ECDH 派生，跨连接重放天然不可能；同连接内 WS 可靠有序，seq 主要防御 (a) CF 边缘节点这类"TLS 在别处终结"的中间重放，(b) 实现 bug 导致的帧乱序。成本仅 8 字节/帧 + 约 5 行，保留。

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

> 现状说明：本节描述的是目标形态。当前代码处于中间态——`SecureSession.wrap/unwrap` 仍是 JSON 信封 + base64 包裹 provider 输出；切 msgpack 时（`wire-protocol.md` §12 阶段 1-3）一并去掉 base64，让 provider 输出直接上帧。

---

## 7. 会话状态与安全原则

- **会话状态是权威**：`SecureSession.is_encrypted` 在握手时按连接确定，之后不变。接收端永远知道该解还是不该解，不需要"试一试"。
- **原则：绝不"解密失败就当明文"。** 解密失败的可能原因（对端模式不同、版本不同、中间人篡改）处理动作完全一致——丢弃 + 回 `error`。区分它们没有价值。
- **唯一明文帧是 `auth`**：会话密钥在服务端解出 `auth` 之后才建立，因此只有这一帧没有密钥可用；握手**成功**时从 `auth_ack` 起（含）全部整帧对称加密（含 `type`），握手**失败**时服务端不回消息层帧、直接关闭 WS（close 4001）——因此不存在"解密失败就当明文"的例外。实现上绝不能把 `auth` 帧塞进 `wrap()`。
- **`auth_ack` 无 data 字段**：手机能成功解开这一帧，本身就是"PC 持有正确会话密钥"的证明，无需再塞 `{"status":"OK"}`；成功帧也不回显 `algo`（握手时早已协商）。
- **判别不靠帧类型，靠会话状态机**：连接建立后按 `needs_auth` 决定期待 `auth` 还是 `hello`，之后按 `is_encrypted` 决定解不解密。（状态机表与 text 帧拒绝策略见 `wire-protocol.md` §10。）

---

## 8. 与 wire-protocol.md 的接口

消息层与加密层的全部接触面，收敛为四条：

1. **握手判定**：`needs_auth` = not (`none` and LAN)（`e2ee.py` 的 `SecureChannel`）。为真时消息层期待 `auth` 消息帧，把 `data`（bytes）交给 `KeyExchange.handle_auth`；成功后用返回的 `algo` + `session_key` 经 `create_provider` 建 Provider，`auth_ack` 起整帧走 Provider。失败 → WS close 4001 + reason（帧层语义见 `wire-protocol.md` §7）。
2. **数据帧**：消息层把编码后的字节交给 `provider.encrypt()`，密文原样上 WS binary 帧；收到 binary 帧交给 `provider.decrypt()`，把返回的字节交给编码层解析。
3. **错误映射**：`CryptoError` → `error(code:"decrypt")`；`ReplayError` → `error(code:"replay")`。统一丢弃 + 回 error，连续 N 次断连（code 表见 `wire-protocol.md` §7 的 error 小节）。
4. **明文模式**：`none` + LAN 时消息层直接编解码，不经加密层，也没有 `auth` / `auth_ack` 帧（握手由 `hello` 完成）。

---

## 9. 实现落点

| 文件                                       | 职责                                                   |
| ---------------------------------------- | ---------------------------------------------------- |
| `phonemic/tunnel/crypto/key_exchange.py` | `KeyExchange`：算法无关密钥交换（§3）                           |
| `phonemic/tunnel/crypto/errors.py`       | `CryptoError` / `DecryptError` / `ReplayError`       |
| `phonemic/tunnel/crypto/base.py`         | `CryptoProvider` ABC（§1.1 / §4）                      |
| `phonemic/tunnel/crypto/xchacha20.py`    | `XChaCha20Provider`（AAD 路径）                          |
| `phonemic/tunnel/crypto/nacl_box.py`     | `XSalsa20Provider`（前缀路径）                             |
| `phonemic/tunnel/e2ee.py`                | `SecureChannel` / `SecureSession`：装配、握手判定、needs_auth |
| `phonemic/resources/crypto_providers.js` | JS 端同构实现（密封 auth、seq 内化）                             |
| `phonemic/resources/mobile.html`         | `SecureClient`：JS 调用侧                                |

依赖：Python `pynacl`（已有）；JS `sodium.js`（已 vendor）。新增算法时：AAD 路径优先选支持 aad 的库（`cryptography` 的 AESGCM）；无 aad 的库走前缀路径。
