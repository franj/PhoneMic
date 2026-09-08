# 鼠标控制面板（摇杆）设计文档

> 状态：设计草案（评审中）· 已按 `wire-protocol.md` v1 校准
> 关联分支：`feature/mobile-control`（原 `mobile-keyboard-viewport`）
> 关联模块：附件面板「鼠标」Tab（替换原规划中的触摸板方案）
> 协议依据：`wire-protocol.md` §2（msgpack 编码）、§6（type 表）、§7（mouse / key 定义）、§10（接收端状态处理）

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

- `maxSpeed`：约 800–1500 px/s，可调（原型默认 1100，滑块范围 200–2500）
- `deadzone`：约 0.1–0.15，中心附近不漂移（原型默认 0.12，滑块范围 0–0.30）
- 曲线：v1 线性（`有效幅度` 直接用）；后续若手感偏硬再换 ease-out
- **曲线在手机端算完**：发出的 `dx`/`dy` 是已算好的**每帧相对位移**，PC 端只做 `moveRel(dx, dy)`，不另做速度处理（`wire-protocol.md` §7 mouse）
- `dx`/`dy` 按整数发，小数余量留在客户端累加到下一帧（同上）

点击 / 拖拽单独处理：
- 左键 / 右键 / 双击 → `mouse` 帧（`a:"click"` / `a:"double"`，带 `btn`）
- 「拖拽」开关 = 按下期间发 `a:"down"`，松开才发 `a:"up"`
- 滚轮 → `a:"wheel"` + `delta`（上滚 −120 / 下滚 +120）
- **按键类按钮（Enter / Esc / Del / Ctrl+Z / 方向键…）走独立的 `key` 帧**（`type:"key"` + `keys` 字段），**不**塞进 `mouse` 的 `a` 里——`a` 的取值严格等于 `wire-protocol.md` §7 mouse 定义的六个

## 3. 组件设计

### 3.1 CSS 如何「塞进类」

`mobile.html` 无构建步骤，JS 语法上无法把 CSS 写进 class 花括号。两条路：

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 类注入 `<style>`（已采用）** | 类持有 `static STYLES` 字符串，首次 `new` 时注入 `<head>` 一个带 id 的 `<style>`；选择器全挂在根类 `.mouse-joystick` 下 | 自包含、零全局污染，与现有全局体系兼容 |
| B. Web Component（Shadow DOM） | `customElements.define` + Shadow DOM 真隔离 | 样式物理隔离，但会切断现有全局 i18n/CSS，事件需 retargeting，偏重 |

**采用方案 A**：HTML 结构 + CSS + 逻辑都在同一个类里，不污染全局。

### 3.2 类 API

```js
class MouseJoystick {
    static STYLES = `/* 全部选择器挂在 .mouse-joystick 下 */`;

    constructor(root, { onCommand, maxSpeed = 1100, deadzone = 0.12 } = {}) {
        this.root = root;            // 鼠标 Tab 内容区的空 <section>
        this.onCommand = onCommand;  // 唯一出口，不直接碰 WS
        this.maxSpeed = maxSpeed;
        this.deadzone = deadzone;
        // ...
        this._injectStyles();
        this._buildDOM();            // 调参行 / 读数行 / 摇杆 / 按钮网格 / 提示行
        this._bindPointer();
        this._rafId = requestAnimationFrame(this._boundLoop);
    }

    // 内部
    _injectStyles() {}   // 首次注入 static STYLES（幂等）
    _buildDOM() {}       // 生成面板内全部元素，文案一律走 t()
    _bindPointer() {}    // Pointer 事件 → 向量
    _loop(t) {}          // rAF：速度模型 → _emitMove
    _emitMove(dx, dy) {} // 整数化 + 余量累加后发 move 帧
    _emitAction(frame) {}// click / double / wheel / key 等一次性动作
    _setDrag(on) {}      // 拖拽开关 → down / up
    destroy() {}         // 解绑事件、取消 rAF、清定时器、清空 DOM
}
```

**解耦原则**：类只调用 `onCommand(frame)`，由外部接线到 `wsClient.send(frame.type, payload)`。
类不依赖网络、可单测，与现有 `KeyboardAccessoryPanel` 同一套路（后者也不碰 WS）。

**`onCommand` 收到的是完整帧对象**（含 `type`），因为鼠标面板有两个出口、两个 type：

```js
{ type: 'mouse', a: 'move',   dx, dy }
{ type: 'mouse', a: 'click' | 'double' | 'down' | 'up', btn }
{ type: 'mouse', a: 'wheel',  delta }
{ type: 'key',   keys: 'ctrl+z' }
```

外部接线时按 type 拆开即可：

```js
onCommand: ({ type, ...payload }) => wsClient.send(type, payload)
```

字段级定义完全对齐 `wire-protocol.md` §7，**本面板不发明任何协议外的字段或 `a` 取值**。

## 4. 面板布局（全部在面板内）

鼠标 Tab 内容区自上而下：

| 区域 | 内容 |
|---|---|
| 调参行 | 速度滑块（200–2500）+ 死区滑块（0–0.30），实时生效 |
| 读数行 | 幅度条（当前偏转可视化）+ 速度数值读数（px/s） |
| 控制区 | 左侧摇杆底盘 + 拇指滑块；右侧 4×4 动作按钮网格 |
| 提示行 | 一行小字提示（i18n） |

4×4 网格是 §4 早期方案（只有右键 + 拖拽）的**扩展**：把按键类动作也收进同一屏，
避免在小面板里再来回切页。左键 / 右键 / 双击 / 拖拽 / 滚轮 落 `mouse` 帧，
Enter / Esc / Del / Ctrl+Z / Ctrl+Y / 方向键 落 `key` 帧。

无浮动层，全部在面板内容区，与左侧 Tab 栏同高。摇杆底盘尺寸由 `ResizeObserver` 自适应：
取所在列可用宽高的较小值，保持圆形。

> 主题：跟随 `mobile.html` 的浅色体系（`#07c160` 主色），不做独立深浅色切换。

## 5. WebSocket 接口

链路以 `wire-protocol.md` 为准：

- 一条 WS 消息 = 一个 **msgpack 编码的 map**，**只走 binary 帧**（§2）。收到 text 帧即判定为未刷新的旧页面，直接关闭。
- 服务端解密后按 `type` 查**分派表**处理，方向错误 / 未知 type → `error(code:"malformed")`（§6 / §10）。
- 现阶段 `api.py` 只分派 `preview` / `send`；`mouse` / `key` 属阶段 5（§12），Panel 侧已就绪，接线点集中在入口一处。

### 5.1 新增 `mouse` 消息类型

帧格式与 `wire-protocol.md` §7 mouse 完全一致：二级动作字段用 `a`、按键字段用 `btn`。
`a` 的取值范围**只有六个**，不含 `key`——按键是另一个 type：

```
{ "type": "mouse", "a": "move",   "dx": 12,  "dy": -5 }   // 相对位移，每帧一次，整数
{ "type": "mouse", "a": "click",  "btn": "left" }          // 或 "right"
{ "type": "mouse", "a": "double", "btn": "left" }          // PC 端 doubleClick()，不拆两帧
{ "type": "mouse", "a": "down",   "btn": "left" }          // 拖拽开始
{ "type": "mouse", "a": "up",     "btn": "left" }          // 拖拽结束
{ "type": "mouse", "a": "wheel",  "delta": -120 }          // 上滚负、下滚正
```

按键类按钮发 `key` 帧（`wire-protocol.md` §7 key），`keys` 直接喂 `send_keys()`：

```
{ "type": "key", "keys": "delete" }
{ "type": "key", "keys": "ctrl+z" }
{ "type": "key", "keys": "shift+enter" }
```

### 5.2 服务端改动（`phonemic/server/api.py`）

`_handle_client_message` 当前只取 `text`：

```python
msg_type = inner.get("type")
text = inner.get("text", "")
if msg_type in ("preview", "send"):
    _manager.bridge.emit(msg_type, text)
```

`mouse` / `key` 不走 `text`，其结构化字段平铺在帧顶层，服务端把整帧交给 PC 端：

```python
msg_type = inner.get("type")
if msg_type in ("preview", "send"):
    _manager.bridge.emit(msg_type, inner.get("text", ""))
elif msg_type in ("mouse", "key"):
    _manager.bridge.emit(msg_type, inner)   # 结构化帧整帧透传
else:
    # 未知 / 方向错误：按 wire-protocol §10 回 error(malformed)
    await _send_frame(websocket, {"type": "error", "code": "malformed", "msg": msg_type})
```

> 注意别把 `mouse` 并入 `preview` / `send` 的 `text` 分支——`inner.get("text","")` 会把结构化帧
> 静默降级成空串（`wire-protocol.md` §10 补充里记的正是这个洞）。

### 5.3 PC 端新增（`phonemic/gui/mouse.py`）

`MouseController` 用 pyautogui（`moveRel` / `click` / `doubleClick` / `mouseDown` / `mouseUp`），
在 `PhoneMic.py` 订阅 bridge 的 `mouse` 事件；`key` 事件沿用已有 `keyboard.py`。
键盘那套 `phonemic/gui/keyboard.py` 已是同一模式，照搬即可。

### 5.4 节流

`move` 从 rAF 循环每帧发一次（约 60/s），**不**每次 `pointermove` 都发。
按 `wire-protocol.md` §2 的估算，字符串版 mouse 帧约 30 字节 → 60fps 下约 1.8 KB/s，可忽略。

一次性动作（click / double / wheel / key）随事件发，不做节流；
滚轮支持长按连发（首次立即发，延迟 350ms 后每 140ms 一格）。

## 6. 决策记录

1. **主题**：**已定**——跟随 `mobile.html` 浅色体系，不做独立深浅色切换。
2. **CSS 方案**：**已定 A**——类注入 `<style>`，选择器挂在 `.mouse-joystick` 下。
3. **默认参数**：**已定**——`maxSpeed=1100` / `deadzone=0.12`（沿用摇杆原型调过的值），滑块可调。
4. **v1 范围**：**已定**——`move` / `click` / `double` / `down` / `up` / `wheel` + 按键网格；`double` 已随本次校准补进 `wire-protocol.md` §7。
5. **mouse 载荷传递**：**已定**——字段平铺于帧顶层（见 §5.1），**不**加 `payload` 包装字段，也**不**复用 `text` 传 JSON 字符串。
6. **按键按钮归属**：**已定**——走独立 `type:"key"` + `keys`，**不**在 `mouse` 里加 `a:"key"`。
7. **WS 接线时机**：**暂不接**——服务端只分派 `preview` / `send`，现在接线会让 `mouse` 帧以 60/s 打 warning 日志。接线点保留在入口，阶段 5 一行接上。
