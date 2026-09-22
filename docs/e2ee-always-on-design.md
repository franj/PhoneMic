# PhoneMic 加密与认证解耦设计

状态：**已实现**（分支 `feature/e2ee-always-on`）。本文档描述对 `crypto-design.md` 和 `wire-protocol.md` 的架构变更——将"加密"与"认证"从绑定关系解耦，加密成为基线（永远开启），认证变为独立选项。两份下游文档已按 §11 实现影响清单同步更新。

实现落地时的两处偏离（以代码为准）：

- §8.1 的 `plaintextAuthData()` **已实现**在 `crypto_providers.js`，但 §8.2 伪代码里的 `_provider.generatePin()` / `_showPin()` / `deriveSessionKey()` 未按字面引入：识别码生成放在 `SecureClient._generatePin()`，Provider 通过 `setPcPublicKey()` 惰性派生会话密钥（`_deriveSharedKey()`），手机端识别码显示由 `WSClient` 的审批浮层统一负责。
- §5.4 的"两次等待共享一个 deadline"已被推翻：`AUTH_TIMEOUT` 语义收窄为**单次**等待上限。审批等待（最长 30s）夹在 `auth` 与 `auth_proof` 之间，若共用一份预算，用户点「允许」时 deadline 已过期、握手会立刻超时。

另有一条**待实施的改进提案**（未实现，见 [§5.5.1](#551-已知弱点与提案改由-pc-指派并密封下发识别码待实施)）：现状的识别码由手机生成并明文传输，可被同网段窃听者抄走复用，从而确定性抢下配对；提案改为**由 PC 指派、密封下发**。

---

## 1. 背景与动机

### 现状：加密与认证绑定

当前架构（`crypto-design.md` §2）的实际模式：

| 模式 | 认证 | 加密 | 信任锚 |
|---|---|---|---|
| `none` + LAN | 无 | 无（明文） | 无 |
| 加密（xsalsa20/xchacha20）+ 任意模式 | 有 | 有 | QR 中的 PC 公钥 |

加密和认证是一一绑定的：要加密就必须认证（SealedBox），不认证就完全没有加密（明文）。这导致 `none` + LAN 模式下通信完全裸奔——同一 WiFi 下的任何人用 Wireshark 即可看到全部按键注入数据。

> `none` + Cloudflare 在代码中不存在实际模式：`mode.py` 的 `effective_algorithm()` 在 Cloudflare 下把 `none` 强制归一为 `auto`（加密），因此 CF 模式下永远是加密+认证的。`none` + CF 的 token 认证路径是死代码，本次变更将其清理。

### 目标：解耦

将"加密"和"认证"拆成两个正交维度：

- **加密**：永远开启。所有模式都做端到端加密（X25519 ECDH → 会话密钥 → AEAD）。
- **认证**：可选，且有两种方式——URL fragment 认证（QR 扫码）和 TOFU 认证（手动审批）。

### 为什么要做这个变更

1. **消除明文通信面**：`none` + LAN 的明文是最大的安全短板。家庭 WiFi 场景下被动嗅探门槛极低。
2. **简化架构**：移除 `PlainProvider`、token 认证路径、`effective_algorithm` 强制加密逻辑——代码路径收敛。
3. **语义更清晰**："加密"和"认证"是两个独立的安全属性，不应互相绑定。用户要的是"防窃听"（加密），不一定需要"防冒充"（认证）。
4. **TOFU 提供无扫码的认证路径**：用户输入 IP 即可连接，PC 端审批后建立信任。首次审批后 PC 公钥存入手机 `localStorage`，重连即走认证路径，无需再次审批。

---

## 2. 新架构：三模式

| # | 隧道模式 | 认证方式 | 加密 | QR 码 | 信任锚 | 首次需审批 |
|---|---|---|---|---|---|---|
| 1 | LAN | TOFU（手动审批） | 有 | `http://ip:port` | 首次无，审批后 PC 公钥存 localStorage | 是 |
| 2 | LAN | URL fragment（扫码） | 有 | `http://ip:port/<secret_path>/#k=<PC公钥>&a=<算法列表>` | QR 中的 PC 公钥 | 否 |
| 3 | Cloudflare | URL fragment（扫码） | 有 | `<cf_url>/<secret_path>/#k=<PC公钥>&a=<算法列表>` | QR 中的 PC 公钥 | 否 |

### 两个正交维度

```
隧道模式：  LAN  |  Cloudflare
认证方式：  TOFU  |  URL fragment
```

理论上 4 种组合，但 **TOFU + Cloudflare 不提供**：CF 公网可达，TOFU 首次连接是匿名 DH（无信任锚），攻击者可抢在真机之前连上骗取审批。因此 CF 模式强制 URL fragment 认证。

### 核心变化

- **`none` 算法和 `PlainProvider` 完全移除**。所有通信都经过 AEAD 加密。
- **认证方式变成 `auth_method` 配置项**：`tofu` 或 `url_fragment`。CF 模式强制 `url_fragment`。
- **模式 2/3 的握手协议不变**——SealedBox 密封 auth → auth_challenge → auth_proof。
- **模式 1（TOFU）首次连接**：手机明文发 auth（含公钥 + 识别码）→ PC 审批 → SealedBox 加密回传 PC 公钥 + nonce → 握手完成。
- **模式 1 重连**：手机用 localStorage 里的 PC 公钥做 SealedBox 密封 auth，与模式 2/3 握手完全一致，无需审批。

---

## 3. 配置模型变更

### 3.1 配置键

| | 旧 | 新 |
|---|---|---|
| 加密/认证开关 | `e2ee_algorithm: "none" \| "auto"` | **删除**（加密永远开启，认证由 `auth_method` 控制） |
| 认证方式 | （隐含在 `e2ee_algorithm` 中） | `auth_method: "tofu" \| "url_fragment"`（默认 `"tofu"`） |
| 隧道模式 | `tunnel_mode: "lan" \| "cloudflare"` | 不变 |

### 3.2 归一化逻辑

```python
def effective_auth_method(auth_method: str, mode: TunnelMode) -> str:
    if mode == TunnelMode.CLOUDFLARE:
        return "url_fragment"       # CF 强制扫码认证
    return auth_method              # LAN 尊重用户选择
```

### 3.3 迁移

| 旧配置 | 新配置 |
|---|---|
| `e2ee_algorithm: "none"`, `tunnel_mode: "lan"` | `auth_method: "tofu"` |
| `e2ee_algorithm: "auto"` (或历史值), `tunnel_mode: "lan"` | `auth_method: "url_fragment"` |
| 任何 `e2ee_algorithm`, `tunnel_mode: "cloudflare"` | `auth_method: "url_fragment"`（强制） |

`settings_manager.py` 加载时做一次性迁移：检测到 `e2ee_algorithm` 键则按上表转换并写入 `auth_method`，删除旧键。

---

## 4. QR 码格式

### 模式 1（LAN + TOFU）

```
http://192.168.1.100:12000
```

- **无 fragment**，无 secret_path，与当前 `none` + LAN 的 QR 完全一致——向后兼容。
- 手机端见 URL 无 `#k=` fragment → 判定 TOFU 模式 → 检查 localStorage 是否有 PC 公钥。

### 模式 2/3（URL fragment 认证）

```
http://192.168.1.100:12000/<secret_path>/#k=<PC公钥base64url>&a=xchacha20,xsalsa20
```

- 与当前加密模式 QR 格式完全一致——不变。
- 手机端见 URL 有 `#k=` fragment → 判定 URL fragment 认证 → 从 `a=` 列表协商算法。

### TOFU 重连时的 URL

TOFU 模式下，手机首次连接成功后 PC 公钥存入 `localStorage`。重连时 URL 仍然是裸 URL（不含 fragment），但手机端逻辑为：

- `localStorage` 有 PC 公钥 → 走 SealedBox 认证路径（重连）
- `localStorage` 无 PC 公钥 → 走 TOFU 首次连接路径（含审批）

### `secret_path` 保留策略

- 模式 2/3：保留 `secret_path`（32 位随机串），作为端点门禁防扫描。
- 模式 1：不生成 `secret_path`，QR 为裸 URL。TOFU 的门禁是审批机制，不需要 URL 级访问控制。

### `append_to_url` 逻辑

```python
def append_to_url(self, url: str) -> str:
    if self._auth_method == "tofu":
        return url                              # TOFU：裸 URL
    if not url.endswith("/"):
        url += "/"
    if self._secret_path:
        url += f"{self._secret_path}/"
    return f"{url}#k={self.get_public_key_b64()}&a={','.join(self.offered_algorithms)}"
```

---

## 5. 握手协议

### 5.1 URL fragment 认证模式（模式 2/3）——不变

```
手机                                              PC
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  明文帧  {"type":"auth","data":<bin SealedBox>}
 │◄─── auth_challenge ─────────────────────────────────│  加密帧  Provider.encrypt({"type":"auth_challenge","nonce":<bin>})
 │──── auth_proof ────────────────────────────────────►│  加密帧  Provider.encrypt({"type":"auth_proof","nonce":<bin>})
 │◄─── config ─────────────────────────────────────────│  加密帧
```

与 `wire-protocol.md` §5 完全一致，代码零改动。手机在 `init()` 时已有 PC 公钥（来自 QR），可立即做 ECDH 并创建 Provider，auth 用 SealedBox 密封 `{algo, pk}`。

### 5.2 TOFU 首次连接（模式 1，无 localStorage）——新增

```
手机                                              PC
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  明文帧  {"type":"auth","algo":"xchacha20","pk":<bin>,"pin":"3847"}
 │                                                      │  ← PC 收到 auth，主界面显示审批通知（识别码 3847）
 │                                                      │  ← 用户核对手机屏幕与 PC 主界面的识别码一致
 │                                                      │  ← 用户点"接受"
 │◄─── auth_challenge ─────────────────────────────────│  SealedBox(phone_public): {pc_public, nonce}
 │──── auth_proof ────────────────────────────────────►│  Provider.encrypt: {"type":"auth_proof","nonce":<bin>}
 │◄─── config ─────────────────────────────────────────│  Provider.encrypt: {"type":"config",...}
 │
 │  手机存储 pc_public 到 localStorage（作为信任锚/token）
```

**第一步 `auth`（明文）**

TOFU 首次连接时手机没有 PC 公钥，无法用 SealedBox 密封。auth 帧为全明文 msgpack map：

```
c→s  {"type":"auth", "algo":"xchacha20", "pk":<bin 32B 手机临时公钥>, "pin":"3847"}
```

- `algo` 明文传输——TOFU 首次连接无信任锚，无法保密算法列表，且无安全意义。
- `pk` 是手机临时 X25519 公钥（32 字节），明文传输——公钥本身就是公开值。
- `pin` 是 4 位识别码（见 §5.6），手机屏幕和 PC 主界面各显示一个，供用户核对。

**PC 端审批（关键步骤）**

PC 收到 auth 后：
1. 解析 auth 帧取出 `algo`、`phone_public`（手机公钥）、`pin`（识别码）。**不做 ECDH，不创建 Provider**——审批通过后才做。
2. **暂停握手**，在 dashboard 主界面显示审批通知：识别码 `3847` + 连接来源 IP + 接受/拒绝按钮。
3. 等待用户操作：
   - **点"接受"** → 做 ECDH（`crypto_scalarmult(pc_private, phone_public)` → `shared` → `KDF` → `session_key`），创建 Provider，继续握手发 auth_challenge。
   - **点"拒绝"** → `WS close(code=4032, reason="rejected")`。
   - **超时（30s 无操作）** → 同拒绝处理，`close(code=4032, reason="timeout")`。

> **审批在 ECDH 之前**：收到 auth 后只提取明文字段（`algo`/`pk`/`pin`），不做任何密钥计算。审批通过后才做 ECDH + 创建 Provider + 发 auth_challenge。被拒绝的连接零计算开销。
>
> **审批必须在发 auth_challenge 之前**：auth_challenge 里包含 PC 公钥（SealedBox 加密），只有审批通过后才发送。这确保 PC 公钥不暴露给未授权的连接方——PC 公钥在此架构中是 token，泄露意味着他人可绕过审批直接走认证路径重连。

> **无痛显示，不弹窗**：审批通知在 dashboard 主界面的固定区域（如状态栏区域）显示，不使用模态弹窗。如果有人不断尝试连接，不会反复弹窗打扰用户——新请求替换旧的待审批通知（或排队），用户按自己的节奏处理。

**第二步 `auth_challenge`（SealedBox 加密）**

审批通过后，PC 生成 16 字节随机 nonce，用 `SealedBox(phone_public)` 加密 `{pc_public, nonce}`：

```
s→c  SealedBox(phone_public): {pc_public: <32B>, nonce: <16B>}
```

- 整个帧是 SealedBox 密文（`crypto_box_seal`），手机用 `phone_private` 解封。
- **PC 公钥不暴露**：SealedBox 只有 `phone_private` 持有者才能解封。被动窃听者拿到密文也无法提取 PC 公钥。这正是"PC 公钥 = token"的实现——它只在审批通过后才通过加密通道传给手机。
- nonce 在 SealedBox 内是明文（解封后可见），无需额外加密——SealedBox 本身已提供机密性。

**手机端处理流程**：

1. `SealedBox.decrypt(rawBytes, phone_private)` → `{pc_public, nonce}`
2. 取 `pc_public` → `ECDH(phone_private, pc_public)` → `shared` → `KDF(shared)` → `session_key`
3. `create_provider("xchacha20", session_key)` → Provider
4. `Provider.encrypt(nonce)` → auth_proof 帧

> 注意：PC 在审批前已做完 ECDH 并创建了 Provider。手机在收到 auth_challenge 后才做 ECDH 并创建 Provider。两端独立计算 `session_key`，ECDH 保证结果一致。

**第三步 `auth_proof`（加密）**

手机用 Provider 加密 nonce 回传：

```
c→s  Provider.encrypt: {"type":"auth_proof", "nonce":<bin>}
```

PC 用自己（审批前创建）的 Provider 解密并校验 nonce 回显正确 → 握手完成，注册连接，发 config。

**手机端持久化**

握手成功后，手机将 `pc_public` 存入 `localStorage`（key 如 `phonemic_pc_pubkey`）。后续重连时读取此值，走认证路径。

### 5.3 TOFU 重连（模式 1，有 localStorage）——等同 URL fragment 认证

重连时手机 `localStorage` 有 PC 公钥，握手与 §5.1 完全一致：

```
手机                                              PC
 │  WS 连接
 │
 │──── auth ──────────────────────────────────────────►│  SealedBox(pc_public): {algo, pk: phone_public}
 │◄─── auth_challenge ─────────────────────────────────│  Provider.encrypt: {"type":"auth_challenge","nonce":<bin>}
 │──── auth_proof ────────────────────────────────────►│  Provider.encrypt: {"type":"auth_proof","nonce":<bin>}
 │◄─── config ─────────────────────────────────────────│  Provider.encrypt: {"type":"config",...}
```

- 手机用 `localStorage` 里的 PC 公钥做 SealedBox 密封 auth → PC 解封成功 = token 验证通过。
- **PC 不需要审批**——能解封 SealedBox 的人持有 PC 公钥（token），等价于已认证。
- 握手帧格式、Provider 加解密路径与 URL fragment 认证模式完全一致。

### 5.4 超时设计

#### 现有超时机制

| 端 | 常量 | 值 | 作用域 |
|---|---|---|---|
| PC | `AUTH_TIMEOUT` (`e2ee.py`) | 10s | 整轮握手绝对预算（auth + auth_proof 共享一个 deadline） |
| 手机 | `connectTimeout` (`mobile.html`) | 3000ms | WS 连接级超时（从 `connect()` 开始到连接就绪） |
| 手机 | `whenReady` | 6000ms | 业务层等待连接就绪 |

现有机制下，认证模式握手很快（PC 收到 auth 后毫秒级返回 auth_challenge），3 秒连接超时足够。但 **TOFU 首次连接**需要等待用户审批，3 秒远远不够。

#### TOFU 首次连接的超时

TOFU 审批阶段需要用户完成：看到审批通知 → 核对识别码 → 点"接受"。这至少需要几秒，可能长达 30 秒（用户没在看屏幕）。

**PC 端审批超时**：

```python
APPROVAL_TIMEOUT = 30  # 秒：TOFU 审批等待时间
```

- PC 收到 auth 后启动审批计时器。
- 30 秒内用户点"接受" → 继续。
- 30 秒内用户点"拒绝" → close 4032。
- 30 秒无操作 → 同拒绝处理，close 4032（reason="timeout"）。

**手机端 auth_challenge 等待超时**：

手机端现有 `connectTimeout = 3000ms` 是 WS 连接级超时（TCP + WS 握手），不覆盖 auth 后的等待。TOFU 模式下，手机发完 auth 后需要等待 auth_challenge，等待时间 = PC 审批时间 + 网络延迟。因此需要一个新的等待超时：

```javascript
// TOFU 首次连接：auth_challenge 等待超时
this.authChallengeTimeout = APPROVAL_TIMEOUT_SEC * 1000 + 5000;  // 35s = 30s 审批 + 5s 容错
```

- 手机发完 auth 后启动 `authChallengeTimeout` 计时器。
- 收到 auth_challenge → 取消计时器，继续握手。
- 超时 → 关闭连接，UI 提示"审批超时，请重试"。
- **5 秒容错**覆盖 PC 审批到发 auth_challenge 的处理延迟 + 网络往返。

> **`connectTimeout`（3s）仍然有效**：它覆盖 WS 连接建立（TCP + WS 握手），在 `onopen` 后清除。`authChallengeTimeout` 在 `onopen` + auth 发送后启动，两者不重叠。

```
手机端时间线（TOFU 首次）：
  connect() ── 3s connectTimeout ──┐
                                   │ onopen
                                   ├─ 发 auth ── 35s authChallengeTimeout ──┐
                                   │                                        │
                                   │              收到 auth_challenge ──── 取消计时器
                                   │              发 auth_proof
                                   │              收到 config → 连接就绪
```

#### 超时值汇总

| 超时 | PC 端 | 手机端 | 说明 |
|---|---|---|---|
| WS 连接建立 | — | 3s（`connectTimeout`） | TCP + WS 握手，不变 |
| 认证模式 auth_challenge 等待 | 10s（`AUTH_TIMEOUT` 内） | 3s（`connectTimeout` 内） | PC 毫秒级返回，足够 |
| TOFU 审批等待 | **30s**（`APPROVAL_TIMEOUT`） | **35s**（`authChallengeTimeout`） | 手机端 = PC 审批 + 5s 容错 |
| TOFU auth_proof 等待 | 10s（`AUTH_TIMEOUT`） | — | 手机发完 auth_proof 后等 config，走 `connectTimeout` 或心跳 |

#### 实现

**PC 端**（`api.py`）：审批等待逻辑见 §7.4 的完整伪代码。`APPROVAL_TIMEOUT = 30` 秒，通过 `asyncio.wait_for` 控制超时。审批通过后才做 ECDH + 创建 Provider + 发 auth_challenge。

`_await_approval` 是一个等待 dashboard 审批信号的异步函数（如 `asyncio.Future`），dashboard UI 通过回调设置结果。

**手机端**（`mobile.html`）：

```javascript
// WSClient.connect() 中，onopen 发 auth 后：
if (this.secure.isTofuFirst) {
    this._authChallengeTimer = setTimeout(() => {
        this._showApprovalTimeout();
        this.ws.close();
    }, 35000);  // APPROVAL_TIMEOUT(30s) + 5s margin
}

// onmessage 收到 auth_challenge 后：
if (this._authChallengeTimer) {
    clearTimeout(this._authChallengeTimer);
    this._authChallengeTimer = null;
}
```

#### 认证模式 / TOFU 重连不变

认证模式和 TOFU 重连不需要审批，PC 毫秒级返回 auth_challenge，现有 `AUTH_TIMEOUT = 10s` 和 `connectTimeout = 3s` 足够。不引入新超时。

### 5.5 4 位识别码

> ⚠️ 本节描述的是**当前实现**。它有一个已知弱点（识别码可被抄走抢配对），改进提案见 [§5.5.1](#551-已知弱点与提案改由-pc-指派并密封下发识别码待实施)。

**生成与显示**

- 手机在 TOFU 首次连接时生成 4 位纯数字识别码：从 `0-9` 中随机取 4 位，如 `3847`。
- 识别码放在 auth 帧的 `pin` 字段中明文传输。
- 手机屏幕显示识别码（如连接等待界面的大字显示）。
- PC 收到 auth 后在主界面审批通知中显示同一个识别码。

**用户核对**

用户对比手机屏幕和 PC 主界面的识别码：
- **一致** → 点"接受"（确认这个连接请求来自自己的手机）。
- **不一致** → 点"拒绝"（可能是他人抢先连接或 MITM）。

**安全作用**

识别码防的是"攻击者抢在真机之前连上 PC，用户不看来源就点接受"：

- 攻击者直接连 PC：攻击者的手机生成自己的识别码（如 `5729`），PC 主界面显示 `5729`，但真机屏幕显示的是 `3847`。用户看到不一致 → 拒绝。
- 攻击者 MITM：MITM 分别与手机和 PC 建立连接，但手机生成的识别码在 auth 帧里传给 MITM，MITM 再连 PC 时用自己的识别码。PC 显示的是 MITM 的码，不是手机的码。用户核对 → 不一致 → 拒绝。

> 识别码不是密码学保护。它的作用是给**用户审批**提供一个可核对的凭据，类似蓝牙配对码。4 位纯数字碰撞概率为 1/10,000（10^4）。
>
> ⚠️ 上面「攻击者每次连接只猜一次、成功率 0.01%」的结论，前提是**识别码不可被攻击者获取**——而现状并不成立：识别码在明文 auth 里，可被抄走复用，成功率实际接近 100%。详见 §5.5.1。

#### 5.5.1 已知弱点与提案：改由 PC 指派并密封下发识别码（**待实施**）

> 本节是**提案**，代码尚未按此实现。

**现状的弱点：识别码可被抄走**

识别码与手机公钥都在明文 auth 里，同网段的被动窃听者能拿到**目标识别码**，然后用自己的密钥对连 PC：

1. 窃听到手机 auth → 得到 `pk_手机` 与 `pin=3847`；
2. 自己连 PC，发 `{pk_攻击者, pin: 3847}`；
3. PC 审批通知显示的码与手机屏幕**完全一致** → 用户点「接受」；
4. 挑战 `SealedBox(pk_攻击者)` 攻击者解得开（他有自己的私钥）→ 拿到 PC 公钥与会话密钥 → 完成 auth_proof。

即**一次被动窃听 + 一次主动连接即可确定性得手**（成功率 ≈ 100%，不需要任何算力）。第 3 步成立，是因为识别码由手机自选、明文可读、可复制；而 §5.5 的论证假设的是"攻击者用自己的识别码"，这一步假设与"明文传输"互相矛盾。

**提案：识别码由 PC 生成，密封下发给当前连接方**

对称性在这里是决定性的：**PC 已从明文 auth 里拿到 `phone_pub`，所以 PC 能密封；手机还没有 `pc_pub`，所以手机不能。** 这是无信任阶段唯一能把一个共享秘密送进手机的通道。

```
手机 → PC:  auth {algo, pk}                                   明文（不再带 pin）
PC  → 手机: sealed {nonce, PIN}  = SealedBox(pk, ...)         新增：PIN 由 PC 生成、每连接独立
手机:       解封（失败即报错）→ 大字显示 PIN，然后什么都不发
用户:       核对手机 PIN 与 PC 审批通知上的 PIN → 一致则点「接受」
PC  → 手机: auth_challenge = SealedBox(pk, {pc_pub, nonce})    审批通过后才发（与现状一致）
手机 → PC:  auth_proof（会话密钥加密）                          与现状一致
```

要点：

- **`pc_pub` 仍然只在审批通过后才发**——PC 公钥是 token，不能提前泄露，这一点不变。
- **第 2 步的 `nonce` 与第 5 步复用同一个**：让"发 PIN 的那条"和"发挑战的那条"必须是同一个对端，PIN 与挑战同源。
- **手机不再生成识别码**：`pin` 字段从 auth 帧删除，`_generatePin()` 与审批浮层的赋值时机改为"收到第 2 步之后"。
- 手机**始终不发**任何与 PIN 相关的帧——没有可复制、可重放的东西。

**为什么这堵住了洞**

攻击者只有两条路，且都不通：

| 攻击者的选择 | 能否让电脑显示手机上的那个 PIN | 能否完成握手 | 结论 |
|---|---|---|---|
| 用**自己的**公钥 | 不能——PIN 由 PC 随机指派，与攻击者任何可控输入无关（**没有枚举空间可刷**） | 能 | 两边 PIN 不同 → 用户拒绝 |
| **抄手机的 auth**（用 `pk_手机` 重放） | 能（PC 就是给这个 pk 发的 PIN） | 不能——挑战密封给 `pk_手机`，攻击者没有对应私钥 | 真机不在攻击者那条连接上，手机屏幕不会出现这个 PIN → 用户拒绝 |

对比现状，两个攻击面同时消失：**抄不走**（PIN 从不出现在明文链路上，只有持有 `sk_手机` 的一方能解封）、**也刷不出**（PIN 与公钥无关，不存在 10^4 的枚举空间）。

这一点**优于"派生码"思路**：派生码的输入含攻击者可控的公钥，必须再引入"先押注后揭晓"的电脑侧随机数才能防离线枚举；而本方案里 PIN 由 PC 独立随机生成，枚举空间根本不存在。（"用私钥签名 PIN"则在任何形态下都无用：签名只证明"持有帧里那把公钥的私钥"，而攻击者本来就持有自己的私钥，自签同值校验恒真。）

**残余风险（边界）**

1. **主动中继 / 在途中人**：能截断并转发的人可以把手机的 auth 转给 PC、再把 PC 的密封 PIN 转给手机，用户核对仍会通过——攻击者成为一条透明中继。但会话密钥是手机与 PC 之间 ECDH 出来的，中继读不到内容、也伪造不了帧（AEAD + seq 拒绝），只能阻断或观察流量特征。**本方案不覆盖这一类**（与"无中间人"前提一致）。
2. **人类因素**：不核对就点「接受」时，任何识别码方案都失效。缓解仍是识别码大字显示 + 一并显示来源 IP（已实现）。
3. **4 位偶然相同**：攻击场景下两边显示的是两个**独立随机**的数，偶然相同概率 1/10⁴（与现状同一量级，但性质从"可确定性复制"变为"只能撞运气"）。提高位数可降低该值，代价是可读性。注意攻击者**无法判断自己是否撞中**——PIN 是密封给手机公钥的，他解不开，所以不存在"刷到命中就停"的可能。
4. **超时预算不变**：手机仍需等待"审批 + 挑战"，35s 上限（30s 审批 + 5s 容错）继续适用；第 2 步本身应计入连接的短超时。

**实施时要改的落点**（供后续拆分任务）

| 位置 | 改动 |
|---|---|
| `phonemic/tunnel/e2ee.py` | `receive_auth` 不再取 `pin`；新增生成 PIN 与密封下发的方法（复用 `SealedBox(phone_public)`） |
| `phonemic/server/api.py` | `_handle_auth` 在收到 auth 后先发 `sealed {nonce, PIN}`，再进入审批等待；审批通过后发挑战 |
| `phonemic/resources/mobile.html` | 删除手机端 PIN 生成；改为收到第 2 步后解封并显示；`pin` 不再入 auth 帧 |
| `docs/wire-protocol.md` §7 | auth 帧去掉 `pin` 字段；新增 `sealed {nonce, PIN}` 帧 |
| 测试 | 现有 TOFU 用例改为"PC 指派 PIN"；新增"窃听者连接后两边 PIN 不同"的用例 |


### 5.6 三种握手路径对比

| 方面 | URL fragment 认证（模式 2/3） | TOFU 首次（模式 1） | TOFU 重连（模式 1） |
|---|---|---|---|
| auth 帧 | SealedBox(pc_public): `{algo, pk}` | 明文 `{algo, pk, pin}` | SealedBox(pc_public): `{algo, pk}` |
| auth_challenge 帧 | Provider 加密整帧 | SealedBox(phone_public): `{pc_public, nonce}` | Provider 加密整帧 |
| auth_proof 帧 | Provider 加密 | Provider 加密 | Provider 加密 |
| config 及之后 | Provider 加密 | Provider 加密 | Provider 加密 |
| PC 端审批 | 不需要 | **需要** | 不需要 |
| 手机何时创建 Provider | `init()`（有 PC 公钥） | 收到 auth_challenge 时 | `init()`（有 PC 公钥 from localStorage） |
| 算法选择 | 从 QR `a=` 列表协商 | 默认 `xchacha20` | 默认 `xchacha20` |
| PC 公钥传递 | QR fragment（带外） | auth_challenge SealedBox（带内加密） | localStorage（首次带内加密获取） |

从 auth_proof 起，三种路径的帧格式和处理逻辑完全一致。差异仅在前两步和审批环节。

### 5.7 状态机

状态机结构不变（S0 → S1 → S2），路径分叉：

```
URL fragment / TOFU 重连:  S0 --auth(SealedBox)--> S1(密钥就绪) --auth_proof--> S2(已认证)
TOFU 首次:                S0 --auth(明文pk+pin)--> S0_pending(待审批) --审批通过--> S1(密钥就绪) --auth_proof--> S2
                          S0_pending --审批拒绝/超时--> close
```

TOFU 首次连接新增 `S0_pending`（待审批）中间状态：auth 已收到，ECDH 已完成，Provider 已创建，但等待用户审批才发 auth_challenge。

`needs_auth`（"需要握手"）对所有模式恒为 `True`——不存在"连上即就绪"的路径。

---

## 6. 密钥交换变更（KeyExchange）

### 6.1 新增方法

```python
class KeyExchange:
    """算法无关的密钥交换。持有 PC 长期 X25519 身份私钥。"""

    def handle_auth(self, sealed_data: bytes) -> tuple[str, bytes]:
        """认证模式 / TOFU 重连：解 SealedBox → 读 algo+pk → ECDH → KDF。"""
        inner = SealedBox(self._pc_private).decrypt(sealed_data)
        d = json.loads(inner)
        algo = d["algo"]
        if algo not in self._allowed:
            raise ValueError(f"algorithm {algo!r} not allowed")
        phone_public = PublicKey(_from_b64(d["pk"]))
        shared = crypto_scalarmult(bytes(self._pc_private), bytes(phone_public))
        session_key = blake2b(shared, digest_size=32).digest()
        return algo, session_key

    def handle_tofu_auth(self, algo: str, phone_public_bytes: bytes) -> bytes:
        """TOFU 首次：algo 和手机公钥均为明文，直接 ECDH → KDF。
        返回 PC 公钥原始字节（用于 SealedBox 加密回传）。"""
        if algo not in self._allowed:
            raise ValueError(f"algorithm {algo!r} not allowed")
        phone_public = PublicKey(phone_public_bytes)
        shared = crypto_scalarmult(bytes(self._pc_private), bytes(phone_public))
        session_key = blake2b(shared, digest_size=32).digest()
        return algo, session_key

    @property
    def public_key_bytes(self) -> bytes:
        """PC 公钥原始字节。TOFU 模式下通过 SealedBox 加密回传给手机。"""
        return bytes(self._pc_private.public_key)
```

三个方法共享相同的 ECDH + KDF 逻辑，区别在 auth 数据的来源：
- `handle_auth`：SealedBox 解封后取参数（认证路径）。
- `handle_tofu_auth`：明文参数直接传入（TOFU 首次路径）。
- `public_key_bytes`：提供 PC 公钥用于 SealedBox 加密回传。

### 6.2 为什么不合并

- `handle_auth` 的输入是一个不透明的 sealed blob（`bin`），消息层不解释其内容。
- `handle_tofu_auth` 的输入是两个明文字段（`algo` str + `pk` bin），消息层需要从 auth 帧中分别取出再传入。
- 输入签名不同，强行合并会让消息层分支判断渗入密钥交换层。

### 6.3 PC 公钥 = token

PC 公钥在此架构中承担 **bearer token** 的角色：

- **URL fragment 认证模式**：PC 公钥在 QR fragment 中带外分发，能密封 SealedBox 即证明持有此 token。
- **TOFU 模式**：PC 公钥在首次审批后通过 SealedBox(phone_public) 加密传给手机，手机存入 localStorage。重连时用此公钥做 SealedBox 密封 auth，PC 解封成功 = token 验证通过。

两种方式的 token 安全性：
- URL fragment：QR 码的物理可见性限制了 token 的传播范围。
- TOFU：SealedBox(phone_public) 加密回传，只有持有 phone_private 的设备才能获取 token。被动窃听者无法提取。

---

## 7. SecureChannel / SecureSession 变更

### 7.1 SecureChannel

```python
class SecureChannel:
    def __init__(self, auth_method: str, mode: str):
        self._auth_method = auth_method     # "tofu" | "url_fragment"
        self._mode = mode
        self._key_exchange = KeyExchange(PrivateKey.generate(), OFFERED_ALGORITHMS)
        if auth_method == "url_fragment":
            self._secret_path = token_urlsafe(32)   # 认证模式：防扫描
        else:
            self._secret_path = ""                  # TOFU：裸 URL
```

变更点：

- **`_algorithm` 字段删除**。不再有 `"none"` 分支，加密永远开启。
- **`_token` 字段删除**。`none` + CF 的 token 认证路径整体移除。
- **`KeyExchange` 永远创建**。所有模式都持有 PC 密钥对——TOFU 模式也需要做 ECDH。
- **`_secret_path` 仅 URL fragment 模式生成**。TOFU 模式为空串，QR 为裸 URL。

### 7.2 SecureSession

`receive_auth()` 增加分叉。TOFU 首次模式下该方法只解析明文字段、不做 ECDH：

```python
def receive_auth(self, auth_msg: dict):
    """返回 (algo, session_key_or_none, pin_or_none, phone_pk_or_none)。
    认证模式 / TOFU 重连：(algo, session_key, None, None)——密钥就绪。
    TOFU 首次：(algo, None, pin, phone_pk)——密钥待审批后建。"""
    if self._channel.auth_method == "url_fragment":
        # 认证模式：SealedBox 路径
        sealed = auth_msg["data"]
        algo, session_key = self._channel.key_exchange.handle_auth(sealed)
        return algo, session_key, None, None
    elif "data" in auth_msg:
        # TOFU 重连（localStorage 有 PC 公钥，手机走 SealedBox）
        sealed = auth_msg["data"]
        algo, session_key = self._channel.key_exchange.handle_auth(sealed)
        return algo, session_key, None, None
    else:
        # TOFU 首次：明文路径，只解析字段，不做 ECDH
        algo = auth_msg["algo"]
        phone_pk = auth_msg["pk"]
        pin = auth_msg.get("pin")
        return algo, None, pin, phone_pk
```

TOFU 首次审批通过后，调用 `complete_tofu_auth()` 做 ECDH + 创建 Provider：

```python
def complete_tofu_auth(self, algo: str, phone_pk: bytes):
    """审批通过后调用：做 ECDH → KDF → 创建 Provider。"""
    algo, session_key = self._channel.key_exchange.handle_tofu_auth(algo, phone_pk)
    self._provider = create_provider(algo, session_key)
```

> TOFU 重连的 auth 帧与 URL fragment 认证的 auth 帧格式完全一致（都是 `{type:"auth", data:<SealedBox>}`），因此 PC 端可通过 `auth_msg` 是否有 `data` 字段判断是认证路径还是 TOFU 首次路径。

`make_auth_challenge()` 在 TOFU 首次模式下返回 SealedBox 加密帧：

```python
def make_auth_challenge(self) -> bytes:
    nonce = secrets.token_bytes(16)
    if self._channel.auth_method == "url_fragment" or self._has_token:
        # 认证模式 / TOFU 重连：整帧 Provider 加密（现有逻辑不变）
        frame = {"type": "auth_challenge", "nonce": nonce}
        return self._provider.encrypt(msgpack.packb(frame))
    else:
        # TOFU 首次：SealedBox(phone_public) 加密 {pc_public, nonce}
        inner = json.dumps({
            "pk": _to_b64(self._channel.key_exchange.public_key_bytes),
            "nonce": _to_b64(nonce),
        })
        sealed = SealedBox(self._phone_public).encrypt(inner.encode())
        frame = {"type": "auth_challenge", "data": sealed}
        return msgpack.packb(frame)      # 不用 Provider 加密——手机还没有 Provider
```

`verify_auth_proof()` 和 `wrap()` / `unwrap()` 两种模式完全一致——密钥就绪后，加解密逻辑不关心密钥是怎么来的。

### 7.3 `needs_auth` 语义

`needs_auth` 恒为 `True`（所有模式都需要握手）。`none` + LAN 的"连上即就绪"路径删除。`api.py` 的 `_handle_auth()` 不再有跳过握手的分支。

### 7.4 TOFU 审批机制

#### PC 端流程

`api.py` 的 `_websocket_endpoint()` 中，TOFU 首次连接的握手流程：

```python
session = _secure_channel.new_session()

# S0：收 auth
auth_msg = await _recv_handshake_frame(websocket, deadline)
algo, session_key, pin, phone_pk = session.receive_auth(auth_msg)

if pin is not None:
    # TOFU 首次：等待用户审批（尚未做 ECDH）
    try:
        approved = await asyncio.wait_for(
            _await_approval(pin, client_ip),
            timeout=APPROVAL_TIMEOUT
        )
    except asyncio.TimeoutError:
        approved = False
    if not approved:
        await websocket.close(code=4032, reason="rejected" if not approved else "timeout")
        return
    # 审批通过：现在才做 ECDH + 创建 Provider
    session.complete_tofu_auth(algo, phone_pk)
else:
    # 认证模式 / TOFU 重连：session_key 已就绪，直接创建 Provider
    session.create_provider(algo, session_key)

# 以下握手与认证模式一致
await _send_frame(websocket, session.make_auth_challenge())
proof = await _recv_handshake_frame(websocket, deadline)
if not session.verify_auth_proof(proof):
    await _send_frame(websocket, session.wrap(msgpack.packb({"type":"error","code":"auth"})))
    await websocket.close()
    return
```

#### 审批 UI

- dashboard 主界面内嵌审批通知（非模态弹窗），显示：
  - 4 位识别码（大字显示）
  - 连接来源 IP
  - "接受" / "拒绝" 两个按钮
- 审批超时（如 30s）自动拒绝。
- 审批期间手机端显示"等待 PC 审批..."。

#### 手机端处理

- 收到 close code 4032（rejected）→ 停止自动重连，UI 提示"连接被拒绝"。
- 审批通过后正常进入数据帧阶段。
- 首次连接成功后，PC 公钥存入 `localStorage`，后续重连无需审批。

#### 天然挤占保护

TOFU 审批机制天然提供了挤占保护：

- 任何新连接都必须经过 PC 用户审批才能注册。
- 攻击者连上后，PC 弹出审批通知，用户看到不是自己手机的识别码 → 拒绝。
- 当前活动连接不受影响——审批中的新连接处于 `S0_pending` 状态，不注册到 `_manager`。
- **不需要额外的挤占保护代码**——审批本身就是门禁。

> URL fragment 认证模式（模式 2/3）保持现有挤占行为不变。能完成 SealedBox 握手的人持有 PC 公钥（信任锚），其连接权限与挤占权限在同一信任级别。

---

## 8. 手机端变更

### 8.1 `crypto_providers.js`

- **`PlainProvider` 删除**。
- **`sealedAuthData()` 用于认证模式 / TOFU 重连**。无认证模式不密封，直接返回明文 `{algo, pk, pin}`。
- 新增 `plaintextAuthData(providerName, phonePublicKey, pin)`：

```javascript
function plaintextAuthData(providerName, phonePublicKey, pin) {
    return {
        algo: providerName,
        pk: phonePublicKey,          // Uint8Array，msgpack 编码为 bin
        pin: pin,                    // 4 位识别码字符串
    };
}
```

- `PROVIDER_CLASSES` 删除 `"none"` 条目。
- 各 Provider 的 `makeAuthData()` 返回值不变（Uint8Array = SealedBox blob），TOFU 首次不走这个方法。

### 8.2 `mobile.html` — SecureClient

`init()` 的分叉：

```javascript
async init() {
    await sodium.ready;
    const frag = this._parseUrlFragment();      // {k, a}

    // 检查 localStorage 是否有 PC 公钥
    const storedKey = localStorage.getItem("phonemic_pc_pubkey");

    if (frag.k !== null) {
        // URL fragment 认证模式（现有逻辑不变）
        this._pcPublicKey = base64ToBytes(frag.k);
        this._algorithm = this._selectAlgorithm(frag.a);
        this._provider = this._createProvider(this._algorithm);
        this._provider.init(this._pcPublicKey);
    } else if (storedKey) {
        // TOFU 重连（localStorage 有 PC 公钥）
        this._pcPublicKey = base64ToBytes(storedKey);
        this._algorithm = "xchacha20";
        this._provider = this._createProvider(this._algorithm);
        this._provider.init(this._pcPublicKey);
    } else {
        // TOFU 首次连接
        this._pcPublicKey = null;                 // 还没有，等 auth_challenge
        this._algorithm = "xchacha20";
        this._pin = generatePin();                // 生成 4 位识别码
        this._showPin(this._pin);                 // 手机屏幕显示识别码
        this._provider = this._createProvider(this._algorithm);
        this._provider.initKeypair();             // 只生成密钥对，不做 ECDH
    }
}
```

`makeAuth()` 的分叉：

```javascript
makeAuth() {
    if (this._pcPublicKey !== null) {
        // 认证模式 / TOFU 重连：SealedBox 密封
        const auth = { type: "auth" };
        const sealed = this._provider.makeAuthData();   // Uint8Array
        auth.data = sealed;
        return auth;
    } else {
        // TOFU 首次：明文
        return {
            type: "auth",
            algo: this._algorithm,
            pk: this._provider.getPhonePublicKey(),      // Uint8Array
            pin: this._pin,                              // 4 位识别码
        };
    }
}
```

`makeAuthProof(rawBytes)` 的分叉：

```javascript
makeAuthProof(rawBytes) {
    if (this._pcPublicKey !== null) {
        // 认证模式 / TOFU 重连：整帧先解密再解析（现有逻辑不变）
        const decrypted = this._provider.decrypt(rawBytes);
        const msg = msgpack.unpack(decrypted);
        const nonce = msg.nonce;
        this._authenticated = true;
        return this._provider.encrypt(
            msgpack.pack({ type: "auth_proof", nonce })
        );
    } else {
        // TOFU 首次：先解封 SealedBox 取 pc_public + nonce
        const sealed = rawBytes;
        const inner = sodium.crypto_box_seal_open(sealed, this._provider.getPhonePublicKey(), this._provider.getPhonePrivateKey());
        const msg = JSON.parse(sodium.to_string(inner));
        const pcPublicKey = base64ToBytes(msg.pk);
        const nonce = base64ToBytes(msg.nonce);

        // 此时才做 ECDH 并初始化 Provider 的会话密钥
        this._provider.deriveSessionKey(pcPublicKey);
        this._pcPublicKey = pcPublicKey;

        // 存入 localStorage（作为 token，下次重连用）
        localStorage.setItem("phonemic_pc_pubkey", msg.pk);

        this._authenticated = true;
        return this._provider.encrypt(
            msgpack.pack({ type: "auth_proof", nonce })
        );
    }
}
```

`encrypt()` / `decrypt()` 无需改动——Provider 创建后，加解密逻辑与模式无关。

### 8.3 识别码生成

```javascript
function generatePin() {
    let pin = "";
    for (let i = 0; i < 4; i++) {
        pin += Math.floor(Math.random() * 10).toString();
    }
    return pin;
}
```

手机端在 TOFU 首次连接等待审批期间，屏幕大字显示识别码（如 `3847`）。

### 8.4 `isEncrypted` 属性

`isEncrypted` 恒为 `True`。`wire-protocol.md` 中依赖 `is_encrypted` 判定"该不该解"的逻辑简化为"永远解"。

### 8.5 sodium.js 永远加载

当前 `none` + LAN 不加载 sodium.js（323KB）。改为全加密后所有模式都需加载。首次连接的加载时间增加约一次 323KB 传输（局域网下 < 50ms）。`mobile.html` 的 sodium 加载逻辑从条件加载改为无条件加载。

### 8.6 PC 密钥变更后的重连流程

**场景**：用户用完手机后切到后台，PC 重启（或切换模式）导致密钥对重新生成。用户拿出手机切回前台，手机尝试重连。

**完整流程**：

```
手机                                               PC
 │  手机从后台恢复，自动重连
 │  localStorage 有旧 PC 公钥 → 走认证路径
 │
 │──── auth ──────────────────────────────────────────►│  SealedBox(old_pc_public): {algo, pk}
 │                                                      │  ← PC 用新私钥解封失败
 │◄── close 4001 ───────────────────────────────────────│  reason="invalid auth data"
 │
 │  手机收到 4001：
 │  1. 清除 localStorage 中的旧 PC 公钥
 │  2. UI 显示"PC 密钥已变更，等待重新审批"
 │  3. 自动重连 → 走 TOFU 首次路径
 │
 │──── auth ──────────────────────────────────────────►│  明文 {algo, pk, pin:"3847"}
 │  手机屏幕显示识别码 3847                               │
 │                                                      │  ← PC 主界面显示审批通知（识别码 3847）
 │                                                      │  ← 用户核对手机与 PC 的识别码一致
 │                                                      │  ← 用户点"接受"
 │◄── auth_challenge ────────────────────────────────────│  SealedBox(phone_public): {pc_public, nonce}
 │──── auth_proof ────────────────────────────────────►│  Provider.encrypt
 │◄── config ───────────────────────────────────────────│  Provider.encrypt
 │
 │  手机存储新 PC 公钥到 localStorage
 │  连接就绪
```

**手机端处理 4001 的逻辑**：

```javascript
// WSClient.onclose 或 auth 失败处理
if (closeCode === 4001) {
    if (localStorage.getItem("phonemic_pc_pubkey")) {
        // 旧密钥失效：清除 localStorage，自动重连走 TOFU
        localStorage.removeItem("phonemic_pc_pubkey");
        this._showStatus("PC 密钥已变更，等待重新审批");
        this._scheduleReconnect();   // 自动重连，不等用户操作
    } else {
        // 全新连接也 4001：可能是实现异常，提示重新扫码
        this._showStatus("认证失败，请重新扫码");
        this.authRejected = true;
    }
}
```

- **自动重连**：收到 4001 且 localStorage 有旧 key → 清除 localStorage → 自动重连（走 TOFU 首次路径）。用户不需要手动操作。
- **手机端 UI 区分**：重连场景（之前有 localStorage）显示"PC 密钥已变更，等待重新审批"；全新连接显示"等待 PC 审批"。仅 UI 文案不同，协议层完全一致。
- **PC 端不区分**：PC 收到的 TOFU auth 帧与全新连接的 auth 帧完全相同（`{algo, pk, pin}`），PC 无法也不需要区分两者。审批通知统一显示"新连接请求 + 识别码"。

### 8.7 其他 localStorage 清理场景

- **用户手动清除**：手机端设置中提供"清除信任的设备"按钮，删除 localStorage 中的 PC 公钥。下次连接走 TOFU 首次路径。
- **PC 切换到 URL fragment 认证模式**：PC 密钥对重新生成（`SecureChannel` 重建），旧 localStorage 公钥失效。手机收到 4001 → 清除 localStorage → 如果新 QR 有 fragment → 走 URL fragment 认证路径；如果新 QR 无 fragment → 走 TOFU 首次路径。
- **localStorage 被清除（浏览器隐私模式等）**：每次连接都是 TOFU 首次路径，每次都需要 PC 审批。这在隐私模式下是合理行为。

---

## 9. 移除清单

| 移除项 | 文件 | 原因 |
|---|---|---|
| `PlainProvider` | `crypto/plain.py` + `crypto_providers.js` | 加密永远开启，无明文路径 |
| `e2ee_algorithm` 配置键 | `settings_manager.py` | 被 `auth_method` 替代 |
| `effective_algorithm()` | `mode.py` | 不再有 `none` → `auto` 的强制逻辑 |
| `_token` 字段 | `e2ee.py` `SecureChannel` | `none` + CF 的 token 认证路径删除 |
| `none` + CF token 认证路径 | `e2ee.py` `SecureSession.receive_auth` | 无明文模式 |
| `none` + LAN 直连路径 | `e2ee.py` `SecureChannel` / `api.py` `_handle_auth` | 无明文模式 |
| `act_algo_none` UI 选项 | `gui/dashboard.py` | 加密选项变为认证方式选择 |
| `"none"` 在 `_PROVIDER_CLASSES` | `crypto/__init__.py` | PlainProvider 删除 |
| `is_encrypted` 的 `False` 分支 | `e2ee.py` / `api.py` / `mobile.html` | 永远为 `True` |

---

## 10. 安全分析

### 10.1 公钥暴露无害论证（被动窃听）

ECDH 共享密钥的计算：

```
PC 端:     shared = crypto_scalarmult(pc_private, phone_public)
手机端:    shared = crypto_scalarmult(phone_private, pc_public)
```

被动窃听者在 TOFU 首次连接中能观测到：
- auth 帧中的 `phone_public`（明文）

但 auth_challenge 中的 `pc_public` 被 SealedBox(phone_public) 加密——窃听者无法解封（没有 phone_private）。即使假设窃听者拿到了两个公钥，也**无法计算 `shared`**——这是 Curve25519 离散对数难题（DLP）。

这与 TLS 1.3 的 ephemeral DH 思路一致：公钥在握手中传输，安全性依赖私钥不泄露。

### 10.2 PC 公钥 = token 的保密性

PC 公钥在此架构中是 bearer token：

| 模式 | PC 公钥传递方式 | 暴露风险 |
|---|---|---|
| URL fragment 认证 | QR fragment（物理带外） | QR 码可见性范围内 |
| TOFU 首次 | SealedBox(phone_public) 加密回传 | **不暴露**——只有 phone_private 持有者能解封 |
| TOFU 重连 | localStorage（首次获取） | 不再传输 |

TOFU 模式下 PC 公钥的保密性甚至**优于** URL fragment 模式：QR 码可能被旁人看到，但 SealedBox 加密回传只有目标手机能解封。

### 10.3 TOFU 首次连接的 MITM 风险

TOFU 首次连接没有信任锚（手机没有 PC 公钥）。主动 MITM 攻击者可以：

1. 拦截手机的 auth 帧，获取 `phone_public` 和 `pin`
2. 生成自己的密钥对 `(mitm_private, mitm_public)`
3. 向 PC 发送 auth：`{algo, pk: mitm_public, pin: <MITM自己的码>}` → PC 显示 MITM 的码
4. 向手机发送 auth_challenge：`SealedBox(phone_public): {mitm_public, nonce}` → 手机做 ECDH(phone_private, mitm_public) → `session_key_2`

**但识别码会暴露 MITM**：
- 手机的屏幕显示 `3847`（手机生成的码）
- PC 的审批通知显示 `5729`（MITM 的码，因为 MITM 向 PC 发了自己的 auth）
- 用户核对 → 不一致 → 拒绝

因此 MITM 攻击在用户核对识别码的前提下**可被检测**。这是检测型防护（类似蓝牙配对码），不是预防型。

> ⚠️ 上面这条论证**依赖"攻击者无法获得真机的识别码"**，而现状并不成立：识别码在明文 auth 里可被抄走，攻击者可以直接复用真机的码骗过核对（§5.5.1 有完整攻击链）。识别码改由 PC 指派并密封下发之后，这条论证才真正成立。

残余风险：用户不核对识别码就点"接受"。缓解措施：PC 审批通知把识别码做大字显示，引导用户核对。

### 10.4 TOFU 重连后的安全性

TOFU 首次审批通过后，手机 localStorage 有 PC 公钥。重连走 SealedBox 认证路径：

- 能解封 SealedBox = 持有 PC 公钥 = token 验证通过。
- MITM 没有 PC 公钥，无法伪造 SealedBox 密封的 auth。
- 安全等级与 URL fragment 认证模式一致。

**localStorage 被窃取的风险**：如果攻击者从手机 localStorage 偷走 PC 公钥，可以伪造 SealedBox auth 绕过审批。但这需要攻击者已入侵手机浏览器——威胁等级远高于网络窃听，不在本架构的威胁模型范围内。

### 10.5 与现状的对比

| 威胁 | 现状（none + LAN） | TOFU 首次 | TOFU 重连 | URL fragment 认证 |
|---|---|---|---|---|
| 被动嗅探 | **完全暴露** | **安全**（ECDH + AEAD） | **安全** | **安全** |
| 主动 MITM | 可 MITM | 可 MITM，但识别码可检测 | **安全**（有信任锚） | **安全**（有信任锚） |
| 连接挤占 | 任何人可挤占 | **天然防护**（需审批） | **天然防护**（需 token） | 持有 QR 者可挤占 |
| 重放 | 无防护 | AEAD seq + nonce | AEAD seq + nonce | AEAD seq + nonce |

TOFU 首次连接相对现状是**明确的净改进**——消除了明文通信，增加了审批门禁。重连后达到与扫码认证一致的安全等级。

### 10.6 认证模式（模式 2/3）安全性不变

认证模式的信任模型与 `crypto-design.md` §2 完全一致：PC 公钥作为带外 bearer token，SealedBox 解封即认证。QR 码带外分发是信任锚，MITM 无法伪造。本次变更不触及该路径的任何代码。

### 10.7 浏览器端 E2EE 的天花板

`crypto-design.md` §2 的浏览器端 E2EE 天花板分析依然适用：明文 HTTP 下发的 `mobile.html` 可被篡改，能改包的人可以注入 JS 偷走 fragment 或 localStorage。TOFU 模式下 fragment 中无 PC 公钥，但 localStorage 中的 PC 公钥可被注入 JS 读取。CF 模式有 TLS 保护链路。

---

## 11. 实现影响清单

| 文件 | 变更类型 | 内容 |
|---|---|---|
| `phonemic/tunnel/crypto/key_exchange.py` | 新增 | `handle_tofu_auth()` 方法 + `public_key_bytes` 属性 |
| `phonemic/tunnel/crypto/plain.py` | **删除** | `PlainProvider` 移除 |
| `phonemic/tunnel/crypto/__init__.py` | 修改 | 移除 `"none"` 注册；`OFFERED_ALGORITHMS` 不变 |
| `phonemic/tunnel/e2ee.py` | 修改 | `SecureChannel` 用 `auth_method` 替代 `_algorithm`；`SecureSession.receive_auth` / `make_auth_challenge` 增加 TOFU 分叉；删除 `_token` 路径 |
| `phonemic/tunnel/mode.py` | 修改 | `effective_algorithm` → `effective_auth_method` |
| `phonemic/utils/settings_manager.py` | 修改 | `e2ee_algorithm` → `auth_method` + 迁移逻辑 |
| `phonemic/server/api.py` | 修改 | `_handle_auth` 无 `needs_auth=False` 跳过分支；auth 帧解析按 `auth_method` 分叉；TOFU 审批等待逻辑；`APPROVAL_TIMEOUT = 30` 常量 |
| `phonemic/PhoneMic.py` | 修改 | `SecureChannel` 构造参数从 `algorithm=` 改为 `auth_method=` |
| `phonemic/gui/dashboard.py` | 修改 | 加密开关 → 认证方式选择（TOFU / 扫码）；CF 模式下强制扫码；TOFU 审批通知 UI（主界面内嵌，非弹窗） |
| `phonemic/resources/crypto_providers.js` | 修改 | 删除 `PlainProvider`；新增 `plaintextAuthData()` |
| `phonemic/resources/mobile.html` | 修改 | `SecureClient.init` / `makeAuth` / `makeAuthProof` 增加分叉；sodium.js 无条件加载；识别码生成与显示；localStorage 持久化 |
| `tests/test_e2ee.py` | 修改 | 移除 `none` 模式测试；新增 TOFU 握手测试 |
| `tests/test_e2ee_server.py` | 修改 | 同上 |

### 测试要点

- TOFU 首次握手往返：`auth(明文+pin) → 审批 → auth_challenge(SealedBox) → auth_proof(加密) → config(加密)`
- TOFU 首次 ECDH 一致性：PC 和手机独立计算 `session_key`，Provider 加解密往返成功
- TOFU 重连握手：等同 URL fragment 认证，`auth(SealedBox) → auth_challenge(Provider加密) → auth_proof(加密)`
- TOFU 审批拒绝：用户点"拒绝" → close 4032，手机端停止重连
- TOFU 审批超时：30s 无操作 → close 4032（reason="timeout"）
- TOFU 手机端 auth_challenge 等待超时：35s（30s 审批 + 5s 容错）后手机端主动断开
- 识别码核对：手机生成码 = PC 显示码
- localStorage 失效（PC 重启）：旧公钥 SealedBox 解封失败 → close 4001 → 手机清除 localStorage → 自动重连走 TOFU 首次路径 → 审批通过 → 新公钥存入 localStorage
- 4001 后自动重连：手机收到 4001 且有 localStorage → 自动清除并重连，不需要用户手动操作
- 认证模式回归：现有 `test_e2ee_server.py::TestAuthHandshake` 全部通过（零改动）
- 配置迁移：`e2ee_algorithm: "none"` → `auth_method: "tofu"`；`e2ee_algorithm: "auto"` → `auth_method: "url_fragment"`

---

## 12. 与现有文档的关系

| 文档 | 影响 |
|---|---|
| `crypto-design.md` | §2 威胁模型表更新（无明文行，新增 TOFU）；§3 新增 `handle_tofu_auth`；§4 删除 `PlainProvider`；§7 `needs_auth` 恒为 True |
| `wire-protocol.md` | §4 加密边界更新（无明文路径）；§5 握手流程增加 TOFU 分支；§6 type 表 `auth` 帧增加 `algo`/`pk`/`pin` 明文字段说明；§10 状态表删除明文行 |
| 本文档 | 以上变更的完整设计依据 |

实现时需同步更新 `crypto-design.md` 和 `wire-protocol.md` 的相关章节，保持文档与代码一致。
