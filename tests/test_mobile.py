"""
mobile.html UI 测试
使用 Playwright + Mock WebSocket 进行前端测试，无需启动真实服务端。
依赖: pytest-playwright (需先运行 playwright install chromium)

Mock 策略：
- 内联加载 sodium.js、msgpack.min.js 和 crypto_providers.js
  （set_content 无法加载外部脚本）
- 强制选择 PlainProvider（不加密，#a=none 语义），UI 全流程走明文帧
- Mock WS 自动建立连接，使 WSClient 进入已连接状态
- triggerMessage 经 secure.encrypt 编码下行帧（本文件的明文模式即 msgpack 字节）
- sent_messages 返回解码后的上行原始内容
"""

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")
RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"
MOBILE_HTML_PATH = RES_DIR / "mobile.html"

# 手机端语言包由 /api/lang.json 提供，而 set_content 下无法真实 fetch。
# 直接把 zh_CN 的 mobile 段注入 window.i18n，等价于服务端返回的内容。
MOBILE_I18N = json.loads(
    (RES_DIR / "locales" / "zh_CN.json").read_text(encoding="utf-8")
)["mobile"]

MOCK_WS_SCRIPT = """
window.__mockWS = {
    sentMessages: [],
    current: null,
    instances: [],

    triggerMessage: function(data) {
        if (this.current && this.current.onmessage) {
            // 与真实服务端一致：经 secure.encrypt 产出线上字节
            //（明文=msgpack / 加密=整帧密文，无外层信封）
            this.current.onmessage({ data: window.__wsClient.secure.encrypt(data) });
        }
    },
    triggerClose: function() {
        if (this.current) {
            if (this.current.onclose) this.current.onclose();
            this.current.readyState = 3;
        }
    },
    triggerOpen: function() {
        if (this.current) {
            if (this.current.onopen) this.current.onopen();
            this.current.readyState = 1;
        }
    },
    clearSent: function() {
        this.sentMessages = [];
    }
};

window.WebSocket = function(url) {
    this.url = url;
    this.readyState = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;

    this.send = function(data) {
        if (this.readyState !== 1) return false;
        try {
            var msg = MessagePack.decode(data);
            window.__mockWS.sentMessages.push(msg);
        } catch(e) { window.__mockWS.sentMessages.push({ raw: 'undecodable' }); }
        return true;
    };

    this.close = function() {
        if (this.readyState === 3) return;
        if (this.onclose) this.onclose();
        this.readyState = 3;
    };

    window.__mockWS.current = this;
    window.__mockWS.instances.push(this);

    var self = this;
    setTimeout(function() {
        if (self.readyState === 0) {
            self.readyState = 1;
            if (self.onopen) self.onopen();
        }
    }, 0);
};
window.WebSocket.OPEN = 1;
window.WebSocket.CONNECTING = 0;
window.WebSocket.CLOSING = 2;
window.WebSocket.CLOSED = 3;
"""


@pytest.fixture
def mobile_page(page):
    html = MOBILE_HTML_PATH.read_text(encoding="utf-8")
    # 内联外部脚本（set_content 无法加载 <script src> 相对路径）
    sodium_js = (RES_DIR / "sodium.js").read_text(encoding="utf-8")
    msgpack_js = (RES_DIR / "msgpack.min.js").read_text(encoding="utf-8")
    crypto_js = (RES_DIR / "crypto_providers.js").read_text(encoding="utf-8")
    html = html.replace('<script src="sodium.js" defer></script>', f"<script>{sodium_js}</script>")
    html = html.replace(
        '<script src="msgpack.min.js" defer></script>', f"<script>{msgpack_js}</script>"
    )
    html = html.replace('<script src="crypto_providers.js" defer></script>', f"<script>{crypto_js}</script>")
    html = html.replace(
        "window.i18n = {};",
        "window.i18n = " + json.dumps(MOBILE_I18N, ensure_ascii=False) + ";",
        1,
    )
    # head 可能带属性（如 data-page-node-id），不能假设精确等于 "<head>"
    # 一并注入开发模式标记：真实环境由服务端下发（api.py:_serve_mobile），
    # set_content 不走服务端，必须手动补上，否则手机端日志模块不会启动。
    boot = (
        "<script>window.__PHONEMIC_DEV__=true;</script>"
        "<script>" + MOCK_WS_SCRIPT + "</script>"
    )
    html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + boot, html, count=1)
    # 暴露 wsClient 供 mock 检查 isEncrypted
    html = html.replace(
        "wsClient.connect();",
        "wsClient.connect(); window.__wsClient = wsClient;",
    )
    # 注入 patch：在主脚本之后、onload 之前，强制使用不加密模式
    patch = (
        "<script>"
        "SecureClient.prototype._parseUrlFragment = function() {"
        "  this._selectedAlgo = 'none';"
        "};"
        "</script>"
    )
    html = html.replace("</body>", patch + "</body>", 1)
    page.set_content(html)
    page.wait_for_function(
        "() => window.__mockWS && window.__mockWS.current && window.__mockWS.current.readyState === 1"
    )
    # none+LAN 模式：无需 auth，等待连接建立
    page.wait_for_function(
        "() => window.__wsClient && window.__wsClient.isConnected"
    )
    page.wait_for_timeout(50)
    # 模拟服务端发送 config 消息
    page.evaluate("() => window.__mockWS.triggerMessage({type: 'config', mobile_max_records: 5})")
    yield page


def sent_messages(page):
    """返回 mock WS 捕获的上行消息（已解码为原始对象）。"""
    return page.evaluate("() => window.__mockWS.sentMessages")


class TestPageLoad:
    def test_page_loads_and_connected(self, mobile_page):
        assert mobile_page.title() == "📱🎙️PhoneMic💬💻"
        assert not mobile_page.locator("#status-bar").is_visible()
        assert mobile_page.locator("#input-box").is_enabled()

    def test_mode_toggle_visible_when_input_empty(self, mobile_page):
        assert mobile_page.locator("#mode-toggle").is_visible()
        mobile_page.locator("#input-box").fill("text")
        assert mobile_page.locator("#mode-toggle").is_hidden()
        mobile_page.locator("#input-box").fill("")
        assert mobile_page.locator("#mode-toggle").is_visible()


class TestManualMode:
    def test_send_button_sends_message(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("hello world")
        mobile_page.locator("#btn-send").click()
        msgs = [m for m in sent_messages(mobile_page) if m["type"] == "send"]
        assert len(msgs) == 1
        assert msgs[0]["text"] == "hello world"
        assert mobile_page.locator("#input-box").input_value() == ""

    def test_enter_key_sends(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("enter test")
        mobile_page.locator("#input-box").press("Enter")
        msgs = [m for m in sent_messages(mobile_page) if m["type"] == "send"]
        assert len(msgs) == 1
        assert msgs[0]["text"] == "enter test"

    def test_shift_enter_does_not_send(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("shift test")
        mobile_page.locator("#input-box").press("Shift+Enter")
        msgs = [m for m in sent_messages(mobile_page) if m["type"] == "send"]
        assert len(msgs) == 0


class TestAutoMode:
    def test_default_is_auto_mode(self, mobile_page):
        assert mobile_page.locator("#btn-send").text_content() == "自动"

    def test_toggle_to_manual(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        assert mobile_page.locator("#btn-send").text_content() == "发送"
        mobile_page.locator("#mode-toggle").click()
        assert mobile_page.locator("#btn-send").text_content() == "自动"

    def test_compositionend_triggers_auto_send(self, mobile_page):
        mobile_page.evaluate("""
            () => {
                const input = document.getElementById('input-box');
                input.value = '语音内容';
                input.dispatchEvent(new CompositionEvent('compositionstart'));
                input.dispatchEvent(new CompositionEvent('compositionend'));
            }
        """)
        mobile_page.wait_for_timeout(100)
        msgs = [m for m in sent_messages(mobile_page) if m["type"] == "send"]
        assert len(msgs) == 1
        assert msgs[0]["text"] == "语音内容"


class TestClearButton:
    def test_clear_empties_input_and_sends_preview(self, mobile_page):
        mobile_page.locator("#input-box").fill("some text")
        mobile_page.locator("#btn-clear").click()
        assert mobile_page.locator("#input-box").input_value() == ""
        previews = [m for m in sent_messages(mobile_page)
                    if m["type"] == "preview" and m["text"] == ""]
        assert len(previews) == 1


class TestPreview:
    def test_input_sends_preview(self, mobile_page):
        mobile_page.locator("#input-box").fill("preview test")
        previews = [m for m in sent_messages(mobile_page)
                    if m["type"] == "preview" and m["text"] == "preview test"]
        assert len(previews) == 1


class TestConfigSync:
    def test_config_updates_max_history(self, mobile_page):
        mobile_page.evaluate(
            "() => window.__mockWS.triggerMessage({type: 'config', mobile_max_records: 10})"
        )
        assert mobile_page.evaluate("() => window.chatManagerInstance.maxHistory") == 10


class TestDisconnect:
    """非选择器路径的断连（首连失败、前台掉线）的界面表现：立刻显示 + 输入禁用。

    这类断连不做静默——界面若还停在「已连接」的样子，用户的操作会静默失败。
    「文件选择器造成的断连」走静默窗口，由 TestDisconnectRecovery 覆盖。
    """

    def _close_and_fail(self, page):
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")   # 掐掉重连，模拟真的断着
        page.evaluate("() => window.__mockWS.triggerClose()")
        page.wait_for_function(
            "() => document.getElementById('status-bar').style.display === 'block'", timeout=2000
        )

    def test_disconnect_shows_status_bar(self, mobile_page):
        self._close_and_fail(mobile_page)
        assert mobile_page.locator("#status-bar").is_visible()
        assert mobile_page.locator("#input-box").is_disabled()

    def test_disconnect_disables_buttons(self, mobile_page):
        self._close_and_fail(mobile_page)
        assert mobile_page.locator("#btn-send").is_disabled()
        assert mobile_page.locator("#btn-clear").is_disabled()

    def test_foreground_disconnect_never_waits_grace_window(self, mobile_page):
        """没唤起过选择器：断连立刻上报，不白等那 3 秒静默窗口。"""
        page = mobile_page
        assert page.evaluate("() => window.__wsClient.connectionGraceMs") == 3000
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")
        page.evaluate("window.__mockWS.triggerClose()")
        # 3 秒窗口的十分之一内就该出来：等到了才说明压根没进窗口
        page.wait_for_function(
            "() => document.getElementById('status-bar').style.display === 'block'", timeout=300
        )
        assert page.locator("#input-box").is_disabled()


class TestChatList:
    def test_send_adds_message_to_chat(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("chat msg")
        mobile_page.locator("#btn-send").click()
        msgs = mobile_page.locator(".message")
        assert msgs.count() == 1
        assert msgs.first.text_content() == "chat msg"

    def test_long_press_appends_to_input(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("press me")
        mobile_page.locator("#btn-send").click()

        msg = mobile_page.locator(".message").first
        box = msg.bounding_box()
        mobile_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        mobile_page.mouse.down()
        mobile_page.wait_for_timeout(600)
        mobile_page.mouse.up()

        assert "press me" in mobile_page.locator("#input-box").input_value()

    def test_click_message_resend(self, mobile_page):
        mobile_page.locator("#mode-toggle").click()
        mobile_page.locator("#input-box").fill("resend me")
        mobile_page.locator("#btn-send").click()

        mobile_page.on("dialog", lambda dialog: dialog.accept())
        mobile_page.locator(".message").first.click()

        msgs = [m for m in sent_messages(mobile_page) if m["type"] == "send"]
        assert len(msgs) == 2
        assert msgs[1]["text"] == "resend me"


def test_transfer_lock_freezes_other_panels(mobile_page):
    """传输期间锁死面板切换与其它操作（发送中误触不得导致状态错乱）。"""
    page = mobile_page
    page.click('#btn-plus')
    page.click('#panel-tabs button[data-panel="file"]')

    locked = "() => document.body.classList.contains('file-transferring')"
    assert page.evaluate(locked) is False

    page.evaluate("window._filePanel._setLock(true)")
    assert page.evaluate(locked) is True
    assert page.evaluate("document.getElementById('btn-plus').disabled") is True
    assert page.evaluate("document.getElementById('btn-send').disabled") is True
    assert page.evaluate("document.getElementById('btn-clear').disabled") is True
    assert page.evaluate("document.getElementById('input-box').disabled") is True
    assert page.evaluate("document.querySelectorAll('#panel-tabs .tab:disabled').length") == 4
    assert page.evaluate(
        "getComputedStyle(document.getElementById('view-keys')).pointerEvents"
    ) == "none"
    assert page.evaluate(
        "getComputedStyle(document.getElementById('view-file')).pointerEvents"
    ) != "none"

    page.evaluate("window._filePanel._setLock(false)")
    assert page.evaluate(locked) is False
    assert page.evaluate("document.getElementById('btn-send').disabled") is False
    assert page.evaluate("document.querySelectorAll('#panel-tabs .tab:disabled').length") == 0


def test_transfer_chunk_size_is_dynamic(mobile_page):
    """分块按体积自适应（协议 §9）：3MB 文件 → 12 块 × 256KB，块数收敛到 ~12。"""
    page = mobile_page
    seq = page.evaluate(
        "async () => {"
        "  const panel = window._filePanel;"
        "  const saved = [];"
        "  const orig = panel.onCommand;"
        "  panel.onCommand = (f) => { saved.push(f.a + ':' + (f.chunk ? f.chunk.length : 0));"
        "                             return true; };"
        "  panel._waitEndAck = () => Promise.resolve(true);"
        "  const fake = { name: 'big.bin', size: 3 * 1024 * 1024,"
        "                 slice: (a, b) => new Blob([new Uint8Array(b - a)]) };"
        "  await panel._start(fake, 'file');"
        "  panel.onCommand = orig;"
        "  return saved.join(',');"
        "}"
    )
    assert seq.startswith("start:0"), seq
    # 3MB / 12 = 256KB，对齐到 256KB 粒度 → 12 块
    assert seq.count("data:262144") == 12, seq
    assert seq.endswith("end:0"), seq


def test_pick_chunk_size_matrix(mobile_page):
    """pickChunkSize：块数收敛 ~12，受服务端下发上限与手机端 15MB 自身上限双重夹逼。"""
    page = mobile_page
    r = page.evaluate(
        "() => {"
        "  const F = window._filePanel.constructor;"
        "  const out = {};"
        "  F.setServerMaxFrame(16 * 1024 * 1024);"
        "  out.s30 = F.pickChunkSize(30 * 1024 * 1024);"
        "  out.s100 = F.pickChunkSize(100 * 1024 * 1024);"
        "  out.s500 = F.pickChunkSize(500 * 1024 * 1024);"
        "  out.tiny = F.pickChunkSize(100 * 1024);"
        "  F.setServerMaxFrame(1 * 1024 * 1024);"
        "  out.capped1m = F.pickChunkSize(500 * 1024 * 1024);"
        "  F.setServerMaxFrame(0);"
        "  out.fallback = F.pickChunkSize(500 * 1024 * 1024);"
        "  return out;"
        "}"
    )
    MB = 1024 * 1024
    assert r["s30"] == 2621440, r                    # 30MB/12 → 2.5MB，12 块
    assert r["s100"] == 8912896, r                   # 8.5MB（向上取整到 256KB 倍数）
    assert r["s500"] == 15 * MB, r                   # 撞手机端自身上限
    assert r["tiny"] == 256 * 1024, r                # 小文件也不低于下限
    assert r["capped1m"] == 1 * MB - 64 * 1024, r    # 服务端只给 1MB → 扣 64KB 边距
    assert r["fallback"] == 4 * MB, r                # 未下发 → 保守默认 4MB


def test_config_message_sets_max_frame_size(mobile_page):
    """下行 config.max_frame_size 驱动分块上限；非法值视为未下发。"""
    page = mobile_page
    assert page.evaluate("() => window._filePanel.constructor.serverMaxFrame") == 0

    page.evaluate(
        "() => window.__mockWS.triggerMessage("
        "{type:'config', mobile_max_records: 10, max_frame_size: 16*1024*1024})"
    )
    page.wait_for_timeout(100)
    assert page.evaluate(
        "() => window._filePanel.constructor.serverMaxFrame") == 16 * 1024 * 1024

    # 非数字：视为未下发，回落到保守默认，不能让分块变成 NaN
    page.evaluate("() => window.__mockWS.triggerMessage({type:'config', max_frame_size: 'abc'})")
    page.wait_for_timeout(100)
    assert page.evaluate("() => window._filePanel.constructor.serverMaxFrame") == 0


class TestDisconnectRecovery:
    """断线恢复路径：Android WebView 在切后台/唤起文件选择器时会把 socket 掐掉（两端只剩 1006）。

    这三条锁定的都是「用户不该感知到这次抖动」：
    未连接时先等重连而不是立刻报错、回前台立即重连、连接正常时不折腾。
    """

    FAKE_FILE = (
        "{name: 'f.bin', size: 1024,"
        " slice: (a, b) => new Blob([new Uint8Array(b - a)])}"
    )

    def test_start_waits_for_reconnect_instead_of_reporting_disconnected(self, mobile_page):
        """断连瞬间发起发送：应等 1s 自动重连成功后照常发出，而不是报「未连接到电脑」。"""
        page = mobile_page
        page.evaluate("window.__mockWS.triggerClose()")
        assert page.evaluate("() => window.__wsClient.getConnected()") is False

        sent = page.evaluate(
            "async () => {"
            "  const panel = window._filePanel;"
            "  panel._waitEndAck = () => Promise.resolve(true);"
            f"  await panel._start({self.FAKE_FILE}, 'file');"
            "  return window.__mockWS.sentMessages.map(m => m.a);"
            "}"
        )
        assert "start" in sent, sent
        assert "end" in sent, sent
        assert page.evaluate("() => window.__wsClient.getConnected()") is True

    def test_start_toast_is_non_blocking(self, mobile_page):
        """真的连不上时给非阻塞浮层（不是 alert —— 它会冻住主线程把重连也一起卡死）。"""
        page = mobile_page
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.evaluate("window.__mockWS.triggerClose()")
        page.evaluate("() => { window.__wsClient.authRejected = true; }")   # 阻止自动重连
        page.evaluate(
            "() => Object.defineProperty(window._filePanel.constructor,"
            " 'RECONNECT_WAIT', { value: 400 })"
        )
        page.evaluate(
            "async () => { await window._filePanel._start("
            f"{self.FAKE_FILE}, 'file'); }}"
        )

        assert dialogs == [], dialogs
        assert page.evaluate("document.querySelectorAll('#toast-host .toast').length") == 1
        assert MOBILE_I18N["panel_file_disconnected"] in page.inner_text("#toast-host .toast")
        assert page.evaluate("() => window._filePanel._state") == "idle"
        # 点一下即消
        page.locator("#toast-host .toast").click()
        assert page.evaluate("document.querySelectorAll('#toast-host .toast').length") == 0

    def test_visible_reconnects_immediately_without_backoff(self, mobile_page):
        """回前台立即重连：不等 1s 退避，否则「选完文件回来」正好落在未连接窗口里。"""
        page = mobile_page
        page.evaluate("window.__mockWS.triggerClose()")
        n_before = page.evaluate("window.__mockWS.instances.length")

        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        # 退避是 1000ms：800ms 内连上才说明走的是快路径
        page.wait_for_function("() => window.__wsClient.isConnected", timeout=800)
        assert page.evaluate("window.__mockWS.instances.length") == n_before + 1

    def test_reconnect_now_is_noop_while_connected(self, mobile_page):
        """连接正常时回前台不得重连（否则每次切前台都要抖一次）。"""
        page = mobile_page
        n_before = page.evaluate("window.__mockWS.instances.length")
        page.evaluate(
            "() => { window.__wsClient.reconnectNow('visible');"
            "  document.dispatchEvent(new Event('visibilitychange')); }"
        )
        page.wait_for_timeout(150)
        assert page.evaluate("window.__mockWS.instances.length") == n_before

    def test_disconnect_stays_silent_inside_grace_window(self, mobile_page):
        """选择器路径的静默宽限窗口：断连后 3 秒内界面完全不动（不显示状态栏、不禁用输入框）。

        唤起文件选择器会把页面判为不可见，Android 常顺手掐掉 socket；选中文件回到前台后
        1s 内就能连回来。这类断连用户根本没做过「断开」的操作，出现任何提示都是打扰。
        （「点取消」不算——取消要照常上报，见 test_picker_cancel_reports_immediately。）
        """
        page = mobile_page
        assert page.evaluate("() => window.__wsClient.connectionGraceMs") == 3000
        page.evaluate("() => window.__wsClient.beginPickerContext()")   # 唤起文件选择器
        page.evaluate("window.__mockWS.triggerClose()")
        page.evaluate("() => window.__wsClient.endPickerContext()")     # 选中了文件
        # 窗口内持续采样：状态栏一次都不许露出来
        page.evaluate(
            "() => { window.__sbSeen = 0;"
            "  window.__sbTimer = setInterval(() => {"
            "    if (document.getElementById('status-bar').style.display === 'block')"
            "      window.__sbSeen++; }, 25); }"
        )
        page.wait_for_function("() => window.__wsClient.getConnected()", timeout=3000)
        page.wait_for_timeout(100)

        assert page.evaluate("window.__sbSeen") == 0, "窗口内重连成功，界面不该出现任何断开提示"
        assert page.evaluate("() => document.getElementById('input-box').disabled") is False

    def test_grace_expiry_only_then_shows_reconnecting(self, mobile_page):
        """只有窗口超时仍未连上，才第一次把断开告诉界面。"""
        page = mobile_page
        page.evaluate(
            "() => { window.__wsClient.connectionGraceMs = 300;"
            "  window.__wsClient.connect = () => {}; }"   # 掐掉重连，模拟真的断着
        )
        page.evaluate("() => window.__wsClient.beginPickerContext()")
        page.evaluate("window.__mockWS.triggerClose()")

        page.wait_for_timeout(150)
        assert page.evaluate("() => document.getElementById('status-bar').style.display") == "none"

        page.wait_for_function(
            "() => document.getElementById('status-bar').style.display === 'block'", timeout=2000
        )
        assert "正在重连" in page.inner_text("#status-bar")
        assert page.evaluate("() => document.getElementById('input-box').disabled") is True

    def test_grace_window_is_not_reset_by_repeated_closes(self, mobile_page):
        """窗口从第一次断连起算、不随重试重置，否则连续快速失败会把界面永远静默下去。"""
        page = mobile_page
        page.evaluate(
            "() => { window.__wsClient.connectionGraceMs = 500;"
            "  window.__wsClient.connect = () => {}; }"
        )
        page.evaluate("() => window.__wsClient.beginPickerContext()")
        page.evaluate("window.__mockWS.triggerClose()")
        page.wait_for_timeout(250)
        page.evaluate("window.__mockWS.triggerClose()")   # 重连握手又失败，再断一次
        page.wait_for_timeout(100)                        # 距首次断连 350ms < 500ms

        assert page.evaluate("() => document.getElementById('status-bar').style.display") == "none"
        page.wait_for_function(
            "() => document.getElementById('status-bar').style.display === 'block'", timeout=2000
        )

    def test_picker_cancel_reports_immediately(self, mobile_page):
        """点了取消：不进静默窗口，立刻恢复「已断开」并禁用操作。

        取消之后用户马上可能打字、点按钮，界面若还停在「已连接」的样子，这些操作会静默
        失败——比看到一个断线提示糟糕得多。
        """
        page = mobile_page
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")
        page.evaluate(
            "() => { window._filePanel._pendingKind = 'file';"
            "  window.__wsClient.beginPickerContext(); }"
        )
        page.evaluate("window.__mockWS.triggerClose()")

        page.wait_for_timeout(100)
        assert page.evaluate("() => document.getElementById('status-bar').style.display") == "none"

        # 回到前台、等不到 change → 判为取消（走真实入口的取消检测）
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function(
            "() => document.getElementById('status-bar').style.display === 'block'", timeout=1000
        )
        assert page.locator("#input-box").is_disabled()

    def test_picker_cancel_while_connected_does_not_report(self, mobile_page):
        """取消但连接完好：什么都不该发生（不能凭空弹出断线提示）。"""
        page = mobile_page
        assert page.evaluate("() => window.__wsClient.getConnected()") is True
        page.evaluate(
            "() => { window._filePanel._pendingKind = 'file';"
            "  window.__wsClient.beginPickerContext(); }"
        )
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(400)   # 越过 250ms 取消判定

        assert page.evaluate("() => document.getElementById('status-bar').style.display") == "none"
        assert page.locator("#input-box").is_enabled()


class TestPhoneLog:
    """手机端日志浮层：手机上没有 devtools，这是唯一能就地查看断连现场的地方。"""

    def _open(self, page):
        page.locator("#phone-log-btn").click()
        assert page.locator("#phone-log-panel").is_visible()

    def test_button_opens_panel_with_translated_labels(self, mobile_page):
        assert mobile_page.locator("#phone-log-btn").is_visible()
        assert not mobile_page.locator("#phone-log-panel").is_visible()

        self._open(mobile_page)
        # 文案走 i18n（夹具注入 zh_CN 的 mobile 段），不该回退成键名
        assert mobile_page.locator('[data-act="clear"]').inner_text() == "清空"
        assert mobile_page.locator('[data-act="close"]').inner_text() == "关闭"
        # 打开时记录一次环境摘要：排查断连时先看这几行
        assert "[ENV]" in mobile_page.locator("#phone-log-body").inner_text()

    def test_console_and_uncaught_errors_are_captured(self, mobile_page):
        mobile_page.evaluate("() => console.warn('[TEST] 主动记录一行')")
        mobile_page.evaluate("() => { setTimeout(() => { throw new Error('boom-test'); }, 0); }")
        mobile_page.wait_for_timeout(100)
        self._open(mobile_page)

        body = mobile_page.locator("#phone-log-body").inner_text()
        assert "[TEST] 主动记录一行" in body
        assert "boom-test" in body      # 未捕获异常平时完全不可见，必须留痕

    def test_close_hides_panel(self, mobile_page):
        self._open(mobile_page)
        mobile_page.locator('[data-act="close"]').click()
        assert not mobile_page.locator("#phone-log-panel").is_visible()

    def test_reopen_does_not_duplicate_env_snapshot(self, mobile_page):
        """重复开关面板不该重复记环境摘要（一次 4 条，纯刷屏）。"""
        for _ in range(3):
            self._open(mobile_page)
            mobile_page.locator('[data-act="close"]').click()
        self._open(mobile_page)
        assert mobile_page.locator("#phone-log-body").inner_text().count("[ENV]") == 4

    def test_clear_empties_panel(self, mobile_page):
        self._open(mobile_page)
        mobile_page.locator('[data-act="clear"]').click()
        assert mobile_page.locator("#phone-log-body").inner_text() == "(暂无日志)"

    def test_forward_toggle_switches_label(self, mobile_page):
        self._open(mobile_page)
        fwd = mobile_page.locator('[data-act="forward"]')
        # 转发默认关闭：只有需要排查时才手动打开
        assert fwd.inner_text() == "转发:关"
        fwd.click()
        assert fwd.inner_text() == "转发:开"

    def test_level_toggle_filters_constant_noise(self, mobile_page):
        """默认 info 档拦掉 log/debug 级的常量噪音，切到 debug 才看得见。"""
        mobile_page.evaluate("() => console.log('[TEST] 每次连接都重复的常量')")
        self._open(mobile_page)
        body = mobile_page.locator("#phone-log-body")
        lvl = mobile_page.locator('[data-act="level"]')
        assert lvl.inner_text() == "级别:info"
        assert "[TEST] 每次连接都重复的常量" not in body.inner_text()

        lvl.click()
        assert lvl.inner_text() == "级别:debug"
        assert "[TEST] 每次连接都重复的常量" in body.inner_text()


class TestPackagedMode:
    """打包版（服务端注入 __PHONEMIC_DEV__ = false）：日志模块完全不启动。"""

    def test_no_log_module_without_dev_mark(self, page):
        html = MOBILE_HTML_PATH.read_text(encoding="utf-8")
        # 模拟打包版：服务端把占位符替换成 false（见 api.py:_serve_mobile）
        html = html.replace(
            "<!--PHONEMIC_DEV_MODE-->",
            "<script>window.__PHONEMIC_DEV__ = false;</script>",
        )
        page.set_content(html)

        assert page.evaluate("() => window.WS_DIAG") is False
        # 入口按钮不创建（打包版连按钮都不保留）
        assert page.evaluate("() => document.getElementById('phone-log-btn')") is None
        # 但空壳接口仍在，保证 FilePanel 调用 PhoneLog.snap() 不报错
        assert page.evaluate("() => window.PhoneLog.snap()") == ""
        # 未接管 console：记录一行也不会进缓冲
        page.evaluate("() => console.log('[TEST] 打包版不应留痕')")
        assert page.evaluate("() => window.PhoneLog.text()") == ""
