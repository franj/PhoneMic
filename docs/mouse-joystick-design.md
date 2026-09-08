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
- 走 WS 发 `mouse` 消息给 PC（动作 `a:"move"` + 相对位移，见 §5.1）

点击 / 拖拽单独处理：
- 轻点摇杆底盘 = 左键单击（`a:"click"`，带涟漪反馈）
- 独立「右键」按钮 = 右击
- 「拖拽」开关 = 按住期间发 `a:"down"`，松开才发 `a:"up"`

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

链路以 `wire-protocol.md` 为准：`WSClient.send(type, obj)` 发送 `{type, ...}` 帧 →
服务端解密后按 `type` 分派、`bridge.emit(type, ...)` → PC 端 `PhoneMic.py` 槽消费。
目前只处理 `preview` / `send` 两类（载荷为 `text` 字符串）。

### 5.1 新增 `mouse` 消息类型

帧格式与 `wire-protocol.md` §7 mouse 保持一致：二级动作字段用 `a`、按键字段用 `btn`。payload 用对象（非字符串）：

```
{ "type": "mouse", "a": "move",  "dx": 12,  "dy": -5 }   // 相对位移，每帧一次
{ "type": "mouse", "a": "click", "btn": "left" }          // 或 "right"
{ "type": "mouse", "a": "down",  "btn": "left" }          // 拖拽开始
{ "type": "mouse", "a": "up",    "btn": "left" }          // 拖拽结束
{ "type": "mouse", "a": "wheel", "delta": -120 }          // 可选：滚动
```

### 5.2 服务端改动（`phonemic/server/api.py`）

`_handle_client_message` 当前只取 `text`：

```python
msg_type = inner.get("type")
text = inner.get("text", "")
if msg_type in ("preview", "send"):
    _manager.bridge.emit(msg_type, text)
```

`mouse` 不走 `text`，其结构化字段（`a` / `dx` / `dy` / `btn` / `delta`）平铺在帧顶层，服务端把整帧交给 PC 端：

```python
msg_type = inner.get("type")
if msg_type in ("preview", "send"):
    text = inner.get("text", "")
    _manager.bridge.emit(msg_type, text)
elif msg_type == "mouse":
    _manager.bridge.emit("mouse", inner)   # 结构化帧整帧透传
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
5. **mouse 载荷传递**：已定——字段平铺于帧顶层（见 §5.1），**不**加 `payload` 包装字段，也**不**复用 `text` 传 JSON 字符串。`WSClient.send("mouse", payload)` 的第二个参数直接就是含 `a`/`dx`/`dy`/`btn` 的对象。
