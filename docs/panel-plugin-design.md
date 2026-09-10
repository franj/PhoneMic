# 手机端附件面板插件化设计

> 状态：**提案，未实现**。本文只定契约与加载机制，实现分期见 §9。
> 相关：`wire-protocol.md`（动作帧定义）、`mouse-joystick-design.md`（现有鼠标面板）

## 1. 背景与真实场景

现在的附件面板（鼠标 / 键盘 / 文件）全部硬编码在 `phonemic/resources/mobile.html` 里：tab 条是写死的三个 `<button>`，键盘与文件面板是写死的静态 DOM + 全局 CSS。要加一个按钮就得改 HTML、加语言包键、重新发版。

目标场景不是"开发者热更新面板"——PC 端和手机端本来就是同一个安装包，版本永远一致，那种需求发版就够了。真正的场景是：

> **最终用户想自定义面板时，把本文丢给 AI，让 AI 生成一个面板文件，放进 PC 端的插件目录，手机端刷新页面就能用。**

用户想放什么按键、什么组合键、什么鼠标动作，完全由他自己（或者说由 AI 替他）决定，PhoneMic 不预设。

## 2. 设计约束：让 AI 一次生成对

这个场景把设计约束整个换掉了。以前是"我怎么写得优雅"，现在是：

| 约束 | 推论 |
|---|---|
| AI 抄示例比读规范准 | 契约文档必须附**完整可抄的示例**，示例的篇幅和重要性不低于规范正文 |
| AI 写 CSS 必然翻车 | CSS 全部由运行时提供，面板文件只声明"这是按钮、上面写什么字"，不碰样式 |
| AI 会漏字段、会臆造字段名 | 字段尽量少、尽量有默认值；运行时对动作帧做**白名单校验**，非法帧丢弃并提示，绝不静默 |
| AI 生成的是善意的、但可能出错的代码 | 风险重心是**健壮性**而非恶意攻击，但仍要防止误用（见 §7） |

**最重要的一条推论**：既然 90% 的面板只是"一堆按钮 + 每个按钮发一帧"，那就**根本不需要执行代码**。用声明式数据描述，运行时自己渲染——这是最强的隔离，因为没有任何被执行的东西。

## 3. 契约 v1：声明式面板

### 3.1 文件位置

PC 端用户数据目录下的 `panels/` 目录（打包后 `resources/` 不可写，具体路径待定，见 §10）。

- `panels/*.json` —— **声明式面板**，默认形态，零代码执行
- `panels/*.js` —— 脚本面板，v1 不支持（见 §8）

> 为什么默认用 `.json` 而不是 `.js`：AI 生成 JSON 比生成 JS 更不容易出错（没有语法变体、没有 `export` 写法差异），而且 `.json` 一定不会被执行。用户口语里说"生成一个 js"，实际拿到 `.json` 更省事。

### 3.2 面板文件结构

```json
{
  "id": "video",
  "tab": { "icon": "🎬", "text": "视频" },
  "grid": 4,
  "buttons": [
    { "text": "播放",  "send": { "type": "key", "keys": "space" } },
    { "text": "音量+", "send": { "type": "key", "keys": "volumeup" }, "repeat": true }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 面板唯一标识。小写字母、数字、下划线、连字符。与文件名不必一致，但冲突时后者覆盖前者 |
| `tab.icon` | 是 | 切换条上显示的图标，单个 emoji |
| `tab.text` | 是 | 切换条上的文字。**直接写字面文本，不走语言包**——用户自定义面板没有翻译的必要 |
| `grid` | 否 | 按钮网格列数，默认 4。按钮数量超出一屏时纵向滚动 |
| `buttons` | 是 | 按钮数组，顺序即渲染顺序（先行后列） |

### 3.3 按钮项

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 按钮上的文字。建议 ≤ 4 个汉字或 ≤ 8 个英文字符 |
| `send` | 是 | 点击时发出的动作帧，**原样交给 WebSocket**，格式见 §4 |
| `repeat` | 否 | `true` 时按住连发：首次立即，350 ms 后每 140 ms 一帧。适合音量、滚轮这类需要连续触发的键 |
| `cls` | 否 | 附加样式类，可选值由运行时规定（如 `wide` 占两格）。v1 只有 `wide`，不填即用默认样式 |

**不做的事**（v1 明确不支持，AI 生成时不要发明这些字段）：

- 不支持自定义 CSS、内联样式、颜色
- 不支持按钮状态（toggle）、长按阈值自定义
- 不支持嵌套、分组、多页
- 不支持发多帧（一个按钮一帧；需要组合键用 `keys: "ctrl+z"` 这类协议原生表达）

## 4. 动作帧速查表

`send` 的内容**原样**作为一帧发出，字段名与取值严格取自 `wire-protocol.md` §7。AI 生成时只需照抄下表，**不要发明新字段**。

### 4.1 按键 `type: "key"`

```json
{ "type": "key", "keys": "space" }
{ "type": "key", "keys": "ctrl+z" }
{ "type": "key", "keys": "ctrl+a, delete" }
```

- `+` 连接修饰键（ctrl / alt / shift / win）
- `,` 分隔多个组合，最多 10 个
- 键名取 pyautogui 的键名：`enter` `esc` `tab` `space` `delete` `backspace` `up` `down` `left` `right` `home` `end` `pageup` `pagedown` `f1`…`f12` `volumeup` `volumedown` `volumemute` `printscreen`
- **安全边界**：`key` 只能表达按键，协议层没有执行程序的入口

### 4.2 鼠标 `type: "mouse"`

```json
{ "type": "mouse", "a": "click",  "btn": "left" }
{ "type": "mouse", "a": "double", "btn": "left" }
{ "type": "mouse", "a": "wheel",  "delta": -120 }
```

| `a` | 伴随字段 | 含义 |
|---|---|---|
| `click` | `btn`: `left` / `right` / `middle` | 单击 |
| `double` | `btn` | 双击。**必须用它，不要连发两次 `click`**——链路时延不可控，会被 OS 判成两次单击 |
| `down` / `up` | `btn` | 按下 / 抬起（用于拖拽，需成对使用） |
| `wheel` | `delta`: 整数，正下负上 | 滚轮，120 为一格 |
| `move` | `dx` / `dy`: 整数像素 | 相对位移。**声明式面板用不上**，摇杆类连续输入专用 |

### 4.3 运行时校验

运行时对 `send` 做白名单校验，不通过则该按钮点击时只在控制台提示、不发帧：

1. `type` 必须是 `key` 或 `mouse`（v1 白名单）
2. `type: "key"` 必须有字符串 `keys`
3. `type: "mouse"` 的 `a` 必须属于上表，且伴随字段齐全
4. 未知字段**忽略**（不报错，保证旧面板在新版本上不会挂）

## 5. 完整示例

### 5.1 视频控制

```json
{
  "id": "video",
  "tab": { "icon": "🎬", "text": "视频" },
  "grid": 4,
  "buttons": [
    { "text": "播放",  "send": { "type": "key", "keys": "space" } },
    { "text": "全屏",  "send": { "type": "key", "keys": "f" } },
    { "text": "音量+", "send": { "type": "key", "keys": "volumeup" },   "repeat": true },
    { "text": "音量-", "send": { "type": "key", "keys": "volumedown" }, "repeat": true },
    { "text": "后退",  "send": { "type": "key", "keys": "left" } },
    { "text": "前进",  "send": { "type": "key", "keys": "right" } },
    { "text": "静音",  "send": { "type": "key", "keys": "volumemute" } },
    { "text": "字幕",  "send": { "type": "key", "keys": "c" } }
  ]
}
```

### 5.2 IDE 快捷键

```json
{
  "id": "ide",
  "tab": { "icon": "⌨️", "text": "IDE" },
  "grid": 3,
  "buttons": [
    { "text": "保存",   "send": { "type": "key", "keys": "ctrl+s" } },
    { "text": "撤销",   "send": { "type": "key", "keys": "ctrl+z" } },
    { "text": "重做",   "send": { "type": "key", "keys": "ctrl+shift+z" } },
    { "text": "搜索",   "send": { "type": "key", "keys": "ctrl+shift+f" } },
    { "text": "跳转",   "send": { "type": "key", "keys": "ctrl+o" } },
    { "text": "终端",   "send": { "type": "key", "keys": "ctrl+`" } },
    { "text": "格式化", "send": { "type": "key", "keys": "ctrl+alt+l" } },
    { "text": "运行",   "send": { "type": "key", "keys": "ctrl+f5" } },
    { "text": "调试",   "send": { "type": "key", "keys": "shift+f9" } }
  ]
}
```

### 5.3 演示控制（鼠标为主）

```json
{
  "id": "ppt",
  "tab": { "icon": "📽️", "text": "演示" },
  "grid": 2,
  "buttons": [
    { "text": "下一页", "send": { "type": "key", "keys": "pagedown" }, "cls": "wide" },
    { "text": "上一页", "send": { "type": "key", "keys": "pageup" },   "cls": "wide" },
    { "text": "左键",   "send": { "type": "mouse", "a": "click", "btn": "left" } },
    { "text": "右键",   "send": { "type": "mouse", "a": "click", "btn": "right" } },
    { "text": "双击",   "send": { "type": "mouse", "a": "double", "btn": "left" } },
    { "text": "滚轮上", "send": { "type": "mouse", "a": "wheel", "delta": -120 }, "repeat": true },
    { "text": "滚轮下", "send": { "type": "mouse", "a": "wheel", "delta": 120 },  "repeat": true },
    { "text": "黑屏",   "send": { "type": "key", "keys": "b" } }
  ]
}
```

## 6. 加载机制

### 6.1 清单路由

新增 `GET /api/panels.json`，与 `/api/lang.json` 同构：

- 加入 `_PUBLIC_PATHS`；加密模式下由 `_normalize_path` 的 `/{secret}/` 前缀自动保护，明文模式下为白名单直出（面板是数据不是代码，泄露无害）
- 响应头 `no-cache, no-store, must-revalidate`，避免手机端缓存住旧清单
- PC 端每次请求时扫描 `panels/` 目录，用 mtime 做短缓存（1 s）。**用户放文件 → 手机刷新 → 出现**，不需要重启服务
- 单个面板解析失败只跳过该面板并在清单里给出 `error` 字段，不影响其他面板

```json
{
  "v": 1,
  "panels": [
    { "id": "keys",  "tab": { "icon": "⌨️", "text": "键盘" }, "grid": 6, "buttons": [ ... ] },
    { "id": "video", "tab": { "icon": "🎬", "text": "视频" }, "grid": 4, "buttons": [ ... ] },
    { "id": "broken", "error": "invalid json: unexpected token" }
  ]
}
```

面板定义都很小（几百字节），**直接内联在清单里**，不额外开 `/api/panels/{id}` 路由，省一轮往返。只有后续 §8 的脚本面板才需要单独取源码。

### 6.2 前端装载

1. `loadI18n()` 之后、`initPanelTabs()` 之前 `await loadPanels()`
2. tab 条**由清单生成**，不再硬编码三个 `<button>`；声明式面板的视图容器在挂载时才创建
3. 声明式面板由内置渲染器根据 `buttons` 生成 DOM，样式类统一为 `pk-*`，CSS 由运行时注入（与 `MouseJoystick.STYLES` 同一套路数，选择器全挂 `.pk-panel`）
4. 所有按钮的输出收敛到同一个出口：`onCommand(frame)` → `wsClient.send(type, payload)`，与 `mouse-joystick-design.md` §3.2 一致
5. 面板数量超过切换条宽度时横向滚动

## 7. 隔离与健壮性

### 7.1 声明式面板：数据即隔离

`.json` 面板没有任何被执行的东西，运行时只读它的字段。这一层不存在逃逸面，也就不存在"限制插件能调用什么函数"的问题——**它压根没有调用能力**。这是本方案最重要的性质。

> 补充：同一 realm 内做"只给插件传受限对象"的沙箱是**做不到**的。ESM 加载的模块与主页面共享 `window` / `document` / `localStorage` / `location.hash`，`new Function`、`with(proxy)`、冻结全局对象都有已知逃逸路径（`globalThis`、`Function('return this')()`）。真隔离只能靠独立 realm（iframe `sandbox` 或 Worker）。所以 v1 干脆不执行代码，绕开整个问题。

### 7.2 运行时容错

AI 生成的文件一定会出错，容错按从外到内三层：

1. **清单层**：单个面板 JSON 解析失败 / 缺 `id` / 缺 `tab` → 跳过该面板，清单里带 `error` 说明，其余面板正常
2. **渲染层**：单个按钮缺 `text` 或 `send` → 该按钮渲染成禁用态并标红，不拖垮整个面板
3. **发送层**：`send` 未通过 §4.3 白名单 → 丢弃并 `console.warn`，绝不明文发给服务端

另外：`grid` 超出合理范围（< 1 或 > 8）夹到边界值；`buttons` 数量上限 64，超出截断。

### 7.3 PC 端静态检查（可选，非安全边界）

加载时扫面板文件里是否出现可疑内容（`.js` 脚本面板才有意义）。对声明式 `.json` 不需要。

## 8. 后续：脚本面板与 iframe 沙箱

摇杆这类连续输入（rAF 循环、速度曲线、小数余量累加）**无法用数据描述**，必须执行代码。若将来支持：

- 面板文件改回 `.js`，清单里标 `"kind": "module"` + `"src": "api/panels/xxx.js"`，源码单独取
- 执行环境用 **`<iframe sandbox="allow-scripts">`**（不给 `allow-same-origin`）：iframe 是 opaque origin，碰不到 parent DOM、读不到 `location.hash`（手机端密钥的所在处），只能通过 `postMessage` 与宿主通信，而**响应哪些消息由宿主决定**——这才是真正意义上"只能调用有限函数"
- 代价：源码不能由 iframe 自己 `import()`（opaque origin 下相对路径与 fetch 都不通），需用 `srcdoc` + postMessage 把源码字符串送进去执行，因此脚本面板不能写 `export default`，要约定一个入口函数；面板高度变化需 postMessage 同步

**结论**：两类面板走不同加载路径，后加脚本面板不破坏已有的声明式面板，因此可以放心先只做 v1。

## 9. 落地分期

| 阶段 | 内容 | 风险 |
|---|---|---|
| 1 | 本文定稿 + 把现有键盘面板改写成 `panels/keys.json`（dogfooding，同时充当 AI 的活样例） | 零 |
| 2 | 服务端 `/api/panels.json` + 目录扫描；前端清单驱动生成 tab 条 | 低 |
| 3 | 内置声明式渲染器（`pk-*` 样式 + `repeat` + `wide`）+ 帧白名单校验 | 低 |
| 4 | 文件面板也改成声明式；鼠标摇杆保留内置组件（连续输入，不属于本契约） | 低 |
| 5 | （按需）脚本面板 + iframe 沙箱 | 中 |

阶段 1 可以完全独立先做：把键盘面板改写成 JSON 后，它既是默认功能，又是最可靠的示例。

## 10. 待定决策

1. **插件目录路径**：用户数据目录下（如 `%APPDATA%\PhoneMic\panels\`），打包后需确认可写与首次运行是否自动创建
2. **内置面板与用户面板的关系**：同名 `id` 时用户面板覆盖内置，还是内置优先？倾向用户优先（否则无法改）
3. **是否也接受 `.js` 声明式面板**（`export default {...}`）：更贴近用户口语，但会引入代码执行面，与 §7.1 冲突。倾向不支持
4. **面板禁用/排序**：是否支持用户在 PC 端界面上开关、排序面板，还是只能靠删文件
5. **明文模式是否加载用户面板**：数据无害，倾向加载，但需确认没有信息泄露（面板文本会出现在手机界面上）
