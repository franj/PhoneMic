"""
mobile.html UI 测试
使用 Playwright + Mock WebSocket 进行前端测试，无需启动真实服务端。
依赖: pytest-playwright (需先运行 playwright install chromium)

Mock 策略：
- 内联加载 sodium.js 和 crypto_providers.js（set_content 无法加载外部脚本）
- 设置 location.hash 为 #a=none&p=none，让 SecureClient 选择 PlainProvider（不加密）
- Mock WS 自动回复 auth_ack，使 WSClient 进入已认证状态
- triggerMessage 将消息包装为 {type:'data', data:base64(JSON)} 格式
- sent_messages 解码 data 格式的消息，返回原始内容
"""

from pathlib import Path

import pytest

pytest.importorskip("playwright")

RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"
MOBILE_HTML_PATH = RES_DIR / "mobile.html"

MOCK_WS_SCRIPT = """
window.__mockWS = {
    sentMessages: [],
    current: null,
    instances: [],

    triggerMessage: function(data) {
        if (this.current && this.current.onmessage) {
            // 包装为 data 信封（与加密协议一致）
            var pt = sodium.from_string(JSON.stringify(data));
            var b64 = sodium.to_base64(pt, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
            this.current.onmessage({ data: JSON.stringify({type: 'data', data: b64}) });
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
            var msg = JSON.parse(data);
            window.__mockWS.sentMessages.push(msg);
            // 自动回复 auth_ack
            if (msg.type === 'auth') {
                var self = this;
                setTimeout(function() {
                    if (self.onmessage) {
                        self.onmessage({ data: JSON.stringify({type: 'auth_ack', algo: msg.algo}) });
                    }
                }, 0);
            }
        } catch(e) { window.__mockWS.sentMessages.push({ raw: data }); }
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
    crypto_js = (RES_DIR / "crypto_providers.js").read_text(encoding="utf-8")
    html = html.replace('<script src="sodium.js"></script>', f"<script>{sodium_js}</script>")
    html = html.replace('<script src="crypto_providers.js"></script>', f"<script>{crypto_js}</script>")
    html = html.replace("__I18N_JSON__", "")
    html = html.replace("<head>", "<head><script>" + MOCK_WS_SCRIPT + "</script>", 1)
    # 注入 patch：在主脚本之后、onload 之前，强制使用不加密模式
    patch = (
        "<script>"
        "SecureClient.prototype._parseUrlFragment = function() {"
        "  this._availableAlgos = ['none'];"
        "  this._preferredAlgo = 'none';"
        "};"
        "SecureClient.prototype._selectAlgorithm = function() {"
        "  this._selectedAlgo = 'none';"
        "};"
        "</script>"
    )
    html = html.replace("</body>", patch + "</body>", 1)
    page.set_content(html)
    page.wait_for_function(
        "() => window.__mockWS && window.__mockWS.current && window.__mockWS.current.readyState === 1"
    )
    # 等待 auth 握手完成
    page.wait_for_function(
        "() => window.__mockWS.sentMessages.some(m => m.type === 'auth')"
    )
    page.wait_for_timeout(50)
    # 模拟服务端发送 config 消息
    page.evaluate("() => window.__mockWS.triggerMessage({type: 'config', mobile_max_records: 5})")
    yield page


def sent_messages(page):
    """解码 sentMessages，将 {type:'data', data:base64} 还原为原始消息。"""
    return page.evaluate("""
        () => {
            return window.__mockWS.sentMessages.map(msg => {
                if (msg.type === 'data') {
                    try {
                        const raw = sodium.from_base64(msg.data, sodium.base64_VARIANT_URLSAFE_NO_PADDING);
                        return JSON.parse(sodium.to_string(raw));
                    } catch(e) { return msg; }
                }
                return msg;
            });
        }
    """)


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
