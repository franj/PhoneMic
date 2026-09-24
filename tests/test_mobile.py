"""
mobile.html UI 测试
使用 Playwright + Mock WebSocket 进行前端测试，无需启动真实服务端。
依赖: pytest-playwright (需先运行 playwright install chromium)

背景：加密与认证已解耦，加密永远开启，明文模式与 PlainProvider 已删除
（见 docs/e2ee-always-on-design.md）。SecureClient 恒需三步握手
（auth → auth_challenge → auth_proof），下行数据帧全部整帧加密。

Mock 策略：
- 内联加载 sodium.js、msgpack.min.js 和 crypto_providers.js
  （set_content 无法加载外部脚本）
- 页面按 **url_fragment 模式**加载：head 的 boot 脚本在 SecureClient.init 之前
  注入 ``location.hash = "#k=<PC公钥>&a=xchacha20"``，故 _authMode === 'url_fragment'，
  由 QR 里的 PC 公钥立即做 ECDH（无需 TOFU 审批）
- mock WS 自持一个**独立 Provider**——克隆客户端 secure 的 provider
  （复制 _phonePrivate/_phonePublicKey/_pcPublicKey/_sharedKey，seq 从 0 起）。
  绝不能复用客户端实例：客户端 tx=上行、rx=下行，mock 恰好相反，共用计数器会
  互相踩踏，帧一多就 seq 失步、解密全挂
- 握手：mock 收到明文 auth 后，用该独立 Provider 加密回
  ``{type:'auth_challenge', nonce:<16B>}``；客户端回加密 auth_proof，握手完成
- 上行：mock 先 MessagePack.decode；解出 ``type==='auth'`` 即明文握手帧（其余帧
  整帧加密、必然解不出），其余帧用独立 Provider 解密后再解出应用层报文
- 下行：triggerMessage 用同一独立实例加密（seq 独立于客户端）
- sent_messages 返回解码后的上行应用层报文（含握手帧）
"""

import base64
import json
import re
from pathlib import Path

import pytest
from nacl.public import PrivateKey

pytest.importorskip("playwright")
RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"
MOBILE_HTML_PATH = RES_DIR / "mobile.html"

# 手机端语言包由 /api/lang.json 提供，而 set_content 下无法真实 fetch。
# 直接把 zh_CN 的 mobile 段注入 window.i18n，等价于服务端返回的内容。
MOBILE_I18N = json.loads(
    (RES_DIR / "locales" / "zh_CN.json").read_text(encoding="utf-8")
)["mobile"]


def _bubble_re(key: str, **subs) -> "re.Pattern[str]":
    """把 i18n 文案编译成「整条匹配」的正则，用于断言气泡文案。

    文案里的占位符分两类：测试自己能填的（`{name}`，通过 `subs` 给出）和**运行期才知道**
    的（`{speed}` —— 完成气泡里的全程平均速率，由 `FilePanel._formatSpeed` 现算）。后者
    一律放宽成 `.*?`。

    ⚠️ 不这么做的后果不是「报错」而是**静默失效**：直接拿
    `MOBILE_I18N[...].replace("{name}", ...)` 去比，字面量里还留着一个 `{speed}`，
    于是 `in bubbles` 永远为假（正向断言假失败），`not in bubbles` 永远为真
    （负向断言退化成空断言——「不该出现的成功气泡」再出现也拦不住）。
    """
    pat = re.escape(MOBILE_I18N[key])
    for name, value in subs.items():
        pat = pat.replace(re.escape("{" + name + "}"), re.escape(str(value)))
    pat = re.sub(r"\\\{[a-z_]+\\\}", ".*?", pat)     # 剩余占位符放宽
    return re.compile("^" + pat + "$")


def _any_bubble(bubbles, key: str, **subs) -> bool:
    """气泡文案列表里是否存在与 i18n 文案匹配的一条。"""
    pattern = _bubble_re(key, **subs)
    return any(pattern.match(b) for b in bubbles)


def _b64url_nopad(raw: bytes) -> str:
    """URL-safe base64 去 padding——与 sodium.base64_VARIANT_URLSAFE_NO_PADDING 一致。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# QR fragment 里的 PC 公钥（``#k=``）。mock 只负责「共享同一会话密钥」的加解密，
# 会话密钥直接克隆自客户端，故不需要 PC 私钥；用真实 X25519 公钥是为保证
# 客户端的 crypto_scalarmult 接受该点（低阶点会被 libsodium 拒绝）。
_PC_PUBLIC_KEY_B64 = _b64url_nopad(bytes(PrivateKey.generate().public_key))

MOCK_WS_SCRIPT = """
window.__mockWS = {
    sentMessages: [],
    current: null,
    instances: [],
    // 模拟「一个正常工作的 PC」：收到 data 块异步回逐块 ack、收到 cancel 回取消回执、
    // 收到 hello 回显探活号。默认关闭——需要走到 end 的用例显式 autoAck(true)
    //（窗口闸门生效后没有 ack 就填不满窗口、走不到 end；取消生效后没有回执就解锁不了界面）。
    autoAckFn: null,
    received: {},
    // mock 侧 Provider：与客户端 secure 的 Provider **共享同一会话密钥**，
    // 但 tx/rx seq 完全独立（客户端 tx=上行、rx=下行，mock 恰好相反）。
    // 复用客户端实例会让两端计数器互相踩踏 ⇒ 帧一多就 seq 失步、解密全挂。
    _mockProvider: null,

    // 克隆客户端 Provider：复制全部密钥材料（含 ECDH 派生），seq 从 0 起。
    // 客户端此刻可能尚未派生 _sharedKey（要到首个加密帧才惰性派生），
    // 这里就地派生，得到与客户端完全一致的会话密钥。
    _cloneProvider: function() {
        var src = window.__wsClient.secure._provider;
        var clone = new src.constructor();
        clone._phonePrivate = src._phonePrivate;
        clone._phonePublicKey = src._phonePublicKey;
        clone._pcPublicKey = src._pcPublicKey;
        clone._sharedKey = src._sharedKey;
        clone._txSeq = 0;
        clone._rxSeq = 0;
        if (!clone._sharedKey && clone._pcPublicKey) clone._deriveSharedKey();
        return clone;
    },
    _provider: function() {
        if (!this._mockProvider) this._mockProvider = this._cloneProvider();
        return this._mockProvider;
    },

    // 模拟 PC 侧握手应答：收到明文 auth 后，用独立 Provider 加密回
    // auth_challenge。异步（setTimeout 0）与真实服务端往返一致；每次握手前
    // 归零 seq，与客户端 makeAuth() 的 reset 对齐（重连即新会话）。
    _replyChallenge: function() {
        var mock = this;
        var provider = mock._provider();
        provider.reset();
        var nonce = sodium.randombytes_buf(16);
        var challenge = provider.encrypt(
            MessagePack.encode({ type: 'auth_challenge', nonce: nonce }));
        setTimeout(function() {
            if (mock.current && mock.current.onmessage) {
                mock.current.onmessage({ data: challenge });
            }
        }, 0);
    },

    autoAck: function(on) {
        this.autoAckFn = on ? function(msg) {
            var mock = window.__mockWS;
            if (msg.type === 'hello') {          // 探活回执：原样回显 t
                setTimeout(function() {
                    mock.triggerMessage({ type: 'hello', t: msg.t });
                }, 0);
                return;
            }
            if (msg.a === 'cancel') {            // 取消回执（协议 §9）
                setTimeout(function() {
                    mock.triggerMessage(
                        { type: 'ack', ref: msg.type, id: msg.id, a: 'cancel' });
                }, 0);
                return;
            }
            var key = msg.type + ':' + msg.id;
            var recv = (mock.received[key] || 0) + msg.chunk.length;
            mock.received[key] = recv;
            setTimeout(function() {
                mock.triggerMessage(
                    { type: 'ack', ref: msg.type, id: msg.id, a: 'data', n: msg.n, received: recv });
            }, 0);
        } : null;
    },

    triggerMessage: function(data) {
        if (this.current && this.current.onmessage) {
            // 与真实服务端一致：经**独立 Provider**加密产出下行线上字节
            //（整帧密文；seq 由 mock 自己的计数器承载，不碰客户端实例）
            var raw = this._provider().encrypt(MessagePack.encode(data));
            this.current.onmessage({ data: raw });
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
        var mock = window.__mockWS;
        var msg = null;
        // 握手首帧 auth 是**明文** msgpack（只有 data 字段被 SealedBox 密封，
        // 算法名藏在密文内），能直接解出来；其余帧整帧加密，MessagePack.decode
        // 必然失败。故先试探解码，type==='auth' 即判定为明文握手帧。
        var plain = null;
        try { plain = MessagePack.decode(data); } catch (e) { plain = null; }
        if (plain && plain.type === 'auth') {
            msg = plain;
            mock.sentMessages.push(msg);
            mock._replyChallenge();       // 加密回 auth_challenge
        } else {
            // 加密帧：用独立 Provider 解密，再解出应用层报文
            try {
                msg = MessagePack.decode(mock._provider().decrypt(data));
                mock.sentMessages.push(msg);
            } catch (e) {
                mock.sentMessages.push({ raw: 'undecodable', error: String(e) });
                msg = null;
            }
        }
        if (msg && mock.autoAckFn &&
            (msg.a === 'data' || msg.a === 'cancel' || msg.type === 'hello')) {
            mock.autoAckFn(msg);
        }
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


def _build_mobile_html(pc_public_key_b64: str) -> str:
    """组装可离线运行的 mobile.html：内联依赖脚本 + 注入 mock WS / i18n / url fragment。

    返回 HTML 文本，由 fixture 用 ``page.set_content`` 装载——``set_content``
    不做真实导航，页面 hostname 因此保持为调用方预先设定的值（见
    ``cloudflare_page``）。

    ``pc_public_key_b64`` 就是 QR fragment 里的 ``k=``：在 head 的 boot 脚本里
    写入 ``location.hash``，早于 ``SecureClient.init``，使页面进入 url_fragment 模式。
    """
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
    #
    # location.hash 必须在这里设：boot 脚本先于主脚本与 SecureClient.init 执行，
    # 页面据此进入 url_fragment 模式（有 k= → 不落 TOFU 审批路径）。
    boot = (
        "<script>window.__PHONEMIC_DEV__=true;"
        "location.hash = '" + "#k=" + pc_public_key_b64 + "&a=xchacha20';"
        "</script>"
        "<script>" + MOCK_WS_SCRIPT + "</script>"
    )
    html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + boot, html, count=1)
    # 暴露 wsClient：mock 需要读它的 secure._provider 来克隆共享会话密钥
    html = html.replace(
        "wsClient.connect();",
        "wsClient.connect(); window.__wsClient = wsClient;",
    )
    return html


def _boot_mobile(page) -> None:
    """装载页面并等 url_fragment 三步握手完成（auth → auth_challenge → auth_proof）。"""
    page.set_content(_build_mobile_html(_PC_PUBLIC_KEY_B64))
    page.wait_for_function(
        "() => window.__mockWS && window.__mockWS.current && window.__mockWS.current.readyState === 1"
    )
    page.wait_for_function(
        "() => window.__wsClient && window.__wsClient.isConnected"
    )
    page.wait_for_timeout(50)
    # 模拟服务端发送 config 消息
    page.evaluate("() => window.__mockWS.triggerMessage({type: 'config', mobile_max_records: 5})")


def _set_wait_timeouts(page, cancel_ms=None, hello_ms=None) -> None:
    """覆写 FilePanel 的两个等待时限（都是派生 getter，见 `LINK` 档案）。

    ``configurable: true`` 不能省：首次 ``defineProperty`` 出来的描述符默认不可重定义，
    同一个用例里改第二次就会抛 TypeError。
    """
    js = ""
    if cancel_ms is not None:
        js += ("  set('CANCEL_ACK_TIMEOUT', " + str(cancel_ms) + ");")
    if hello_ms is not None:
        js += ("  set('HELLO_TIMEOUT', " + str(hello_ms) + ");")
    page.evaluate(
        "() => { const C = window._filePanel.constructor;"
        "  const set = (n, v) => Object.defineProperty(C, n,"
        "      { value: v, configurable: true });"
        + js + " }"
    )


@pytest.fixture
def mobile_page(page):
    _boot_mobile(page)
    yield page


@pytest.fixture
def cloudflare_page(page):
    """在 *.trycloudflare.com 下装载同一份 mobile.html，用于验证「按链路取分块上限」。

    真实隧道无法在测试里复现，这里只做路由拦截伪造域名、不真出网。
    ``set_content`` 不改变页面 URL，所以 hostname 会一直保持为伪造域名。
    """
    page.context.route(
        "https://phonemic-test.trycloudflare.com/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><head></head><body></body></html>",
        ),
    )
    page.goto("https://phonemic-test.trycloudflare.com/")
    host = page.evaluate("() => location.hostname")
    assert host == "phonemic-test.trycloudflare.com", f"路由拦截未生效：hostname={host!r}"
    _boot_mobile(page)
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


def test_transfer_chunk_size_follows_file_size(mobile_page):
    """分块按体积自适应（协议 §9）：3MB → 12 块 × 256KB。

    夹具页面跑在 localhost ⇒ 局域网档，上限 LINK_LAN.chunkMax = 1MB。
    这里接管 ``transport.sendFrame`` 同步回 ack —— 窗口闸门生效后，没有 ack 就走不到 end。
    （FilePanel 不走 onCommand 回调注入，帧出口就是 ``panel.transport.sendFrame``。）
    """
    page = mobile_page
    seq = page.evaluate(
        "async () => {"
        "  const panel = window._filePanel;"
        "  const tr = panel.transport;"
        "  const saved = [];"
        "  let recv = 0;"
        "  tr.sendFrame = (f) => { saved.push(f.a + ':' + (f.chunk ? f.chunk.length : 0));"
        "                          if (f.a === 'data') { recv += f.chunk.length;"
        "                            window.__mockWS.triggerMessage({ type: 'ack', ref: 'file',"
        "                              id: f.id, a: 'data', n: f.n, received: recv }); }"
        "                          return true; };"
        "  panel._waitEndAck = () => Promise.resolve(true);"
        "  const fake = { name: 'big.bin', size: 3 * 1024 * 1024,"
        "                 slice: (a, b) => new Blob([new Uint8Array(b - a)]) };"
        "  await panel._start(fake, 'file');"
        # 复原成原型上的方法。注意 evaluate 的字符串被隐式拼接成**一行**，
        # 里面绝不能写 `//` 行注释 —— 它会把后面所有代码一起吃光。
        "  delete tr.sendFrame;"
        "  return saved.join(',');"
        "}"
    )
    assert seq.startswith("start:0"), seq
    assert seq.count("data:262144") == 12, seq
    assert seq.endswith("end:0"), seq


def test_pick_chunk_size_converges_to_12_chunks_within_bounds(mobile_page):
    """pickChunkSize：块数收敛 ~12、对齐 256KB，并夹在 [256KB, 1MB] 内。"""
    page = mobile_page
    r = page.evaluate(
        "() => {"
        "  const F = window._filePanel.constructor;"
        "  const MB = 1024 * 1024;"
        "  const out = {};"
        "  F.setServerMaxFrame(16 * 1024 * 1024);"
        "  out.s100k = F.pickChunkSize(100 * 1024);"
        "  out.s2 = F.pickChunkSize(2 * MB);"
        "  out.s3 = F.pickChunkSize(3 * MB);"
        "  out.s3p = F.pickChunkSize(3 * MB + 1);"
        "  out.s6 = F.pickChunkSize(6 * MB);"
        "  out.s12 = F.pickChunkSize(12 * MB);"
        "  out.s1g = F.pickChunkSize(1024 * MB);"
        "  F.setServerMaxFrame(512 * 1024);"
        "  out.capped = F.pickChunkSize(1024 * MB);"
        "  F.setServerMaxFrame(0);"
        "  out.fallback = F.pickChunkSize(1024 * MB);"
        "  return out;"
        "}"
    )
    KB = 1024
    MB = 1024 * 1024
    assert r["s100k"] == 256 * KB, r                # 小文件也不低于下限（1 块）
    assert r["s2"] == 256 * KB, r                   # 2MB / 12 → 256KB（8 块）
    assert r["s3"] == 256 * KB, r                   # 3MB / 12 正好 256KB（12 块）
    assert r["s3p"] == 512 * KB, r                  # 刚过 3MB → 对齐到 512KB（7 块）
    assert r["s6"] == 512 * KB, r                   # 6MB / 12 → 512KB（12 块）
    assert r["s12"] == MB, r                        # 12MB / 12 → 1MB（12 块）
    assert r["s1g"] == MB, r                        # 大文件撞 1MB 上限，不再放大
    assert r["capped"] == 512 * KB - 64 * KB, r     # 服务端只给 512KB → 扣 64KB 边距
    assert r["fallback"] == MB, r                   # 未下发 → 上限 1MB


class TestLinkAwareChunkCap:
    """分块上限按链路取值（协议 §9）：*.trycloudflare.com 一档，其余一律一档。

    判定只看 hostname——非 trycloudflare 一律按局域网处理（含局域网 IP、localhost、
    以及将来可能出现的自定义域名反代）。两档的随链路参数集中在 LINK_LAN / LINK_CF
    两份档案里，调值只改那一处。
    """

    def test_lan_page_uses_lan_cap(self, mobile_page):
        r = mobile_page.evaluate(
            "() => { const F = window._filePanel.constructor;"
            "        return {host: location.hostname, cf: F.isCloudflare, max: F.CHUNK_MAX,"
            "                lan: F.LINK_LAN.chunkMax, cfMax: F.LINK_CF.chunkMax}; }"
        )
        assert r["cf"] is False, r
        assert r["max"] == r["lan"] == 1024 * 1024, r

    def test_cloudflare_page_uses_cf_cap(self, cloudflare_page):
        r = cloudflare_page.evaluate(
            "() => { const F = window._filePanel.constructor;"
            "        return {host: location.hostname, cf: F.isCloudflare, max: F.CHUNK_MAX,"
            "                lan: F.LINK_LAN.chunkMax, cfMax: F.LINK_CF.chunkMax}; }"
        )
        assert r["cf"] is True, r
        assert r["max"] == r["cfMax"] == 256 * 1024, r

    def test_cloudflare_chunk_size_is_always_256kb(self, cloudflare_page):
        """CF 档 min == max == 256KB，clamp 恒取 256KB，与文件大小及服务端下发值无关。"""
        r = cloudflare_page.evaluate(
            "() => { const F = window._filePanel.constructor; const MB = 1024 * 1024;"
            "        F.setServerMaxFrame(16 * 1024 * 1024);"
            "        return {tiny: F.pickChunkSize(1024), small: F.pickChunkSize(512 * 1024),"
            "                mid: F.pickChunkSize(10 * MB), big: F.pickChunkSize(500 * MB)}; }"
        )
        assert set(r.values()) == {256 * 1024}, r


class TestLinkAwareTimeouts:
    """超时也按链路取值（协议 §9）：CF 上行窄、排队深，实测一块 256KB 连回执要 8~10s。

    拿局域网的短超时去等它，正常等待会被误判成超时——白白发探活、白白把滑动窗口降级；
    反过来把 CF 的宽超时套在局域网上，则「PC 真卡死了」这种异常迟迟发现不了。

    断言的是**关系**与**来源**，不是具体数字：数字是留给人的调参旋钮，改它不该挂测试；
    但「CF 档必须更宽」和「getter 必须真的从档案取值」是设计约束，改坏了必须挂。
    """

    # 派生 getter ← 档案字段
    FIELDS = {
        "CHUNK_ACK_TIMEOUT": "chunkAck",
        "CANCEL_ACK_TIMEOUT": "cancelAck",
        "HELLO_TIMEOUT": "hello",
    }

    # 「改档案里的值 ⇒ 派生 getter 跟着变」，两个档位各测一次。
    # 注意不能把 mobile_page 与 cloudflare_page 放进同一个用例：两者共用同一个 page
    # fixture 实例，后装的那个会把页面导航走。
    OVERRIDE = (
        "() => { const F = window._filePanel.constructor;"
        "        Object.defineProperty(F, 'LINK_LAN',"
        "          {value: Object.assign({}, F.LINK_LAN, {chunkAck: 11111})});"
        "        Object.defineProperty(F, 'LINK_CF',"
        "          {value: Object.assign({}, F.LINK_CF, {chunkAck: 22222})});"
        "        return F.CHUNK_ACK_TIMEOUT; }"
    )

    def _read(self, page):
        return page.evaluate(
            "() => { const F = window._filePanel.constructor; const used = {};"
            "        for (const k of ['CHUNK_ACK_TIMEOUT', 'CANCEL_ACK_TIMEOUT', 'HELLO_TIMEOUT']) {"
            "            used[k] = F[k]; }"
            "        return {isCF: F.isCloudflare, lan: F.LINK_LAN, cf: F.LINK_CF, used: used}; }"
        )

    def _expected(self, profile):
        return {k: profile[field] for k, field in self.FIELDS.items()}

    def test_lan_page_uses_lan_timeouts(self, mobile_page):
        r = self._read(mobile_page)
        assert r["isCF"] is False, r
        assert r["used"] == self._expected(r["lan"]), r

    def test_cloudflare_page_uses_cf_timeouts(self, cloudflare_page):
        r = self._read(cloudflare_page)
        assert r["isCF"] is True, r
        assert r["used"] == self._expected(r["cf"]), r

    def test_cf_timeouts_are_wider_than_lan(self, cloudflare_page):
        r = self._read(cloudflare_page)
        for key, field in self.FIELDS.items():
            assert r["cf"][field] > r["lan"][field], (key, r["cf"][field], r["lan"][field])
        # 实测（2026-09-18 真机，CF + 256KB 块）：取消帧要 8~10s 才被服务端读到。
        # 超时若落在这个量级以内，正常等待就会被误判成超时 ⇒ 必须留出余量。
        assert r["cf"]["cancelAck"] > 10_000, r["cf"]
        assert r["cf"]["chunkAck"] > 10_000, r["cf"]

    def test_derived_getter_follows_profile_lan(self, mobile_page):
        assert mobile_page.evaluate(self.OVERRIDE) == 11111

    def test_derived_getter_follows_profile_cf(self, cloudflare_page):
        assert cloudflare_page.evaluate(self.OVERRIDE) == 22222


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


class TestSlidingWindow:
    """滑动窗口闸门：在途未确认块数到达 MAX_INFLIGHT 就停住不发（协议 §9）。

    动机：`bufferedAmount` 只覆盖「浏览器 → 网络栈」，数据一旦离开浏览器（手机内核、
    无线在途、CF 回源、PC 内核、写盘）全是本端盲区 ⇒ 浏览器侧能一路「发得出去」而链路
    早已积压，表现为「bufferedAmount ≈ 0 却传得极慢、取消后很久才停」。等对端 ack 才是
    真正端到端的节流：在途量被钉死在 MAX_INFLIGHT 块以内（=1 即停等，>1 即滑动窗口）。

    ⚠️ 断言一律从页面读 `MAX_INFLIGHT`（`_window`），**不写死 1 或 2**。它是 `FilePanel`
    上的可调常量，写死了下次再调窗口就会重演「实现改了、用例没跟上」——本类正是从
    「停等（=1）」演进过来的。
    """

    FAKE = ("{name: 'sw.bin', size: 3 * 1024 * 1024,"
            " slice: (a, b) => new Blob([new Uint8Array(b - a)])}")
    ID = 1                      # mobile_page 上首次传输的 id（_start 里自增得到）
    CHUNK = 256 * 1024          # 3MB / 12 块（远多于一个窗口，才看得出闸门在起作用）

    def _start_async(self, page):
        page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.FAKE}, 'file'); }}"
        )

    def _window(self, page):
        """当前滑动窗口大小 = 在途未确认块数上限，从页面读而不写死。"""
        return page.evaluate("() => window._filePanel.constructor.MAX_INFLIGHT")

    def _data_count(self, page):
        return page.evaluate(
            "() => window.__mockWS.sentMessages.filter(m => m.a === 'data').length")

    def _wait_data_count(self, page, count, timeout=30000):
        """等已发出的 data 帧恰好 `count` 条。"""
        page.wait_for_function(
            "() => window.__mockWS.sentMessages.filter(m => m.a === 'data').length"
            f" === {count}",
            timeout=timeout,
        )

    def _ack(self, page, n):
        """模拟 PC 对第 n 块的逐块 ack（累计 received 取前 n+1 块）。"""
        page.evaluate(
            "() => window.__mockWS.triggerMessage("
            f"{{type:'ack', ref:'file', id:{self.ID}, a:'data', n:{n},"
            f" received:{(n + 1) * self.CHUNK}}})"
        )

    def test_no_ack_fills_only_the_window(self, mobile_page):
        """核心断言：一个 ack 都不回时，只发得出「一个窗口」的块，然后停在闸门上。"""
        page = mobile_page
        window = self._window(page)
        self._start_async(page)
        self._wait_data_count(page, window)
        page.wait_for_timeout(400)      # 若无闸门，这段时间够把 12 块全推出去
        assert self._data_count(page) == window
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.a === 'end')") is False

    def test_one_ack_releases_exactly_one_chunk(self, mobile_page):
        """放行一块 ack 只再多发一块 —— 严格「在途 ≤ MAX_INFLIGHT 块」。"""
        page = mobile_page
        window = self._window(page)
        self._start_async(page)
        self._wait_data_count(page, window)

        # 块 0 被 ack ⇒ 恰好解锁第 `window` 块（后者的闸门条件是 ack(n − MAX_INFLIGHT) = ack(0)）
        self._ack(page, 0)
        self._wait_data_count(page, window + 1)
        page.wait_for_timeout(200)      # 再等等：不该冒出第 window+1 块（块 1 还没 ack）
        assert self._data_count(page) == window + 1

    def test_disconnect_while_waiting_for_chunk_ack_finishes(self, mobile_page):
        """等块 ack 期间断连：ACK 不会再来，必须收尾，不能卡死在闸门上。"""
        page = mobile_page
        window = self._window(page)
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")   # 掐掉重连
        self._start_async(page)
        self._wait_data_count(page, window)

        page.evaluate("window.__mockWS.triggerClose()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=3000)
        assert self._data_count(page) == window     # 没有继续把剩余块推出去
        assert page.evaluate("() => window._filePanel._gateGaveUp") is False  # 是断连，不是超时降级

    def test_ack_timeout_degrades_to_pipeline(self, mobile_page):
        """ack 通道失灵（超时）→ 退回流水线，而不是把传输拖成「1 块 / 超时」。"""
        page = mobile_page
        page.evaluate(
            "() => Object.defineProperty(window._filePanel.constructor,"
            " 'CHUNK_ACK_TIMEOUT', { value: 200 })"
        )
        self._start_async(page)
        page.wait_for_function(
            "() => window.__mockWS.sentMessages.some(m => m.a === 'end')")
        assert self._data_count(page) == 12
        assert page.evaluate("() => window._filePanel._gateGaveUp") is True


class TestStartFrameFailure:
    """`start` 帧发不出去必须**就地失败收尾** —— 它是唯一没有兜底的帧出口（协议 §9）。

    `_start` 开头那次 `getConnected()` 只是上一刻的快照，而那一刻还没有任何订阅能替
    `start` 兜底（`ack` / `WS_CLOSE` 的订阅都要等下一行 `_waitChunkAck` 才挂上）。若不管
    返回值：断连 + 重连的竞态下 data 会打到服务端已被 `abort_all()` 清掉的会话
    （`_file_receiver` 是**模块级单例**，会话按 `id` 全局路由），服务端回
    `error(malformed)`，用户看到一次**连 `.part` 都没建起来**的无理由失败。
    """

    FAKE = ("{name: 'sf.bin', size: 3 * 1024 * 1024,"
            " slice: (a, b) => new Blob([new Uint8Array(b - a)])}")
    FAILED = MOBILE_I18N["bubble_file_failed"].replace("{name}", "sf.bin")

    def _start_with_dead_start_frame(self, page):
        """只让 `start` 发不出去：模拟「就在这几行之间连接断了」。"""
        page.evaluate(
            "() => {"
            "  const tr = window._filePanel.transport;"
            "  const orig = tr.sendFrame;"
            "  tr.sendFrame = (f) => f.a === 'start' ? false : orig.call(tr, f);"
            "}"
        )
        page.evaluate(
            f"() => {{ window.__sendP = window._filePanel._start({self.FAKE}, 'file'); }}"
        )

    def test_no_data_is_sent_when_start_never_went_out(self, mobile_page):
        page = mobile_page
        self._start_with_dead_start_frame(page)
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=3000)

        # 核心：start 没上线 ⇒ 一块 data 都不许发（发了就是打到服务端不存在的会话上）
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.filter(m => m.a === 'data').length") == 0
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.a === 'end')") is False
        # 就地失败收尾：界面解锁 + 失败气泡（既不卡在 sending，也不谎报成功）
        assert page.evaluate("() => window._filePanel._file") is None
        assert page.evaluate(
            "() => document.body.classList.contains('file-transferring')") is False
        bubbles = page.evaluate(
            "() => Array.from(document.querySelectorAll('.message')).map(m => m.textContent)")
        assert self.FAILED in bubbles
        assert not _any_bubble(bubbles, "bubble_file_done", name="sf.bin")


class TestEndAck:
    """「发送成功」必须由 PC 的 end ack 判定——等不到就如实报「未确认」，绝不猜成功。

    这是「手机显示发送成功、PC 还要 30s+ 才打完『文件接收完成』」那个现象的根治点：
    数据离开浏览器 ≠ PC 已落盘，所以进度条封顶 99%，气泡也只认 end ack。
    """

    FAKE = ("{name: 'x.bin', size: 3 * 1024 * 1024,"
            " slice: (a, b) => new Blob([new Uint8Array(b - a)])}")

    def _send_async(self, page, auto_ack=True):
        """起一次发送但不 await：返回时数据已全部交出，正卡在等 end ack。

        auto_ack=True 让 mock 对每个 data 块回逐块 ack —— 窗口闸门生效后，
        没有 ack 就填不满窗口、走不到 end。
        """
        page.evaluate(f"() => window.__mockWS.autoAck({str(auto_ack).lower()})")
        page.evaluate(
            "() => {"
            "  const panel = window._filePanel;"
            f"  window.__sendP = panel._start({self.FAKE}, 'file');"
            "}"
        )
        page.wait_for_function("() => window.__mockWS.sentMessages.some(m => m.a === 'end')")

    def _bubbles(self, page):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('.message')).map(m => m.textContent)")

    def test_success_requires_end_ack(self, mobile_page):
        page = mobile_page
        self._send_async(page)

        # 数据已全部交出、块 ack 也全部回来了（窗口保证在途 ≤ MAX_INFLIGHT 块），但 end ack 未到
        # ⇒ 封顶 99% + 文案「等待电脑确认」，**绝不放行 100% 与成功气泡**。
        assert page.evaluate("() => window._filePanel._shownPct") == 99
        assert page.evaluate("() => window._filePanel._endConfirmed") is False
        assert "等待电脑确认" in page.evaluate(
            "() => document.querySelector('#view-file .fp-pct').textContent")

        page.evaluate(
            "() => window.__mockWS.triggerMessage("
            "{type:'ack', ref:'file', id:1, a:'end', received: 3*1024*1024})")
        page.wait_for_function("() => window._filePanel._state === 'idle'")

        assert page.evaluate("() => window._filePanel._shownPct") == 100
        assert _any_bubble(self._bubbles(page), "bubble_file_done", name="x.bin")

    def test_local_estimate_cannot_outrun_ack_by_more_than_one_chunk(self, mobile_page):
        """真机实测回归：10.1MB 的 jpg，手机已全部交出，PC 只 ack 了 2MB。

        旧算法 `max(本地估算, ack)` 会让本地估算（≈100%）胜出 → 假 99%。
        新算法 `ack + min(在途量, 1 块)` = 2MB + 1MB = 31%，与 PC 实际进度同量级。
        """
        page = mobile_page
        page.evaluate(
            "() => {"
            "  const p = window._filePanel;"
            "  const size = 10102935;"                     # 真机那份 MVIMG_*.jpg
            "  p._file = { name: 'x.jpg', size };"
            "  p._chunkSize = 1024 * 1024;"
            "  p._sentBytes = size; p._ackedBytes = 2 * 1024 * 1024;"
            "  p._shownPct = 0; p._endConfirmed = false;"
            "  p._progressTick();"
            "}"
        )
        assert page.evaluate("() => window._filePanel._shownPct") == 31

    def test_progress_caps_at_99_until_end_ack(self, mobile_page):
        """ack 推进到只剩尾巴时仍封顶 99%：100% 只由 end ack 解锁。"""
        page = mobile_page
        page.evaluate(
            "() => {"
            "  const p = window._filePanel;"
            "  const size = 10102935;"
            "  p._file = { name: 'x.jpg', size };"
            "  p._chunkSize = 1024 * 1024;"
            "  p._sentBytes = size; p._ackedBytes = 9 * 1024 * 1024;"
            "  p._shownPct = 0; p._endConfirmed = false;"
            "  p._progressTick();"
            "}"
        )
        assert page.evaluate("() => window._filePanel._shownPct") == 99

    def test_chunk_ack_moves_progress_without_end_ack(self, mobile_page):
        """逐块 ack 让进度条反映「PC 已收到」——本地估算看不见那一段。"""
        page = mobile_page
        self._send_async(page)
        page.evaluate(
            "() => {"
            "  const p = window._filePanel;"
            "  p._sentBytes = 0; p._ackedBytes = 0; p._shownPct = 0;"
            "  p._endConfirmed = false;"
            "  p._chunkSize = 1024 * 1024;"          # 前瞻上限固定成 1 块，断言只看语义
            "  window.__prog = [p._shownPct];"
            "  p._progressTick();            window.__prog.push(p._shownPct);"   # 无任何信号 → 0
            "  p._ackedBytes = 2 * 1024 * 1024; p._progressTick();"
            "  window.__prog.push(p._shownPct);"                                 # PC 已收 2/3 → 67
            "  p._sentBytes = 2 * 1024 * 1024 + 512 * 1024; p._progressTick();"
            "  window.__prog.push(p._shownPct);"                                 # 在途 0.5MB → 前瞻 0.5MB → 83
            "  p._sentBytes = 3 * 1024 * 1024; p._progressTick();"
            "  window.__prog.push(p._shownPct);"                                 # 在途 1MB → 前瞻封顶 1 块 → 99
            "  p._sentBytes = 0; p._ackedBytes = 0; p._progressTick();"
            "  window.__prog.push(p._shownPct);"                                 # 信号消失 → 不倒退
            "}"
        )
        assert page.evaluate("() => window.__prog") == [0, 0, 67, 83, 99, 99]

    def test_end_ack_timeout_reports_unknown_not_success(self, mobile_page):
        """保险超时只报「未确认」：既不谎报成功，也不谎报失败。"""
        page = mobile_page
        page.evaluate(
            "() => Object.defineProperty(window._filePanel.constructor,"
            " 'END_ACK_TIMEOUT', { value: 200 })"
        )
        self._send_async(page)
        page.wait_for_function("() => window._filePanel._state === 'idle'")

        bubbles = self._bubbles(page)
        assert MOBILE_I18N["bubble_file_unknown"].replace("{name}", "x.bin") in bubbles
        assert not _any_bubble(bubbles, "bubble_file_done", name="x.bin")

    def test_disconnect_while_awaiting_ack_reports_failure(self, mobile_page):
        """等 ack 期间连接断了：ACK 不会再来，立刻报失败，不让用户干等。"""
        page = mobile_page
        self._send_async(page)
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")   # 掐掉重连
        page.evaluate("window.__mockWS.triggerClose()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)

        bubbles = self._bubbles(page)
        assert MOBILE_I18N["bubble_file_failed"].replace("{name}", "x.bin") in bubbles
        assert not _any_bubble(bubbles, "bubble_file_done", name="x.bin")


class TestCancelAck:
    """取消也有回执：收到服务端 `ack(a:"cancel")` 才解锁界面（协议 §9）。

    动机：本地停发只说明「我不再发了」，服务端那边可能还在收队列里的残留块、
    还在写 `.part`；此前手机上一点取消就立刻解锁并弹「已取消」，与服务端的真实
    状态无关。回执超时后退一步发 `hello` 探活——同一有序通道，回执能回来就说明
    排在它前面的 `cancel` 帧已被服务端读出。两条路都超时就地放行，绝不干等。
    """

    FAKE = ("{name: 'cx.bin', size: 3 * 1024 * 1024,"
            " slice: (a, b) => new Blob([new Uint8Array(b - a)])}")
    ID = 1                      # mobile_page 上首次传输的 id（_start 里自增得到）
    CANCELED = MOBILE_I18N["bubble_file_canceled"].replace("{name}", "cx.bin")

    def _start_async(self, page, auto_ack, until="data"):
        """起一次发送。until="data" 停在「第 0 块已发出、闸门未放行」处；
        until="end" 一路发到 end（需 auto_ack=True，否则填不满窗口、走不到 end）。"""
        page.evaluate(f"() => window.__mockWS.autoAck({str(auto_ack).lower()})")
        page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.FAKE}, 'file'); }}"
        )
        if until == "end":
            page.wait_for_function(
                "() => window.__mockWS.sentMessages.some(m => m.a === 'end')")
        else:
            # ⚠️ 这里**不能**写 `=== 1`。auto_ack=True 时 mock 会逐块回 ack，闸门一路放行、
            # 块数一路涨，「恰好 1 块」只是个极短的瞬态：轮询撞上才过，撞不上就 30s 超时
            # （同一条用例、同一份实现，上一轮过这一轮挂，就是这么来的）。
            # 这一步要的只是「发送已经开始」，所以用 `>= 1`。
            page.wait_for_function(
                "() => window.__mockWS.sentMessages.filter(m => m.a === 'data').length >= 1")

    def _locked(self, page):
        return page.evaluate(
            "() => document.body.classList.contains('file-transferring')")

    def _sent(self, page):
        return page.evaluate("() => window.__mockWS.sentMessages")

    def _bubbles(self, page):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('.message')).map(m => m.textContent)")

    def _cancel_ack(self, page):
        page.evaluate(
            "() => window.__mockWS.triggerMessage("
            f"{{type:'ack', ref:'file', id:{self.ID}, a:'cancel'}})")

    def test_ui_stays_locked_until_cancel_ack(self, mobile_page):
        """点取消后界面**仍锁定**；回执到达才解锁 + 落「已取消」气泡。"""
        page = mobile_page
        self._start_async(page, auto_ack=False)     # 先不给回执，专看「不放行」
        page.evaluate("() => window._filePanel._cancel()")

        page.wait_for_function("() => window._filePanel._state === 'canceling'")
        page.wait_for_timeout(150)
        assert self._locked(page) is True, "回执未到就不该解锁界面"
        assert page.evaluate("() => window._filePanel._cancelRequested") is True
        assert any(m.get("a") == "cancel" for m in self._sent(page)), \
            "取消帧必须已发出，否则服务端不知道要停"

        self._cancel_ack(page)
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        assert self._locked(page) is False
        bubbles = self._bubbles(page)
        assert self.CANCELED in bubbles
        # 取消不是失败：_cancel 摘掉 _file 后，_start 的 _finish 不该再补一条失败气泡
        assert MOBILE_I18N["bubble_file_failed"].replace("{name}", "cx.bin") not in bubbles

    def test_cancel_ack_timeout_probes_with_hello(self, mobile_page):
        """回执超时 → 发 hello 探活；收到回执即判定取消已送达并解锁。"""
        page = mobile_page
        _set_wait_timeouts(page, cancel_ms=100, hello_ms=5000)
        self._start_async(page, auto_ack=False)
        page.evaluate("() => window._filePanel._cancel()")

        page.wait_for_function(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'hello')", timeout=3000)
        # 设计前提：cancel 必须排在 hello **之前** —— 同一有序通道，能读到后者
        # 才说明先读到了前者，探活回执才有证明力
        kinds = [(m.get("type"), m.get("a")) for m in self._sent(page)]
        assert kinds.index(("file", "cancel")) < kinds.index(("hello", None))
        assert self._locked(page) is True, "探活回执还没回来，仍不该解锁"

        token = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'hello').t")
        page.evaluate(f"() => window.__mockWS.triggerMessage({{type:'hello', t:{token}}})")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        assert self._locked(page) is False
        assert self.CANCELED in self._bubbles(page)

    def test_stale_probe_ack_does_not_unlock_a_later_cancel(self, mobile_page):
        """上一次取消留下的探活回执，不能满足**这一次**的等待。

        取消会在同一条连接上反复发生。探活号取自本次传输的 `id`，所以每一次的
        号都不同；若它退化成「每次从 1 重数」的计数器，旧回执就会冒充本次的，
        解锁一个其实还没被服务端确认的取消 —— 而旧回执证明的只是**上一次**的
        `cancel` 已被读出。
        """
        page = mobile_page
        # 第一次：探活也超时，快速放行，留下一个已经作废的号
        _set_wait_timeouts(page, cancel_ms=100, hello_ms=100)
        self._start_async(page, auto_ack=False)
        page.evaluate("() => window._filePanel._cancel()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=3000)
        stale = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'hello').t")

        # 第二次：探活窗口留宽，让「冒充」这一步有充裕的操作时间。
        # 不借 _start_async —— 它等的是「data 帧总数 == 1」，只对首次传输成立。
        _set_wait_timeouts(page, cancel_ms=100, hello_ms=5000)
        page.evaluate("() => window.__mockWS.autoAck(false)")
        page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.FAKE}, 'file'); }}"
        )
        page.wait_for_function(
            "() => window.__mockWS.sentMessages.filter(m => m.type === 'file'"
            "        && m.a === 'start').length === 2", timeout=3000)
        second_id = page.evaluate(
            "() => window.__mockWS.sentMessages.filter(m => m.type === 'file'"
            "        && m.a === 'start').pop().id")
        page.evaluate("() => window._filePanel._cancel()")
        page.wait_for_function(
            "() => window.__mockWS.sentMessages.filter(m => m.type === 'hello').length === 2",
            timeout=3000)
        fresh = page.evaluate(
            "() => window.__mockWS.sentMessages.filter(m => m.type === 'hello').pop().t")
        assert second_id != self.ID, "两次传输各有各的 id"
        assert stale != fresh, "探活号必须跨次不重复（号源 = 本次传输的 id）"

        # 拿上一次的号冒充 → 不算数
        page.evaluate(f"() => window.__mockWS.triggerMessage({{type:'hello', t:{stale}}})")
        page.wait_for_timeout(150)
        assert page.evaluate("() => window._filePanel._state") == "canceling", \
            "旧号的回执不该结束本次等待"
        assert self._locked(page) is True, "本次取消仍未确认，界面不该解锁"

        # 本次的号 → 正常放行
        page.evaluate(f"() => window.__mockWS.triggerMessage({{type:'hello', t:{fresh}}})")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        assert self._locked(page) is False

    def test_cancel_hard_timeout_unlocks_anyway(self, mobile_page):
        """两条路都超时：就地放行界面，绝不把用户永久锁在「正在取消」。"""
        page = mobile_page
        _set_wait_timeouts(page, cancel_ms=100, hello_ms=100)
        self._start_async(page, auto_ack=False)
        page.evaluate("() => window._filePanel._cancel()")

        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=3000)
        assert self._locked(page) is False
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'hello')") is True
        assert self.CANCELED in self._bubbles(page)

    def test_healthy_server_ack_needs_no_probe(self, mobile_page):
        """正常 PC：回执立刻回来，不浪费一次探活。"""
        page = mobile_page
        self._start_async(page, auto_ack=True)
        page.evaluate("() => window._filePanel._cancel()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)

        assert self._locked(page) is False
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'hello')") is False
        assert self.CANCELED in self._bubbles(page)

    def test_cancel_while_awaiting_end_ack_does_not_double_finish(self, mobile_page):
        """数据已全交出、正等 end ack 时点取消：收尾只发生一次（不重复解锁/不重复气泡）。"""
        page = mobile_page
        self._start_async(page, auto_ack=True, until="end")
        page.evaluate("() => window._filePanel._cancel()")

        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        bubbles = self._bubbles(page)
        assert [b for b in bubbles if b == self.CANCELED] == [self.CANCELED], \
            "「已取消」气泡只该出现一次"
        assert not _any_bubble(bubbles, "bubble_file_done", name="cx.bin")
        assert self._locked(page) is False


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


class TestSignalBus:
    """WSClient.on 必须是一对多（mobile.html 1.3 节 SignalHub）。

    旧实现是 ``messageHandlers.set(type, handler)`` 的**单槽 Map**：后注册者**静默顶掉**
    前者，且一条日志都没有。FilePanel 常驻订阅 ack/error，在「等取消回执」那段时间里
    又会额外订阅 ack/hello（见 ``_awaitCancelAck``）—— 同一 type 上并存两个订阅者正是
    现在的常态，旧写法会让先注册的那个永远收不到帧。这几条用例把它钉死：谁改回单槽，
    这里立刻红。

    用例里的 ref 一律用 ``'probe'``：FilePanel 的 ack handler 按 ``ref !== _kind`` 过滤，
    探针帧会被它忽略，不会污染传输状态。
    """

    PROBE = "{type:'ack', ref:'probe', id:0, a:'end'}"

    def _slots(self, page, kind="ack"):
        """某 type 当前的槽数（FilePanel 构造时已注册 ack / error）。"""
        return page.evaluate(
            "() => { const s = window.__wsClient.hub.map.get('" + kind + "');"
            "        return s ? s.slots.length : 0; }"
        )

    def test_two_subscribers_both_receive(self, mobile_page):
        """同一 type 的两个订阅者都要收到 —— 旧实现只有后注册的那个收得到。"""
        hits = mobile_page.evaluate(
            "() => {"
            "  const hits = [];"
            "  const off1 = window.__wsClient.on('ack', () => hits.push('a'));"
            "  const off2 = window.__wsClient.on('ack', () => hits.push('b'));"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  off1(); off2();"
            "  return hits.join(',');"
            "}"
        )
        assert hits == "a,b", hits

    def test_new_subscriber_does_not_evict_existing(self, mobile_page):
        """注册新订阅者后槽数应当**增加**，而不是被覆盖后仍是 1。"""
        before = self._slots(mobile_page)          # FilePanel 自己的订阅
        assert before >= 1, "FilePanel 构造时的订阅不该消失"
        after = mobile_page.evaluate(
            "() => {"
            "  const off = window.__wsClient.on('ack', () => {});"
            "  const n = window.__wsClient.hub.map.get('ack').slots.length;"
            "  off();"
            "  return n;"
            "}"
        )
        assert after == before + 1, (before, after)
        assert self._slots(mobile_page) == before, "注销后必须回到原样"

    def test_unsubscribe_is_idempotent(self, mobile_page):
        """connect 的返回值就是注销函数，重复调用是安全的空操作。"""
        r = mobile_page.evaluate(
            "() => {"
            "  let n = 0;"
            "  const off = window.__wsClient.on('ack', () => n++);"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  const before = n;"
            "  off(); off();"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  return {before: before, after: n};"
            "}"
        )
        assert r == {"before": 1, "after": 1}, r

    def test_once_fires_only_once(self, mobile_page):
        n = mobile_page.evaluate(
            "() => {"
            "  let n = 0;"
            "  window.__wsClient.once('ack', () => n++);"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  return n;"
            "}"
        )
        assert n == 1, n

    def test_throwing_subscriber_does_not_break_others(self, mobile_page):
        """异常隔离：一个订阅者抛异常，排在它后面的订阅者照常收到。"""
        r = mobile_page.evaluate(
            "() => {"
            "  const hits = [];"
            "  const off1 = window.__wsClient.on('ack', () => { throw new Error('boom'); });"
            "  const off2 = window.__wsClient.on('ack', () => hits.push('after'));"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  off1(); off2();"
            "  return hits.join(',');"
            "}"
        )
        assert r == "after", r

    def test_abort_signal_auto_unsubscribes(self, mobile_page):
        """opts.signal：一个 AbortController 就是 Qt 的那个 Connection 句柄。"""
        r = mobile_page.evaluate(
            "() => {"
            "  let n = 0;"
            "  const ac = new AbortController();"
            "  window.__wsClient.on('ack', () => n++, { signal: ac.signal });"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  const before = n;"
            "  ac.abort();"
            "  window.__mockWS.triggerMessage(" + self.PROBE + ");"
            "  return {before: before, after: n};"
            "}"
        )
        assert r == {"before": 1, "after": 1}, r

    def test_config_manager_supports_two_subscribers(self, mobile_page):
        """ConfigManager 也换成 SignalHub：多订阅者 + 可注销（旧写法没有 off）。"""
        r = mobile_page.evaluate(
            "() => {"
            "  const cfg = window.chatManagerInstance.config;"
            "  const hits = [];"
            "  const off1 = cfg.on('maxHistory', (v) => hits.push('a' + v));"
            "  const off2 = cfg.on('maxHistory', (v) => hits.push('b' + v));"
            "  cfg.set('maxHistory', 77);"
            "  off1(); off2();"
            "  return hits.join(',');"
            "}"
        )
        assert r == "a77,b77", r


class TestLocalizedWaits:
    """等待局部化 / 信号化后新增的保证（mobile.html §6.5）。

    这批用例守的不是「功能还能用」（那些老用例已经守着了），而是这次重构**换来的性质**：
    订阅先于发帧、订阅用完即退、断连通知不被静默宽限期吞掉 —— 三条都极易在后续改动里
    悄悄退化，且退化了功能「看起来还是对的」，只是偶尔慢/偶尔漏。
    """

    FAKE = ("{name: 'lw.bin', size: 3 * 1024 * 1024,"
            " slice: (a, b) => new Blob([new Uint8Array(b - a)])}")
    ID = 1

    SLOTS = (
        "() => { const m = window.__wsClient.hub.map;"
        "  const n = (t) => (m.get(t) ? m.get(t).slots.length : 0);"
        "  return {ack: n('ack'), hello: n('hello')}; }"
    )

    def _start_at_gate(self, page, auto_ack=False):
        """起一次发送，停在「窗口已填满、闸门正等着 ack」处。

        等的必须是**窗口填满**（= `MAX_INFLIGHT` 块）而不是「≥ 1 块」：只有块数涨到窗口
        上限，发送循环才会走到 `_waitChunkAck` 并挂上闸门的那个 `ack` 订阅 —— 下面用例断
        言的 `ack` 槽数（常驻 1 + 闸门 1）依赖的正是它。撞在窗口没填满的瞬态上会数错。
        """
        page.evaluate(f"() => window.__mockWS.autoAck({str(auto_ack).lower()})")
        page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.FAKE}, 'file'); }}"
        )
        page.wait_for_function(
            "() => window.__mockWS.sentMessages.filter(m => m.a === 'data').length"
            " === window._filePanel.constructor.MAX_INFLIGHT")

    def _cancel_ack(self, page):
        page.evaluate(
            "() => window.__mockWS.triggerMessage("
            f"{{type:'ack', ref:'file', id:{self.ID}, a:'cancel'}})")

    def test_cancel_subscription_is_live_before_the_frame_goes_out(self, mobile_page):
        """**先挂订阅、再发 cancel 帧**：回执同步回来也不能漏（漏了界面就白锁到超时）。

        这条能红的前提正是「订阅先于发帧」：把 ``_cancel`` 里的两行调换顺序，回执会在
        还没有任何订阅者的时候到达并丢掉，界面只能等探活超时才解锁。
        """
        page = mobile_page
        self._start_at_gate(page)
        page.evaluate(
            "() => {"
            "  const panel = window._filePanel;"
            "  panel.transport.sendFrame = (f) => {"
            "    if (f.a === 'cancel') {"
            "      window.__mockWS.triggerMessage("
            "        {type: 'ack', ref: f.type, id: f.id, a: 'cancel'});"
            "    }"
            "    return true;"
            "  };"
            "}"
        )
        page.evaluate("() => window._filePanel._cancel()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'hello')") is False, \
            "回执已同步回来，不该再走 hello 探活那条路"

    def test_cancel_wait_subscriptions_are_released(self, mobile_page):
        """取消等待的订阅只活它那一段，等完必须回到常驻数（否则每次取消漏一份）。

        等待分两段：先等 `ack(a:"cancel")`；超时才挂 `hello` 的订阅。后者是**就地挂在
        探活发帧之前**的（`_awaitCancelAck::probe`），所以那一步之前 `hello` 槽数为 0
        ——这正是「探活发出去没有」不必另记标志位的原因：订阅存在本身即等价。
        """
        page = mobile_page
        _set_wait_timeouts(page, cancel_ms=200, hello_ms=5000)
        self._start_at_gate(page)
        before = page.evaluate(self.SLOTS)
        assert before == {"ack": 2, "hello": 0}, before   # 常驻 1 + 窗口闸门 1

        page.evaluate("() => window._filePanel._cancel()")
        during = page.evaluate(self.SLOTS)
        # 闸门的订阅随 _tx.abort() 退掉，取消等待挂上 ack；探活尚未发出 ⇒ hello 仍为 0
        assert during == {"ack": 2, "hello": 0}, during

        page.wait_for_function(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'hello')",
            timeout=2000)
        probing = page.evaluate(self.SLOTS)
        assert probing == {"ack": 2, "hello": 1}, probing   # 探活已发出 ⇒ 订阅就位

        self._cancel_ack(page)
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        after = page.evaluate(self.SLOTS)
        assert after == {"ack": 1, "hello": 0}, after

    def test_probe_subscription_is_live_before_the_probe_goes_out(self, mobile_page):
        """探活订阅同样**就地挂在发帧之前**：回执同步回来也不能漏。

        把 `probe()` 里「挂订阅」与「发帧」两行调换，回执会在还没有订阅者时到达并丢掉，
        等待只能走到硬超时 —— 下面把 hello 时限设成 5000ms，而只等 2000ms，正是为了让
        这种退化表现为超时失败，而不是「慢一点但还是过」。
        """
        page = mobile_page
        _set_wait_timeouts(page, cancel_ms=100, hello_ms=5000)
        self._start_at_gate(page)
        page.evaluate(
            "() => {"
            "  const panel = window._filePanel;"
            "  panel.transport.sendFrame = (f) => {"
            "    if (f.type === 'hello') {"
            "      window.__mockWS.triggerMessage({type: 'hello', t: f.t});"
            "    }"
            "    return true;"
            "  };"
            "}"
        )
        page.evaluate("() => window._filePanel._cancel()")
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=2000)
        assert page.evaluate("() => window._filePanel._state") == "idle"

    def test_close_signal_beats_the_silent_grace_window(self, mobile_page):
        """WS_CLOSE **不受静默宽限期影响**：等 ack 的传输必须立刻收尾。

        宽限期（_startGrace）只决定「界面表不表态」，它是给「唤起文件选择器顺带被掐了
        socket、回前台 1s 内自愈」那种抖动用的；但「这条连接确实断了」是个事实，正在等
        ack 的传输必须马上据此收尾，不能陪它耗完 3s 宽限。若有人把断连检测改挂到
        WS_STATUS 上（那条会被宽限期吞掉），这条立刻红。
        """
        page = mobile_page
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")   # 掐掉重连
        page.evaluate("() => window.__wsClient.beginPickerContext()")       # 之后的断连都进静默窗口
        self._start_at_gate(page)
        page.evaluate("window.__mockWS.triggerClose()")
        # 宽限期是 3000ms：这里 1500ms 内收尾，就说明没在等它
        page.wait_for_function("() => window._filePanel._state === 'idle'", timeout=1500)
        assert page.evaluate("() => window._filePanel._gateGaveUp") is False, \
            "是断连，不该被当成 ack 通道失灵"

    def test_connection_status_travels_through_the_hub(self, mobile_page):
        """连接状态从 hub 走（WS_STATUS），不再是 document 上的 'ws-status' CustomEvent。"""
        page = mobile_page
        page.evaluate(
            "() => { window.__st = [];"
            "  window.__wsClient.on('_status', (st) => window.__st.push(st.connected)); }"
        )
        page.evaluate("window.__mockWS.triggerClose()")
        page.wait_for_function("() => window.__st.includes(false)", timeout=2000)
        # 断连会排上 1s 退避重连（connect 没被掐），连上后应当再收一个 true
        page.wait_for_function("() => window.__st.includes(true)", timeout=5000)
