"""
集成测试：Python SecureChannel ↔ JS SecureClient 端到端加密通信

使用 Playwright + Mock WebSocket，Python 端用 SecureChannel 处理 auth 握手和加解密。
参考 test_mobile.py 的模式：内联加载外部脚本，set_content 加载页面，patch 注入密钥。

与 test_js_crypto.py 的区别：
- test_js_crypto 测试 Provider 层（直接调用 encrypt/decrypt）
- 本测试测试协议层（SecureChannel.wrap/unwrap ↔ SecureClient.encrypt/decrypt + WSClient 消息收发）

关键设计：
- set_content 加载 HTML（与 test_mobile.py 一致）
- patch _parseUrlFragment 注入 PC 公钥和 a= 算法列表（绕过 location.hash 限制）
- 不 patch _selectAlgorithm — 让真实的协商选择逻辑运行
- Mock WebSocket 不自动回复 auth_ack — Python 手动处理并发送
- 参数化测试 xsalsa20 和 xchacha20 两种算法（通过调整 a= 列表顺序让客户端分别选中）
"""

import json
from pathlib import Path

import pytest

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS
from phonemic.tunnel.e2ee import SecureChannel

pytest.importorskip("playwright")

RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"

MOCK_WS_SCRIPT = """
window.__mockWS = {
    sentMessages: [],
    current: null,
    triggerMessage: function(jsonString) {
        if (this.current && this.current.onmessage) {
            this.current.onmessage({ data: jsonString });
        }
    },
    triggerClose: function() {
        if (this.current) {
            if (this.current.onclose) this.current.onclose();
            this.current.readyState = 3;
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
        } catch(e) { window.__mockWS.sentMessages.push({ raw: data }); }
        return true;
    };
    this.close = function() {
        if (this.readyState === 3) return;
        if (this.onclose) this.onclose();
        this.readyState = 3;
    };
    window.__mockWS.current = this;
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


def _prepare_html(channel, offered, force_none_algo=False):
    """读取 mobile.html，内联外部脚本，注入 Mock WebSocket 和密钥参数。

    offered: 服务端 a= 下发的算法优先级列表，客户端按序协商选择第一个自身支持的。
    """
    html = (RES_DIR / "mobile.html").read_text(encoding="utf-8")
    sodium_js = (RES_DIR / "sodium.js").read_text(encoding="utf-8")
    crypto_js = (RES_DIR / "crypto_providers.js").read_text(encoding="utf-8")
    html = html.replace('<script src="sodium.js" defer></script>', f"<script>{sodium_js}</script>")
    html = html.replace('<script src="crypto_providers.js" defer></script>', f"<script>{crypto_js}</script>")
    html = html.replace("__I18N_JSON__", "")
    html = html.replace("<head>", "<head><script>" + MOCK_WS_SCRIPT + "</script>", 1)

    # patch _parseUrlFragment：注入 PC 公钥/token 和 a= 算法列表
    pc_pubkey_b64 = channel.get_public_key_b64()
    offered_js = ",".join(f"'{a}'" for a in offered)
    patch = "<script>SecureClient.prototype._parseUrlFragment = function() {"
    if pc_pubkey_b64:
        patch += f"  this._pcPublicKeyB64 = '{pc_pubkey_b64}';"
    patch += f"  this._selectedAlgo = this._selectAlgorithm([{offered_js}]);"
    patch += "};"
    if force_none_algo:
        patch += (
            "SecureClient.prototype._parseUrlFragment = function() {"
            "  this._pcPublicKeyB64 = 'fake_token';"
            "  this._selectedAlgo = 'none';"
            "};"
        )
    patch += "</script>"
    html = html.replace("</body>", patch + "</body>", 1)

    # 暴露 wsClient 供 Python 端检查
    html = html.replace(
        "wsClient.connect();",
        "wsClient.connect(); window.__wsClient = wsClient;",
    )
    return html


def _wait_auth(page):
    """等待 JS 发送 auth 消息并返回。"""
    page.wait_for_function(
        "() => window.__mockWS && window.__mockWS.sentMessages.some(m => m.type === 'auth')"
    )
    return page.evaluate(
        "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
    )


def _process_auth(page, channel):
    """Python 处理 auth 并发送 auth_ack。"""
    auth_msg = _wait_auth(page)
    assert channel.receive_auth(auth_msg) is True
    ack = channel.make_auth_ack()
    page.evaluate(
        "(msg) => window.__mockWS.triggerMessage(msg)",
        json.dumps(ack),
    )
    page.wait_for_function(
        "() => window.__wsClient && window.__wsClient.isConnected"
    )


@pytest.fixture(params=["xsalsa20", "xchacha20"])
def secure_pair(page, request):
    """参数化 fixture：Python SecureChannel + JS SecureClient（已认证）。

    服务端下发完整优先级列表；为覆盖两种算法，将测试算法置于列表首位，
    验证客户端按序协商后回传选择。
    """
    algo = request.param
    pc = SecureChannel(algorithm="auto")
    channel = pc.new_session()
    offered = [algo] + [a for a in OFFERED_ALGORITHMS if a != algo]
    html = _prepare_html(pc, offered)
    page.set_content(html)
    _process_auth(page, channel)
    yield page, channel, algo


# ---------- 握手测试 ----------

class TestHandshake:
    def test_auth_success(self, secure_pair):
        """握手成功：Python 已认证，JS 已连接。"""
        page, channel, algo = secure_pair
        assert channel.is_authenticated
        assert not channel.is_rejected
        assert page.evaluate("() => window.__wsClient.isConnected") is True

    def test_algorithm_selected_correctly(self, secure_pair):
        """JS 选择的算法与指定的优先算法一致。"""
        page, channel, algo = secure_pair
        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert auth_msg["algo"] == algo

    def test_auth_ack_received(self, secure_pair):
        """auth_ack 被正确处理，SecureClient 已认证。"""
        page, channel, algo = secure_pair
        assert page.evaluate("() => window.__wsClient.secure.isAuthenticated") is True

    def test_ui_enabled_after_auth(self, secure_pair):
        """认证后 UI 可用。"""
        page, channel, algo = secure_pair
        assert page.locator("#input-box").is_enabled()
        assert page.locator("#btn-send").is_enabled()
        assert not page.locator("#status-bar").is_visible()


# ---------- JS → Python ----------

class TestJSToPython:
    def test_send_message(self, secure_pair):
        """JS → Python：发送消息，Python 解密验证。"""
        page, channel, algo = secure_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("hello from JS")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        data_msgs = [m for m in sent if m["type"] == "data"]
        send_msgs = [channel.unwrap(m) for m in data_msgs]
        send_msgs = [m for m in send_msgs if m and m.get("type") == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "hello from JS"

    def test_preview_message(self, secure_pair):
        """JS → Python：输入触发 preview，Python 解密验证。"""
        page, channel, algo = secure_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("preview text")

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        data_msgs = [m for m in sent if m["type"] == "data"]
        assert len(data_msgs) >= 1
        decrypted = channel.unwrap(data_msgs[-1])
        assert decrypted["type"] == "preview"
        assert decrypted["text"] == "preview text"

    def test_multiple_messages_sequential(self, secure_pair):
        """JS → Python：连续发送多条消息，全部正确解密。"""
        page, channel, algo = secure_pair
        page.locator("#mode-toggle").click()

        texts = ["msg1", "msg2", "msg3"]
        for text in texts:
            page.locator("#input-box").fill(text)
            page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        data_msgs = [m for m in sent if m["type"] == "data"]
        send_msgs = [channel.unwrap(m) for m in data_msgs]
        send_msgs = [m for m in send_msgs if m and m.get("type") == "send"]
        assert len(send_msgs) == len(texts)
        for i, text in enumerate(texts):
            assert send_msgs[i]["text"] == text

    def test_message_displayed_in_chat(self, secure_pair):
        """JS 发送的消息显示在聊天列表中。"""
        page, channel, algo = secure_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("chat msg")
        page.locator("#btn-send").click()

        msgs = page.locator(".message")
        assert msgs.count() >= 1
        assert "chat msg" in msgs.last.text_content()


# ---------- Python → JS ----------

class TestPythonToJS:
    def test_config_message(self, secure_pair):
        """Python → JS：发送 config，JS 应用配置。"""
        page, channel, algo = secure_pair
        wrapped = channel.wrap({"type": "config", "mobile_max_records": 10})
        page.evaluate(
            "(msg) => window.__mockWS.triggerMessage(msg)",
            json.dumps(wrapped),
        )
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 10

    def test_config_update_twice(self, secure_pair):
        """Python → JS：连续发送两次 config 更新。"""
        page, channel, algo = secure_pair

        wrapped1 = channel.wrap({"type": "config", "mobile_max_records": 5})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(wrapped1))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 5

        wrapped2 = channel.wrap({"type": "config", "mobile_max_records": 20})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(wrapped2))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 20

    def test_receive_send_no_error(self, secure_pair):
        """Python → JS：接收 send 回声不影响连接。"""
        page, channel, algo = secure_pair
        wrapped = channel.wrap({"type": "send", "text": "echo from Python"})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(wrapped))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.__wsClient.isConnected") is True


# ---------- 双向通信 ----------

class TestRoundtrip:
    def test_bidirectional_communication(self, secure_pair):
        """双向通信：Python → JS → Python → JS。"""
        page, channel, algo = secure_pair

        # Python → JS: config
        wrapped = channel.wrap({"type": "config", "mobile_max_records": 5})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(wrapped))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 5

        # JS → Python: send
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("roundtrip")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        data_msgs = [m for m in sent if m["type"] == "data"]
        send_msgs = [channel.unwrap(m) for m in data_msgs]
        send_msgs = [m for m in send_msgs if m and m.get("type") == "send"]
        assert send_msgs[-1]["text"] == "roundtrip"

        # 验证消息显示在聊天列表
        msgs = page.locator(".message")
        assert msgs.count() >= 1
        assert "roundtrip" in msgs.last.text_content()

        # Python → JS: 再次更新 config
        wrapped2 = channel.wrap({"type": "config", "mobile_max_records": 15})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(wrapped2))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 15


# ---------- 算法拒绝 ----------

class TestAlgorithmRejection:
    def test_none_rejected(self, page):
        """none 算法被 PC 端拒绝，JS 断开连接。"""
        pc = SecureChannel(algorithm="auto")
        channel = pc.new_session()
        html = _prepare_html(pc, ["xsalsa20"], force_none_algo=True)
        page.set_content(html)

        auth_msg = _wait_auth(page)
        assert auth_msg["algo"] == "none"

        assert channel.receive_auth(auth_msg) is False
        assert channel.is_rejected
        assert "not allowed" in channel.reject_reason

        ack = channel.make_auth_ack()
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(ack))

        page.wait_for_function("() => !window.__wsClient.isConnected", timeout=2000)
        assert page.evaluate("() => window.__wsClient.isConnected") is False
        assert page.locator("#status-bar").is_visible()
        assert page.locator("#input-box").is_disabled()


# ---------- 算法协商 ----------

class TestAlgorithmNegotiation:
    """a= 为服务端算法优先级列表，客户端按序选择第一个自身支持的。"""

    def test_client_prefers_first_offered(self, page):
        """客户端选择列表中首个支持的算法。"""
        pc = SecureChannel(algorithm="auto")
        html = _prepare_html(pc, ["xchacha20", "xsalsa20"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")
        assert page.evaluate("() => window.__wsClient.secure._selectedAlgo") == "xchacha20"

    def test_client_falls_back_along_list(self, page):
        """列表首位不支持时按序回退到下一个支持的算法。"""
        pc = SecureChannel(algorithm="auto")
        html = _prepare_html(pc, ["aes-256-gcm", "xchacha20", "xsalsa20"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")
        assert page.evaluate("() => window.__wsClient.secure._selectedAlgo") == "xchacha20"

    def test_no_common_algorithm_stops_connect(self, page):
        """无共同算法：不建立 WebSocket 连接，提示重新扫码。"""
        pc = SecureChannel(algorithm="auto")
        html = _prepare_html(pc, ["aes-256-gcm", "aegis256"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")

        assert page.evaluate("() => window.__wsClient.secure.algoUnsupported") is True
        # 未创建 WebSocket
        assert page.evaluate("() => window.__mockWS.current") is None
        text = page.locator("#status-bar").inner_text()
        assert "重新扫码" in text
        assert page.locator("#input-box").is_disabled()

    def test_server_offered_list_priority(self):
        """服务端 URL 下发完整优先级列表：xchacha20 优先于 xsalsa20。"""
        assert OFFERED_ALGORITHMS[0] == "xchacha20"
        pc = SecureChannel(algorithm="auto")
        url = pc.append_to_url("https://x.trycloudflare.com")
        assert "a=xchacha20,xsalsa20" in url

    def test_auth_echoes_client_choice(self, secure_pair):
        """auth 消息回传客户端协商出的算法，服务端 accept 并在 ack 中回显。"""
        page, channel, algo = secure_pair
        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert auth_msg["algo"] == algo


# ---------- 断线处理 ----------

class TestDisconnect:
    def test_disconnect_updates_ui(self, secure_pair):
        """认证后断开连接，UI 显示断线状态。"""
        page, channel, algo = secure_pair
        assert page.locator("#input-box").is_enabled()

        page.evaluate("() => window.__mockWS.triggerClose()")
        page.wait_for_timeout(100)

        assert page.locator("#status-bar").is_visible()
        assert page.locator("#input-box").is_disabled()
        assert page.locator("#btn-send").is_disabled()
        assert page.evaluate("() => window.__wsClient.isConnected") is False


# ---------- none+LAN 模式 ----------

@pytest.fixture
def none_lan_pair(page):
    """none+LAN 模式：无认证，明文 JSON。"""
    pc = SecureChannel(algorithm="none", mode="lan")
    channel = pc.new_session()
    html = _prepare_html(pc, ["none"])
    page.set_content(html)
    page.wait_for_function("() => window.__wsClient && window.__wsClient.isConnected")
    yield page, channel


class TestNoneLAN:
    def test_no_auth_needed(self, none_lan_pair):
        """none+LAN: 无需 auth，直接连接。"""
        page, channel = none_lan_pair
        assert not channel.needs_auth
        assert not channel.is_encrypted
        assert channel.is_authenticated
        assert page.evaluate("() => window.__wsClient.isConnected") is True

    def test_no_auth_message_sent(self, none_lan_pair):
        """none+LAN: JS 不发送 auth 消息。"""
        page, channel = none_lan_pair
        auth_msgs = page.evaluate(
            "() => window.__mockWS.sentMessages.filter(m => m.type === 'auth')"
        )
        assert len(auth_msgs) == 0

    def test_send_plaintext_message(self, none_lan_pair):
        """none+LAN: JS → Python 明文消息。"""
        page, channel = none_lan_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("hello plaintext")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in sent if m["type"] == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "hello plaintext"

        unwrapped = channel.unwrap(send_msgs[-1])
        assert unwrapped["text"] == "hello plaintext"

    def test_receive_plaintext_config(self, none_lan_pair):
        """none+LAN: Python → JS 明文 config。"""
        page, channel = none_lan_pair
        msg = channel.wrap({"type": "config", "mobile_max_records": 7})
        assert msg["type"] == "config"
        page.evaluate("(m) => window.__mockWS.triggerMessage(m)", json.dumps(msg))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 7

    def test_bidirectional_plaintext(self, none_lan_pair):
        """none+LAN: 双向明文通信。"""
        page, channel = none_lan_pair

        # Python → JS
        msg = channel.wrap({"type": "config", "mobile_max_records": 3})
        page.evaluate("(m) => window.__mockWS.triggerMessage(m)", json.dumps(msg))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 3

        # JS → Python
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("lan roundtrip")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in sent if m["type"] == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "lan roundtrip"

    def test_disconnect_updates_ui(self, none_lan_pair):
        """none+LAN: 断线后 UI 显示断线状态。"""
        page, channel = none_lan_pair
        assert page.locator("#input-box").is_enabled()

        page.evaluate("() => window.__mockWS.triggerClose()")
        page.wait_for_timeout(100)

        assert page.locator("#status-bar").is_visible()
        assert page.locator("#input-box").is_disabled()
        assert page.evaluate("() => window.__wsClient.isConnected") is False


# ---------- none+Cloudflare 模式 ----------

@pytest.fixture
def none_cf_pair(page):
    """none+Cloudflare 模式：token 认证，明文 JSON。"""
    pc = SecureChannel(algorithm="none", mode="cloudflare")
    channel = pc.new_session()
    html = _prepare_html(pc, ["none"])
    page.set_content(html)
    _process_auth(page, channel)
    yield page, channel, pc


class TestNoneCloudflare:
    def test_token_auth_success(self, none_cf_pair):
        """none+CF: token 认证成功。"""
        page, channel, pc = none_cf_pair
        assert channel.needs_auth
        assert not channel.is_encrypted
        assert channel.is_authenticated
        assert page.evaluate("() => window.__wsClient.isConnected") is True

    def test_auth_uses_token(self, none_cf_pair):
        """none+CF: auth 消息包含 token。"""
        page, channel, pc = none_cf_pair
        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert auth_msg["algo"] == "none"
        token = pc.get_public_key_b64()
        assert auth_msg["data"] == token

    def test_send_plaintext_after_auth(self, none_cf_pair):
        """none+CF: 认证后明文消息（JS → Python）。"""
        page, channel, pc = none_cf_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("cf plaintext")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in sent if m["type"] == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "cf plaintext"

        unwrapped = channel.unwrap(send_msgs[-1])
        assert unwrapped["text"] == "cf plaintext"

    def test_receive_plaintext_config(self, none_cf_pair):
        """none+CF: Python → JS 明文 config。"""
        page, channel, pc = none_cf_pair
        msg = channel.wrap({"type": "config", "mobile_max_records": 8})
        page.evaluate("(m) => window.__mockWS.triggerMessage(m)", json.dumps(msg))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 8

    def test_bidirectional_plaintext(self, none_cf_pair):
        """none+CF: 双向明文通信。"""
        page, channel, pc = none_cf_pair

        # Python → JS
        msg = channel.wrap({"type": "config", "mobile_max_records": 15})
        page.evaluate("(m) => window.__mockWS.triggerMessage(m)", json.dumps(msg))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 15

        # JS → Python
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("cf roundtrip")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in sent if m["type"] == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "cf roundtrip"


class TestNoneCloudflareRejection:
    def test_wrong_token_rejected(self, page):
        """none+CF: 错误 token 被拒绝。"""
        pc = SecureChannel(algorithm="none", mode="cloudflare")
        channel = pc.new_session()
        html = _prepare_html(pc, ["none"], force_none_algo=True)
        page.set_content(html)

        auth_msg = _wait_auth(page)
        assert auth_msg["algo"] == "none"
        assert auth_msg["data"] == "fake_token"

        assert channel.receive_auth(auth_msg) is False
        assert channel.is_rejected
        assert "token mismatch" in channel.reject_reason

        ack = channel.make_auth_ack()
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", json.dumps(ack))

        page.wait_for_function("() => !window.__wsClient.isConnected", timeout=2000)
        assert page.evaluate("() => window.__wsClient.isConnected") is False
        assert page.locator("#status-bar").is_visible()
        assert page.locator("#input-box").is_disabled()


# ---------- 认证失败：用户提示与重连策略 ----------

class TestAuthFailureUX:
    """认证失败后：明确提示用户、停止无意义重连；密钥缺失时不发起连接。"""

    def test_rejected_ack_shows_warning_and_stops_reconnect(self, page):
        """模拟旧二维码：服务端拒绝认证后提示重新扫码，且不再自动重连。"""
        pc = SecureChannel(algorithm="auto")
        html = _prepare_html(pc, ["xsalsa20"])
        page.set_content(html)
        _wait_auth(page)

        # 服务端拒绝（对应二维码过期 / 密钥不匹配）
        page.evaluate(
            "(msg) => window.__mockWS.triggerMessage(msg)",
            json.dumps({"type": "auth_ack", "rejected": True, "reason": "auth data processing failed"}),
        )

        page.wait_for_function("() => window.__wsClient.authRejected === true", timeout=2000)
        # 状态栏提示重新扫码，输入框禁用
        text = page.locator("#status-bar").inner_text()
        assert "重新扫码" in text
        assert page.locator("#input-box").is_disabled()
        # 不再安排重连
        assert page.evaluate("() => window.__wsClient.reconnectTimer") is None

    def test_missing_key_does_not_connect(self, page):
        """加密算法但 URL 无密钥（如浏览器丢失 hash）：直接提示，不建立连接。"""
        pc = SecureChannel(algorithm="auto")
        html = _prepare_html(pc, ["xsalsa20"])
        # 去掉注入的公钥，模拟 hash 丢失
        html = html.replace(f"this._pcPublicKeyB64 = '{pc.get_public_key_b64()}';", "")

        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready", timeout=5000)

        # 未创建 WebSocket，状态栏提示重新扫码
        assert page.evaluate("() => window.__mockWS.current") is None
        text = page.locator("#status-bar").inner_text()
        assert "重新扫码" in text
        assert page.locator("#input-box").is_disabled()

    def test_reconnect_fixed_interval(self, secure_pair):
        """断连重连保持固定间隔：保证后台切回前台时快速重连。"""
        page, channel, algo = secure_pair
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 2000

        # 多次断连后间隔仍保持 2000，不递增
        page.evaluate("() => window.__mockWS.triggerClose()")
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 2000
        page.evaluate("() => window.__mockWS.triggerClose()")
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 2000
        assert page.evaluate("() => window.__wsClient.reconnectTimer") is not None
