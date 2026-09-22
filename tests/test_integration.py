"""
集成测试：Python SecureChannel ↔ JS SecureClient 端到端加密通信

使用 Playwright + Mock WebSocket，Python 端用 SecureChannel 处理 auth 握手和加解密。
参考 test_mobile.py 的模式：内联加载外部脚本，set_content 加载页面，patch 注入密钥。

与 test_js_crypto.py 的区别：
- test_js_crypto 测试 Provider 层（直接调用 encrypt/decrypt）
- 本测试测试协议层（SecureChannel.wrap/unwrap ↔ SecureClient.encrypt/decrypt + WSClient 消息收发）

架构前提（docs/e2ee-always-on-design.md）：
- 加密永远开启，不存在明文模式；`none` 算法与 `PlainProvider` 已删除，
  因此也没有「连上即就绪」的路径——`needs_auth` / `is_encrypted` 恒为 True。
- `SecureChannel(auth_method=..., mode=...)`：url_fragment（扫码）或 tofu（手动审批）。
  本文件默认覆盖 url_fragment 认证与 TOFU 首次连接；CF 模式强制 url_fragment
  （tunnel/mode.effective_auth_method）。

关键设计：
- set_content 加载 HTML（与 test_mobile.py 一致）
- patch _parseUrlFragment 注入认证方式 / PC 公钥 / a= 算法列表
  （set_content 装载的是 about:blank 不透明源文档，location.hash 与 localStorage
  均不可用，只能直接给出这些参数；TOFU 路径另用内存 localStorage 替身）
- 不 patch _selectAlgorithm — 让真实的协商选择逻辑运行
- Mock WebSocket 不自动回帧 — Python 手动完成握手（auth_challenge / auth_proof）
- 参数化测试 xsalsa20 和 xchacha20 两种算法（通过调整 a= 列表顺序让客户端分别选中）
"""

import re
from pathlib import Path
import json

import pytest

from phonemic.tunnel.crypto import OFFERED_ALGORITHMS
from phonemic.tunnel.e2ee import SecureChannel
from phonemic.tunnel.frame import encode as frame_encode
from phonemic.tunnel.mode import TunnelMode, effective_auth_method

pytest.importorskip("playwright")

RES_DIR = Path(__file__).parent.parent / "phonemic" / "resources"

# 手机端语言包由 /api/lang.json 提供，而 set_content 下无法真实 fetch。
# 直接把 zh_CN 的 mobile 段注入 window.i18n，等价于服务端返回的内容。
MOBILE_I18N = json.loads(
    (RES_DIR / "locales" / "zh_CN.json").read_text(encoding="utf-8")
)["mobile"]

MOCK_WS_SCRIPT = """
window.__mockWS = {
    sentMessages: [],
    current: null,
    // bin 字段（Uint8Array）无法跨 Playwright 边界传输，统一转成 int 数组
    toPlain: function(msg) {
        const out = {};
        for (const k in msg) {
            out[k] = (msg[k] instanceof Uint8Array) ? Array.from(msg[k]) : msg[k];
        }
        return out;
    },
    triggerMessage: function(byteList) {
        if (this.current && this.current.onmessage) {
            this.current.onmessage({ data: new Uint8Array(byteList) });
        }
    },
    // code 可省略；传 4001 即模拟「服务端在密钥建立前拒绝握手」
    triggerClose: function(code, reason) {
        if (this.current) {
            if (this.current.onclose) {
                this.current.onclose({ code: code, reason: reason || '', wasClean: true });
            }
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
            var msg = MessagePack.decode(data);
            // 明文帧（可解码为对象）→ dict 存入；密文帧 → 保存原始字节供 Python 解密
            if (msg && typeof msg === 'object') {
                window.__mockWS.sentMessages.push(window.__mockWS.toPlain(msg));
            } else {
                window.__mockWS.sentMessages.push({ raw: Array.from(data) });
            }
        } catch(e) {
            window.__mockWS.sentMessages.push({ raw: Array.from(data) });
        }
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


def _prepare_html(channel, offered, tofu_first=False):
    """读取 mobile.html，内联外部脚本，注入 Mock WebSocket 和握手参数。

    offered: 服务端 a= 下发的算法优先级列表，客户端按序协商选择第一个自身支持的。
    tofu_first: True 模拟「URL 无 k= 且本地无已存公钥」→ TOFU 首次连接（明文 auth
        携带 algo/pk/pin，随后等待 PC 审批）；默认 False 模拟 url_fragment 认证
        （SealedBox 密封 auth）。

    `_parseUrlFragment` 整体替换：set_content 装载的是 about:blank 不透明源文档，
    真实实现里的 location.hash 与 localStorage 都不可用，只能直接给出认证方式、
    PC 公钥与算法列表。TOFU 路径还要读写 localStorage（存/清 PC 公钥），故另注入
    内存替身——仅补上文档上下文缺的浏览器能力，不放宽任何协议断言。
    """
    html = (RES_DIR / "mobile.html").read_text(encoding="utf-8")
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
    # localStorage 替身必须在这里（早于主脚本）：about:blank 下读写 localStorage
    # 会抛 SecurityError，而 TOFU 路径要存/清 PC 公钥（e2ee-always-on-design §8.6）。
    storage_stub = (
        "<script>(function(){var m={};Object.defineProperty(window,'localStorage',"
        "{configurable:true,value:{getItem:function(k){return k in m?m[k]:null;},"
        "setItem:function(k,v){m[k]=String(v);},removeItem:function(k){delete m[k];}}});"
        "})();</script>"
    )
    boot = (
        "<script>window.__PHONEMIC_DEV__=true;</script>"
        + storage_stub
        + "<script>" + MOCK_WS_SCRIPT + "</script>"
    )
    html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + boot, html, count=1)
    # patch _parseUrlFragment：注入认证方式 / PC 公钥 / a= 算法列表
    offered_js = ",".join(f"'{a}'" for a in offered)
    patch = "<script>SecureClient.prototype._parseUrlFragment = function() {"
    if tofu_first:
        # 无 k=、无已存公钥 → TOFU 首次：只给算法列表，认证方式保持默认
        patch += "  this._authMode = 'tofu_first';"
    else:
        pc_pubkey_b64 = channel.get_public_key_b64()
        patch += f"  this._pcPublicKeyB64 = '{pc_pubkey_b64}';"
        patch += f"  this._pcPublicKeyRaw = this._fromB64('{pc_pubkey_b64}');"
        patch += "  this._authMode = 'url_fragment';"
    patch += f"  this._selectedAlgo = this._selectAlgorithm([{offered_js}]);"
    patch += "};</script>"
    html = html.replace("</body>", patch + "</body>", 1)

    # 暴露 wsClient 供 Python 端检查
    html = html.replace(
        "wsClient.connect();",
        "wsClient.connect(); window.__wsClient = wsClient;",
    )
    return html


def _as_frame(msg):
    """把 mock WS 捕获的明文帧还原为 Python 帧（bin 字段由 int 数组转回 bytes）。

    mock WS 的 toPlain 把 Uint8Array 转成 int 数组才能跨 Playwright 边界；msgpack
    里是 bin 的字段（url_fragment 的 auth.data、TOFU 首次的 auth.pk）都得转回 bytes，
    否则下游 receive_auth 会判为非法编码。
    """
    frame = dict(msg)
    for key in ("data", "pk"):
        if isinstance(frame.get(key), list):
            frame[key] = bytes(frame[key])
    return frame


def _decode_sent(channel, msg):
    """把 mock WS 捕获的一条 JS 上行帧还原为应用层 dict。

    密文帧（mock 存 {raw: int[]}）走会话解密；明文帧（已解码 dict）
    重新编码后由会话按明文还原。
    """
    if isinstance(msg, dict) and "raw" in msg:
        return channel.unwrap(bytes(msg["raw"]))
    return channel.unwrap(frame_encode(_as_frame(msg)))


def _wait_auth(page):
    """等待 JS 发送 auth 消息并返回。"""
    page.wait_for_function(
        "() => window.__mockWS && window.__mockWS.sentMessages.some(m => m.type === 'auth')"
    )
    msg = page.evaluate(
        "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
    )
    return _as_frame(msg)


def _answer_challenge_and_send_config(page, channel):
    """发 auth_challenge、校验 JS 回的 auth_proof，再下发首个 config。

    auth_challenge 由 ``make_auth_challenge()`` 直接产出**线上字节**
    （url_fragment 下已整帧加密），不能再经 ``channel.wrap`` 二次加密。
    客户端收到 config 后即 isConnected，这是**乐观接入**（与 TLS 1.3 客户端
    Finished 同形）；被拒由 error(code="auth") 或 close 4001 体现。
    """
    challenge_bytes = channel.make_auth_challenge()
    page.evaluate(
        "(msg) => window.__mockWS.triggerMessage(msg)",
        list(challenge_bytes),
    )
    # auth_proof 是加密帧 → mock 按「raw」保存（明文帧才会被解成 dict）
    page.wait_for_function(
        "() => window.__mockWS.sentMessages.some(m => m.raw)", timeout=3000
    )
    proof_raw = page.evaluate(
        "() => { const m = window.__mockWS.sentMessages.find(m => m.raw);"
        " return m ? m.raw : null; }"
    )
    assert proof_raw, "客户端未回 auth_proof"
    assert channel.verify_auth_proof(
        channel.unwrap(bytes(proof_raw))
    ) is True, "auth_proof 未能通过 nonce 校验"

    page.evaluate(
        "(msg) => window.__mockWS.triggerMessage(msg)",
        list(channel.wrap({"type": "config", "mobile_max_records": 20})),
    )
    page.wait_for_function(
        "() => window.__wsClient && window.__wsClient.isConnected"
    )


def _process_auth(page, channel):
    """跑完 url_fragment 握手，并下发首个 config——等价 api._handle_auth 的认证分支。

    auth(SealedBox) → receive_auth 交出 (algo, session_key) → create_provider →
    auth_challenge(Provider 加密) → auth_proof(Provider 加密) → config。

    注意 TOFU 首次连接不走这里（receive_auth 只返回 pin、不建密钥，须先过审批），
    见 `_process_tofu_first_auth`。
    """
    auth_msg = _wait_auth(page)
    algo, session_key, pin, phone_pk = channel.receive_auth(auth_msg)
    assert pin is None, "url_fragment 认证不应携带识别码（无审批环节）"
    assert session_key is not None, "SealedBox 解封后应拿到会话密钥"
    assert phone_pk is None
    channel.create_provider(algo, session_key)

    _answer_challenge_and_send_config(page, channel)


def _deliver_sealed_pin(page, channel):
    """TOFU 首次第 2 步：把密封下发的识别码帧交给 JS，等它大字显示出来。

    手机这一侧解封用的是**自己**的私钥；只有真机（持有 sk_手机）做得到，
    所以窃听者即便抄到整条明文链路也读不出这个码。解封后手机**不回任何帧**。
    """
    before = page.evaluate("() => window.__mockWS.sentMessages.length")
    page.evaluate(
        "(msg) => window.__mockWS.triggerMessage(msg)",
        list(channel.make_sealed_pin()),
    )
    page.wait_for_function(
        "() => document.getElementById('approval-pin').textContent.length > 0",
        timeout=2000,
    )
    shown = page.locator("#approval-pin").inner_text()
    assert shown == channel.pin, "手机显示的必须是 PC 指派的那一个识别码"
    after = page.evaluate("() => window.__mockWS.sentMessages.length")
    assert after == before, "收到识别码后手机不应回任何帧（无可复制、可重放的东西）"
    return shown


def _process_tofu_first_auth(page, channel):
    """跑完 TOFU 首次握手，并下发首个 config——等价 api._handle_auth 的 TOFU 分支。

    明文 auth(algo/pk) → receive_auth 只交出 (pin, phone_pk)，**不做 ECDH** →
    第 2 步把 PC 指派的识别码密封下发给这一方 → 审批放行 →
    complete_tofu_auth 建 Provider → SealedBox(phone_public) 挑战 →
    auth_proof(Provider 加密) → config。审批通过后手机把 PC 公钥存入 localStorage。
    """
    auth_msg = _wait_auth(page)
    algo, session_key, pin, phone_pk = channel.receive_auth(auth_msg)
    assert pin is not None, "TOFU 首次应由 PC 指派一个识别码"
    assert re.fullmatch(r"\d{4}", pin), f"识别码应为 4 位数字: {pin}"
    assert session_key is None, "审批前不应完成 ECDH（零计算开销）"
    assert phone_pk is not None

    _deliver_sealed_pin(page, channel)          # 用户先在手机屏幕上看到这个码
    channel.complete_tofu_auth(algo, phone_pk)  # 核对一致 → 点「允许」

    _answer_challenge_and_send_config(page, channel)


@pytest.fixture(params=["xsalsa20", "xchacha20"])
def secure_pair(page, request):
    """参数化 fixture：Python SecureChannel + JS SecureClient（已认证）。

    服务端下发完整优先级列表；为覆盖两种算法，将测试算法置于列表首位，
    验证客户端按序协商后回传选择。
    """
    algo = request.param
    pc = SecureChannel(auth_method="url_fragment")
    channel = pc.new_session()
    offered = [algo] + [a for a in OFFERED_ALGORITHMS if a != algo]
    html = _prepare_html(pc, offered)
    page.set_content(html)
    _process_auth(page, channel)
    yield page, channel, algo


# ---------- 握手测试 ----------

class TestHandshake:
    def test_auth_success(self, secure_pair):
        """握手成功：Python 已认证，JS 已连接。

        `is_rejected` / `reject_reason` 随本次架构变更一并删除（握手失败现在直接
        以 CryptoError 抛出、由 api 归为 close 4001，没有「记录拒绝原因」的会话字段），
        故这里改为断言新语义下恒真的握手前提。
        """
        page, channel, algo = secure_pair
        assert channel.is_authenticated
        assert channel.needs_auth is True
        assert channel.is_encrypted is True
        assert page.evaluate("() => window.__wsClient.isConnected") is True

    def test_algorithm_selected_correctly(self, secure_pair):
        """JS 选择的算法与指定的优先算法一致。

        algo 密封在 auth.data 内（线上不明文），通过服务端解封后的
        协商结果验证；线上仅能确认 auth 帧不带明文 algo。
        """
        page, channel, algo = secure_pair
        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert "algo" not in auth_msg  # 线上不泄露算法
        assert channel.negotiated_algorithm == algo

    def test_auth_proof_completes_handshake(self, secure_pair):
        """auth_challenge 被正确处理、auth_proof 已回，SecureClient 已认证。"""
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
        send_msgs = [_decode_sent(channel, m) for m in sent]
        send_msgs = [m for m in send_msgs if m and m.get("type") == "send"]
        assert len(send_msgs) >= 1
        assert send_msgs[-1]["text"] == "hello from JS"

    def test_preview_message(self, secure_pair):
        """JS → Python：输入触发 preview，Python 解密验证。"""
        page, channel, algo = secure_pair
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("preview text")

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        frames = [f for f in (_decode_sent(channel, m) for m in sent) if f]
        decrypted = frames[-1]
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
        send_msgs = [_decode_sent(channel, m) for m in sent]
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
            list(wrapped),
        )
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 10

    def test_config_update_twice(self, secure_pair):
        """Python → JS：连续发送两次 config 更新。"""
        page, channel, algo = secure_pair

        wrapped1 = channel.wrap({"type": "config", "mobile_max_records": 5})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", list(wrapped1))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 5

        wrapped2 = channel.wrap({"type": "config", "mobile_max_records": 20})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", list(wrapped2))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 20

    def test_receive_send_no_error(self, secure_pair):
        """Python → JS：接收 send 回声不影响连接。"""
        page, channel, algo = secure_pair
        wrapped = channel.wrap({"type": "send", "text": "echo from Python"})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", list(wrapped))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.__wsClient.isConnected") is True


# ---------- 双向通信 ----------

class TestRoundtrip:
    def test_bidirectional_communication(self, secure_pair):
        """双向通信：Python → JS → Python → JS。"""
        page, channel, algo = secure_pair

        # Python → JS: config
        wrapped = channel.wrap({"type": "config", "mobile_max_records": 5})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", list(wrapped))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 5

        # JS → Python: send
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("roundtrip")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [_decode_sent(channel, m) for m in sent]
        send_msgs = [m for m in send_msgs if m and m.get("type") == "send"]
        assert send_msgs[-1]["text"] == "roundtrip"

        # 验证消息显示在聊天列表
        msgs = page.locator(".message")
        assert msgs.count() >= 1
        assert "roundtrip" in msgs.last.text_content()

        # Python → JS: 再次更新 config
        wrapped2 = channel.wrap({"type": "config", "mobile_max_records": 15})
        page.evaluate("(msg) => window.__mockWS.triggerMessage(msg)", list(wrapped2))
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 15


# ---------- 算法拒绝（用例已移除，覆盖位置见下方说明） ----------
#
# 原 TestAlgorithmRejection.test_none_rejected 依赖 `algorithm="none"` 明文路径：
# 手机端选 'none'、服务端以「token 不匹配」拒绝。该路径随架构变更整体删除
# （docs/e2ee-always-on-design.md §9：`none` 算法、PlainProvider、token 认证路径全部移除），
# `none` 不再是可协商算法，也不是合法的 Provider——这条用例的语义已不存在，故删除。
#
# 等价的真实覆盖仍在：
#   - 「算法不在允许列表 → 服务端拒绝」：test_e2ee.py::test_receive_auth_unsupported_algo_raises
#   - 「认证前失败 → close 4001 → 界面提示重新扫码并停止重连」：
#     TestAuthFailureUX.test_close_4001_shows_warning_and_stops_reconnect


# ---------- 算法协商 ----------

class TestAlgorithmNegotiation:
    """a= 为服务端算法优先级列表，客户端按序选择第一个自身支持的。"""

    def test_client_prefers_first_offered(self, page):
        """客户端选择列表中首个支持的算法。"""
        pc = SecureChannel(auth_method="url_fragment")
        html = _prepare_html(pc, ["xchacha20", "xsalsa20"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")
        assert page.evaluate("() => window.__wsClient.secure._selectedAlgo") == "xchacha20"

    def test_client_falls_back_along_list(self, page):
        """列表首位不支持时按序回退到下一个支持的算法。"""
        pc = SecureChannel(auth_method="url_fragment")
        html = _prepare_html(pc, ["aes-256-gcm", "xchacha20", "xsalsa20"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")
        assert page.evaluate("() => window.__wsClient.secure._selectedAlgo") == "xchacha20"

    def test_no_common_algorithm_stops_connect(self, page):
        """无共同算法：不建立 WebSocket 连接，提示重新扫码。

        列表里刻意混入已删除的 'none'：它不再对应任何 Provider，必须和别的未知
        算法一样被跳过，而不能像旧明文模式那样被选中（`none` 与 PlainProvider
        已随本次架构变更移除，见 docs/e2ee-always-on-design.md §9）。
        """
        pc = SecureChannel(auth_method="url_fragment")
        html = _prepare_html(pc, ["none", "aes-256-gcm", "aegis256"])
        page.set_content(html)
        page.wait_for_function("() => window.__wsClient && window.__wsClient.secure._ready")

        assert page.evaluate("() => window.__wsClient.secure._selectedAlgo") is None
        assert page.evaluate("() => window.__wsClient.secure.algoUnsupported") is True
        # 未创建 WebSocket
        assert page.evaluate("() => window.__mockWS.current") is None
        text = page.locator("#status-bar").inner_text()
        assert "重新扫码" in text
        assert page.locator("#input-box").is_disabled()

    def test_server_offered_list_priority(self):
        """服务端 URL 下发完整优先级列表：xchacha20 优先于 xsalsa20。"""
        assert OFFERED_ALGORITHMS[0] == "xchacha20"
        pc = SecureChannel(auth_method="url_fragment")
        url = pc.append_to_url("https://x.trycloudflare.com")
        assert "a=xchacha20,xsalsa20" in url

    def test_auth_echoes_client_choice(self, secure_pair):
        """客户端协商出的算法密封在 auth.data 内，服务端解封后按其建 Provider。"""
        page, channel, algo = secure_pair
        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert "algo" not in auth_msg  # algo 在密封 blob 内，不明文回传
        assert channel.negotiated_algorithm == algo


# ---------- 断线处理 ----------

def _disconnect_without_recovery(page, timeout=2000):
    """断开连接、掐掉重连，等界面如实上报断线。

    非选择器路径的断连（这里是普通前台断连）不做静默，立刻显示状态栏。静默窗口只留给
    「唤起文件选择器造成的断连」，由 test_mobile.py::TestDisconnectRecovery 覆盖。
    """
    page.evaluate("() => { window.__wsClient.connect = () => {}; }")
    page.evaluate("() => window.__mockWS.triggerClose()")
    page.wait_for_function(
        "() => document.getElementById('status-bar').style.display === 'block'", timeout=timeout
    )


class TestDisconnect:
    def test_disconnect_updates_ui(self, secure_pair):
        """认证后断开（非选择器路径）：UI 立刻显示断线状态。"""
        page, channel, algo = secure_pair
        assert page.locator("#input-box").is_enabled()

        _disconnect_without_recovery(page)

        assert page.locator("#status-bar").is_visible()
        assert page.locator("#input-box").is_disabled()
        assert page.locator("#btn-send").is_disabled()
        assert page.evaluate("() => window.__wsClient.isConnected") is False


# ---------- TOFU 首次连接（LAN 模式 1） ----------
#
# 原「none+LAN 模式」整组用例（TestNoneLAN / none_lan_pair fixture）已删除：
# 它们断言 `not channel.needs_auth` / `not channel.is_encrypted`、JS 不发 auth、
# 明文收发——这些都是 `algorithm="none"` 的明文直连路径，随本次架构变更消失
# （docs/e2ee-always-on-design.md §7.3/§8.4：needs_auth 与 is_encrypted 恒为 True，
# 不存在「连上即就绪」）。恒真语义已在 test_e2ee.py::TestStateMachine 中断言，
# 明文收发则没有了对应物，故不再重复。
#
# 取而代之：LAN 模式现在只有两条真实路径——TOFU 首次（审批）与 TOFU 重连
# （等同 url_fragment）。后者由上面的 url_fragment 用例覆盖（帧格式完全相同，
# 见 design §5.3），这里补 TOFU 首次这条 JS ↔ Python 的集成路径。


class TestTofuFirst:
    """TOFU 首次连接：明文 auth（含识别码）→ PC 审批 → SealedBox 挑战。

    取代原先的 TestAuthFailureUX.test_missing_key_does_not_connect——那条用例
    断言「URL 缺 k= → 不建连接、提示重新扫码」，前提是「URL fragment 认证是唯一
    合法路径」。新架构下无 k= 正是 TOFU 模式（design §4），合法且必须建连等待审批，
    故按新语义重写为下面的用例。
    """

    def test_plaintext_auth_has_no_pin_and_pin_is_sealed_down(self, page):
        """无 k= → TOFU 首次：auth 明文 {algo, pk} 不带识别码，识别码由 PC 密封下发。

        识别码**不上明文链路**是这次改动的核心：同网段窃听者即便抄到整条 auth，
        也拿不到一个可以让电脑显示出来的码（design §5.5.1）。
        """
        pc = SecureChannel(auth_method="tofu", mode="lan")
        channel = pc.new_session()
        assert channel.needs_auth is True and channel.is_encrypted is True

        html = _prepare_html(pc, ["xsalsa20"], tofu_first=True)
        page.set_content(html)

        auth_msg = _wait_auth(page)
        assert auth_msg["type"] == "auth"
        assert "data" not in auth_msg, "TOFU 首次无 PC 公钥，无法密封 auth"
        assert "pin" not in auth_msg, "识别码不再由手机提供（见 design §5.5.1）"
        assert auth_msg["algo"] == "xsalsa20"
        assert isinstance(auth_msg["pk"], bytes) and len(auth_msg["pk"]) == 32, "手机公钥明文（32B）"
        # 服务端尚未下发：手机屏幕上还一个码都没有
        assert not page.locator("#approval-overlay").is_visible()
        assert page.locator("#approval-pin").inner_text() == ""
        # 尚未收到 auth_challenge（等审批）→ 未接入
        assert page.evaluate("() => window.__wsClient.isConnected") is False

        # 服务端收到明文 auth：只解析字段、不做 ECDH（审批前零密钥计算）
        algo, session_key, pin, phone_pk = channel.receive_auth(auth_msg)
        assert algo == "xsalsa20"
        assert session_key is None and phone_pk is not None
        assert re.fullmatch(r"\d{4}", pin), f"识别码应为 PC 指派的 4 位数字: {pin}"

        # 第 2 步：密封下发 → 手机屏幕显示同一个码（=_deliver_sealed_pin 的断言）
        shown = _deliver_sealed_pin(page, channel)
        assert shown == pin
        assert page.locator("#approval-overlay").is_visible()
        assert page.evaluate("() => window.__wsClient.isConnected") is False

    def test_full_handshake_and_encrypted_roundtrip(self, page):
        """审批放行后完成握手：SealedBox 挑战 → auth_proof → 加密双向通信。"""
        pc = SecureChannel(auth_method="tofu", mode="lan")
        channel = pc.new_session()
        html = _prepare_html(pc, ["xsalsa20"], tofu_first=True)
        page.set_content(html)

        _process_tofu_first_auth(page, channel)

        assert channel.is_authenticated
        assert page.evaluate("() => window.__wsClient.isConnected") is True
        # 审批浮层收起、识别码不再显示
        assert not page.locator("#approval-overlay").is_visible()

        # JS → Python：加密 send 能解密（两端独立 ECDH 得到同一会话密钥）
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("tofu hello")
        page.locator("#btn-send").click()
        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in (_decode_sent(channel, m) for m in sent)
                     if m and m.get("type") == "send"]
        assert send_msgs[-1]["text"] == "tofu hello"


# ---------- Cloudflare 模式（强制 url_fragment 认证） ----------
#
# 原「none+Cloudflare」两组用例（TestNoneCloudflare / TestNoneCloudflareRejection）
# 基于 `algorithm="none"` 的 token 认证 + 明文 msgpack，已随架构变更删除：CF 模式
# 不再有 token 明文回传路径，认证与加密都走 url_fragment（SealedBox）。
# 「凭据不对 → 服务端拒绝」的等价覆盖见
# test_e2ee.py::test_receive_auth_wrong_key_raises；
# 「认证前失败 → close 4001 → 界面提示重新扫码」见
# TestAuthFailureUX.test_close_4001_shows_warning_and_stops_reconnect。

@pytest.fixture
def cf_pair(page):
    """Cloudflare 模式：auth_method 被 effective_auth_method 强制为 url_fragment。

    按真实装配方式取值（PhoneMic.py 即这样构造 SecureChannel）：CF 公网可达，
    TOFU 首次连接无信任锚（攻击者可抢先连上骗取审批），故 CF 下用户的 auth_method
    一律归一为 url_fragment（docs/e2ee-always-on-design.md §2）。
    """
    auth_method = effective_auth_method("tofu", TunnelMode.CLOUDFLARE)
    assert auth_method == "url_fragment"
    pc = SecureChannel(auth_method=auth_method, mode="cloudflare")
    assert pc.secret_path, "url_fragment 模式应生成 secret_path 作为端点门禁"

    channel = pc.new_session()
    html = _prepare_html(pc, list(OFFERED_ALGORITHMS))
    page.set_content(html)
    _process_auth(page, channel)
    yield page, channel, pc


class TestCloudflareUrlFragment:
    def test_handshake_is_sealed_not_plaintext_token(self, cf_pair):
        """CF 模式：auth 是 SealedBox 密文（bin），线上无明文 algo / token。"""
        page, channel, pc = cf_pair
        assert channel.needs_auth is True
        assert channel.is_encrypted is True
        assert channel.is_authenticated
        assert page.evaluate("() => window.__wsClient.isConnected") is True

        auth_msg = page.evaluate(
            "() => window.__mockWS.sentMessages.find(m => m.type === 'auth')"
        )
        assert "algo" not in auth_msg, "算法密封在密文内，不明文传输"
        assert "token" not in auth_msg, "token 认证路径已删除"
        assert isinstance(auth_msg["data"], list), "auth.data 应为 bin（SealedBox 密文）"
        assert channel.negotiated_algorithm in OFFERED_ALGORITHMS

    def test_encrypted_roundtrip(self, cf_pair):
        """CF 模式：双向通信均为整帧加密（Python 解密 JS 帧，JS 解密 Python 帧）。"""
        page, channel, pc = cf_pair

        # Python → JS：加密 config，JS 解密后应用
        page.evaluate(
            "(m) => window.__mockWS.triggerMessage(m)",
            list(channel.wrap({"type": "config", "mobile_max_records": 8})),
        )
        page.wait_for_timeout(50)
        assert page.evaluate("() => window.chatManagerInstance.maxHistory") == 8

        # JS → Python：加密 send，Python 解密得到原文
        page.locator("#mode-toggle").click()
        page.locator("#input-box").fill("cf roundtrip")
        page.locator("#btn-send").click()

        sent = page.evaluate("() => window.__mockWS.sentMessages")
        send_msgs = [m for m in (_decode_sent(channel, m) for m in sent)
                     if m and m.get("type") == "send"]
        assert send_msgs[-1]["text"] == "cf roundtrip"


# ---------- 认证失败：用户提示与重连策略 ----------

class TestAuthFailureUX:
    """认证失败后：明确提示用户、停止无意义重连。"""

    def test_close_4001_shows_warning_and_stops_reconnect(self, page):
        """过期二维码（PC 密钥已变更）：服务端在密钥建立前 close 4001 → 提示重新扫码。"""
        pc = SecureChannel(auth_method="url_fragment")
        html = _prepare_html(pc, ["xsalsa20"])
        page.set_content(html)
        _wait_auth(page)

        # 解封失败（二维码过期 / 密钥不匹配）：服务端不发明文拒绝帧，只关连接
        page.evaluate("() => window.__mockWS.triggerClose(4001)")

        page.wait_for_function("() => window.__wsClient.authRejected === true", timeout=2000)
        # 状态栏提示重新扫码，输入框禁用
        text = page.locator("#status-bar").inner_text()
        assert "重新扫码" in text
        assert page.locator("#input-box").is_disabled()
        # 不再安排重连
        assert page.evaluate("() => window.__wsClient.reconnectTimer") is None

    def test_rejected_proof_shows_warning(self, page):
        """auth_proof 校验失败：服务端回加密的 error(code="auth") → 同样停止重连。

        这是「认证后失败」那条路径——密钥已在手，所以拒绝是加密帧而非 close 码。
        """
        pc = SecureChannel(auth_method="url_fragment")
        channel = pc.new_session()
        html = _prepare_html(pc, ["xsalsa20"])
        page.set_content(html)

        auth_msg = _wait_auth(page)
        algo, session_key, pin, phone_pk = channel.receive_auth(auth_msg)
        assert pin is None and session_key is not None
        channel.create_provider(algo, session_key)

        page.evaluate(
            "(msg) => window.__mockWS.triggerMessage(msg)",
            list(channel.wrap({"type": "error", "code": "auth"})),
        )

        page.wait_for_function("() => window.__wsClient.authRejected === true", timeout=2000)
        assert page.locator("#input-box").is_disabled()
        assert page.evaluate("() => window.__wsClient.reconnectTimer") is None

    # test_missing_key_does_not_connect 已移除：它断言「URL 无 k= → 不建连接、提示
    # 重新扫码」，前提是 url_fragment 为唯一合法路径。新架构下无 k= 即 TOFU 模式
    # （docs/e2ee-always-on-design.md §4），必须建连并等待审批，故重写为
    # TestTofuFirst.test_plaintext_auth_carries_pin_not_sealed。

    def test_reconnect_fixed_interval(self, secure_pair):
        """断连重连保持固定间隔：保证后台切回前台时快速重连。"""
        page, channel, algo = secure_pair
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 1000

        # 多次断连后间隔仍保持 2000，不递增
        page.evaluate("() => window.__mockWS.triggerClose()")
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 1000
        page.evaluate("() => window.__mockWS.triggerClose()")
        assert page.evaluate("() => window.__wsClient.reconnectInterval") == 1000
        assert page.evaluate("() => window.__wsClient.reconnectTimer") is not None
