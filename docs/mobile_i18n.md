# 移动端国际化（i18n）重构设计说明书

> 版本：2.0  
> 状态：定稿  
> 关联：PC 端语言包（`zh_CN.json` / `en_US.json`）、HTTP 接口 `/api/lang.json`  
> 目标：独立实现，不涉及鼠标面板等其他模块

---

## 1. 背景与目标

### 1.1 现状
- PC 端已有完整的国际化资源文件（`zh_CN.json` / `en_US.json`），其中 `mobile` 命名空间包含了手机端界面所需的所有翻译键值。
- 手机端页面（`mobile.html`）目前使用硬编码的 `DEFAULT_I18N` 对象和 HTML 内联占位符 `__I18N_JSON__`，这两者与 PC 端语言设置完全脱节，导致：
  - 当用户在 PC 端切换语言后，手机端必须重新修改代码或模板才能同步。
  - 翻译内容在两处维护（PC 端和手机端），容易不一致，维护成本高。

### 1.2 目标
1. **单一数据源**：由 PC 端作为唯一权威来源，通过 HTTP 接口提供当前语言下的 `mobile` 翻译数据。
2. **零硬编码**：手机端不再包含任何翻译文本，所有显示文字均从服务器获取。
3. **健壮的后备机制**：当接口不可用或某个键缺失时，页面依然可用，且能清晰提示缺失信息，帮助开发者定位问题。
4. **无缝同步**：PC 端切换语言后，手机端刷新页面即可自动获得新语言，无需额外部署。
5. **保持安全**：语言包接口不暴露敏感信息，可置于公开路径，同时利用相对路径自动适配加密前缀，避免扫描器发现。

---

## 2. 接口设计

### 2.1 端点
- **URL**：`/api/lang.json`（**相对路径**，将在前端使用 `fetch('api/lang.json')` 调用）
- **方法**：`GET`

### 2.2 行为
- 服务端根据当前 PC 端设置的语言（例如 `zh_CN` 或 `en_US`），加载对应的 JSON 文件，提取其中 `mobile` 子对象，并作为响应返回。
- 响应格式：纯 JSON 对象，键值对扁平（例如 `{ "btn_send": "发送", "greeting": "你好，{name}" }`）。

### 2.3 缓存控制
为防止浏览器缓存旧语言，响应头必须包含：
```
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
```

### 2.4 安全考虑
- 该接口仅返回界面文本，不包含任何密钥、会话标识或用户数据，因此可安全公开。
- 为支持加密模式（URL 带有随机前缀如 `/{secret}/`），前端使用**相对路径**请求，实际请求路径为 `/{secret}/api/lang.json`，扫描器无法枚举随机目录，安全性进一步提高。

### 2.5 服务端实现（`api.py`）

1. **添加路由处理函数**：
```python
async def _serve_lang_json(request: Request) -> Response:
    try:
        i18n = I18n.instance()
        mobile_data = i18n.get_section("mobile")  # 返回 dict
        return JSONResponse(
            content=mobile_data,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    except Exception as e:
        logger.error(f"Failed to get language data: {e}")
        return JSONResponse(content={}, status_code=500)
```

2. **在 `_PUBLIC_PATHS` 白名单中添加 `/api/lang.json`**，以便明文模式也能正常访问：
```python
_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/sodium.js",
    "/crypto_providers.js",
    "/msgpack.min.js",
    "/ws",
    "/api/lang.json",      # 新增
}
```

3. **在 `_dispatch_http` 中分发**：
```python
if normalized == "/":
    return _serve_mobile()
if normalized == "/api/lang.json":
    return _serve_lang_json(request)
# ... 其他路由
```

---

## 3. 前端加载与翻译函数

### 3.1 加载流程
在 `mobile.html` 的 `window.onload` 中，首先执行语言包加载，再初始化其他组件。

```javascript
// 语言数据容器
window.i18n = {};

// 加载函数
async function loadI18n() {
    try {
        const res = await fetch('api/lang.json');  // 相对路径自动适配前缀
        if (res.ok) {
            window.i18n = await res.json();
        } else {
            console.warn('i18n load failed, using fallback keys.');
            window.i18n = {};
        }
    } catch (e) {
        console.warn('Network error loading i18n, using fallback keys.', e);
        window.i18n = {};
    }
}

// 入口
window.onload = async () => {
    await loadI18n();   // 必须先加载语言
    // 然后初始化 ChatManager, WSClient, UIController 等
};
```

### 3.2 翻译函数 `t(key, params)`

#### 3.2.1 签名
```typescript
function t(key: string, params?: Record<string, string | number>): string
```

#### 3.2.2 核心逻辑（含后备策略）

```javascript
function t(key, params) {
    // 1. 获取翻译文本
    const text = window.i18n?.[key];
    
    // 2. 若翻译缺失（后备模式）
    if (text === undefined || text === null) {
        if (!params) {
            return key;                           // 无参数：直接返回键名
        }
        // 有参数：返回 "key(参数1=值1, 参数2=值2)" 形式，避免报错
        const paramStr = Object.entries(params)
            .map(([k, v]) => `${k}=${v}`)
            .join(', ');
        return `${key}(${paramStr})`;
    }
    
    // 3. 翻译存在，且需要格式化（替换 {placeholder}）
    if (params) {
        return text.replace(/\{(\w+)\}/g, (match, p1) => {
            const val = params[p1];
            return val !== undefined ? String(val) : match;   // 缺失时保留占位符
        });
    }
    
    // 4. 翻译存在，无需格式化
    return text;
}
```

#### 3.2.3 后备策略详解

| 场景 | 调用示例 | 返回值 | 说明 |
|------|---------|--------|------|
| 有翻译 + 无参数 | `t('btn_send_auto')` | `"自动"` | 正常 |
| 有翻译 + 有参数 | `t('greeting', {name: 'Tom'})` | `"你好，Tom"` | 正常替换 |
| 缺翻译 + 无参数 | `t('not_exist')` | `"not_exist"` | 清晰显示缺失键名 |
| **缺翻译 + 有参数** | `t('not_exist', {id: 123})` | `"not_exist(id=123)"` | **安全兜底**，不报错且保留参数信息，便于调试 |

> **为什么不用默认值或空字符串？**  
> 保留键名或 `key(参数)` 形式能明确指示缺失项，帮助开发者快速发现并补充翻译文件，而不会掩盖问题。

---

## 4. 与现有代码的集成

### 4.1 需要删除的旧代码
- 删除 `DEFAULT_I18N` 大对象（约 50 行）。
- 删除 `<script id="i18n-data" type="application/json">` 及其内容。
- 删除 `var i18nData = {};` 和 `window.i18n = Object.assign({}, DEFAULT_I18N, i18nData);` 相关逻辑。

### 4.2 需要替换的引用
全局搜索 `window.i18n.`，替换为 `t('...')` 调用。例如：

| 原代码 | 新代码 |
|--------|--------|
| `window.i18n.input_placeholder_auto` | `t('input_placeholder_auto')` |
| `window.i18n.dbg_handshake_ok.replace('{ms}', elapsed)` | `t('dbg_handshake_ok', {ms: elapsed})` |
| `window.i18n.status_encrypted_algo.replace('{algo}', algo)` | `t('status_encrypted_algo', {algo})` |
| `statusBar.innerText = message \|\| window.i18n.status_disconnected_text` | `statusBar.innerText = message \|\| t('status_disconnected_text')` |

> 注意：所有带 `{placeholder}` 的翻译都必须改为传入参数对象，而不是手动 `replace`。

### 4.3 与 Web Components 的配合
如果后续将 UI 拆分为 Web Components，这些组件内部可直接调用 `t(key, params)` 获取翻译，无需额外传递 i18n 对象，因为 `t` 是全局函数。

---

## 5. 测试策略

- **单元测试**（JSDOM）：
  - 测试 `t` 函数在各种输入下的输出（有/无翻译，有/无参数，参数缺失等）。
  - 确保后备返回值格式符合预期，不抛出异常。
- **集成测试**（真实浏览器）：
  - 启动服务，访问 `mobile.html`，检查网络请求是否成功获取 `/api/lang.json`。
  - 断网或接口返回 500 时，验证页面仍能显示后备键名，不白屏。
  - 切换 PC 端语言后，刷新手机页面，验证文本更新。
- **手动测试**：遍历所有界面文字，确保无遗漏。

---

## 6. 实施计划

| 步骤 | 任务 | 预计工时 |
|------|------|----------|
| 1 | 后端：实现 `_serve_lang_json` 并注册路由，添加白名单 | 0.5 天 |
| 2 | 前端：添加 `loadI18n` 和 `t` 函数 | 0.5 天 |
| 3 | 前端：全局替换所有 `window.i18n.xxx` 为 `t('xxx')`，调整带占位符的调用 | 1 天 |
| 4 | 前端：删除旧 i18n 相关代码 | 0.5 天 |
| 5 | 联调测试，修复问题 | 0.5 天 |
| 合计 | | 3 天 |

---

## 7. 风险与应对

| 风险 | 应对措施 |
|------|----------|
| 语言包接口不可用，页面完全依赖后备，用户看到 `key(参数)` 形式 | 后备形式清晰，用户能理解，且开发者可快速定位缺失键。 |
| 参数对象与占位符不匹配（如 `t('greeting', {})`） | `replace` 会保留原占位符（如 `"{name}"`），不会崩溃，但显示不完美；可后续增强校验。 |
| 网络缓慢导致语言加载阻塞页面渲染 | `loadI18n` 是异步等待，但可考虑添加超时（如 2 秒）后强行继续，后续优化。 |
| PC 端语言文件格式变动（如重命名 `mobile` 字段） | 需同步更新 `get_section('mobile')` 调用，或使用更健壮的提取逻辑。 |

---

## 8. 未来扩展

- 支持通过 WebSocket 推送语言变更指令，实现无刷新切换语言（需 PC 端配合发送 `lang_change` 消息）。
- 增加语言选择器，让手机端用户可临时覆盖 PC 端语言（需额外接口支持）。
- 支持复数形式、日期格式化等高级 i18n 需求（可引入 `Intl` 或轻量库）。

---

**文档版本**：v2.0  
**作者**：PhoneMic Team  
**日期**：2026-09-08