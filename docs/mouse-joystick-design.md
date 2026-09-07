# 鼠标控制面板（摇杆）设计文档

> 状态：设计草案（待评审）
> 关联分支：`feature/mobile-control`（原 `mobile-keyboard-viewport`）
> 关联模块：附件面板「鼠标」Tab（替换原规划中的触摸板方案）

## 1. 目标与背景

PhoneMic 的附件面板原计划「鼠标」Tab 用**触摸板（相对位移）**模型：手指划多少、鼠标动多少。
它的天生短板是面板只有约 300px 高，手指划到边缘就没地方划了，鼠标就停。

改用**王者荣耀式虚拟摇杆（速度模型）**：
- 推向某方向 → 鼠标持续往该方向动
- 推得越远 → 动得越快（远快近慢）
- 松手 → 停

摇杆是「保持方向」而非「消耗位移」，**不存在划没空间**的问题，更适合小块面板远程控鼠标。

## 2. 交互模型（velocity model）

每帧（`requestAnimationFrame`）读取摇杆向量：

```
方向 = 圆心 → 拇指 的单位向量
幅度 m = clamp(距离 / 半径, 0, 1)
有效幅度 = m < 死区 ? 0 : (m - 死区) / (1 - 死区)   // 死区防抖
速度 = 方向 × maxSpeed × 曲线(有效幅度)
鼠标位移 = 速度 × dt
```

- `maxSpeed`：约 800–1500 px/s，可调（原型默认 1200）
- `deadzone`：约 0.1–0.15，中心附近不漂移（原型默认 0.12）
- 曲线：线性或 ease-out，轻推慢、满推快；手感靠用户使用积累
- 走 WS 发给 PC 的 `mouse.move(相对位移)`

点击 / 拖拽单独处理：
- 轻点摇杆底盘 = 左键单击（带涟漪反馈）
- 独立「右键」按钮 = 右击
- 「拖拽」开关 = 按住期间 `mouse down`，松开才 `up`

## 3. 组件设计

### 3.1 CSS 如何「塞进类」

`mobile.html` 无构建步骤，JS 语法上无法把 CSS 写进 class 花括号。两条路：

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 类注入 `<style>`（推荐）** | 类持有 `static STYLES` 字符串，首次 `new` 时注入 `<head>` 一个带 id 的 `<style>`；选择器全挂在根类 `.mouse-joystick` 下 | 自包含、零全局污染，与现有全局体系兼容 |
| B. Web Component（Shadow DOM） | `customElements.define` + Shadow DOM 真隔离 | 样式物理隔离，但会切断现有全局 i18n/CSS，事件需 retargeting，偏重 |

**采用方案 A**：HTML 结构 + CSS + 逻辑都在同一个类里，不污染全局。

### 3.2 类 API

```js
class MouseJoystick {
    static STYLES = `/* 全部选择器挂在 .mouse-joystick 下 */`;

    constructor(root, { onCommand, maxSpeed = 1200, deadzone = 0.12, sensitivity = 1 }) {
        this.root = root;            // 鼠标 Tab 内容区的 div
        this.onCommand = onCommand;  // 唯一出口，不直接碰 WS
        this.maxSpeed = maxSpeed;
        this.deadzone = deadzone;
        this.sensitivity = sensitivity;
        this.dragging = false;
        this._injectStyles();
        this._buildDOM();            // 摇杆 / 幅度条 / 速度读数 / 右键 / 拖拽
        this._bindPointer();
        this._raf = this._loop.bind(this);
        requestAnimationFrame(this._raf);
    }

    // 内部
    _injectStyles() {}   // 首次注入 static STYLES
    _buildDOM() {}       // 生成面板内全部元素
    _bindPointer() {}    // Pointer 事件 → 向量
    _loop() {}           // rAF：速度模型 → 调用 onCommand
    _emitMove(dx, dy) {}
    _emitClick(button) {}
    _setDrag(on) {}
    destroy() {}         // 解绑事件、取消 rAF
}
```

**解耦原则**：类只调用 `onCommand(payload)`，由外部接线到 `wsClient.send("mouse", payload)`。
类不依赖网络、可单测，与现有 `KeyboardAccessoryPanel` 同一套路（后者也不碰 WS）。

## 4. 面板布局（全部在面板内）

鼠标 Tab 内容区包含：
- 摇杆底盘（下中）+ 拇指滑块
- 上方：幅度条（当前偏转可视化）+ 速度数值读数
- 摇杆右侧：圆形「右键」按钮
- 摇杆下方：「拖拽」开关（按下态高亮）

无浮动层，全部在面板内容区，与左侧 Tab 栏同高。布局参考图见
`docs/panel-concepts/mouse-tab/UI_mockup_*.png`（浅色方案）。

## 5. WebSocket 接口

现有链路：`WSClient.send(type, text)` → 加密为 `{type:'data', data}` →
服务端 `api.py` 解密后 `bridge.emit(type, text)` → PC 端 `PhoneMic.py` 槽消费。
目前只处理 `preview` / `send` 两类。

### 5.1 新增 `mouse` 消息类型

payload 用对象（非字符串）：

```
{ "type": "mouse", "action": "move",  "dx": 12,  "dy": -5 }   // 相对位移，每帧一次
{ "type": "mouse", "action": "click", "button": "left" }       // 或 "right"
{ "type": "mouse", "action": "down",  "button": "left" }       // 拖拽开始
{ "type": "mouse", "action": "up",    "button": "left" }       // 拖拽结束
{ "type": "mouse", "action": "wheel", "delta": 3 }             // 可选：滚动
```

### 5.2 服务端改动（`phonemic/server/api.py`）

`_handle_client_message` 当前只取 `text`：

```python
msg_type = inner.get("type")
text = inner.get("text", "")
if msg_type in ("preview", "send"):
    _manager.bridge.emit(msg_type, text)
```

改为同时取结构化 payload（保留 `text` 兼容旧类型）：

```python
msg_type = inner.get("type")
text = inner.get("text", "")
payload = inner.get("payload", text)   # mouse 等新类型走 payload
if msg_type in ("preview", "send", "mouse"):
    _manager.bridge.emit(msg_type, payload)
```

### 5.3 PC 端新增（`phonemic/gui/mouse.py`）

`MouseController` 用 pyautogui（`moveRel` / `click` / `drag`），在 `PhoneMic.py` 订阅 bridge 的 `mouse` 事件。
键盘那套 `phonemic/gui/keyboard.py` 已是同一模式，照搬即可。

### 5.4 节流

`move` 从 rAF 循环每帧发一次（约 60/s），**不**每次 `pointermove` 都发。

## 6. 待定决策

1. **主题**：浅色（已出图）还是深色？
2. **CSS 方案**：A（注入 style，推荐）还是 B（Web Component）？
3. **默认参数**：沿用 `maxSpeed=1200 / deadzone=0.12`，还是另定？
4. **v1 范围**：最小集 `move/click/drag`，还是带 `wheel` 滚动？
5. **payload 字段**：服务端新增 `payload` 字段（干净），还是复用 `text` 传 JSON 字符串（改动最小）？
