# PhoneMic JS 加密库耦合现状（换库前的账）

状态：**现状说明 + 待办**。换库（`sodium.js` → noble 系列）暂缓，本文先把账记清楚：
**测试与生产代码各自耦合在哪、该往哪收口**，避免将来换库时才发现测试要跟着一起动。

**全文只认一条判据：测试只能依赖「线上字节格式 + 接口语义」，不能依赖「用的是哪个库」。**
换库时若测试需要改，错的不是换库这件事，而是测试当初依赖错了东西。

相关文档：`crypto-design.md`（加密层设计）、`e2ee-always-on-design.md`（握手与信任模型）、
`http-upload-design.md`（上传密钥与 MAC）。本文只谈**库的选择与耦合**，不重复那些内容。

---

## 1. 为什么这条判据成立 【理由】

判据能成立，靠的是**权威在 Python 端**：

- 线上字节格式的权威是 **PyNaCl**，它不参与 JS 侧的换库。
- 因此「JS ↔ Python」互通的用例**天然抗换库**；「JS ↔ JS」自环用例不抗。
- 换库后若只有自环用例红，红的是**参照物**（对端是拿同一个库手搭的），不是实现。

顺带一条推论：**换库时该红的用例，红得越准越好**。若换库后跨端用例红了，那是 noble
实现与 libsodium 字节不一致的真信号——正是换库最需要的那类保护。

---

## 2. 分层模型 【背景】

```
测试
  ↓
Provider 接口        encrypt / decrypt / makeAuthData / injectSessionKey
  ↓
原语 shim            seal / aead / x25519 / blake2b / b64url / random   ← 现在不存在
  ↓
具体库               libsodium.js  ·  @noble/ciphers + curves + hashes
```

现状是缺了中间那层：`crypto_providers.js` 直接调全局 `sodium` 对象，测试里的
「PC 侧对端」也直接调 `sodium`。两条线都在**绕过 Provider 接口戳到最底层**。

---

## 3. 现状清单 【背景】

### 3.1 真耦合：PC 侧对端是手搭的（5 条）【背景】

`tests/test_js_crypto.py` 里这 5 条，被测对象走 Provider 接口，但**验证它的「PC 侧对端」
是拿 `sodium` 的 API 现场搭出来的**（`crypto_box_keypair` / `crypto_scalarmult` /
`crypto_generichash` / `crypto_box_open_easy_afternm` / `crypto_aead_xchacha20poly1305_ietf_*`）：

| 用例 | 手搭的对端在做什么 |
| --- | --- |
| `test_js_roundtrip_phone_encrypt_pc_decrypt` | 用 sodium 重算会话密钥并解密手机端的帧 |
| `test_js_roundtrip_pc_encrypt_phone_decrypt` | 用 sodium 加密，让手机端 Provider 解 |
| `test_set_pc_public_key_invalidates_cached_session_key`（XSalsa20） | 用 sodium 的两把 PC 私钥分别尝试解密，验证旧密钥已作废 |
| `test_js_roundtrip`（XChaCha20） | 同上第一条，换成 AEAD 原语 |
| `test_set_pc_public_key_invalidates_cached_session_key`（XChaCha20） | 同上第三条，换成 AEAD 原语 |

问题不在它们**会不会红**，而在它们**恒绿但零信息量**：sodium 验 sodium，验的是
「同一个库跟自己兼容」。换库时红的也是参照物没了，不是实现错了。

### 3.2 工具级耦合：base64 / utf8 / random 混进来了 【背景】

`tests/test_js_crypto.py` 里 `sodium.*` 共 **121 处**，按 API 拆：

| API | 次数 | 性质 |
| --- | --- | --- |
| `base64_VARIANT_URLSAFE_NO_PADDING` | 31 | 编码常量，与加密无关 |
| `from_base64` | 17 | 编码工具，与加密无关 |
| `from_string` | 16 | utf8 工具，与加密无关 |
| `to_base64` | 14 | 编码工具，与加密无关 |
| `crypto_box_keypair` | 10 | 造密钥材料 |
| `to_string` | 8 | utf8 工具 |
| `crypto_scalarmult` / `crypto_generichash` | 各 5 | 真·密码学，但只是重算会话密钥 |
| `crypto_box_NONCEBYTES` | 4 | 常量 |
| 其余（aead / box / randombytes） | 11 | 真·密码学 |

**base64、utf8、随机数根本不是加密**，它们出现在加密测试里只是因为 sodium 顺手提供。
这是纯机械改动量，也是最该先收口的一类。

### 3.3 加载与路由 plumbing 【背景】

换库必然要动的文件名与加载方式：

| 位置 | 现状 |
| --- | --- |
| `phonemic/resources/mobile.html` | `<script src="sodium.js" defer>`；`SecureClient.init` 里 `await sodium.ready`；`_b64Variant` 取自 sodium 常量 |
| `phonemic/server/api.py` | 提供 `/sodium.js` 与 `/sodium.js.gz` 两个路由；静态资源白名单与分派表各列一次 |
| `tests/test_js_crypto.py` | 把 `sodium.js` 整个读进来内联注入；就绪判据是 `typeof sodium !== 'undefined'` |
| `tests/test_mobile.py` | 同样内联注入；mock WS 里用 `sodium.randombytes_buf` 造 nonce |
| `tests/test_integration.py` | 同样内联注入 |
| `tests/test_backend.py` | 断言 `/sodium.js` 与 `/sodium.js.gz` 路由可用，且响应体 > 100000 字节 |
| `tests/test_dispatcher.py` | 静态路由清单里含 `/sodium.js` |

### 3.4 抗换库的部分（已经做对的）【背景】

这几处**不需要改**，而且它们才是换库时真正有用的守卫：

- `tests/test_js_crypto.py` 的 7 条跨端用例：`test_cross_platform_js_encrypt_py_decrypt`
  ×2、`test_cross_platform_py_encrypt_js_decrypt` ×2、`test_cross_platform_full_handshake`、
  `test_unseal_tofu_challenge_matches_python`、`test_unseal_assigned_pin_matches_python`
  ——对端全是 PyNaCl，独立于 JS 库选择。
- `tests/test_upload.py`：**零 sodium**。上传的加密只走 Provider 接口，服务端是 Python。
- `tests/test_mobile.py` 的 mock WS：用 `_cloneProvider()` **复制客户端 Provider 的内部字段**
  （`_phonePrivate` / `_phonePublicKey` / `_pcPublicKey` / `_sharedKey`），不碰任何库 API。
  这个写法天然抗换库，值得保留。

---

## 4. 收口方案：抽出「原语 shim」【规范】

目标形状：**换库只改 shim + `mobile.html` 的 script src + 静态路由文件名，测试一行不动。**

shim 暴露语义级 API，不含任何库的名字：

```
randomBytes(n)                       → Uint8Array
boxKeypair()                         → { publicKey, privateKey }
x25519Shared(privateKey, publicKey)  → Uint8Array(32)
x25519Public(privateKey)             → Uint8Array(32)
blake2b32(data, key?)                → Uint8Array(32)   /* key 省略即无 key 模式 */
xsalsa20Seal(key, nonce, data)       → Uint8Array       /* 24B nonce，带 16B tag */
xsalsa20Open(key, nonce, ct)         → Uint8Array       /* 失败抛异常 */
xchacha20Seal / xchacha20Open        同上
sealedBox(pk, data) / sealedBoxOpen(sk, pk, ct)
b64urlEncode / b64urlDecode          无 padding
utf8Encode / utf8Decode
```

三条约束：

1. **`crypto_providers.js` 只调 shim**，不出现任何具体库的名字。
2. **测试里手搭的「PC 侧对端」也只调 shim**，不出现任何具体库的名字。
3. `mobile.html` 里的 `sodium.ready` 与 sodium 的 base64 常量一并收进 shim——
   它们是 sodium 独有的东西，不该出现在业务代码里。

做完这一步，换库就退化成「改 shim 内部实现」，而 §3.1 那 5 条自环用例会自动从
「sodium 验 sodium」变成「shim 验 shim」——信息量依旧不高，但至少不再阻塞换库。

---

## 5. 换 noble 的映射表 【背景】

供将来实施时对照，暂不执行。

| 现在（sodium.js） | noble 替代 | 备注 |
| --- | --- | --- |
| `crypto_box_easy_afternm(body, nonce, key)` | `@noble/ciphers/salsa.js` 的 `xsalsa20poly1305(key, nonce).encrypt(body)`，或 `secretbox(key, nonce).seal(body)` | 与 Python 的 `nacl.secret.SecretBox` 完全等价；nonce 24B，tag 16B |
| `crypto_box_open_easy_afternm` | `.decrypt(ct)` | 篡改时抛异常 |
| `crypto_aead_xchacha20poly1305_ietf_*` | `@noble/ciphers/chacha.js` 的 `xchacha20poly1305(key, nonce)` | nonce 24B |
| `crypto_scalarmult` / `_base` | `@noble/curves` 的 `x25519.getSharedSecret` / `getPublicKey` | raw X25519 |
| `crypto_generichash(32, shared)` | `@noble/hashes` 的 `blake2b(shared, { dkLen: 32 })` | **无 key** |
| `crypto_generichash(32, msg, kMac)` | `blake2b(msg, { key: kMac, dkLen: 32 })` | 上传 MAC，有 key |
| `crypto_box_seal` / `crypto_box_seal_open` | 见 §6.2 | noble 官方没有 |
| `randombytes_buf` | `@noble/ciphers/utils.js` 的 `randomBytes` | |
| `to_base64` / `from_base64`（URLSAFE 无 padding） | 自己写 | noble 只提供 hex 与 utf8 |
| `from_string` / `to_string` | `TextEncoder` / `TextDecoder` | |
| `await sodium.ready` | 无 | noble 是同步的，WASM 才需要 ready |

体积收益：`sodium.js` 1MB / gzip 323KB → 三件套合计约 10~20KB gzip。

---

## 6. 换 noble 的三个坑 【理由】

### 6.1 会话加密必须走 secretbox，不能用 cryptoBoxEasy 【理由】

本仓的会话密钥是 **raw X25519 + 无 key 的 BLAKE2b(32)**：Python 端在 `key_exchange.py`
的 `handle_auth` / `handle_tofu_auth` 里先 `crypto_scalarmult` 再 `blake2b(...).digest()`，
JS 端在 `crypto_providers.js` 的 `_deriveSharedKey` 里同构实现。

它**不是** libsodium 的 `crypto_box_beforenm`——后者对 shared secret 多做一次 HSalsa20。
所以：

- 对称侧只能喂**自己派生好的 32 字节密钥**给 `xsalsa20poly1305` / `secretbox`；
- 若误用 `cryptoBoxEasy` 这类「高层 box」API，两端的密钥会差一层 hsalsa，
  表现是**握手通过但每帧解密失败**——不报错，静默不通。

### 6.2 sealed box 不在 noble 官方包里 【理由】

libsodium 的 `crypto_box_seal` 是高层构造，noble 官方三件套不提供。两条路：

- 用第三方 `@serenity-kit/noble-sodium`（MIT，基于 noble 构建，含 `cryptoBoxSeal` /
  `cryptoBoxSealOpen`，另提供 `libsodium-wrappers` 的 drop-in 导出；其测试用
  libsodium-wrappers 交叉验证）。代价是多一个非官方依赖。
- 自己实现：eph 密钥对 → `nonce = blake2b(eph_pk || recv_pk, dkLen=24)` →
  `crypto_box_easy(msg, nonce, eph_sk, recv_pk)`，其中 box 的密钥还要过一次 hsalsa。

无论哪条，都必须靠 §3.4 那批跨端用例守住——**只有 PyNaCl 能判定字节对不对**。

### 6.3 ESM 与「无构建流程」的摩擦 【理由】

noble 是 **ESM-only**（虽有 standalone 单文件，仍是 ESM），而 `mobile.html` 现在是
**全文无 import、靠全局脚本**的。这比测试耦合更麻烦，因为会牵动加载顺序：

- 改 `<script type="module">`：`crypto_providers.js` 定义的全局（`NaClBoxProvider` 等）
  就没了，`test_js_crypto.py` 的就绪判据 `typeof NaClBoxProvider !== 'undefined'` 也跟着废；
- 或者自己 bundle 成 IIFE：等于给 `mobile.html` 引入一条它现在没有的构建步骤。

---

## 7. 建议顺序 【规范】

1. **抽 shim**（§4）——零行为变化，`crypto_providers.js`、`mobile.html` 的两个 sodium 用法、
   以及测试里手搭的对端全部改调 shim。跑全绿。
2. **shim 内部换对称与 KDF**（xsalsa / xchacha / blake2b / x25519）。跨端那 7 条应照绿。
3. **换 sealed box**（§6.2），由 `test_cross_platform_full_handshake` 与两个
   `test_unseal_*_matches_python` 守着。
4. **处理加载方式、静态路由文件名、删掉 `sodium.js.gz`**（§3.3、§6.3）。

第 1 步可以独立于换库先做——它本身就让测试少依赖一层。
