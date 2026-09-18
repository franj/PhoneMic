# PhoneMic 通信协议设计（v1 · MessagePack）

状态：**§12 阶段 0–3 已落地**（编解码封装、加密层重构、三步握手，含 `hello` 删除与 `auth_ack` 退役）；阶段 4–7 见 §12。本文档是手机端与服务端实现新协议时的唯一对齐依据。

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
明文模式：  WS binary 帧 = msgpack(map)
加密模式：  WS binary 帧 = 加密层输出的密文字节（内部布局由算法实现决定，见 crypto-design.md §6）
```

- 加密层的输入输出都是字节：明文方向吃 `msgpack(map)`，密文方向原样上帧。密文内部布局（nonce、seq 承载路径）是加密层内部事务，编码层与 type 表都不感知。
- 加密覆盖**整个 map，包括 `type`**——元数据不泄露（攻击端看不出你在发 mouse 还是 file）。

---

## 4. 加密（边界约定，详见 crypto-design.md）

加密层的完整设计——威胁与信任模型、`KeyExchange` 密钥交换、`CryptoProvider` 对称封装、seq 防重放、算法实现、线上密文布局——见 **`crypto-design.md`**。本节只约定消息层看得见的部分。

**架构一句话**：`KeyExchange`（算法无关：X25519 `SealedBox` 解封 → 读 `algo` → ECDH → KDF）解出 `session_key`，交给对应 `CryptoProvider`（纯对称 AEAD，`encrypt(bytes) / decrypt(bytes)`，防重放 seq 完全内化）。加密层对上层编码不可见——本协议选 msgpack，加密层并不关心。

| 帧 | 加密方式 |
|---|---|
| `auth` | **唯一明文帧**：`type` 明文（服务端要先知道这是 auth）；`data` 为 SealedBox 密文，内含 `algo` 与手机临时 X25519 公钥，均不出现在线上明文 |
| `auth_challenge` 及之后所有帧 | **对称整帧加密**（含 `type`），Provider 输出直接上帧（`none+LAN` 无此帧，全程明文） |

- **会话状态是权威**：`is_encrypted` 握手时确定、之后不变，接收端永远知道该不该解；**绝不"解密失败就当明文"**（安全论证见 `crypto-design.md` §7）。
- **CF 强制加密**：Cloudflare 模式下 `none` 被归一为 `auto`（`mode.py` 的 `effective_algorithm`），**不存在明文 CF**（历史模式 `none`+CF 已删）。因此 `needs_auth` 对所有非 `none+LAN` 连接为真。
- **`none` + LAN**：全程明文，**一条握手帧都没有**（`needs_auth` 为 False），连上即可发数据帧。
- **`auth.data` 统一编码为 `bin`**：保证该字段类型唯一，接收端不需要按模式分支判断是 str 还是 bin。
- **握手失败分两类**（详见第 5 / 7 节）：**认证前**（解封失败 / `algo` 不在列表）此刻无密钥可用 → 不回消息层帧，直接 WS close 4001 + reason；**认证后**（`auth_proof` 的 `nonce` 不符 / 超时）已有会话密钥 → 回**加密的** `error(code:"auth")` 再关闭。
- **信任模型**：PC 公钥 = 带外 bearer token，能密封即认证（`crypto-design.md` §2）。

### 4.1 密钥交换独立成类（算法无关）

公钥交换这一步——`SealedBox` 解封 → 读出 `algo` + 手机临时公钥 → ECDH → KDF 派生会话密钥——**对 XChaCha20 / XSalsa20 / AES-GCM 全部相同，纯 X25519，与具体 AEAD 无关**。因此它必须脱离 `CryptoProvider`，单独成一个算法无关的 `KeyExchange` 类：不解密 `auth.data` 就不知道 `algo`、不知道 `algo` 就无法实例化 Provider，而解封本身恰恰与算法无关——把 `SealedBox`/ECDH/KDF 塞进 Provider 等于要求它在构造前先解开自己的构造参数，逻辑上不成立。

接口、`auth` 内层结构（`{"algo","pk"}`，JSON 编码）、两段式调用顺序、新增算法流程与完整死锁论证见 `crypto-design.md` §3 / §4；seq 防重放的承载（AAD 优先 / 前缀兜底）与校验状态见其 §5。

---

## 5. 握手流程

```
手机                                                  服务端
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  明文帧  {"type":"auth","data":<bin>}
 │◄─── auth_challenge ─────────────────────────────────│  加密帧  {"type":"auth_challenge","nonce":<bin>}
 │──── auth_proof ────────────────────────────────────►│  加密帧  {"type":"auth_proof","nonce":<bin>}
 │◄─── config ─────────────────────────────────────────│  加密帧  {"type":"config","key","value"}
 │◄─── error ──────────────────────────────────────────│  加密帧  {"type":"error","code":"auth"}     ← 仅 auth_proof 校验失败时
 │
 │  …… 正常数据消息 ……
```

`none + LAN` 下一条握手帧都没有，连上直接进数据帧：

```
 │◄─── config ─────────────────────────────────────────│  明文帧（无握手，config 照样下发）
```

- 三条握手帧仅在 `needs_auth` 时出现。**`none+LAN` 一条都不发**（`e2ee.py:268` `needs_auth` 为 False），也就没有这里描述的任何失败路径。
- `auth.data` 的密封内容与密钥交换流程见 `crypto-design.md` §3；本文档只定义三条握手帧的帧格式（见第 7 节）。
- **握手三步的理由**：`auth` 解封成功只证明"发送方持有 PC 公钥"，**不证明这一帧是刚发出来的**——把录下的 `auth` 原样重放同样解得开，而且重放者拿不到手机私钥、算不出 `session_key`，服务端却会照常承认它。因此解封成功后服务端必须给出一个**本连接现场生成的新鲜值**，客户端能回显它才算握手完成。
- **新鲜度不外发**：`nonce` 只活这一次握手，因此握手**不需要任何跨连接状态**——不需要客户端持久化任何东西（对比"轮换密钥对 / 盐 / 续期 token"这类方案，它们都要把新鲜度存进长期凭据，而那恰是唯一必须靠二维码带外分发的东西）。`nonce` 也不进 `SecureSession` 的字段，它是 `_handle_auth` 协程里的一个局部变量。
- **seq 语义不变**：`auth_challenge` 是下行的 0 号加密帧（客户端 `rxSeq=0`），`auth_proof` 是上行的 0 号加密帧（服务端 `rxSeq=0`），`config` 起为 1 号——与旧的 `auth` / `auth_ack` 两步版完全一致。
- **没有版本协商帧**：`mobile.html` 由服务端下发（`api.py` 里 `html.replace` 注入），两端永远同版本，**不存在需要协商的版本号**——"服务端旧、页面新"这个方向在架构上不可能出现。
  唯一可能的错配是**手机上那个页面还没刷新**（本次启动之前加载的旧页面），而它由编码层直接识破：旧页面发 **JSON text 帧**，新协议只认 binary 帧，收到 text 帧即关闭连接并提示重新扫码（见 §2 / §10）。这条规则比"在握手里声明版本"更强——它作用在**每一帧**上，且 `none+LAN` 下同样有效。

---

## 6. type 表

`type` 取值为字符串。上行（手机 → 服务端）与下行（服务端 → 手机）不共用取值空间，由各侧分派表天然保证方向正确。

### 上行

| type | 说明 | payload 字段 |
|---|---|---|
| `auth` | 密钥交换（加密模式，仅 needs_auth） | `data`(bin) |
| `auth_proof` | 回显握手 `nonce`，证明持有会话密钥（加密模式，仅 needs_auth） | `nonce`(bin) |
| `preview` | 输入预览 | `text` |
| `send` | 文本上屏 | `text` |
| `key` | 模拟按键 | `keys` |
| `mouse` | 鼠标控制 | `a`、`dx`/`dy`/`btn`/`delta` |
| `file` | 传文件（分块） | `a`、`id`、`name`、`size`、`chunks`、`n`、`chunk` |
| `photo` | 图片写入系统剪贴板（分块，**不落盘**） | `a`、`id`、`name`、`size`、`chunks`、`n`、`chunk`（线格式同 `file`，`name` 对剪贴板无意义） |
| `pong` | 心跳应答：回复下行的 `ping` | — |
| `hello` | 探活请求：取消回执超时后的兜底（见 §9） | `t` |

### 下行

| type | 说明 | payload 字段 | 来源 |
|---|---|---|---|
| `auth_challenge` | 握手挑战：材料已解封，给出本连接的 `nonce` | `nonce`(bin) | 迁移已有 |
| `config` | 单键配置推送 | `key`、`value` | 迁移 `push_config` |
| `reconnect` | 要求重新扫码 | `reason` | 迁移 `request_client_rescan` |
| `rekey` | 密钥轮换 | — | 预留 |
| `error` | 错误通知 | `code`、`msg` | 新增 |
| `ack` | 传输确认（`data` 每块一帧、`end` 一帧、`cancel` 一帧） | `ref`、`id`、`a`、`received`、`n`（仅 `a:"data"`） | 新增 |
| `status` | 状态同步 | `muted`、`mode` | 新增 |
| `ping` | 应用层心跳探测（促对端回 `pong`） | — | 新增 |
| `hello` | 探活回执：原样回显上行 `hello` 的探活号 | `t` | 新增 |

心跳走**应用层 `ping` / `pong`**（见 §7），原生 WebSocket ping/pong **已关闭**（`uvicorn` 的 `ws_ping_interval=None`）。原方案（原生心跳）在大文件上传时会把一条健康的连接判死，机理见 §7。

`hello` 与心跳的区别：心跳是**连接级**保活（服务端发起、判死用），`hello` 是**事务级**探活（手机端发起、只用来给「取消」这个动作补一个端到端凭证），见 §9。

> 断线重连策略：**不做地址重定向**。连接断开后手机端重新扫码连接即可，因此 type 表里不需要 `redirect` 这一项。

---

## 7. 各 type 详细定义

### auth / auth_challenge / auth_proof

三步握手，**线上帧的明文部分只有第一步**：

```
c→s  {"type":"auth", "data":<bin>}                  ← 唯一明文帧
s→c  {"type":"auth_challenge", "nonce":<bin>}       ← 加密帧
c→s  {"type":"auth_proof", "nonce":<bin>}           ← 加密帧
```

**第一步 `auth`（明文）**

`data` 是用 PC 公钥 `SealedBox` 密封的**不透明字节**，消息层不解释其内容——内层结构 `{"algo","pk"}`（JSON 编码）、`algo` 校验、ECDH/KDF 全在 `crypto-design.md` §3。要点：

- `algo` 取自二维码 fragment 的 `a=` 列表，服务端解封后校验其属于下发列表。**`algo` 只在密文里出现，从不以明文传输**。
- 新协议已无 `none`+CF 明文模式，因此 `data` 在所有 `needs_auth` 会话里都是 SealedBox 密文，不再有 token 分支。
- 解封成功**不等于**认证通过，它只建立会话密钥；认证要等第三步（见下）。

**第二步 `auth_challenge`（加密）**

服务端解封成功后立即下发，字段只有一个 `nonce`：

- **`nonce` 是 16 字节随机值**（`bin`），由服务端现场生成，生命周期只有这一次握手。
- **只要求"每连接不同"，不要求"不可预测"**：重放者没有会话密钥，连 `nonce` 都读不到，猜中它毫无意义。要挡的是"录下 `auth` + `auth_challenge` + `auth_proof` 整段按序重放"——`nonce` 一旦复用，录下的 `auth_proof` 就会通过。
- 因此**不要把 `ts` 之类既有字段升格成它**：毫秒级时间戳原则上会碰撞（同一毫秒内的两条连接 → 同一个值），而且会把一个纯装饰字段悄悄变成安全关键字段，日后无人查得出来。
- 该 `nonce` 是**消息层的握手随机数**，与 `crypto-design.md` §6 密文布局里的 AEAD nonce（24 字节、由库自动生成、消息层不可见）没有关系。
- **它不重复"我已经承认你的材料"这件事**：手机能解开这一帧，本身就是"PC 持有正确会话密钥"的证明，不需要再加一句话；`nonce` 在这里的唯一职责是提供新鲜度。

**第三步 `auth_proof`（加密）**

只回显 `nonce`，不含其它内容：

- **能回出一个"本轮才产生、且被会话密钥正确加密的值"，就是持有会话密钥的证明。** 加密本身已经承载了"我解得开你的挑战"。
- **手机在发出 `auth_proof` 之后即可认为通道已建立**（乐观，与 TLS 1.3 客户端 `Finished` 同形）。服务端随后下发的 `config` 是它的确认；若被拒，则会收到 `error(code:"auth")`。**不额外回一条"握手成功"帧**——那只会重复 `config` 已经传达的信息。
- `auth_challenge` / `auth_proof` 都是**整帧加密**（含 `type`），字段作为 map 键藏在密文内，不新增任何明文面。

**握手失败分两类**，判据是"此刻能不能加密"：

| 时机 | 触发 | 处理 |
|---|---|---|
| **认证前** | `SealedBox` 解不开、或解封后的 `algo` 不在下发列表 | **不回任何消息层帧**，直接 `WS close: code=4001, reason=<拒绝原因>`。原因由 `e2ee.py` 给出（`missing auth data` / `invalid auth data encoding` / `key exchange failed: …` / `algorithm '…' not allowed`），`api._close_reason()` 按**字节**裁到 100 以内——close 帧的 reason 上限是 123 字节，按字符裁会让非 ASCII 撑爆它 |
| **认证后** | `auth_proof` 的 `nonce` 不符，或等不到该帧（超时） | 已持有会话密钥 → 回**加密的** `error(code:"auth")`，然后关闭连接 |

- 认证前用 close 帧而非明文 `auth_ack(rejected)`，是为了不破坏"绝不解密失败就当明文"的原则：手机端收到 `auth` 后等待的要么是**一条能解密的加密帧**，要么是**连接关闭**，无需"先试解密、失败再当明文解析"。close reason 在密钥建立前本就明文，泄露无害。
- 认证后用 `error` 帧而非 close reason：close reason 有 123 字节上限、部分中间商会丢弃，而 `error` 是加密的、可以带完整语义。一句话：**可加密就回 `error` 帧，不可加密就 close。**
- **两次等待共享同一个 10 秒预算**：`_handle_auth` 先算出绝对截止时刻 `deadline = time.monotonic() + AUTH_TIMEOUT`（`e2ee.AUTH_TIMEOUT = 10`），两次 `_recv_handshake_frame(websocket, deadline)` 都用它的**剩余量**作为 `asyncio.wait_for` 的超时。因此「每一步都拖到超时」不会把握手时长翻倍——整轮上限仍是 10 秒。

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

连接握手成功后服务端**主动下发一次**，**各加密模式一致**——不挂在握手的任何一条消息上，因此 `none+LAN`（无握手帧）同样收得到；加密模式下它顺带充当了"通道已建立"的确认（见 §7 握手小节）：

```
{"type":"config", "mobile_max_records":10, "max_frame_size":16777216}
```

- **`max_frame_size`**：服务端 WebSocket 单帧上限（字节），即 `uvicorn` 的 `ws_max_size`（`api.py:WS_MAX_FRAME_SIZE`，当前 16MB）。手机端据此**收紧**文件分块上限，但不会越过该链路的 `CHUNK_MAX`，见 §9。
  为什么必须下发：超帧会被服务端以 **close 1009 断开整条连接**（不是丢弃单帧），无法靠 `收到错误就换小块重试` 来兜底，只能两端对齐后主动限流。
- **手机端必须容错**：字段缺失或非法（非数字 / 非有限值 / 小于 512KB）一律视为 `未下发`，分块上限落到该链路的 `CHUNK_MAX`（局域网 1MB / Cloudflare 256KB），绝不允许产出 `NaN` 分块。

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

**为什么放弃原生心跳**：原生心跳由 websockets 库的 `keepalive_ping` 实现，它只等**与本次 ping 匹配的那一个 pong 帧**——业务数据到达对它"一票都不投"。而 pong 与文件数据**同向**（手机 → 服务端）、共用同一条 TCP 有序通道 ⇒ 大文件上传时 pong 只能排在客户端发送缓冲之后。客户端水位 `HIGH_WATER` 为 4MB，链路低于约 130KB/s 时该队列排空即超过 30s 超时，服务端会把一条**正在正常传输的**连接判死（close 1011）。现象就是"链路健康、数据哗哗地传，看门狗却判死"的假死。（补充：客户端后来加了「停等」节流，在途量 ≤ 1 块，pong 的最大排队长度也随之从 4MB 降到 1 块 —— 与本节的应用层保活共同消除了这个假死面。）

**应用层判活为什么能根治**：判活依据从"pong 回来了吗"换成"**有没有收到任何对端消息**"。文件数据块本身就是最强的存活证据 ⇒ 传输期间服务端**根本不发 ping**（时间戳一直被数据块刷新），不存在与业务数据争抢同一条有序通道的问题。

服务端判活规则（阈值定义在 `phonemic/server/api.py` 模块级常量）：

| 状态 | 动作 |
|---|---|
| 收到任意帧（含每个文件块） | 刷新 `last_msg_ts`，不做任何心跳动作 |
| 空闲 ≥ 45s | 发一帧 `{"type":"ping"}`；此后每轮检查都重发，直到收到消息或判死 |
| 空闲 ≥ 60s | 判死，`close(code=1011, reason="keepalive ping timeout")` |

- **阈值取 45s / 60s 的原因**：必须**小于 Cloudflare 的 WebSocket idle 超时**（约 100s 无数据即掐断），否则会出现"CF 已掐断、服务端还以为连着"的窗口期。45s 发探测、60s 判死，留足余量；同时也必须小于手机端可能的最长静默期。
- **关闭码沿用 1011**：与原生心跳超时的码保持一致，手机端既有日志解析（`1011 = 服务端心跳超时`）无需改动。
- **`pong` 必须进服务端白名单**（`_handle_client_message`）：否则每收到一次心跳应答都会回一帧 `error: malformed`，两端互刷告警。
- **对手机端的最低要求**：注册 `on('ping')` 并回一帧 `pong`。缺这一步，空闲连接会在 60s 后被服务端判死。
- **待补（0.6.2）**：手机端目前没有任何自己的判活逻辑（只依赖 `onclose` / `onerror`），静默断网且连接空闲时可能长时间显示"已连接"。原生心跳关闭后这层感知更弱，需补"多久没收到服务端消息即自判重连"。

### hello

```
上行  {"type":"hello", "t":1}
下行  {"type":"hello", "t":1}
```

`t` 是手机端自增的**探活号**，服务端**原样回显**。方向**单向**：手机端发，服务端回；服务端从不主动发 `hello`。

用途只有一个——**给「取消」补一个端到端凭证**（完整时序见 §9「取消的回执」）：手机端发出 `cancel` 后若迟迟等不到 `ack(a:"cancel")`，就发一帧 `hello`；只要回执能回来，就说明排在 `hello` 之前的 `cancel` 帧**已被服务端从 socket 读出**（同一条可靠有序通道，`_handle_client_message` 按收帧顺序执行）。

- **回显 `t` 而不是裸回一帧**：手机端可能因超时重发探活，迟到的旧回执会满足下一次等待。带号比对后，只认当前在等的那个号。
- **服务端侧等价于白名单 + 回帧**（`phonemic/server/api.py::_handle_client_message` 的 `hello` 分支）：漏了它，每帧探活都会落地成 `error: malformed`。
- **不参与保活判死**：收到 `hello` 同样刷新 `last_msg_ts`（任何帧都刷新），但它不承担保活职责——保活仍是服务端的 `ping` / `pong`。

---

## 8. seq 与防重放（见 crypto-design.md §5）

`seq` 是**加密层防护**，不是消息内容：它**不进 msgpack map**，计数、承载、校验全部在 `CryptoProvider` 内部完成，外部（帧编解码层 / `SecureSession`）完全感知不到其存在。这正是不把加密设计写死在本协议里的原因之一——将来换掉 MessagePack（→ CBOR 等），`seq` 的承载方式与状态**零改动**。

对消息层的影响收敛为一处：解密异常按类型映射为 `error` 的 code——`DecryptError` → `decrypt`、`ReplayError` → `replay`（接收端状态表见第 10 节）。承载路径（AAD 优先 / 8 字节大端前缀兜底）、单调校验、reset 语义与边际价值分析见 `crypto-design.md` §5。

---

## 9. 分块传输（file / photo）

子协议用 map 字段表达，**不需要设计字节子头**：

```
{"type":"file", "a":"start", "id":7, "name":"a.pdf", "size":1048576, "chunks":16}
{"type":"file", "a":"data",  "id":7, "n":0, "chunk":<bin chunkSize>}
{"type":"file", "a":"data",  "id":7, "n":1, "chunk":<bin chunkSize>}
{"type":"file", "a":"end",   "id":7}
{"type":"file", "a":"cancel","id":7}
```

- **分块大小按体积自适应，块数收敛到 ~12，夹在 [256KB, CHUNK_MAX]**（`FilePanel.pickChunkSize`）：
  `chunk = clamp(ceil(size / 12) 向上取整到 256KB 倍数, 256KB, cap)`，
  其中 `cap = max(256KB, min(CHUNK_MAX, config.max_frame_size − 64KB))`；服务端未下发 `max_frame_size` 时 `cap = CHUNK_MAX`。
  - **CHUNK_MAX 按链路取两档，判定只看 hostname**（`FilePanel.isCloudflare`，正则 `\.trycloudflare\.com$`，不区分大小写）：
    - `*.trycloudflare.com` → `CHUNK_MAX_CF = 256KB`。此时**上下限相等 ⇒ clamp 恒取 256KB**，与文件大小无关。
    - 其余一律按局域网 → `CHUNK_MAX_LAN = 1MB`（含局域网 IP、`localhost`、以及将来可能出现的自定义域名反代）。
    - 两档各自独立成常量，**调大小只改那一行**；`pickChunkSize` 本身不需要知道链路。
  - **为什么 CF 档压到 256KB**（2026-09-18 定稿）：CF 回源段窄、延迟高，大块只省 per-chunk 开销，代价却是进度粒度变粗、手机端内存峰值变高，**且把更多数据堆在途**——`end` 的确认要等这一坨全部排空才回得来（慢链路上这正是「手机报成功、PC 还在写」的直接来源）。局域网两段都短，取 1MB 即可。
  - **为什么要自适应而不是恒定上限**：恒定上限时 2MB 文件只有 2 块，每次 ack 跳半格，进度条像在跳变；收敛到 ~12 块后小文件也有小步可走，而大文件本来就会撞上限。
  - **上限可由服务端收紧**（见 §5 `config`）：下发值就是服务端 `uvicorn ws_max_size`（当前 16MB），扣掉边距后仍高于两档上限，所以只有服务端显式调小才生效。
  - **减 64KB 安全边距**：覆盖 msgpack bin 头（5B）+ nonce（24B）+ MAC（16B）+ seq（8B）。
  - **下限 256KB**：再小则每块固定开销（编解码、线程切换、进度条重排）占比过高。
  - 典型取值（局域网）：100KB → 256KB（1 块）、3MB → 256KB（12 块）、6MB → 512KB（12 块）、≥12MB → 1MB。
  - 典型取值（Cloudflare）：恒 256KB——16.9MB → 68 块、100MB → 400 块、1GB → 4096 块。
  - 块大小同时就是进度条「至多领先 ack 一块」的前瞻量 ⇒ **块越小前瞻越准**，这是 CF 档取 256KB 的额外收益。
  - 每块回一帧 ack；1GB 在 CF 档下约 4096 帧 ≈ 164KB 回程，可忽略。
- **顺序由 WebSocket 保证**（可靠有序），重组只需 `bytearray.extend`；`n` 仅用于进度显示与 ACK。
- **`id` 由手机端生成**：每连接内从 1 单调递增的整数，作为一次传输会话的标识；服务端按 `id` 路由 buffer，无需跨端协商。
- 接收端状态机：`start` 建 buffer → 每个 `data` 执行 `buf.extend(chunk)` → `end` 触发落盘（file）或写剪贴板（photo）。
- **`a:"cancel"`**：发送端主动中止。手机端发送期间 UI 锁定为「仅取消可点」，点取消即发此帧（同时本地立刻停发）；接收端关闭半成品文件、删除临时文件、丢弃会话状态，并发回 `ack(a:"cancel")`。
- **取消的回执**（`FilePanel._cancel` / `_awaitCancelAck`）：**界面不立刻解锁**，要等服务端回执才恢复可用——本地停发只说明「我不再发了」，服务端那边可能还在收队列里的残留块、还在写 `.part`，此时解锁会让用户以为已经干净了。三级等待，逐级放宽但**一定有终点**：
  1. `ack(a:"cancel")` —— 服务端已丢弃会话，板上钉钉（`CANCEL_ACK_TIMEOUT = 2s`）。
  2. `hello` 回执 —— ①超时后发一帧 `{"type":"hello", "t":N}`；回执带同一个号回来，即判定取消已送达（`HELLO_TIMEOUT = 2s`）。
  3. 硬超时 / 断连 —— 就地放行界面，绝不把用户永久锁在「正在取消」里；此时若连接已断，服务端侧的会话也会随连接一起被 `abort_all` 清掉。
  - 第 ② 步成立的前提是**同一有序通道**：`cancel` 先入队、`hello` 后被读出，能读到后者就说明先读到了前者；`cancel` 入队后必被后台消费者处理（若连接在中途断掉，`abort_all` 与取消等价）。
  - ②③ 只影响「多快解锁」，不影响「是否声称已取消」——三种出口落的是同一条气泡（`bubble_file_canceled` / `bubble_photo_canceled`），都带 `[FILE]` 日志。
  - **幂等**：`cancel` 落到不存在的会话（已 `end` / 已取消 / 从未 `start`）时同样回 `ack(a:"cancel")`。回执的语义是「取消已生效」，不是「这个 id 我认得」——这也是该路径此前只有一行 `cancel 无匹配会话` 日志、手机端无从判断的修复。
- **`ack` 分三级**：`data` 块**逐块回** `{"type":"ack", "ref":"file", "id":7, "a":"data", "n":0, "received":1048576}`；`end` 收齐落盘后再回 `{"type":"ack", "ref":"file", "id":7, "a":"end", "received":16900126}`；`cancel` 确认会话已丢弃后回 `{"type":"ack", "ref":"file", "id":7, "a":"cancel"}`（`photo` 线格式相同，`ref` 为 `"photo"`）。
  - `data` 级 ack 的语义是「这一块已写入 `<final>.part`」（`photo` 是「已进内存 buffer」），由写盘消费者在完成后发出 ⇒ 回程速率恰好等于真实落盘速率。
  - 它补的是发送端**观测不到**的那一段：`bufferedAmount` 只覆盖「浏览器 → 网络栈」，再往后（手机内核、无线在途、CF 回源、PC 内核、uvicorn、写盘）全是盲区。逐块 ack 让进度条终于能反映「PC 收到了多少」。
  - 它还有第二个用途：**停等闸门的放行信号**（见下「发送端节流」）。闸门的依据是 ack 里的 `n`（块序号），所以接收端**必须回 `n`** —— 尾块不满时从 `received` 推不出块号。
  - 成本可忽略：1MB 块下每帧 ack ≈ 40B，1GB 文件也只有约 40KB 回程；手机端由进度更新驱动重绘，不额外重排。
  - **`end` 级 ack 是唯一的成功判据**，发送端只有拿到它才报「成功」。
- **进度 = `ack 累计字节 + min(未确认在途量, 1 块)`，且单调不减**：`未确认在途量 = max(0, 本地已上网字节 − ack 累计字节)`，`本地已上网字节 = 累计 send 字节 − WebSocket.bufferedAmount`。
  - 真实交付量被**有序信道夹在两个可观测边界**之间：`ack 累计`是**下界**（PC 已落盘，板上钉钉），`本地已上网`是**上界**（再往后手机内核 → 无线在途 → CF 回源 → PC 内核 → uvicorn → 写盘全是盲区）。
  - **不取上界**：手机→CF 快、CF→PC 慢时上界瞬间饱和到 100%，这正是「进度条 99%、PC 才收到 2MB」的成因（2026-09-18 真机实测）。**`max(本地估算, ack)` 同样是取上界**——`max` 不限制上界能跑多远，任何「有信号就取大」的写法都会退回这个 bug。
  - **「至多 1 块的前瞻」必须有上限**：不加 cap 时式子退化为 `ack + (上界 − ack) = 上界`，等于没改。上限取块大小，用来吃掉一块的在途与 ack 回程延迟，避免 ack 未到时长时间停顿。若要最保守，把上限设 0（纯 ack）即可。停等生效后在途 ≤ `MAX_INFLIGHT` 块，这一项实际就等于「当前那一块在不在途」，前瞻不会再跑远。
  - 已知副作用（可接受）：块数很少的小文件，1 块前瞻占比偏大（3 块的文件最多显示到 33%）。
  - **不要**在 `send()` 返回后立即累加整块——`send()` 只是把数据排进浏览器缓冲就返回，会让上界瞬间满格、随后长时间不动，观感等同卡死（Cloudflare 场景下尤为明显）。
  - 发送端应在背压轮询（每 20ms 采样 `bufferedAmount`）中持续刷新；`bufferedAmount` 会让上界回落，故进度取历史最大值，只前进不后退。
- **100% 只由 `end` ack 解锁**：收到 `ack(a:"end")` 前进度封顶 99%，文案切「等待电脑确认…」。数据「已发出」≠「PC 已落盘」——提前给 100% 正是「手机报成功、PC 还在写」的观感来源。
- **`end` 前收齐校验**：`end` 时若 `received != size` 视为丢块，接收端删除半成品并回 `error(malformed)`，**不落截断文件、不写损坏图片**。
- **发送端等 `end` ack 的出口只有三个**（`FilePanel._waitEndAck`），**任何一条都不会把「没等到」当成成功**：
  - 收到 `ack(a:"end")` → 成功；
  - 收到 `error` 帧、或等待期间连接断开 → 失败；
  - 保险超时（3min，正常落盘远快于它）→ 如实报「未确认」——既不谎报成功，也不谎报失败，由用户核对 PC 后自行决定是否重发。
- **发送端节流：停等（stop-and-wait）为主，`bufferedAmount` 为兜底**。
  - **主线是停等**：发第 `n` 块之前，必须等到第 `n − MAX_INFLIGHT` 块已被 ack（`FilePanel._waitChunkAck`；`MAX_INFLIGHT = 1` 即严格「收到上一块 ack 才发下一帧」）。理由：`bufferedAmount` 只覆盖「浏览器 → 网络栈」，数据一旦离开浏览器（手机内核、无线在途、CF 回源、PC 内核、写盘）全是盲区 ⇒ 只看它会让浏览器侧一路「发得出去」而链路早已积压（表现为 `bufferedAmount ≈ 0` 却传得极慢、取消后很久才停）。等 ack 才是**端到端**节流：在途量被钉死在 `MAX_INFLIGHT` 块以内，取消延迟也从「在途积压 ÷ 吞吐」降到「1 块 ÷ 吞吐」。
  - **代价**：吞吐上限 ≈ `MAX_INFLIGHT × 块大小 ÷ RTT`。局域网几乎无感（1MB ÷ 5ms）；Cloudflare 上 256KB ÷ 120ms ≈ 2MB/s。想更快就调大 `MAX_INFLIGHT` —— 那仍是「在途 ≤ N 块」的滑动窗口，不是退回无节制流水线。
  - **兜底仍是 `bufferedAmount`**：超过 `HIGH_WATER`（4MB）暂停发送、降到 `LOW_WATER`（1MB）再继续（`FilePanel._waitDrain`，每 20ms 采样）。停等生效时它基本不会被触发。
  - **ack 超时必须降级**（`CHUNK_ACK_TIMEOUT = 15s`，且**只降级一次**）：ack 通道若失灵，整体退回流水线并置 `_gateGaveUp`，而不是把传输拖成「1 块 / 超时」。协议层面 ack 走同一条可靠有序通道，正常不会丢，这条纯粹是防死锁。
- **发送期间 WS 单会话单文件**：一次连接同时只进行一次 `file` 传输（`start` 后未 `end`/`cancel` 前收到新 `start` 视为协议错误，回 `error(malformed)`）。v1 不做队列。

### 9.1 两个 sink：file 落盘、photo 剪贴板

`file` 与 `photo` 的**线格式完全相同**（同一套 `start`/`data`/`end` 子协议、`id`/`size`/`chunks`/`n`/`chunk` 字段），区别只在接收端的"落地方式"——这正是它俩必须拆成两个独立消息类型（type）的根因：

- **`file` → 磁盘**：字节重组后写入本地文件（路径/目录见 §13 #3）。**文件不进剪贴板**——即便是图片文件，也走 `file`（落到磁盘），不走高亮 `photo`。
- **`photo` → 剪贴板**：字节重组后**直接写入系统剪贴板**，不写任何磁盘文件。设计目的就是"手机拍一张 → 电脑剪贴板里能直接 Ctrl+V 粘贴"。因此 `photo` 的 `name` 字段对剪贴板无意义（剪贴板里没有文件名概念），可忽略或省略。

> **跨平台剪贴板图片格式（实现注意，非协议层）**：剪贴板里放图不是"塞字节"那么简单，各平台有专属格式——这是 `photo` 必须独立于 `file` 的第二个硬理由（落地逻辑与平台强相关，和"写磁盘"是两套完全不同的代码路径）：
> - **Windows**：`CF_DIB` / `CF_DIBV5`（位图），或注册的 `PNG` 格式（`CFSTR_PNG` = `"PNG"`）。PhoneMic 技术栈是 PySide6/Qt，**实际实现走 Qt 剪贴板**（`phonemic/gui/clipboard.py:copy_image`）：`QImage.fromData(字节)` 解码后 `QApplication.clipboard().setImage()`，Qt 内部自动注册 `CF_DIB` / `CF_DIBV5` / `PNG` 多格式，粘贴进微信 / Word / 画图 / 浏览器均可用——不需要 pywin32 + Pillow。
> - **Linux**：X11 用 MIME 类型 `image/png`（`xclip -selection clipboard -t image/png`）；Wayland 用 `wl-copy --type image/png`。
> - **macOS**：`NSPasteboard` 的 `NSPasteboardTypePNG`（需 pyobjc 或 `osascript` 桥接）。
>
> 协议层只原样传二进制字节、不关心具体格式；实现层要按平台分支。当前 PhoneMic 仅 Windows 桌面，先实现 `CF_DIB` / `PNG`；Linux 支持（未来规划）时再补 `wl-copy` / `xclip` 分支。

### 9.2 file 接收端落地规则（实现约定）

- **落地目录**：`~/Downloads/PhoneMic/`，不存在则创建（含父目录）。v1 写死该默认值，不做配置项；将来加"用户指定目录"时只改接收端一处。
- **文件名**：沿用手机端上传的原文件名（取 `Path` 末段，剥离任何路径分隔符）。
- **重名自动改号**：目标已存在时按 `name(1).ext` → `name(2).ext` 递增；若已存在 `name(3).ext`，新文件取最大已用序号 +1（即 `name(4).ext`）。序号规则只看 `(n)` 后缀数字。
- **流式写盘**：`data` 逐块写入 `<final>.part` 临时文件（放线程/线程池执行，不阻塞事件循环），`end` 收齐后改名去掉 `.part`。改名前再查一次重名（start 分配的号码可能被中途占用）。
- **异常清理**：`cancel` 或连接断开 → 关闭句柄、删除 `.part`、丢弃会话。

---

## 10. 接收端状态处理

| 会话期望 | 收到 | 判定 | 动作 |
|---|---|---|---|
| 明文 | binary 解包成功 | OK | 按分派表处理 |
| 明文 | binary 解包失败 | MALFORMED | 丢弃，回 `error(code:"malformed")` |
| 加密 | 解密成功（provider 内部已校验 seq 单调，外部不可见） | OK | 按分派表处理 |
| 加密 | 解密抛 `ReplayError`（仅前缀路径；AAD 路径下重放表现为 MAC 失败，归入下一行） | REPLAY | 丢弃，回 `error(code:"replay")` |
| 加密 | 解密失败 | DECRYPT_FAIL | 丢弃，回 `error(code:"decrypt")`；连续 N 次断连 |
| 加密（已发 `auth_challenge`、未收 `auth_proof`） | 收到**任何其它帧** | PENDING_PROOF | 一律丢弃（**不得执行**），回 `error(code:"auth")` 并断连 |
| 任意 | `type` 不在分派表内（含方向错误） | BAD_TYPE | 丢弃，回 `error(code:"malformed")` |
| 任意 | WS text 帧 | 未刷新的旧页面 | 关闭连接，手机端提示重新扫码 |

> 补充：格式问题与编码方式无关，现有 JSON 协议里也有同样的洞——`none+LAN` 下未刷新的旧页面发来 `{type:"data",...}`，`inner.get("text","")` 返回空串，`bridge.emit("send","")` 什么都不发生。**binary/text 分帧规则就是修掉它的东西**：旧页面发的是 JSON text 帧，新服务端收到 text 帧一律关闭连接并提示重新扫码。它作用在每一帧上、且 `none+LAN` 下同样有效，比"在握手里声明版本号"更强。

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
| 7 | `file` / `photo` 分块 | 真机 |

> **进度（2026-09-15）**：阶段 1–3 已落地（编解码封装、加密层重构、三步握手），阶段 4–7 见各行内备注。

**第 1 阶段单独做**：编解码是纯函数，能完整进 pytest，正好补上"mobile.html 没有测试覆盖"这个洞；且后续接网络出问题时可确定不是编解码的锅。

---

## 13. 待定项

| # | 问题 | 现状 |
|---|---|---|
| 1 | `msgpack` C 扩展在 Nuitka 打包下是否顺利 | 未验证，失败则切 `cbor2` |
| 2 | 是否启用 AAD | **已定：AAD 优先**（XChaCha20 / AES-GCM 用 aad 带 `seq`），不支持 aad 的 XSalsa20 用 8 字节大端前缀兜底；细节已移交 `crypto-design.md` §5 |
| 3 | file 落地目录、photo 是否直接写剪贴板 | **已定：photo 直接写剪贴板、不落盘；file 落盘、不进剪贴板（图片文件也走 file）。file 落地目录 v1 写死 `~/Downloads/PhoneMic/`（自动创建），配置项后续再加（落地/命名规则见 §9.2）** |
| 4 | 面板按钮将来是否由 PC 下发 | v1 内置；插件化方案见 `panel-plugin-design.md`（声明式 JSON 面板，用户可让 AI 生成后放入插件目录） |
| 5 | 是否兼容未刷新的旧页面（旧 JSON 协议） | **已定：不兼容**。页面由服务端下发，不存在需要协商的版本号；旧页面发 JSON **text 帧**，新服务端收到 text 帧即关闭连接并提示重新扫码（§2 / §5 / §10）。因此也不需要 `hello` 这类版本声明帧 |
| 6 | `photo` 是否并入 `file`（加 `dest` 字段：`file`/`clipboard`） | **已定：保持独立**。根因有二：① `file`→磁盘、`photo`→剪贴板是两条平台强相关的落地管线（剪贴板图片格式见 §9.1）；② `photo` 纯为剪贴板设计、不落盘，`file` 纯为磁盘、不进剪贴板，语义正交。代价多一条代码路径，可接受 |
