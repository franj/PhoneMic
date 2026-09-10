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
    html = html.replace("<head>", "<head><script>" + MOCK_WS_SCRIPT + "</script>", 1)
    # head 可能带属性（如 data-page-node-id），不能假设精确等于 "<head>"
    html = re.sub(
        r"<head[^>]*>",
        lambda m: m.group(0) + "<script>" + MOCK_WS_SCRIPT + "</script>",
        html,
        count=1,
    )
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
    def test_disconnect_shows_status_bar(self, mobile_page):
        mobile_page.evaluate("() => window.__mockWS.triggerClose()")
        assert mobile_page.locator("#status-bar").is_visible()
        assert mobile_page.locator("#input-box").is_disabled()

    def test_disconnect_disables_buttons(self, mobile_page):
        mobile_page.evaluate("() => window.__mockWS.triggerClose()")
        assert mobile_page.locator("#btn-send").is_disabled()
        assert mobile_page.locator("#btn-clear").is_disabled()


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
