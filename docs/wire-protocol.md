# PhoneMic 通信协议设计（v1 · MessagePack）

状态：**设计稿，评审中，尚未落地**。本文档是手机端与服务端实现新协议时的唯一对齐依据。

**加密层设计已抽取为独立文档 `crypto-design.md`**——它内容无关（可承载 JSON / msgpack / CBOR 任意编码），本文档只保留消息层与加密层的边界约定，加密细节一律引用之。

---

## 1. 背景与目标

### 现状

手机端（`phonemic/resources/mobile.html`）与服务端（`phonemic/server/api.py`）之间目前是 JSON over WebSocket：

- 明文模式：`{"type":"send","text":"..."}`
- 加密模式：`{"type":"data","data":"<base64url(nonce+ciphertext)>"}`

服务端 `api.py:351` 只读取 `message["text"]`，PC 端 `PhoneMic.py:269-271` 按 `event_type` 分发。现有消息类型：

| 方向 | 现有类型 |
|---|---|
| 上行 | `preview`、`send`、`auth` |
| 下行 | `config`（`api.py:188 push_config`）、`reconnect`（`api.py:202 request_client_rescan`）、`auth_ack` |

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

**包括 `auth` / `auth_ack`。** 其中**只有 `auth` 不能加密**（会话密钥尚未建立；到 `auth_ack` 时密钥已就绪，整帧加密，见第 4 节），但两者都完全可以用 msgpack 编码——**"不能加密"和"不能用 msgpack"是两回事**。统一编码换来的好处：

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
| `bin` | 文件/图片分块、`auth` 的 `data` | **原生类型，不做 base64** |

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
| `auth_ack` 及之后所有帧 | **对称整帧加密**（含 `type`），Provider 输出直接上帧（`none+LAN` 无此帧，全程明文） |

- **会话状态是权威**：`is_encrypted` 握手时确定、之后不变，接收端永远知道该不该解；**绝不"解密失败就当明文"**（安全论证见 `crypto-design.md` §7）。
- **CF 强制加密**：Cloudflare 模式下 `none` 被归一为 `auto`（`mode.py` 的 `effective_algorithm`），**不存在明文 CF**（历史模式 `none`+CF 已删）。因此 `needs_auth` 对所有非 `none+LAN` 连接为真。
- **`none` + LAN**：全程明文，**没有 `auth` 帧**（`needs_auth` 为 False），握手由 `hello` 完成。
- **`auth.data` 统一编码为 `bin`**：保证该字段类型唯一，接收端不需要按模式分支判断是 str 还是 bin。
- **握手失败**：服务端不回消息层帧，直接 WS close 4001 + reason（见第 5 / 7 节）。
- **信任模型**：PC 公钥 = 带外 bearer token，能密封即认证（`crypto-design.md` §2）。

### 4.1 密钥交换独立成类（算法无关）

公钥交换这一步——`SealedBox` 解封 → 读出 `algo` + 手机临时公钥 → ECDH → KDF 派生会话密钥——**对 XChaCha20 / XSalsa20 / AES-GCM 全部相同，纯 X25519，与具体 AEAD 无关**。因此它必须脱离 `CryptoProvider`，单独成一个算法无关的 `KeyExchange` 类：不解密 `auth.data` 就不知道 `algo`、不知道 `algo` 就无法实例化 Provider，而解封本身恰恰与算法无关——把 `SealedBox`/ECDH/KDF 塞进 Provider 等于要求它在构造前先解开自己的构造参数，逻辑上不成立。

接口、`auth` 内层结构（`{"algo","pk"}`，JSON 编码）、两段式调用顺序、新增算法流程与完整死锁论证见 `crypto-design.md` §3 / §4；seq 防重放的承载（AAD 优先 / 前缀兜底）与校验状态见其 §5。

---

## 5. 握手流程

```
手机                                           服务端
 │  WS 连接
 │──── auth（明文帧）{"type":"auth","data":<bin>} ─────►│   仅 needs_auth 模式
 │◄─── auth_ack（加密帧）{"type":"auth_ack"} ────────────────│   握手成功
 │◄─── 或 WS 关闭（close 4001 + reason）──────────────│   握手失败，无密钥可用
 │
 │──── hello {"type":"hello","v":1} ─────────────►│   总是发
 │◄─── config {"type":"config","key","value"} ─────│   或 error {"type":"error","code":"version"}
 │
 │  …… 正常数据消息 ……
```

- `auth` / `auth_ack` 仅在 `needs_auth` 时出现。**`none+LAN` 没有 auth**（`e2ee.py:268` `needs_auth` 为 False）。
- `auth.data` 的密封内容与密钥交换流程见 `crypto-design.md` §3；本文档只定义 `auth` 消息的帧格式（见第 7 节）。
- **`hello` 总是发**，正是靠它补上 `none+LAN` 下"模式/版本不一致时连报错机会都没有"这个洞。
- **版本天然一致**：`mobile.html` 由服务端下发（`api.py` 里 `html.replace` 注入），两端永远同版本，无灰度兼容问题。`hello` 的实际作用退化为**检测手机上那个页面还没刷新**——版本不符回 `error(code:"version")`，页面提示"请刷新"。

---

## 6. type 表

`type` 取值为字符串。上行（手机 → 服务端）与下行（服务端 → 手机）不共用取值空间，由各侧分派表天然保证方向正确。

### 上行

| type | 说明 | payload 字段 |
|---|---|---|
| `auth` | 密钥交换（加密模式，仅 needs_auth） | `data`(bin) |
| `hello` | 版本声明 | `v` |
| `preview` | 输入预览 | `text` |
| `send` | 文本上屏 | `text` |
| `key` | 模拟按键 | `keys` |
| `mouse` | 鼠标控制 | `a`、`dx`/`dy`/`btn`/`delta` |
| `file` | 传文件（分块） | `a`、`id`、`name`、`size`、`chunks`、`n`、`chunk` |
| `photo` | 图片写入系统剪贴板（分块，**不落盘**） | `a`、`id`、`name`、`size`、`chunks`、`n`、`chunk`（线格式同 `file`，`name` 对剪贴板无意义） |

### 下行

| type | 说明 | payload 字段 | 来源 |
|---|---|---|---|
| `auth_ack` | 握手应答 | 成功时为加密空帧 `{"type":"auth_ack"}`；失败不回消息帧，以 WS close（4001 + reason）传递 | 迁移已有 |
| `config` | 单键配置推送 | `key`、`value` | 迁移 `push_config` |
| `reconnect` | 要求重新扫码 | `reason` | 迁移 `request_client_rescan` |
| `rekey` | 密钥轮换 | — | 预留 |
| `error` | 错误通知 | `code`、`msg` | 新增 |
| `ack` | 分块确认 | `ref`、`id`、`n`、`received` | 新增 |
| `status` | 状态同步 | `muted`、`mode` | 新增 |

心跳**不进 type 表**，用 WebSocket 原生 ping/pong。

> 断线重连策略：**不做地址重定向**。连接断开后手机端重新扫码连接即可，因此 type 表里不需要 `redirect` 这一项。

---

## 7. 各 type 详细定义

### auth / auth_ack

**线上帧的明文部分只有 `type`**：
```
{"type":"auth", "data":<bin>}
```

`data` 是用 PC 公钥 `SealedBox` 密封的**不透明字节**，消息层不解释其内容——内层结构 `{"algo","pk"}`（JSON 编码）、`algo` 校验、ECDH/KDF 全在 `crypto-design.md` §3。要点：

- `algo` 取自二维码 fragment 的 `a=` 列表，服务端解封后校验其属于下发列表。**`algo` 只在密文里出现，从不以明文传输**。
- 新协议已无 `none`+CF 明文模式，因此 `data` 在所有 `needs_auth` 会话里都是 SealedBox 密文，不再有 token 分支。

加密帧（会话密钥已建立，整帧加密，含 `type`）：

```
{"type":"auth_ack"}
```

- **无 `data`、无明文 `algo`**：手机能解开这一帧即证明 PC 持有正确会话密钥，且 `algo` 在 `auth.data` 里早已协商过（`crypto-design.md` §7）。
- `none+LAN` 模式**不发 `auth` / `auth_ack` 这一对帧**（见第 5 节），握手直接由 `hello` 完成。

**握手失败时服务端不回 `auth_ack`**——此时 PC 还没有会话密钥，无法加密，也没有任何可下发的机密。服务端直接关闭 WS 连接：

```
WS close: code=4001, reason="algo not offered" | "bad sealed box"
```

- 失败原因只可能来自解封阶段：`SealedBox` 解不开（公钥/数据损坏），或解封后的 `algo` 不在下发列表。
- 用 close 帧而非明文 `auth_ack(rejected)`，是为了不破坏"绝不解密失败就当明文"的原则：手机端收到 `auth` 后等待的要么是**一条能解密的加密帧**（成功），要么是**连接关闭**（失败），无需"先试解密、失败再当明文解析"。close reason 在密钥建立前本就明文，泄露无害。

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
{"type":"mouse", "a":"down",  "btn":"left"}
{"type":"mouse", "a":"up",    "btn":"left"}
{"type":"mouse", "a":"wheel", "delta":-120}
```

- 采用**速度模型**（摇杆远快近慢），`dx`/`dy` 是**每帧相对位移像素**，由 `requestAnimationFrame` 循环驱动，约 60 次/秒。
- **加速曲线在手机端计算**：摇杆偏移 → 速度映射（具体曲线为客户端实现细节，如二次/指数映射）产出最终 `dx`/`dy`；PC 端只做 `moveRel(dx, dy)`，**不另做速度处理**。协议只规定 `dx`/`dy` 是已算好的相对位移，不规定曲线形状。
- PC 端新增 `phonemic/gui/mouse.py`，用 pyautogui 的 `moveRel` / `click` / `mouseDown` / `mouseUp`，照搬 `keyboard.py` 的模式。

### config

```
{"type":"config", "key":"mobile_max_records", "value":50}
```

**单键单值**结构，迁移自 `api.py:188 push_config`。现有代码发的是扁平形式 `{"type":"config","mobile_max_records":50}`，新协议统一规范为 `key` / `value` 两字段，便于通用分派。

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
| `version` | 版本不支持 | 提示"请刷新页面" |
| `mode` | 模式不匹配 | 提示"请刷新页面" |
| `decrypt` | 解密失败 / 密钥失效 | 提示"请刷新页面"，连续 N 次则断连 |
| `replay` | seq 未递增 | 丢弃，连续 N 次则断连 |
| `ratelimit` | 限流 | 退避重试 |
| `malformed` | 非法消息 / 未知 type | 记录日志，丢弃 |

### status

服务端→手机的状态同步，字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `muted` | bool | 麦克风静音状态 |
| `mode` | str | 当前隧道模式（`lan` / `cloudflare` / `none`） |

完整语义随阶段 5（§12）落地时细化，v1 先定字段。

---

## 8. seq 与防重放（见 crypto-design.md §5）

`seq` 是**加密层防护**，不是消息内容：它**不进 msgpack map**，计数、承载、校验全部在 `CryptoProvider` 内部完成，外部（帧编解码层 / `SecureSession`）完全感知不到其存在。这正是不把加密设计写死在本协议里的原因之一——将来换掉 MessagePack（→ CBOR 等），`seq` 的承载方式与状态**零改动**。

对消息层的影响收敛为一处：解密异常按类型映射为 `error` 的 code——`DecryptError` → `decrypt`、`ReplayError` → `replay`（接收端状态表见第 10 节）。承载路径（AAD 优先 / 8 字节大端前缀兜底）、单调校验、reset 语义与边际价值分析见 `crypto-design.md` §5。

---

## 9. 分块传输（file / photo）

子协议用 map 字段表达，**不需要设计字节子头**：

```
{"type":"file", "a":"start", "id":7, "name":"a.pdf", "size":1048576, "chunks":16}
{"type":"file", "a":"data",  "id":7, "n":0, "chunk":<bin 256KB>}
{"type":"file", "a":"data",  "id":7, "n":1, "chunk":<bin 256KB>}
{"type":"file", "a":"end",   "id":7}
```

- **分块大小固定 256KB**。uvicorn 16MB 上限下有 64 倍余量；分块是为了出进度条，以及让 mouse 帧能插进来不被大块堵住。
- **顺序由 WebSocket 保证**（可靠有序），重组只需 `bytearray.extend`；`n` 仅用于进度显示与 ACK。
- **`id` 由手机端生成**：每连接内从 1 单调递增的整数，作为一次传输会话的标识；服务端按 `id` 路由 buffer，无需跨端协商。
- 接收端状态机：`start` 建 buffer → 每个 `data` 执行 `buf.extend(chunk)` → `end` 触发落盘（file）或写剪贴板（photo）。
- **`ack` 仅作进度回报**（字段 `received` = 已收字节数），v1 **不做窗口流控**——服务端顺序接收全部 `data` 即可，`ack` 用于前端进度条与断点提示，不反压发送端。

### 9.1 两个 sink：file 落盘、photo 剪贴板

`file` 与 `photo` 的**线格式完全相同**（同一套 `start`/`data`/`end` 子协议、`id`/`size`/`chunks`/`n`/`chunk` 字段），区别只在接收端的"落地方式"——这正是它俩必须拆成两个独立消息类型（type）的根因：

- **`file` → 磁盘**：字节重组后写入本地文件（路径/目录见 §13 #3）。**文件不进剪贴板**——即便是图片文件，也走 `file`（落到磁盘），不走高亮 `photo`。
- **`photo` → 剪贴板**：字节重组后**直接写入系统剪贴板**，不写任何磁盘文件。设计目的就是"手机拍一张 → 电脑剪贴板里能直接 Ctrl+V 粘贴"。因此 `photo` 的 `name` 字段对剪贴板无意义（剪贴板里没有文件名概念），可忽略或省略。

> **跨平台剪贴板图片格式（实现注意，非协议层）**：剪贴板里放图不是"塞字节"那么简单，各平台有专属格式——这是 `photo` 必须独立于 `file` 的第二个硬理由（落地逻辑与平台强相关，和"写磁盘"是两套完全不同的代码路径）：
> - **Windows**：`CF_DIB` / `CF_DIBV5`（位图），或注册的 `PNG` 格式（`CFSTR_PNG` = `"PNG"`）。常用 `pywin32`(`win32clipboard`) + `Pillow` 把 PNG 转 `CF_DIB` 或登记 `PNG` 格式。
> - **Linux**：X11 用 MIME 类型 `image/png`（`xclip -selection clipboard -t image/png`）；Wayland 用 `wl-copy --type image/png`。
> - **macOS**：`NSPasteboard` 的 `NSPasteboardTypePNG`（需 pyobjc 或 `osascript` 桥接）。
>
> 协议层只原样传二进制字节、不关心具体格式；实现层要按平台分支。当前 PhoneMic 仅 Windows 桌面，先实现 `CF_DIB` / `PNG`；Linux 支持（未来规划）时再补 `wl-copy` / `xclip` 分支。

---

## 10. 接收端状态处理

| 会话期望 | 收到 | 判定 | 动作 |
|---|---|---|---|
| 明文 | binary 解包成功 | OK | 按分派表处理 |
| 明文 | binary 解包失败 | MALFORMED | 丢弃，回 `error(code:"malformed")` |
| 加密 | 解密成功（provider 内部已校验 seq 单调，外部不可见） | OK | 按分派表处理 |
| 加密 | 解密抛 `ReplayError`（仅前缀路径；AAD 路径下重放表现为 MAC 失败，归入下一行） | REPLAY | 丢弃，回 `error(code:"replay")` |
| 加密 | 解密失败 | DECRYPT_FAIL | 丢弃，回 `error(code:"decrypt")`；连续 N 次断连 |
| 任意 | `type` 不在分派表内（含方向错误） | BAD_TYPE | 丢弃，回 `error(code:"malformed")` |
| 任意 | WS text 帧 | 未刷新的旧页面 | 关闭连接，手机端提示重新扫码 |

> 补充：格式问题与编码方式无关，现有 JSON 协议里也有同样的洞——`none+LAN` 下未刷新的旧页面发来 `{type:"data",...}`，`inner.get("text","")` 返回空串，`bridge.emit("send","")` 什么都不发生。`hello` + `error` 是唯一能修掉它的东西。

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
| 2 | 加密层重构（设计见 crypto-design.md）：新增 `KeyExchange` 类，`CryptoProvider` 收窄为纯 AEAD 封装（构造只收 `session_key`）；`create_provider` 改签名为 `(algo, session_key)`；`SecureChannel` 持有 `KeyExchange` 实例；`receive_auth` 改为两段式。删 `e2ee.py` 旧 base64 与 `make_auth_ack_data` | 单测：`handle_auth` 给定 sealed → 出正确 `session_key`；各 Provider `encrypt`/`decrypt` 往返 |
| 3 | 握手层：`auth` / `auth_ack` / `hello` / `config` / `error`，WS 全 binary 分流（两段式握手顺序见 crypto-design.md §3.4） | 连上后看 config 回包 |
| 4 | 迁移 `preview` / `send` | 真机 |
| 5 | 新增 `key` / `mouse` / `status` | 真机 |
| 6 | 面板 UI（按钮集内置） | 真机 |
| 7 | `file` / `photo` 分块 | 真机 |

**第 1 阶段单独做**：编解码是纯函数，能完整进 pytest，正好补上"mobile.html 没有测试覆盖"这个洞；且后续接网络出问题时可确定不是编解码的锅。

---

## 13. 待定项

| # | 问题 | 现状 |
|---|---|---|
| 1 | `msgpack` C 扩展在 Nuitka 打包下是否顺利 | 未验证，失败则切 `cbor2` |
| 2 | 是否启用 AAD | **已定：AAD 优先**（XChaCha20 / AES-GCM 用 aad 带 `seq`），不支持 aad 的 XSalsa20 用 8 字节大端前缀兜底；细节已移交 `crypto-design.md` §5 |
| 3 | file 落地目录、photo 是否直接写剪贴板 | **已定：photo 直接写剪贴板、不落盘；file 落盘、不进剪贴板（图片文件也走 file）。file 落地目录待配置（默认下载目录或用户指定）** |
| 4 | 面板按钮将来是否由 PC 下发 | v1 内置；若要则加 `[{label, keys}]`，不复用 `VoiceCommand` |
| 5 | 是否兼容未刷新的旧页面（旧 JSON 协议） | 建议否——页面由服务端下发；text 帧直接关闭并提示重新扫码 |
| 6 | `photo` 是否并入 `file`（加 `dest` 字段：`file`/`clipboard`） | **已定：保持独立**。根因有二：① `file`→磁盘、`photo`→剪贴板是两条平台强相关的落地管线（剪贴板图片格式见 §9.1）；② `photo` 纯为剪贴板设计、不落盘，`file` 纯为磁盘、不进剪贴板，语义正交。代价多一条代码路径，可接受 |
