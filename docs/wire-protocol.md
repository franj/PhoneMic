# PhoneMic 通信协议设计（v1 · MessagePack）

状态：**§12 阶段 0–3、7、8 已落地**（编解码封装、加密层重构、三步握手、加密与认证解耦、HTTP 分片上传）；阶段 4–6 见 §12。本文档是手机端与服务端实现新协议时的唯一对齐依据。

**加密层设计已抽取为独立文档 `crypto-design.md`**——它内容无关（可承载 JSON / msgpack / CBOR 任意编码），本文档只保留消息层与加密层的边界约定，加密细节一律引用之。

---

## 1. 背景与目标

### 迁移前的状态（JSON over WebSocket）

手机端（`phonemic/resources/mobile.html`）与服务端（`phonemic/server/api.py`）之间目前是 JSON over WebSocket：

- 明文模式：`{"type":"send","text":"..."}`
- 加密模式：`{"type":"data","data":"<base64url(nonce+ciphertext)>"}`

服务端 `api.py:351` 只读取 `message["text"]`，PC 端 `PhoneMic.py:269-271` 按 `event_type` 分发。现有消息类型：

| 方向 | 现有类型 |
|---|---|
| 上行 | `preview`、`send`、`auth` |
| 下行 | `config`（`api.py:188 push_config`）、`reconnect`（`api.py:202 request_client_rescan`）、`auth_ack` |

> **已落地**：本文档定义的 msgpack 协议（§12 阶段 1–3，含三步握手）**已经实现**，上面这张表是迁移前的快照，保留它是为了说明「为什么要换」。当前类型清单见 §6。

### 为什么要换

下个版本要完成整个控制面板，新增 `key` / `mouse` 等消息类型，并预留 `file` / `photo`。继续用 JSON 有两个硬伤：

1. **二进制内容必须 base64**，体积 +33%，且 uvicorn 默认 `ws_max_size = 16MB`，单条 WS 消息超限会被直接拒绝——传文件/图片会提前撞上这个上限。
2. 每帧约 35 字节的信封开销。mouse 以 60fps 发送时约 2 KB/s，虽可接受，但没必要。

### 为什么不自己设计字节格式

字段编号、长度前缀、紧凑整数、版本演进——这些是 protobuf / MessagePack / CBOR 已经解决的问题，自造等于重复一遍且没有它们的成熟测试。protobuf 需要 `protoc` 代码生成，会侵入 `uv + Nuitka + NSIS` 构建链，对项目这十来个简单消息类型不划算。

**选 MessagePack**：自描述（像 JSON，无 schema）、无构建步骤、`bin` 是原生类型（免 base64）、Python 与 JS 都有成熟小体积实现。

---

## 2. 编码层：MessagePack

### 核心模型

> **一个 WebSocket 消息 = 一个 MessagePack 编码的 map。**

不需要 magic、不需要长度字段、不需要版本号字节——MessagePack 解包失败即"格式不对"，这正是它相对手写字节格式最大的便利。

### 全部消息统一用 msgpack，没有例外

**包括 `auth` / `auth_challenge` / `auth_proof`。** 其中**只有 `auth` 不能加密**（会话密钥尚未建立；到 `auth_challenge` 时密钥已就绪，整帧加密，见第 4 节），但三者都完全可以用 msgpack 编码——**"不能加密"和"不能用 msgpack"是两回事**。统一编码换来的好处：

- 全程只有一种编解码路径，`api.py` 里 `message.get("text")` 分支可以整个删掉；
- `auth` 的 `data`（sealed box 公钥）由 base64 字符串改为 `bin`，握手那次 base64 编解码也一并去掉。

因此 **WS 只使用 binary 帧**。收到 text 帧即判定为**未刷新的旧页面**，直接关闭连接——顺带成了版本检查。

> **关于措辞：是"未刷新的旧页面"，不是"旧客户端"。**
> `mobile.html` 由服务端下发（`api.py` 的 `html.replace` 注入），所以不存在"版本不一致的客户端"。
> 真正会出现的是**手机浏览器里还开着本次启动之前加载的页面**——PC 重启、升级或切换加密模式后，
> 该页面的代码与二维码 fragment 都属于旧实例。下文一律称**未刷新的旧页面**。

### 类型使用

| MessagePack 类型 | 用于 | 说明 |
|---|---|---|
| `map` | 每条消息本身 | 键为 `str` |
| `str` | 所有文本、字段名、**`type` 字段值** | 必须是合法 UTF-8 |
| `int` | 坐标、块号、文件大小 | 变长编码，小整数只占 1 字节（`seq` 不在此列——它由加密层承载，不进 msgpack，见 `crypto-design.md` §5） |
| `bin` | 文件/图片分块、`auth` 的 `data`、握手 `nonce` | **原生类型，不做 base64** |

文本与二进制可以在同一个 map 里混编，解码端按类型头精确还原，边界不会混淆：

```
{"type":"file", "a":"data", "id":7, "n":13, "chunk":<bin>}
   ↑str         ↑str        ↑int   ↑int    ↑bin 原始字节
```

### type 字段用字符串而不是整数

**理由不是"省掉枚举"，而是少一层定义**：每侧反正都要有一张 type → 处理函数的分派表，

```python
HANDLERS = {"send": on_send, "key": on_key, "mouse": on_mouse}
```

用整数时需要「常量表 + 分派表」两层，用字符串时**分派表的键本身就是常量**。

- **字节代价可忽略**：mouse 整帧字符串版约 30 字节、整数版约 20 字节，60fps 下差约 600 B/s。
- **方向校验自动化**：服务端分派表里没有 `config`，收到即回 `error`——不再需要"下行用 0x40 位段"这类约定。
- **代价**：拼写错误不会在发送端报错，靠接收端"未知 type"回 `error` 暴露。

### 字段名用完整拼写

`type` / `seq` / `text` / `dx` 而非 `o` / `s` / `t`。单帧多花约 3 字节（60fps 下 180 B/s），换取代码可读性。

### 必坑项

1. **Python 用 `bytes`、JS 用 `Uint8Array` 表示 bin。** 一旦误传字符串，会被按 UTF-8 编码，二进制数据**静默损坏且不报错**。这是本方案唯一会造成静默数据损坏的地方，必须在编码入口断言类型。
2. **显式传参，别依赖默认值**（msgpack-python 1.0 前后默认值变过）：
   ```python
   payload = msgpack.packb(frame, use_bin_type=True)
   frame   = msgpack.unpackb(raw, raw=False)   # 键均为 str，保持默认严格模式
   ```
   `raw=False` 让 `str` 类型解出 `str`、`bin` 类型解出 `bytes`；不传的话两者都变 `bytes`。
3. **整数精度**：JS 只有双精度（2⁵³）。文件大小、块号、坐标都在安全范围内，但**不要传纳秒时间戳之类的大整数**，Python 会编成 uint64，JS 端会变 BigInt 或丢精度。
4. **map 键顺序**：编码保留插入顺序，但不要拿编码后的字节做签名或 AAD 校验——如需，先约定规范化顺序。

---

## 3. 消息通用结构

### 公共字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | str | **必需**，见第 6 节 |

> **`seq` 不属于 msgpack 报文**：防重放的 `seq` 是加密层防护，不进 msgpack map，且完全在 `CryptoProvider` 内部维护，调用方不可见（`crypto-design.md` §5）。这样将来换掉 MessagePack（→ CBOR 等）也不需要改动 `seq` 的承载方式与状态。下文各消息的示例都不含 `seq`，因为它根本不出现在应用层报文里。

### 帧信封

```
WS binary 帧 = 加密层输出的密文字节（内部布局由算法实现决定，见 crypto-design.md §6）
```

- 加密层的输入输出都是字节：明文方向吃 `msgpack(map)`，密文方向原样上帧。密文内部布局（nonce、seq 承载路径）是加密层内部事务，编码层与 type 表都不感知。
- 加密覆盖**整个 map，包括 `type`**——元数据不泄露（攻击端看不出你在发 mouse 还是 file）。

---

## 4. 加密（边界约定，详见 crypto-design.md）

加密层的完整设计——威胁与信任模型、`KeyExchange` 密钥交换、`CryptoProvider` 对称封装、seq 防重放、算法实现、线上密文布局——见 **`crypto-design.md`**。本节只约定消息层看得见的部分。

**架构一句话**：`KeyExchange`（算法无关：X25519 `SealedBox` 解封 → 读 `algo` → ECDH → KDF）解出 `session_key`，交给对应 `CryptoProvider`（纯对称 AEAD，`encrypt(bytes) / decrypt(bytes)`，防重放 seq 完全内化）。加密层对上层编码不可见——本协议选 msgpack，加密层并不关心。

**加密永远开启，认证方式可选**（设计依据见 `e2ee-always-on-design.md`）：`auth_method` 取 `url_fragment`（QR 扫码）或 `tofu`（手动审批），Cloudflare 模式强制 `url_fragment`。没有"不加密"这一档。

| 帧 | 加密方式 |
|---|---|
| `auth`（URL fragment / TOFU 重连） | **明文帧**：`type` 明文（服务端要先知道这是 auth）；`data` 为 SealedBox 密文，内含 `algo` 与手机临时 X25519 公钥，均不出现在线上明文 |
| `auth`（TOFU 首次） | **明文帧**：手机尚无 PC 公钥、无从密封，故 `algo` / `pk`(bin) 两项明文（见 §5）。**不带识别码**——识别码由 PC 指派、密封下发（`e2ee-always-on-design.md` §5.5.1） |
| `sealed`（TOFU 首次第 2 步） | **明文帧**：`type` 明文，`data` 为 SealedBox(phone_public) 密文，内含 PC 指派给本连接的 4 位识别码与握手 `nonce` |
| `auth_challenge` 及之后所有帧 | **对称整帧加密**（含 `type`），Provider 输出直接上帧。**例外**：TOFU 首次的 `auth_challenge` 用 `SealedBox(phone_public)` 加密（此时手机还没有 Provider） |

- **会话状态是权威**：`is_encrypted` **恒为真**，接收端永远知道该解；**绝不"解密失败就当明文"**（安全论证见 `crypto-design.md` §7）。
- **CF 强制扫码认证**：Cloudflare 模式下 `effective_auth_method` 强制 `url_fragment`（公网可达 ⇒ TOFU 首次无信任锚，攻击者可抢先骗取审批），**不存在 TOFU+CF**。
- **`needs_auth` 恒为真**：所有连接都必须完成握手，**不存在"连上即进数据帧"的路径**。
- **`auth.data` 统一编码为 `bin`**：保证该字段类型唯一，接收端不需要按模式分支判断是 str 还是 bin。
- **握手失败分三类**（详见第 5 / 7 节）：**认证前**（解封失败 / `algo` 不在列表）此刻无密钥可用 → 不回消息层帧，直接 WS close 4001 + reason；**TOFU 审批被拒 / 超时** → WS close 4032；**认证后**（`auth_proof` 的 `nonce` 不符 / 超时）已有会话密钥 → 回**加密的** `error(code:"auth")` 再关闭。
- **信任模型**：PC 公钥 = 带外 bearer token，能密封即认证；TOFU 首次用"人工审批 + 4 位识别码核对"临时替代信任锚，审批后 PC 公钥存入手机 `localStorage` 成为后续的 token（`crypto-design.md` §2 / §3.5）。该识别码**由 PC 指派后密封下发**：它既不在 auth 帧里，也不在任何未经加密的字段里，手机全程不发送它（`e2ee-always-on-design.md` §5.5.1）。

### 4.1 密钥交换独立成类（算法无关）

公钥交换这一步——`SealedBox` 解封 → 读出 `algo` + 手机临时公钥 → ECDH → KDF 派生会话密钥——**对 XChaCha20 / XSalsa20 / AES-GCM 全部相同，纯 X25519，与具体 AEAD 无关**。因此它必须脱离 `CryptoProvider`，单独成一个算法无关的 `KeyExchange` 类：不解密 `auth.data` 就不知道 `algo`、不知道 `algo` 就无法实例化 Provider，而解封本身恰恰与算法无关——把 `SealedBox`/ECDH/KDF 塞进 Provider 等于要求它在构造前先解开自己的构造参数，逻辑上不成立。

接口、`auth` 内层结构（`{"algo","pk"}`，JSON 编码）、两段式调用顺序、新增算法流程与完整死锁论证见 `crypto-design.md` §3 / §4；seq 防重放的承载（统一为「明文前 8 字节」）与校验状态见其 §5。

---

## 5. 握手流程

### 5.1 URL fragment 认证 / TOFU 重连

```
手机                                                  服务端
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  明文帧  {"type":"auth","data":<bin SealedBox>}
 │◄─── auth_challenge ─────────────────────────────────│  加密帧  {"type":"auth_challenge","nonce":<bin>}
 │──── auth_proof ────────────────────────────────────►│  加密帧  {"type":"auth_proof","nonce":<bin>}
 │◄─── config ─────────────────────────────────────────│  加密帧  {"type":"config","key","value"}
 │◄─── error ──────────────────────────────────────────│  加密帧  {"type":"error","code":"auth"}     ← 仅 auth_proof 校验失败时
 │
 │  …… 正常数据消息 ……
```

### 5.2 TOFU 首次连接（无信任锚，需人工审批）

```
手机                                                  服务端
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  明文帧  {"type":"auth","algo":"xchacha20","pk":<bin>}
 │                                                      │  ← PC 随机指派 4 位识别码（每连接独立）
 │◄─── sealed ──────────────────────────────────────────│  明文帧  {"type":"sealed","data":<bin SealedBox(phone_pk)>}
 │  手机解封 → 大字显示识别码，**不回任何帧**
 │                                                      │  ← 主界面显示审批通知（同一个识别码 + 来源 IP）
 │                                                      │  ← 用户核对两边识别码一致 → 点「允许」
 │                                                      │  ← 批准后才做 ECDH + 建 Provider
 │◄─── auth_challenge ─────────────────────────────────│  SealedBox(phone_pk): {"data":<bin {pc_pk,nonce}>}
 │──── auth_proof ────────────────────────────────────►│  加密帧  Provider.encrypt({"type":"auth_proof","nonce":<bin>})
 │◄─── config ─────────────────────────────────────────│  加密帧
 │
 │  手机把 pc_pk 存入 localStorage（信任锚，后续重连走 5.1）
 │
 │  ── 拒绝 / 30s 未操作 / 被取代 / 手机掉线 ─────────────►│  WS close 4032（reason= rejected / timeout / superseded / disconnected）
```

- `auth_challenge` **必须等审批通过才发**：它是 PC 公钥（= token）的唯一带内分发通道，提前暴露等于让未授权的连接方拿到 token 绕过审批（`crypto-design.md` §3.5）。同一条理由不适用于第 2 步的 `sealed`——它里面只有识别码与 nonce、**不含 PC 公钥**，所以可以在审批前下发（而且必须：用户开始核对之前，手机上得先有一个码）。
- 审批在 ECDH **之前**：收到明文 auth 只解析字段，被拒绝的连接零密钥计算开销。
- 审批是**一条连接一个实例**、进队列后由用户按 id 结算（`e2ee-always-on-design.md` §7.4）：同一 IP 的新连接取代该 IP 的旧请求（`superseded`）；用户批准一条会作废其余待审批请求（`superseded`）；对端断开立刻摘除（`disconnected`）。这四个 reason 只是 close 帧的文案，**不改变线上帧格式**。
- 手机发完 `auth` 后等挑战的上限是 **35s**（= PC 审批超时 30s + 5s 容错），起算点是 `auth` 发出那一刻，因此第 2 步（纯下行一张帧，正常毫秒级返回）与第 4 步共用这同一份预算。`connectTimeout`（3s）只覆盖 WS 连接建立、在 `onopen` 时清除，不覆盖这段人工等待。
- PC 侧两次等待（`auth` / `auth_proof`）各自计一份 `AUTH_TIMEOUT`（10s），审批时长不占用任何一方——否则"用户点了允许、auth_proof 却立刻超时"。
- TOFU 重连（localStorage 有 PC 公钥）完全走 5.1，**不需要审批**：能密封 SealedBox 即持有 token。

### 5.3 通用规则

- 三条握手帧**必然出现**：`needs_auth` 恒为真，没有"一条握手帧都不发"的模式。
- `auth.data` 的密封内容与密钥交换流程见 `crypto-design.md` §3；本文档只定义三条握手帧的帧格式（见第 7 节）。
- **握手三步的理由**：`auth` 解封成功只证明"发送方持有 PC 公钥"，**不证明这一帧是刚发出来的**——把录下的 `auth` 原样重放同样解得开，而且重放者拿不到手机私钥、算不出 `session_key`，服务端却会照常承认它。因此解封成功后服务端必须给出一个**本连接现场生成的新鲜值**，客户端能回显它才算握手完成。
- **新鲜度不外发**：`nonce` 只活这一次握手，因此握手**不需要任何跨连接状态**——不需要客户端持久化任何东西（对比"轮换密钥对 / 盐 / 续期 token"这类方案，它们都要把新鲜度存进长期凭据，而那恰是唯一必须靠二维码带外分发的东西）。`nonce` 也不进 `SecureSession` 的字段，它是 `_handle_auth` 协程里的一个局部变量。
  - TOFU 首次是唯一例外：它落地的跨连接状态是 **`localStorage` 里的 PC 公钥**，那是**信任锚**而非新鲜度（每一次握手仍然需要现场 `nonce`）。
- **seq 语义不变**：`auth_challenge` 是下行的 0 号加密帧（客户端 `rxSeq=0`），`auth_proof` 是上行的 0 号加密帧（服务端 `rxSeq=0`），`config` 起为 1 号。TOFU 首次的 Provider 在 `init()` 时已建好但尚无密钥，收到挑战后两端各自 `reset()` 归零，因此计数器同样从 0 对齐。
- **没有版本协商帧**：`mobile.html` 由服务端下发（`api.py` 里 `html.replace` 注入），两端永远同版本，**不存在需要协商的版本号**——"服务端旧、页面新"这个方向在架构上不可能出现。
  唯一可能的错配是**手机上那个页面还没刷新**（本次启动之前加载的旧页面），而它由编码层直接识破：旧页面发 **JSON text 帧**，新协议只认 binary 帧，收到 text 帧即关闭连接并提示重新扫码（见 §2 / §10）。这条规则比"在握手里声明版本"更强——它作用在**每一帧**上。

---

## 6. type 表

`type` 取值为字符串。上行（手机 → 服务端）与下行（服务端 → 手机）不共用取值空间，由各侧分派表天然保证方向正确。

### 上行

| type | 说明 | payload 字段 |
|---|---|---|
| `auth` | 密钥交换（**必发**） | URL fragment / TOFU 重连：`data`(bin SealedBox)；TOFU 首次：`algo`(str)、`pk`(bin) |
| `auth_proof` | 回显握手 `nonce`，证明持有会话密钥（**必发**） | `nonce`(bin) |
| `preview` | 输入预览 | `text` |
| `send` | 文本上屏 | `text` |
| `key` | 模拟按键 | `keys` |
| `mouse` | 鼠标控制 | `a`、`dx`/`dy`/`btn`/`delta` |
| `upload_begin` | 发起一次文件／图片上传（①阶段协商） | `id`、`ref`、`name`、`size` |
| `upload_cancel` | 中止上传：作废服务端的会话（**单向、幂等、无回帧**） | `sid` |
| `pong` | 心跳应答：回复下行的 `ping` | — |

### 下行

| type | 说明 | payload 字段 | 来源 |
|---|---|---|---|
| `auth_challenge` | 握手挑战：给出本连接的 `nonce` | URL fragment / TOFU 重连：`nonce`(bin)；TOFU 首次：`data`(bin SealedBox `{pc_pk,nonce}`) | 迁移已有 |
| `sealed` | TOFU 首次：PC 指派给本连接的 4 位识别码（含同源 `nonce`） | `data`(bin SealedBox `{pin,nonce}`) | 新增（`e2ee-always-on-design.md` §5.5.1） |
| `config` | 单键配置推送 | `key`、`value` | 迁移 `push_config` |
| `reconnect` | 要求重新扫码 | `reason` | 迁移 `request_client_rescan` |
| `rekey` | 密钥轮换 | — | 预留 |
| `error` | 错误通知（**连接级**：解密／重放／畸形帧） | `code`、`msg` | 新增 |
| `upload_ready` | 上传①阶段通过：下发会话号、片大小与两把一次性钥匙 | `id`、`sid`、`chunk`、`saved`、`expires`、`k_mac`、`k_body` | 新增 |
| `upload_error` | 上传①阶段被拒（**流程内**，要跟 `id` 对号） | `id`、`code`、`msg` | 新增 |
| `status` | 状态同步 | `muted`、`mode` | 新增 |
| `ping` | 应用层心跳探测（促对端回 `pong`） | — | 新增 |

心跳走**应用层 `ping` / `pong`**（见 §7），原生 WebSocket ping/pong **已关闭**（`uvicorn` 的 `ws_ping_interval=None`）。原方案（原生心跳）在慢链路的大文件传输中会把一条健康的连接判死，机理见 §7。

上传的四条控制帧是**流程内**的：它们要跟 `id`（传输号）或 `sid`（会话号）对号，因此与连接级的 `error` / `status` 分开取值空间——完整定义见 §9。文件与图片的**数据面不在 WS 上**（HTTP 分片 `PUT`），所以这张表里只有控制帧、没有数据帧。

> 断线重连策略：**不做地址重定向**。连接断开后手机端重新扫码连接即可，因此 type 表里不需要 `redirect` 这一项。

---

## 7. 各 type 详细定义

### auth / auth_challenge / auth_proof

三步握手，**线上帧的明文部分只有第一步**（TOFU 首次同样是第一步明文，只是载荷形态不同）：

```
URL fragment 认证 / TOFU 重连：
c→s  {"type":"auth", "data":<bin SealedBox>}                  ← 明文帧（内容为密文）
s→c  {"type":"auth_challenge", "nonce":<bin>}                 ← 加密帧
c→s  {"type":"auth_proof", "nonce":<bin>}                     ← 加密帧

TOFU 首次（无信任锚）：
c→s  {"type":"auth", "algo":"xchacha20", "pk":<bin>}           ← 明文帧（两项均明文，**不含识别码**）
s→c  {"type":"sealed", "data":<bin SealedBox>}          ← 明文帧（内容为密文：PC 指派识别码 + nonce）
c→s  （手机解封后只显示，**不回任何帧**）
s→c  {"type":"auth_challenge", "data":<bin SealedBox>}        ← 明文帧（内容为密文，审批通过后才发）
c→s  {"type":"auth_proof", "nonce":<bin>}                     ← 加密帧
```

**第一步 `auth`（明文）**

`data` 是用 PC 公钥 `SealedBox` 密封的**不透明字节**，消息层不解释其内容——内层结构 `{"algo","pk"}`（JSON 编码）、`algo` 校验、ECDH/KDF 全在 `crypto-design.md` §3。要点：

- `algo` 取自二维码 fragment 的 `a=` 列表，服务端解封后校验其属于下发列表。**`algo` 只在密文里出现，从不以明文传输**。
- 新协议已无 `none` 模式，因此 `data` 在认证路径下恒为 SealedBox 密文，不存在 token 分支。
- 解封成功**不等于**认证通过，它只建立会话密钥；认证要等第三步（见下）。
- **TOFU 首次没有 `data`**：手机还没有 PC 公钥、无从密封，故 `algo`(str) / `pk`(bin 32B 手机临时公钥) 两项明文。服务端以"帧里有没有 `data`"区分两条路径：有 `data` ⇒ 解封建 Provider；无 `data` ⇒ 只解析字段、指派识别码、等审批。判据不含 `auth_method`——TOFU 重连也走 `data` 路径（`crypto-design.md` §3.5）。

**第 1.5 步 `sealed`（TOFU 首次专属；明文帧，内容为 SealedBox 密文）**

服务端收到明文 auth 后立即下发：指派一个 4 位识别码，连同本次握手的 16 字节 `nonce` 一起用
`SealedBox(phone_public)` 密封：

```
s→c  {"type":"sealed", "data":<bin SealedBox(phone_public): {"pin":"3847","nonce":<b64>}>}
```

- **只有持有 `phone_private` 的一方读得到**——这是它即便走在明文链路上仍算"带外凭据"的唯一理由。
- 手机解封后**只做显示**，不回任何帧：链路上因此不存在可复制、可重放的识别码。
- 里面那个 `nonce` 就是第 2 步 `auth_challenge` 要复用的同一个值，客户端据此校验两条下行帧出自同一个对端；不符即拒绝，不进握手。
- 它可以在审批前下发，因为它**不含 PC 公钥**——PC 公钥（= token）仍然只在审批通过后才出现在线上。
- 手机上没有"发回去"这一步：解封失败即按握手失败处理（不要把间歇性解密失败当成"重试就好"的问题）。

**第二步 `auth_challenge`（加密；TOFU 首次为 SealedBox）**

服务端解封成功后立即下发，字段只有一个 `nonce`（TOFU 首次则打包在 SealedBox 里，附带 PC 公钥）：

- **`nonce` 是 16 字节随机值**（`bin`），由服务端现场生成，生命周期只有这一次握手。
- **只要求"每连接不同"，不要求"不可预测"**：重放者没有会话密钥，连 `nonce` 都读不到，猜中它毫无意义。要挡的是"录下 `auth` + `auth_challenge` + `auth_proof` 整段按序重放"——`nonce` 一旦复用，录下的 `auth_proof` 就会通过。
- 因此**不要把 `ts` 之类既有字段升格成它**：毫秒级时间戳原则上会碰撞（同一毫秒内的两条连接 → 同一个值），而且会把一个纯装饰字段悄悄变成安全关键字段，日后无人查得出来。
- 该 `nonce` 是**消息层的握手随机数**，与 `crypto-design.md` §6 密文布局里的 AEAD nonce（24 字节、由库自动生成、消息层不可见）没有关系。
- **它不重复"我已经承认你的材料"这件事**：手机能解开这一帧，本身就是"PC 持有正确会话密钥"的证明，不需要再加一句话；`nonce` 在这里的唯一职责是提供新鲜度。
- **TOFU 首次必须等审批通过才发**，且用 `SealedBox(phone_pk)` 加密 `{"pk":<PC 公钥>,"nonce":<16B>}`——此刻手机还没有 Provider，无法收对称加密帧；同时这也保证 PC 公钥（= token）不会在审批前泄露给对端。手机解封后即可得到 PC 公钥、派生会话密钥，并把 PC 公钥存入 `localStorage`。审批未通过时服务端发 `WS close: code=4032`，reason ∈ `rejected`（用户拒绝）/ `timeout`（30s 无操作）/ `superseded`（被同 IP 的新连接取代或被用户批准的别条清场）/ `disconnected`（对端已断开）。
- **TOFU 首次的 `nonce` 不再现场生成**：它复用第 1.5 步 `sealed` 帧里那一个——两条下行帧由此绑定在同一个对端上，手机检验二者逐字节相同后才进握手。

**第三步 `auth_proof`（加密）**

只回显 `nonce`，不含其它内容：

- **能回出一个"本轮才产生、且被会话密钥正确加密的值"，就是持有会话密钥的证明。** 加密本身已经承载了"我解得开你的挑战"。
- **手机在发出 `auth_proof` 之后即可认为通道已建立**（乐观，与 TLS 1.3 客户端 `Finished` 同形）。服务端随后下发的 `config` 是它的确认；若被拒，则会收到 `error(code:"auth")`。**不额外回一条"握手成功"帧**——那只会重复 `config` 已经传达的信息。
- `auth_challenge` / `auth_proof` 都是**整帧加密**（含 `type`），字段作为 map 键藏在密文内，不新增任何明文面。
- TOFU 首次的两端 Provider 都从 `seq=0` 起算：手机在 `init()` 时建 Provider（无密钥），收到挑战后派生密钥并 `reset()`；服务端在审批通过后建 Provider。因此 `auth_proof` 仍是双方的第 0 号加密帧。

**握手失败分三类**，判据是"此刻能不能加密 + 卡在哪一步"：

| 时机 | 触发 | 处理 |
|---|---|---|
| **认证前** | `SealedBox` 解不开、或解封后的 `algo` 不在下发列表 | **不回任何消息层帧**，直接 `WS close: code=4001, reason=<拒绝原因>`。原因由 `e2ee.py` 给出（`missing auth data` / `invalid auth data encoding` / `key exchange failed: …` / `algorithm '…' not allowed`），`api._close_reason()` 按**字节**裁到 100 以内——close 帧的 reason 上限是 123 字节，按字符裁会让非 ASCII 撑爆它 |
| **TOFU 审批** | 用户点「拒绝」，或 30s 内无操作，或该请求被同 IP 的新连接取代，或对端断开 | `WS close: code=4032`，reason ∈ `rejected` / `timeout` / `superseded` / `disconnected`。此时尚未做 ECDH、也未发 `auth_challenge`，**没有任何密钥可用**，故同样只能 close。手机端收到 4032 后停止自动重连并提示 |
| **认证后** | `auth_proof` 的 `nonce` 不符，或等不到该帧（超时） | 已持有会话密钥 → 回**加密的** `error(code:"auth")`，然后关闭连接 |

- 认证前用 close 帧而非明文 `auth_ack(rejected)`，是为了不破坏"绝不解密失败就当明文"的原则：手机端收到 `auth` 后等待的要么是**一条能解密的加密帧**，要么是**连接关闭**，无需"先试解密、失败再当明文解析"。close reason 在密钥建立前本就明文，泄露无害。
- 认证后用 `error` 帧而非 close reason：close reason 有 123 字节上限、部分中间商会丢弃，而 `error` 是加密的、可以带完整语义。一句话：**可加密就回 `error` 帧，不可加密就 close。**
- **两次等待各自计一份 10 秒预算**：`_handle_auth` 在收 `auth` 前算 `deadline = time.monotonic() + AUTH_TIMEOUT`（`e2ee.AUTH_TIMEOUT = 10`），收 `auth_proof` 时**重新计一份**。原因：TOFU 首次的审批等待（最长 30s）夹在两次等待之间，若共用一份预算，用户点到「允许」时 deadline 早已过期、`auth_proof` 会立刻超时。语义是"单次等待的上限"，不是"整轮握手的墙钟预算"。

> **为什么退役 `auth_ack` 这个名字**（而不是改语义沿用）：旧页面（本协议之前那版）判定握手成功的条件是「解出来的 `auth_ack.status === 'OK'`」。若新帧沿用 `auth_ack` 这个名字，**未刷新的旧页面会先显示"已连接"，等 `auth_proof` 超时后才被服务端踢掉**，界面来回抖；改名后它在解析阶段就失败，直接落到"请刷新页面"路径上——虽然按第 5 节它本该先被 text 帧规则拦下，但两道防线互不冲突。顺带丢掉的那条 `ts` 字段纯属装饰（旧客户端只查 `status`，服务端也不存它），**不要**把它升格成挑战 `nonce`。

### key

```
{"type":"key", "keys":"ctrl+z"}
{"type":"key", "keys":"ctrl+a, delete"}
```

`keys` 直接喂给 `phonemic/gui/keyboard.py:send_keys()`，格式与 `validate_key_sequence()` 一致：`+` 连接修饰键，`,` 分隔多个组合（最多 10 个）。**不做二进制编码**——符号键二进制化没有收益，且失去可读性。

**安全边界**：`key` 走 `send_keys()` → `validate_key_sequence()`，只能表达 pyautogui 键名，**无法执行程序**。除非将来新增 `exec` 这样的消息类型，否则手机端不具备命令执行能力。

> 面板快捷键按钮**直接发 `type` 为 `key` 的消息**，与 `VoiceCommand` 无关。`VoiceCommand` 是"语音文本 → 按键动作"的映射（如"确定" → enter），在 PC 端拦截 `send` 文本时生效，属 PC 本地配置，不进入本协议。

### mouse

```
{"type":"mouse", "a":"move",  "dx":12, "dy":-3}
{"type":"mouse", "a":"click", "btn":"left"}
{"type":"mouse", "a":"double","btn":"left"}
{"type":"mouse", "a":"down",  "btn":"left"}
{"type":"mouse", "a":"up",    "btn":"left"}
{"type":"mouse", "a":"wheel", "delta":-120}
```

- 采用**速度模型**（摇杆远快近慢），`dx`/`dy` 是**每帧相对位移像素**，由 `requestAnimationFrame` 循环驱动，约 60 次/秒。
- **加速曲线在手机端计算**：摇杆偏移 → 速度映射（具体曲线为客户端实现细节，如二次/指数映射）产出最终 `dx`/`dy`；PC 端只做 `moveRel(dx, dy)`，**不另做速度处理**。协议只规定 `dx`/`dy` 是已算好的相对位移，不规定曲线形状。
- `dx`/`dy` 为**整数**（像素）。客户端按帧算出的是小数，需自行做余量累加（发整数部分、留小数部分到下一帧），否则每帧截断会累积出可感知的速度偏差。
- PC 端已实现 `phonemic/gui/mouse.py`，用 pyautogui 的 `moveRel` / `click` / `doubleClick` / `mouseDown` / `mouseUp` / `scroll`，照搬 `keyboard.py` 的模式。
- **`double` 是独立动作，不拆成两帧 `click`**：双击判定依赖两次按下的时间间隔，手机 → WS → PC 这条链路的时延不可控，连发两帧 `click` 大概率被 OS 判成两次单击。由 PC 端用 `doubleClick()` 一次完成。

### config

```
{"type":"config", "key":"mobile_max_records", "value":50}
```

**单键单值**结构，迁移自 `api.py:188 push_config`。现有代码发的是扁平形式 `{"type":"config","mobile_max_records":50}`，新协议统一规范为 `key` / `value` 两字段，便于通用分派。

连接握手成功后服务端**主动下发一次**，**所有认证方式一致**——不挂在握手的任何一条消息上；它同时充当了"通道已建立"的确认（见 §7 握手小节）：

```
{"type":"config", "mobile_max_records":10}
```

- **`max_frame_size` 已退役**（09-23，随 WS 分块传输一起）：它当初存在的唯一理由是「手机端据此**收紧文件分块**上限」，而现在的片大小由服务端随 `upload_ready` 下发（§9.1）、客户端不再自算片大小 ⇒ 这个字段没有任何读取方。服务端 `uvicorn` 的 `ws_max_size`（`api.py:WS_MAX_FRAME_SIZE`，当前 16MB）仍然生效，但 WS 上现在只剩控制帧，撞不到它。
- **手机端必须容错**：认不出的 `key` 一律忽略——不要因为服务端多发了（或将来少发了）某个键而报错或产出 `NaN`。

> v1 **不下发面板按钮列表**，按钮集内置在 `mobile.html`。将来若要 PC 可配置，再加独立的 `[{label, keys}]` 结构，不复用 `VoiceCommand`。

### reconnect

```
{"type":"reconnect", "reason":"config_changed"}
```

语义：算法/模式切换导致 URL（随机路径、公钥、token）变化，旧连接重连必然失败。手机端收到后**停止自动重连、提示重新扫码**；服务端随后关闭连接（`api.py:228`）。

### error

`code` 用简单单词：

| code | 含义 | 客户端处理 |
|---|---|---|
| `auth` | 握手 `nonce` 校验失败（重放 / 实现异常） | 提示"请重新扫码"，断开后停止重连 |
| `decrypt` | 解密失败 / 密钥失效 | 提示"请刷新页面"，连续 N 次则断连 |
| `replay` | seq 未递增 | 丢弃，连续 N 次则断连 |
| `ratelimit` | 限流 | 退避重试 |
| `malformed` | 非法消息 / 未知 type | 记录日志，丢弃 |

> `version` / `mode` 两个码随 `hello` 一起删除，它们本来就是 `hello` 的专属产物。版本天然一致，无需协商；而"未刷新的旧页面"这一情形**只能在 WS close 里表达**——旧页面读的是 JSON，发给它的 msgpack `error` 帧它根本解析不了。该情形由 binary/text 分帧规则识破（见 §2 / §5 / §10）。

### status

服务端→手机的状态同步，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `muted` | bool | 麦克风静音状态 |
| `mode` | str | 当前隧道模式（`lan` / `cloudflare` / `none`） |

完整语义随阶段 5（§12）落地时细化，v1 先定字段。

### ping / pong

应用层心跳，**替代**原生 WebSocket ping/pong（后者已在服务端关闭）。

```
下行  {"type":"ping"}
上行  {"type":"pong"}
```

无 payload 字段。方向**单向**：服务端发 `ping`，手机端收到即回 `pong`。手机端**从不主动发** `ping`。

**为什么放弃原生心跳**：原生心跳由 websockets 库的 `keepalive_ping` 实现，它只等**与本次 ping 匹配的那一个 pong 帧**——业务数据到达对它"一票都不投"。而 pong 与文件数据**同向**（手机 → 服务端）、共用同一条 TCP 有序通道 ⇒ 大文件上传时 pong 只能排在客户端发送缓冲之后。客户端水位 `HIGH_WATER` 为 4MB，链路低于约 130KB/s 时该队列排空即超过 30s 超时，服务端会把一条**正在正常传输的**连接判死（close 1011）。现象就是"链路健康、数据哗哗地传，看门狗却判死"的假死。（补充：文件与图片后来改走 HTTP 分片 `PUT`，见 §9 —— WS 上不再承载大块数据，`pong` 与业务数据争抢同一条有序通道这件事从根上消失了。本节的应用层保活仍然必要：正因为 WS 现在只剩控制帧，上传期间它反而更安静，判活更依赖这条链路。）

**应用层判活为什么能根治**：判活依据从"pong 回来了吗"换成"**有没有收到任何对端消息**"。任何上行活动都是存活证据——文本、按键、鼠标，以及**一次上传的每一片 `PUT`**（HTTP 活动刷新的是同一个时间戳，§9.7）⇒ 对端忙的时候服务端**根本不发 ping**，不存在与业务数据争抢同一条有序通道的问题。

服务端判活规则（阈值定义在 `phonemic/server/api.py` 模块级常量）：

| 状态 | 动作 |
|---|---|
| 收到任意帧；或命中一次上传分片的 `PUT` | 刷新 `last_msg_ts`，不做任何心跳动作 |
| 空闲 ≥ 45s | 发一帧 `{"type":"ping"}`；此后每轮检查都重发，直到收到消息或判死 |
| 空闲 ≥ 60s | 判死，`close(code=1011, reason="keepalive ping timeout")` |

- **阈值取 45s / 60s 的原因**：必须**小于 Cloudflare 的 WebSocket idle 超时**（约 100s 无数据即掐断），否则会出现"CF 已掐断、服务端还以为连着"的窗口期。45s 发探测、60s 判死，留足余量；同时也必须小于手机端可能的最长静默期。
- **关闭码沿用 1011**：与原生心跳超时的码保持一致，手机端既有日志解析（`1011 = 服务端心跳超时`）无需改动。
- **`pong` 必须进服务端白名单**（`_handle_client_message`）：否则每收到一次心跳应答都会回一帧 `error: malformed`，两端互刷告警。
- **上传分片的 HTTP 活动必须刷新同一个时间戳**：不然一条健康的大片上传（CF 上 15MB 一片约 2 分钟）会先在 60s 时把 WS 判死，再被「WS 关闭 ⇒ 该连接名下上传会话全部作废」那条规则杀掉（§9.7）。上传会话为此持有连接的判活对象。
- **对手机端的最低要求**：注册 `on('ping')` 并回一帧 `pong`。缺这一步，空闲连接会在 60s 后被服务端判死。
- **待补（0.6.2）**：手机端目前没有任何自己的判活逻辑（只依赖 `onclose` / `onerror`），静默断网且连接空闲时可能长时间显示"已连接"。原生心跳关闭后这层感知更弱，需补"多久没收到服务端消息即自判重连"。

## 8. seq 与防重放（见 crypto-design.md §5）

`seq` 是**加密层防护**，不是消息内容：它**不进 msgpack map**，计数、承载、校验全部在 `CryptoProvider` 内部完成，外部（帧编解码层 / `SecureSession`）完全感知不到其存在。这正是不把加密设计写死在本协议里的原因之一——将来换掉 MessagePack（→ CBOR 等），`seq` 的承载方式与状态**零改动**。

对消息层的影响收敛为一处：解密异常按类型映射为 `error` 的 code——`DecryptError` → `decrypt`、`ReplayError` → `replay`（接收端状态表见第 10 节）。承载路径（两种算法统一为「明文前 8 字节」）、单调校验、reset 语义与边际价值分析见 `crypto-design.md` §5。

---

## 9. 文件与图片上传（HTTP 分片）

一次上传由三段拼成，**数据面不在 WebSocket 上**：

| 段 | 承载 | 内容 |
|---|---|---|
| ① 协商 | WS | `upload_begin` →（`upload_ready` \| `upload_error`） |
| ② 数据 | HTTP | 逐片 `PUT /api/upload/<sid>`，一片一个请求 |
| ③ 终止（可选） | WS | `upload_cancel`，单向、幂等、无回帧 |

片大小、为什么不能继续用 WS 传、CF 上的两重瓶颈，见 `http-upload-design.md` §1–§3；本节只定**线上的样子**与会踩的坑。

### 9.1 四条控制帧

四个 type 都**平铺在帧顶层**（不加 `payload`，见 §3「帧信封」），且整帧加密（加密永远开启）。

**上行**

```
{"type":"upload_begin", "id":7, "ref":"file", "name":"a.pdf", "size":1048576}
{"type":"upload_cancel","sid":"<sid>"}
```

| 字段 | 含义 |
|---|---|
| `id` | 传输号：**连接内从 1 单调递增**的整数 |
| `ref` | `"file"`（落盘）或 `"photo"`（写系统剪贴板）。取 UI 上的按钮名（`doc`/`image`）是错的 |
| `name` | 原文件名。`ref:"photo"` 时可省略（剪贴板没有文件名概念） |
| `size` | **明文**总字节数（不是密文长度、不是片数）；`0` 表示空文件 |
| `sid` | 上传会话号。只有服务端产生，只在 `upload_ready` 里下发；客户端从不自己造 |

**下行**

```
{"type":"upload_ready", "id":7, "sid":"<sid>", "chunk":15728640,
 "saved":"a.pdf", "expires":300, "k_mac":<bin 32>, "k_body":<bin 32>}

{"type":"upload_error", "id":7, "code":"too_large", "msg":"..."}
```

| 字段 | 含义 |
|---|---|
| `id` | 回显请求的 `id`，客户端据此与那次传输对号（**必须比对，不能只看类型**：上一次取消留下的迟到帧会冒充这一次） |
| `sid` | 上传会话号：`PUT /api/upload/<sid>` 的路径参数，也是 `X-Pm-Mac` 的被签内容之一 |
| `chunk` | 服务端建议的**片大小**（明文字节）。客户端**不自算**，直接拿它切片（当前 15MB） |
| `saved` | 服务端已分配好的最终文件名（重名已改号，见 §9.7）。可能与 `name` 不同 |
| `expires` | 会话 TTL（秒）。信息性字段，客户端不据此设定时器 |
| `k_mac` | 请求认证密钥，32 字节二进制 |
| `k_body` | 片体加密密钥，32 字节二进制 |

`upload_error.code`：

| code | 触发条件 | 客户端归类 |
|---|---|---|
| `bad_ref` | `ref` 不在 `{"file","photo"}` 里 | 参数不对，**不给重发入口** |
| `bad_args` | `size` 不是非负整数／`name` 类型不对／建会话抛 `ValueError` | 同上 |
| `too_large` | `ref:"photo"` 且 `size > 32MB`（图片要整图驻留内存才能写剪贴板） | 太大了，**不给重发入口** |

- **`msg` 只进日志，客户端不显示**：界面文案由客户端按 `code` 走自己的语言包；直接显示会让文案语言跟着电脑走，与手机界面不一致。
- **为什么单开 `upload_error` 而不复用 `error`**：`error` 是**连接级**通道（`code` 取 `decrypt`／`replay`／`malformed` 那一类），而上传的失败面是**流程内**的 —— 要跟 `id` 对号、要跟那条进度气泡联动。这也解释了为什么**没有** `error(code:"upload")` 这个写法。
- **`upload_error` 只在 ① 阶段出现**：②阶段的失败一律走 HTTP 状态码（§9.3），不占 WS 的帧空间。

### 9.2 时序

正常：

```
手机                                   服务端
 │ upload_begin ───────────────────────▶  校验 ref/size/name、大小上限
 │                                       建会话：sid + 两把钥匙 + 分配落盘名 + 建 .part
 │ ◀─────────────────────── upload_ready
 │ PUT /api/upload/<sid>  (offset 0) ───▶  验签 → offset 判定 → 解密 → 写 .part
 │ ◀─────────────────────── {"received":N}
 │            … 逐片重复（**串行**：上一片响应回来才发下一片）…
 │ PUT /api/upload/<sid>  (最后一片) ───▶  收齐 → sink.finish() 改名落盘
 │ ◀──────────── {"received":size,"done":true,"saved":"a.pdf"}
```

- **②阶段的失败不需要协商**：非 200 就是终止信号，客户端停下并落一条气泡（§9.6）。
- **①阶段失败**（`upload_error`）时**一片都不会发**，服务端连会话都没建（`too_large`/`bad_ref`/`bad_args` 都在建会话之前判）。
- **取消**（③）：手机端发一帧 `upload_cancel{sid}` + 本地 `xhr.abort()`，**界面立刻解锁**。取消是**单向**的：不等回帧、不做探活兜底，失败也不致命（TTL 兜底，§9.5）。

### 9.3 HTTP 端点

```
PUT /api/upload/<sid>
Content-Type: application/octet-stream
X-Pm-Offset: <本片在文件中的明文起始偏移>
X-Pm-Len:    <本片明文长度>
X-Pm-Mac:    <签名，URL-safe base64 无 padding>
Content-Length: <X-Pm-Len + 48>   ← 浏览器自动填，服务端校验
```

- **路径不带页面前缀**（09-23 定）：固定就是 `/api/upload/<sid>`，客户端必须写**根绝对路径**。理由：上传 URL 会出现在**每一片**的请求行与访问日志里，带上 `/{secret}/` 等于把入口前缀持续广播出去；`sid` 是 24 字节真随机，做选择器足够。⇒ 代价是 `api.py::_normalize_path` 的**两条分支**都要给它前缀放行（加密模式的 secret 分支 + 裸 URL 的精确白名单分支），**漏一条静默 404**。
- **不要用相对路径**：在 `/{secret}/` 页面下相对路径会变成 `/{secret}/api/upload/<sid>`，落进另一条分支。
- **不用 `POST`，也不用 `DELETE`**：分片的定位信息（offset）天然是「往哪个位置写」的语义，`PUT` 才幂等；取消已改走 WS 帧，故 `DELETE` 不放行。

响应表：

| 状态 | 含义 | 响应体 | 是否作废会话 |
|---|---|---|---|
| 200 | 收下（可能是 `done`） | `{"received":N}`，收齐时为 `{"received":N,"done":true,"saved":"..."}` | 否 |
| 200 | **重复片**：`offset + len <= received`，即这一片已经在盘上了 | `{"received":N}` | **否** |
| 400 | `Content-Length ≠ X-Pm-Len + 48`（头自相矛盾） | 空 | 是 |
| 401 | 缺少/格式错的头，或**会话不存在／已过期／签名不符** | 空 | **否**（红线） |
| 409 | offset 不是「当前该写的那一段」（跳片／部分重叠），或解密失败、明文长度不符 | `{"received":N}` | 是 |
| 413 | `len > chunk`，或 `offset + len > size` | 空 | 是 |
| 500 | 写盘失败（磁盘满等） | 空 | 是 |

两条不可写反的红线：

- **401 绝不碰会话**：LAN 上没有 TLS，`sid` 在 URL 里明文可见 —— 一个只知道 `sid` 的人随便发个签名错的 PUT，如果能作废会话，那**一个未认证的包就能毁掉别人正在传的文件**（纯 DoS）。
- **重复片只能「忽略」，不能「作废」**：否则录下一片重放即可 DoS。

拒绝时的行为按「**能不能拿出凭证**」分两类，分别对应 §6.1 那个 RST 坑：

| 分类 | 例子 | 处理 |
|---|---|---|
| 拿不出凭证 | 401 全部情形 | **一个字节都不读**、直接关连接 |
| 自己人但状态不对 | 400 / 409 / 413 / 500 | **先把 body 读完再回**，排空上限 = 片大小 + 64KB |

- 只读一点点就关是**错的**：「关连接时接收缓冲还有未读数据」本身就是 RST 的充要条件；Windows 上客户端看到的是 `WinError 10053`，表现为**概率性失败**。半吊子排空 = 白做：要么全读、要么不读。

### 9.4 请求认证（`X-Pm-Mac`）

```
被签内容 = "PUT\n<sid>\n<offset>\n<len>"
算法     = keyed BLAKE2b，32 字节摘要
  Python: hashlib.blake2b(msg, key=k_mac, digest_size=32).digest()
  JS:     sodium.crypto_generichash(32, msg, k_mac)
线上形态 = URL-safe base64 无 padding
比较     = 恒定时间（`hmac.compare_digest`）
```

- **三项缺一不可**：只签随机数的话，一份合法签名配上一个改过的 offset 就能覆盖文件的别的位置。
- **⚠️ 不要用 `crypto_auth`**：PyNaCl 1.6 的 `nacl.bindings` 里没有它（`crypto_onetimeauth` 也没有），服务端一调用就是 `AttributeError`。keyed BLAKE2b 才是两端都有的同一条构造。
- **头认证 ≠ body 加密**：MAC 只证明「这个请求是本次上传的持有者发的」，body 另有自己的 AEAD（§9.5）。两者用的是**两把不同的钥匙**（§9.6）。

### 9.5 body 密文布局

```
nonce(24) ‖ AEAD(片序号(8B 大端) ‖ 明文片) ‖ tag(16)
```

- **固定开销恒为 48 字节**，两种算法一致 ⇒ 线上长度恒等于 `X-Pm-Len + 48`，可精确预计算、不需要 chunked 编码。⚠️ 那 8 字节虽然由 provider 内部打上／剥掉，**但服务端必须把它算进 Content-Length 校验**：少了它，每一片合法请求都会被挡成 400（而且是「自己人状态不对」那一档，会顺带作废会话）。
- **片序号统一为「明文前 8 字节」，两种算法（XChaCha20 / XSalsa20）一致**：不用 AAD，也不改 `crypto/` 的接口。片序号的作用是让**同一会话内的片不可重排、不可换位**（换位置 ⇒ 解密失败）。
- **`decrypt()` 返回的已经是应用明文**：8 字节前缀在 provider 内部剥掉了。外面再剥一次就会吃掉正文的头 8 字节，且**只在片 ≥ 9 字节时才表现为内容错位**，极难发现。
- **每片独立加密**：片与片之间不共享 nonce，也不要求解密顺序（乱序在服务端被 offset 判定挡掉，见 §9.6）。

### 9.6 服务端判定顺序与三行表

判定顺序固定为 **验签 → offset 判定 → 解密**，不能颠倒：

```
offset < 0 或 len < 0              → 400，作废
session.done                       → 200 + done（幂等：只服务「最后一片的响应丢了、客户端重发」）
len > chunk                        → 413，作废
offset + len > size                → 413，作废
offset == received                 → 200，读写盘（**唯一允许进解密的一支**）
offset + len <= received           → 200，忽略（重复片）
其余（offset > received／部分重叠）→ 409，作废
```

- **为什么必须先判定再解密**：只有 `offset == received` 的片允许进解密，重复片与错位片在前两步就已处理掉 ⇒ provider 那句「seq 必须严格相等」在重复片上不会被触发，重放一片不会升级成「解密失败 ⇒ 作废」。
- **「解密成功但写盘失败」不能靠回退 `received` 重来**：`decrypt()` 一成功，provider 的 `_rx_seq` 就前进且不可回退，回退会让它与 `received` 永久错位、后面每一片都解不开。正确做法是**作废会话**（回 500）。同理，解密失败回 **409 而不是 401** —— 401 那一档有「绝不碰会话」的硬约束，而这里必须作废。
- **幂等收尾**：`session.done` 之后只回 200 + `done`，不接受任何新写入（body 密钥此刻已经摘掉了）。

### 9.7 会话生命周期

- **谁建**：①阶段 `upload_begin`（校验通过后立刻建，含分配落盘名、建 `.part`）。**一片都还没发时它就已经存在了** —— 这是「取消必须显式发帧」的原因：用户在「片与片的间隙」点取消时根本没有在途 PUT，靠 PUT 收尾去删是永远等不到的。
- **谁删**：唯一入口是 `UploadManager.abort_session()`（幂等）。四个触发点：③阶段的 `upload_cancel`、②阶段的拒绝分支、**WS 连接关闭**、TTL 扫描。
- **WS 断连 ⇒ 该连接名下所有会话立即作废**（`abort_for_conn`）。⇒ **上传会话的生命周期 ⊆ WS 连接的生命周期**，**不做重传、不做续传**：任何一片失败即放弃整条上传并提示用户重发。
- **TTL 兜底**：默认 300s，每收到**合法**分片就顺延（`touch_ttl`）。它只兜「连接还活着但谁也不动了」与服务端重启这类边角 —— 主力是连接级作废。扫描周期 30s。
- **HTTP 活动必须刷新 WS 判活**（§9.10 第 4 条）：否则一条健康的大片上传会在 60s 时被判死，然后被上面那条规则杀掉。
- ⚠️ **HTTP 连接断开（`ClientDisconnect`）不是作废理由**：客户端 `xhr.abort()` 或页面被切走时，在途 PUT 会以「对端关机」收场，服务端读到的是半截密文 —— 此时**既不解密、也不作废会话**，更不许把半截密文送去解密（那会记成一条 `decrypt` 失败，把「客户端走了」伪装成「解密失败」）。谁走的就让谁去清理：取消有 `upload_cancel`、断连有 `abort_for_conn`、都没有就等 TTL。
- **密钥卫生**：收尾（`done`）后摘掉 `k_body` 与 provider，但**留着 `k_mac`**（幂等 `done` 那条路要靠它验签）；会话被作废时两把都摘。

### 9.8 两个 sink：file 落盘、photo 剪贴板

`file` 与 `photo` 的**线上格式完全相同**（同一套 ①／②／③ 流程，同样的字段），区别只在接收端的「落地方式」——这正是它俩必须拆成两个 `ref` 的根因：

- **`file` → 磁盘**：字节重组后写入本地文件（目录/命名见 §9.9）。**文件不进剪贴板** —— 即便是图片文件，也走 `file`（落到磁盘），不走高亮的 `photo`。
- **`photo` → 剪贴板**：字节重组后**直接写入系统剪贴板**，不写任何磁盘文件。设计目的就是「手机拍一张 → 电脑剪贴板里能直接 Ctrl+V 粘贴」。因此 `photo` 的 `name` 对剪贴板无意义（剪贴板里没有文件名概念），可省略或忽略；也正因为它要**整图驻留内存**，才有 32MB 那道上限。

> **跨平台剪贴板图片格式（实现注意，非协议层）**：剪贴板里放图不是「塞字节」那么简单，各平台有专属格式——这是 `photo` 必须独立于 `file` 的第二个硬理由（落地逻辑与平台强相关，和「写磁盘」是两套完全不同的代码路径）：
> - **Windows**：`CF_DIB` / `CF_DIBV5`（位图），或注册的 `PNG` 格式（`CFSTR_PNG` = `"PNG"`）。PhoneMic 技术栈是 PySide6/Qt，**实际实现走 Qt 剪贴板**（`phonemic/gui/clipboard.py:copy_image`）：`QImage.fromData(字节)` 解码后 `QApplication.clipboard().setImage()`，Qt 内部自动注册 `CF_DIB` / `CF_DIBV5` / `PNG` 多格式，粘贴进微信 / Word / 画图 / 浏览器均可用——不需要 pywin32 + Pillow。
> - **Linux**：X11 用 MIME 类型 `image/png`（`xclip -selection clipboard -t image/png`）；Wayland 用 `wl-copy --type image/png`。
> - **macOS**：`NSPasteboard` 的 `NSPasteboardTypePNG`（需 pyobjc 或 `osascript` 桥接）。
>
> 协议层只原样传二进制字节、不关心具体格式；实现层要按平台分支。当前 PhoneMic 仅 Windows 桌面，先实现 `CF_DIB` / `PNG`；Linux 支持（未来规划）时再补 `wl-copy` / `xclip` 分支。

### 9.9 file 接收端落地规则（实现约定）

- **落地目录**：`~/Downloads/PhoneMic/`，不存在则创建（含父目录）。v1 写死该默认值，不做配置项；将来加「用户指定目录」时只改接收端一处。
- **文件名**：沿用手机端上传的原文件名（取 `Path` 末段，剥离任何路径分隔符）。
- **重名自动改号**：目标已存在时按 `name(1).ext` → `name(2).ext` 递增；若已存在 `name(3).ext`，新文件取最大已用序号 +1（即 `name(4).ext`）。序号规则只看 `(n)` 后缀数字。
- **整段写入 + 收尾**（`phonemic/gui/file.py:FileSink`）：建会话时就 `open('<final>.part','wb')`，每收下一片 `write()` 一次，收齐后 `flush + fsync + 关句柄 + rename` 去掉 `.part`。改名前再查一次重名（建会话时分配的号码可能被中途占用）。
- **`.part` 也算「已占用」**：分配文件名时必须同时看 `<final>` 与 `<final>.part`，否则两个同名会话会双双选到同一个名字、写到同一个 `.part` 上（后开的那个 `open('wb')` 直接把前一个截断）。
- **异常清理**：`upload_cancel` / 任一片被拒 / WS 断开 → 关句柄、删 `.part`、丢弃会话（`abort()` 是幂等的）。

### 9.10 客户端行为约束

1. **必须用 XHR，不能用 `fetch`**：`fetch` 要拿上传进度得靠 `duplex:"half"` + `ReadableStream`，Safari / iOS / 微信内置浏览器都不支持，而手机正是主平台。
2. **串行逐片，不并发**：片级并发会让片乱序到达，也意味着内存峰值 × N。15MB 一片已让 RTT 部分可以忽略。
3. **进度 = 服务端确认的 `received` + 在飞那一片已发出的字节**：`received` 是「已落盘」的下界，在飞片的字节是唯一能观测到的、确定会落盘的量 —— 再往后（CF 回源、PC 内核、写盘）全是盲区。只允许领先**一片**，且**单调不减**。
4. **⚠️ 进度不要用 `event.total` 当分母**：iOS 18 WebKit bug #277286 在上传完成前会给出 2⁶⁴ 量级的值。分母用自己算的 `len`。
5. **100% 只由响应里的 `done: true` 解锁**：数据「已发出」≠「PC 已落盘」，提前满格正是「手机说成功、PC 还在写」那个观感的来源。
6. **全部片发完却没等到 `done` ⇒ 报「未确认」**：既不猜成功也不猜失败，并顺手作废那个不会再有进展的会话。等 `upload_ready` 也有一个保险超时（LAN 10s / CF 30s），超时同样报「未确认」。
7. **两条时限按链路取**（`FilePanel.LINK_LAN` / `LINK_CF`）：等 `ready` 局域网 10s / CF 30s；单片 PUT 兜底局域网 2min / CF 10min。它们兜的是「链路彻底卡死」，不是速度承诺 —— CF 上 15MB 一片实测约 2 分钟（上行整形 ≈120KB/s）。
8. **取消是单向的**：一帧 `upload_cancel` + 本地 `xhr.abort()`，**界面立刻解锁**，不等任何回执。取消不需要确认：服务端收到帧就删 `.part`，帧丢了 TTL 也兜底。
9. **取消恰好卡在「帧已发出、`upload_ready` 还没回来」时**：`ready` 到达后要补发一帧 `upload_cancel`（把服务端刚建出的空 `.part` 收掉），而不是让它干等 TTL。
10. **断连即在飞的那一片就地收尾**：服务端已把该连接名下的会话全部作废（§9.7），等它只会撞 401。断连信号必须用**事务级**的那一个（`WS_CLOSE`），不能用给界面看的 `WS_STATUS` —— 后者会被静默宽限期吞掉（见 §7「`ping` / `pong`」下那段说明）。
11. **等待用的订阅必须先于发帧挂上，用完即退**：①阶段的 `upload_ready`/`upload_error` 订阅挂在 `upload_begin` 之前（局域网一个来回只要几毫秒，先发后订会漏帧）；订阅随这次等待一起退掉，否则上一次取消留下的迟到帧会冒充这一次（再加上 `id` 比对兜一层）。

### 9.11 两类结果分别走哪里

- **①③阶段的失败**：走 WS 控制帧（`upload_error` / 断连信号），客户端本地渲染成**右侧结果气泡**。上传是**用户自己发起的动作**，结果是对这个动作的反馈，它本来就是「用户说」的消息 —— 左侧留白，只给将来真正的 PC 主动消息（电脑发文件 / 通知，见 §13）。
- **②阶段的失败**：走 HTTP 状态码，按上面的响应表映射成同一套气泡文案（401 → 「电脑上这次上传已失效」、413 → 「太大了」、409 → 「进度对不上」……）。
- **`done` 与其余收尾共用一条气泡通道**，靠文案里的图标（📄/🖼️）区分 `file` 与 `photo`；「必然被同样拒掉」的原因（`too_large` / `bad_args`）**不给重发入口** —— 让用户点进去再失败一次，比不给按钮更糟。

---

## 10. 接收端状态处理

| 会话期望 | 收到 | 判定 | 动作 |
|---|---|---|---|
| 认证前（等 `auth`） | 非 `auth` 帧 / 无法解包 | MALFORMED | **不回消息层帧**，关闭连接（close 1000） |
| 认证前（等 `auth`） | `auth` 解封失败 / `algo` 不在列表 | AUTH_REJECT | **不回消息层帧**，`close(4001, reason)` |
| 认证前（TOFU 等审批） | 用户拒绝 / 30s 超时 / 被同 IP 新连接取代 / 对端断开 | APPROVAL_REJECT | **不回消息层帧**，`close(4032, reason ∈ rejected/timeout/superseded/disconnected)` |
| 已认证 | binary 解包 / 解密成功（provider 内部已校验 seq 单调，外部不可见） | OK | 按分派表处理 |
| 已认证 | 解密成功、但明文前 8 字节的 seq 不等于期望值 ⇒ 抛 `ReplayError` | REPLAY | 丢弃，回 `error(code:"replay")` |
| 已认证 | 解密抛 `DecryptError`（MAC 失败：密钥错 / 密文或 nonce 被篡改） | DECRYPT_FAIL | 丢弃，回 `error(code:"decrypt")`；连续 N 次断连 |
| 加密（已发 `auth_challenge`、未收 `auth_proof`） | 收到**任何其它帧** | PENDING_PROOF | 一律丢弃（**不得执行**），回 `error(code:"auth")` 并断连 |
| 任意 | `type` 不在分派表内（含方向错误） | BAD_TYPE | 丢弃，回 `error(code:"malformed")` |
| 任意 | WS text 帧 | 未刷新的旧页面 | 关闭连接，手机端提示重新扫码 |

> 加密永远开启，因此**不存在"明文会话期望"这一行**：每一条连接都要先过握手，`is_encrypted` 恒为真。

> 补充：格式问题与编码方式无关，旧 JSON 协议里也有同样的洞——未刷新的旧页面发来 `{type:"data",...}`，`inner.get("text","")` 返回空串，`bridge.emit("send","")` 什么都不发生。**binary/text 分帧规则就是修掉它的东西**：旧页面发的是 JSON text 帧，新服务端收到 text 帧一律关闭连接并提示重新扫码。它作用在每一帧上，比"在握手里声明版本号"更强。

---

## 11. 依赖与构建

| 侧 | 依赖 | 说明 |
|---|---|---|
| Python | `msgpack>=1.1,<2` | **C 扩展**。Nuitka 一般能处理，但多一层风险；纯 Python 回退：`MSGPACK_PUREPYTHON=1` |
| JS | `@msgpack/msgpack` | vendor UMD 构建到 `phonemic/resources/`，与 `sodium.js` 同一做法 |

`pyproject.toml` 的 `dependencies` 需新增 `msgpack`。

**替代方案**：`cbor2`（纯 Python，打包风险更低）+ 更小的 JS 库，能力几乎等价。若 Nuitka 打包 `msgpack` 出问题，切 CBOR 的改动面仅限编解码函数。

---

## 12. 落地顺序

| 阶段 | 内容 | 验证方式 |
|---|---|---|
| 0 | 本文档评审通过 | review |
| 1 | `msgpack` 依赖接入 + 编解码封装（后端 `phonemic/tunnel/frame.py`、前端同构模块），**纯函数、不碰网络** | pytest + node 跑同一套测试向量 |
| 2 | 加密层重构（设计见 crypto-design.md）：新增 `KeyExchange` 类，`CryptoProvider` 收窄为纯 AEAD 封装（构造只收 `session_key`）；`create_provider` 改签名为 `(algo, session_key)`；`SecureChannel` 持有 `KeyExchange` 实例；`receive_auth` 改为两段式。删 `e2ee.py` 旧 base64 信封与 `make_auth_ack`（握手帧改名见阶段 3） | 单测：`handle_auth` 给定 sealed → 出正确 `session_key`；各 Provider `encrypt`/`decrypt` 往返 |
| 3 | 握手层：`auth` / `auth_challenge` / `auth_proof` / `config` / `error`，WS 全 binary 分流（三步握手流程见 §5、帧格式见 §7）。**已落地**；`hello` 已删、`auth_ack` 已退役 | 连上后看 config 回包；重放录下的旧 `auth` 被拒（`test_e2ee.py::TestAuthReplayProtection`、`test_e2ee_server.py::TestAuthHandshake::test_replayed_auth_cannot_steal_the_connection`） |
| 4 | 迁移 `preview` / `send` | 真机 |
| 5 | 新增 `key` / `mouse` / `status` | 真机（`key` / `mouse` 已落地，`status` 未做） |
| 6 | 面板 UI（按钮集内置） | 真机 |
| 7 | `file` / `photo` 分块 —— **已改为 HTTP 分片上传**（设计见 `http-upload-design.md`）：WS 只留四条控制帧（§9.1），数据走 `PUT /api/upload/<sid>`；旧的 WS 分块、`ack` 三级回帧、取消的三级等待、`hello` 探活与 `max_frame_size` 全部退役（§9.11） | 真机 + `tests/test_upload.py`（服务端）+ `tests/test_mobile.py`（客户端）+ `tests/test_file.py`（落盘） |
| 8 | 加密与认证解耦（设计见 `e2ee-always-on-design.md`）：移除 `none` / `PlainProvider` / token 路径，`auth_method` 取代 `e2ee_algorithm`，新增 TOFU 首次握手（明文 auth → PC 指派并密封下发识别码 → 审批 → SealedBox 挑战）与 `close 4032` | `test_e2ee.py`（三种握手路径＋窃听者一组）＋ `test_e2ee_server.py::TestTofuHandshake`（下发顺序 / 审批 / 拒绝 / 重连 / 审批慢于 AUTH_TIMEOUT）＋ `test_e2ee_server.py::TestTofuEavesdropper` ＋ 配置迁移测试 |

> **进度（2026-09-23）**：阶段 1–3、7、8 已落地（编解码封装、加密层重构、三步握手、加密与认证解耦、HTTP 分片上传），阶段 4–6 见各行内备注。

**第 1 阶段单独做**：编解码是纯函数，能完整进 pytest，正好补上"mobile.html 没有测试覆盖"这个洞；且后续接网络出问题时可确定不是编解码的锅。

---

## 13. 待定项

| # | 问题 | 现状 |
|---|---|---|
| 1 | `msgpack` C 扩展在 Nuitka 打包下是否顺利 | 未验证，失败则切 `cbor2` |
| 2 | 是否启用 AAD | **已定：不用 AAD（09-23）**。两种算法都把 `seq` 焊成**明文前 8 字节**，上传侧因此对「这次用的哪个算法」完全无感、`crypto/` 接口也不用动；细节见 `crypto-design.md` §5.1 |
| 3 | file 落地目录、photo 是否直接写剪贴板 | **已定：photo 直接写剪贴板、不落盘；file 落盘、不进剪贴板（图片文件也走 file）。file 落地目录 v1 写死 `~/Downloads/PhoneMic/`（自动创建），配置项后续再加（落地/命名规则见 §9.9）** |
| 4 | 面板按钮将来是否由 PC 下发 | v1 内置；插件化方案见 `panel-plugin-design.md`（声明式 JSON 面板，用户可让 AI 生成后放入插件目录） |
| 5 | 是否兼容未刷新的旧页面（旧 JSON 协议） | **已定：不兼容**。页面由服务端下发，不存在需要协商的版本号；旧页面发 JSON **text 帧**，新服务端收到 text 帧即关闭连接并提示重新扫码（§2 / §5 / §10）。因此不需要任何版本声明帧 |
| 6 | `photo` 是否并入 `file`（加 `dest` 字段：`file`/`clipboard`） | **已定：保持独立**。根因有二：① `file`→磁盘、`photo`→剪贴板是两条平台强相关的落地管线（剪贴板图片格式见 §9.8）；② `photo` 纯为剪贴板设计、不落盘，`file` 纯为磁盘、不进剪贴板，语义正交。代价多一条代码路径，可接受 |
