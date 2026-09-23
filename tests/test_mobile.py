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

分片上传（docs/http-upload-design.md）另装一套 HTTP 替身（``window.__mockXHR``）：
WS 上只剩 ``upload_begin`` / ``upload_ready`` / ``upload_error`` / ``upload_cancel``
四个控制帧，数据面全部是 ``PUT /api/upload/<sid>``，所以「到底传了什么」要看 XHR
替身的记录。
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
    // `upload_begin` 的应答方式（控制面只剩这一段需要「一个正常工作的 PC」）：
    //   'ok'    —— 回 `upload_ready`（片大小 + 两把钥匙）
    //   'error' —— 回 `upload_error`（code 取 readyCode）
    //   'none'  —— 什么都不回（测「等 ready」的保险超时）
    // readyDelay 用来把应答推后，好让用例抢在 ready 之前做动作（取消 / 断连）；
    // 取 -1 表示**同步**回帧，用来钉住「订阅先于发帧」。
    readyMode: 'ok',
    readyCode: 'too_large',
    readyDelay: 0,
    readySid: 'sid-1',
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

    resetUpload: function() {
        this.readyMode = 'ok';
        this.readyCode = 'too_large';
        this.readyDelay = 0;
        this.readySid = 'sid-1';
    },

    /** 发一帧 `upload_ready`：片大小与两把钥匙都从 HTTP 替身里读（同一份真源） */
    _sendReady: function(id) {
        var q = window.__mockXHR;
        this.triggerMessage({
            type: 'upload_ready', id: id, sid: this.readySid,
            chunk: q.chunk, saved: 'pc/up.bin', expires: 300,
            k_mac: q.wireMac(), k_body: q.wireBody() });
    },

    /** 推一帧 `upload_progress`：②阶段片内进度的**唯一来源**（§5.13） */
    pushProgress: function(received, sid) {
        this.triggerMessage({
            type: 'upload_progress', sid: sid || this.readySid, received: received });
    },

    /**
     * 应答一次 `upload_begin`：先告诉 HTTP 替身「这次传多大、会话号是什么」，再按
     * readyMode 回控制帧。两把钥匙在这里现发（**每次上传一把新的**，与 WS 上那套
     * ECDH 会话密钥无关）—— 会话隔离正是靠这个。
     */
    answerUploadBegin: function(msg) {
        var mock = this;
        var q = window.__mockXHR;
        q.size = msg.size;
        q.sid = mock.readySid;
        q.ids[mock.readySid] = 0;        // 新会话的 received 从 0 起
        if (mock.readyMode === 'none') return;
        if (mock.readyMode === 'error') {
            var fail = function() {
                mock.triggerMessage({ type: 'upload_error', id: msg.id,
                                      code: mock.readyCode, msg: 'debug text' });
            };
            if (mock.readyDelay < 0) fail(); else setTimeout(fail, mock.readyDelay);
            return;
        }
        q.issue();
        if (mock.readyDelay < 0) mock._sendReady(msg.id);
        else setTimeout(function() { mock._sendReady(msg.id); }, mock.readyDelay);
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
        if (msg && msg.type === 'upload_begin') mock.answerUploadBegin(msg);
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


# 数据面（HTTP PUT）的替身。与 MOCK_WS_SCRIPT 分开：一个管控制面，一个管数据面。
# 用 raw 字符串：里面有正则的 ``\/`` 转义，普通字符串会触发无效转义警告。
MOCK_XHR_SCRIPT = r"""
// 分片上传的 HTTP 侧替身（docs/http-upload-design.md §5.2）：只记录**客户端发了什么**，
// 不模拟真实的验签 / 解密 —— 服务端的判定顺序由 tests/test_upload.py 覆盖，跨语言
// 一致性由 tests/test_js_crypto.py 覆盖。这里要证明的只有三件事：
//
//   * 每片的 URL / 三个头 / 密文长度都对（MAC 用同一把 k_mac 与同一套 uploadMac 复算）；
//   * `verify()` 拿 k_body 另建一个**全新**的 provider 逐片解回明文 —— 若客户端复用了
//     跑 WS 的那个实例，它的 seq 早被握手帧推进过，这里第一片就解不开；
//   * 进度只由「服务端确认的 received + 在飞那片的已发字节」驱动（谎报的 event.total
//     不该有任何影响）。
window.__mockXHR = {
    puts: [],            // {url, method, sid, offset, len, mac, macOk, body, bodyLen}
    ids: {},             // sid -> 已确认字节（服务端那份 received 的替身）
    size: 0,             // 声明总长：由 upload_begin 帧写入（服务端①阶段就知道）
    chunk: 128,          // upload_ready 下发的片大小。用例调小，便于构造多片
    sid: 'sid-1',
    kMac: null,          // 32 字节，替身自己复算签名用
    kBody: null,         // 32 字节，verify() 用
    keySize: 32,         // 下发的钥匙长度；≠32 时用例在验证「长度不对就作废会话」
    mode: 'ok',          // ok | fail | status | timeout | hang
    status: 0,           // mode==='status' 时回的状态码
    hangFrom: null,      // 第 N 片之后不再响应（1 起数），null = 全部响应
    failFrom: null,      // 第 N 片起改回 'status' + failStatus（1 起数）
    failStatus: 409,
    doneFlag: true,      // false = 永不带 done（测「未确认」）
    emitProgress: true,  // 替身照真 XHR 的规矩报 upload.onprogress；本设计**不消费**它
                         // （§5.13 进度改由服务端推）。留着是当**回归守卫**：用它造出
                         // 假的本地进度，验证进度条不受影响。false = 完全不报。
    lieProgress: false,  // true = 报 2^53 的 loaded / total（复刻 iOS 18 WebKit bug #277286）
    aborted: 0,          // 被 abort() 的次数
    last: null,          // 最近一个替身 XHR 实例

    reset: function() {
        this.puts = []; this.ids = {}; this.size = 0; this.chunk = 128;
        this.sid = 'sid-1'; this.kMac = null; this.kBody = null; this.keySize = 32;
        this.mode = 'ok'; this.status = 0; this.hangFrom = null;
        this.failFrom = null; this.failStatus = 409; this.doneFlag = true;
        this.emitProgress = true; this.lieProgress = false;
        this.aborted = 0; this.last = null;
    },

    /** 服务端发钥匙：确定性字节，用例要拿同一把复算签名 / 解密 */
    issue: function() {
        this.kMac = new Uint8Array(32);
        this.kBody = new Uint8Array(32);
        for (var i = 0; i < 32; i++) {
            this.kMac[i] = (i * 7 + 1) & 0xff;
            this.kBody[i] = (i * 11 + 3) & 0xff;
        }
    },
    /** 上线时真正发出去的两把钥匙（长度由 keySize 决定，默认正好合法） */
    wireMac: function() { return this.kMac ? this.kMac.slice(0, this.keySize) : null; },
    wireBody: function() { return this.kBody ? this.kBody.slice(0, this.keySize) : null; },

    /** 用 k_body 的**全新** provider 逐片解密（顺带验证「上传用独立的加密流」） */
    verify: function() {
        var key = this.wireBody();
        if (!key || key.length !== 32) return { error: 'no usable k_body' };
        var p = new PROVIDER_CLASSES[window.__wsClient.secure.algorithm]();
        p.injectSessionKey(key);
        var out = [];
        for (var i = 0; i < this.puts.length; i++) {
            try {
                var pt = p.decrypt(this.puts[i].body);
                out.push({ ok: true, len: pt.length, head: pt.length ? pt[0] : -1 });
            } catch (e) {
                out.push({ ok: false, error: String(e) });
            }
        }
        return { parts: out };
    },

    _respond: function(xhr, req) {
        var mock = this;
        var mode = mock.mode, status = mock.status;
        if (mock.failFrom !== null && mock.puts.length >= mock.failFrom) {
            mode = 'status'; status = mock.failStatus;
        }
        /* hang：永不响应 —— 留给「取消 / 断连那一刻请求还挂在半空」的用例 */
        if (mode === 'hang' || (mock.hangFrom !== null && mock.puts.length > mock.hangFrom)) return;
        if (mode === 'timeout') {
            setTimeout(function() { if (xhr.ontimeout) xhr.ontimeout(); }, 0);
            return;
        }
        if (mode === 'fail') {
            setTimeout(function() { if (xhr.onerror) xhr.onerror(); }, 0);
            return;
        }
        if (mode === 'status') {
            setTimeout(function() {
                xhr.status = status; xhr.responseText = '';
                if (xhr.onload) xhr.onload();
            }, 0);
            return;
        }
        var recv = (mock.ids[req.sid] || 0) + req.len;
        mock.ids[req.sid] = recv;
        var body = { received: recv };
        if (mock.doneFlag && recv >= mock.size) {
            body.done = true;
            body.saved = 'saved-' + req.sid;
        }
        setTimeout(function() {
            xhr.status = 200;
            xhr.responseText = JSON.stringify(body);
            if (xhr.onload) xhr.onload();
        }, 0);
    }
};

window.XMLHttpRequest = function() {
    var mock = window.__mockXHR;
    var self = this;
    this.readyState = 0;
    this.status = 0;
    this.responseText = '';
    this.timeout = 0;
    this.upload = {};          // 真 XHR 的 upload 对象：进度事件挂在它上面
    this._headers = {};
    this._aborted = false;

    this.open = function(method, url) {
        self._method = method; self._url = url; self.readyState = 1;
    };
    this.setRequestHeader = function(k, v) { self._headers[String(k).toLowerCase()] = v; };
    this.send = function(body) {
        self.readyState = 2;
        var m = /\/api\/upload\/([^\/?]+)/.exec(self._url || '');
        var req = {
            url: self._url, method: self._method, sid: m ? m[1] : '',
            offset: Number(self._headers['x-pm-offset']),
            len: Number(self._headers['x-pm-len']),
            mac: self._headers['x-pm-mac'],
            body: body, bodyLen: body ? body.length : 0,
        };
        req.macOk = mock.kMac
            ? (encodeMac(uploadMac(mock.kMac, req.sid, req.offset, req.len)) === req.mac)
            : null;
        mock.puts.push(req);
        mock.last = self;
        self.readyState = 3;
        if (mock.emitProgress && self.upload.onprogress) {
            var loaded = mock.lieProgress ? Math.pow(2, 53) : req.bodyLen;
            self.upload.onprogress({ loaded: loaded, total: loaded });
        }
        mock._respond(self, req);
    };
    this.abort = function() {
        if (self._aborted) return;
        self._aborted = true;
        mock.aborted += 1;
        if (self.onabort) self.onabort();
    };
};
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
        "<script>" + MOCK_XHR_SCRIPT + "</script>"
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


def _set_file_timeouts(page, begin_ms=None, put_ms=None) -> None:
    """覆写 FilePanel 的两个等待时限（都是派生 getter，见 `LINK` 档案）。

    ``configurable: true`` 不能省：首次 ``defineProperty`` 出来的描述符默认不可重定义，
    同一个用例里改第二次就会抛 TypeError。
    """
    js = ""
    if begin_ms is not None:
        js += ("  set('BEGIN_TIMEOUT', " + str(begin_ms) + ");")
    if put_ms is not None:
        js += ("  set('PUT_TIMEOUT', " + str(put_ms) + ");")
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


# ================================================ 分片上传（docs/http-upload-design.md）
#
# 数据面走 HTTP PUT，控制面才走 WS，所以这里有两套替身：
#   * ``window.__mockXHR`` —— 逐片 PUT 的落脚点：记录 URL / 三个头 / 密文长度，并能用
#     同一把 ``k_body`` 另建 provider 复算解密（``verify()``）；
#   * ``window.__mockWS``  —— 只管 ``upload_begin`` → ``upload_ready``（含两把钥匙）/
#     ``upload_error`` 这一段协商，外加 ``upload_cancel`` 的收帧。
# 真实服务端的判定顺序（验签 → offset → 解密）由 tests/test_upload.py 覆盖；这里只证明
# 客户端**发了什么、怎么收尾**。


def _upload_bubbles(page):
    """右侧结果气泡：文案 + 是否完成态 + 是否挂了重发引用（``_fileRef``）。"""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#chat-list .message'))"
        "  .filter(m => m.classList.contains('file-done')"
        "            || m.classList.contains('file-cancel'))"
        "  .map(m => ({text: m.textContent, done: m.classList.contains('file-done'),"
        "              retry: !!m._fileRef}))"
    )


def _bubble_texts(page):
    return [b["text"] for b in _upload_bubbles(page)]


def _puts(page):
    """已发出的分片（剔除 body 大对象，只留可断言的标量）。"""
    return page.evaluate(
        "() => window.__mockXHR.puts.map(p => ({"
        "  url: p.url, method: p.method, sid: p.sid, offset: p.offset,"
        "  len: p.len, mac: p.mac, macOk: p.macOk, bodyLen: p.bodyLen}))"
    )


def _control(page):
    """WS 上发出的上传控制帧（按发送顺序）。"""
    return page.evaluate(
        "() => window.__mockWS.sentMessages"
        "  .filter(m => m.type === 'upload_begin' || m.type === 'upload_cancel')"
    )


def _pct(page):
    """进度条上**显示过的**百分比（单调不减 ⇒ 也等于「曾经到过的最高值」）。"""
    return page.evaluate("() => window._filePanel._shownPct")


def _fill_width(page):
    return page.evaluate("() => document.querySelector('.fp-fill').style.width")


def _rate_text(page):
    """进度条下方那行速率文案（§5.13：最近 10 秒平均）。"""
    return page.evaluate("() => document.querySelector('.fp-rate').textContent")


# 完成气泡里拼的「· 全程平均速率」：值随时间变，只能验形状不能验定值
_RATE_SUFFIX = re.compile(r" · [0-9.]+ (?:B|KB|MB)/s$")


def _strip_rate(text):
    """剥掉完成气泡上附加的速率后缀，好让文案断言仍可比常量。"""
    return _RATE_SUFFIX.sub("", text)


class _Upload:
    """一次上传的公共动作：造文件、发起、配置替身、等收尾。"""

    ID = 1                    # mobile_page 上首次传输的 id（_start 里自增得到）
    NAME = "up.bin"
    SID = "sid-1"

    def __init__(self, page):
        self.page = page

    @staticmethod
    def fake(size, name):
        """骗得过 FilePanel 的「文件」：它只用到 name / size / slice()。

        内容填 7，好在解密后断言「确实解回了原明文」，而不是一片零。
        """
        return (f"{{name: {name!r}, size: {size},"
                " slice: (a, b) => new Blob([new Uint8Array(b - a).fill(7)])}")

    def setup(self, xhr=None, ws=None):
        """复位两套替身并按需覆写字段（键名与替身里的字段一一对应）。"""
        xq = "".join(f"q.{k} = {json.dumps(v)};" for k, v in (xhr or {}).items())
        self.page.evaluate("() => { const q = window.__mockXHR; q.reset();" + xq + " }")
        wq = "".join(f"m.{k} = {json.dumps(v)};" for k, v in (ws or {}).items())
        self.page.evaluate("() => { const m = window.__mockWS; m.resetUpload();" + wq + " }")

    def start(self, size, kind="file", name=None):
        """发起一次上传（不等收尾）。

        ``_start`` 到第一个 await 之前全是同步的，而页面已连上 ⇒ 返回时 ``_state``
        必定已是 'busy'，后面等 idle 不会误判成「还没开始」。
        """
        self.page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.fake(size, name or self.NAME)}, '{kind}'); }}"
        )

    def push_progress(self, received, sid=None):
        """从 PC 侧推一帧 ``upload_progress``（§5.13 片内进度的唯一来源）。"""
        self.page.evaluate(
            "([r, s]) => window.__mockWS.pushProgress(r, s)",
            [received, sid or self.SID])

    def wait_begin(self):
        self.page.wait_for_function(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'upload_begin')")

    def busy(self):
        return self.page.evaluate("() => window._filePanel._state") == "busy"

    def settled(self, timeout=4000):
        """等这一次上传收尾，返回收尾时的观测量。"""
        self.page.wait_for_function(
            "() => window._filePanel._state === 'idle'", timeout=timeout)
        return self.page.evaluate(
            "() => ({pct: window._filePanel._shownPct,"
            "        locked: document.body.classList.contains('file-transferring')})")


@pytest.fixture
def up(mobile_page):
    """分片上传的公共动作对象（每个用例一份干净的替身状态）。"""
    h = _Upload(mobile_page)
    h.setup()
    return h


class TestUploadNegotiation:
    """① 阶段：``upload_begin`` → ``upload_ready``（带两把钥匙）/ ``upload_error``。

    这一段全在 WS 上（帧小，既不撞 CF 的上行整形也不撞 256KiB 截断），HTTP 要等钥匙
    到手才开始。四条出路都要落一条明确的气泡，且**一片都不该发**。
    """

    def test_begin_frame_carries_kind_name_and_size(self, up):
        """``ref`` 是面板类型，服务端靠它选落点：'file' 落盘 / 'photo' 进剪贴板。"""
        up.start(256, kind="photo", name="pic.png")
        up.wait_begin()
        assert _control(up.page) == [
            {"type": "upload_begin", "id": _Upload.ID, "ref": "photo",
             "name": "pic.png", "size": 256}
        ]

    def test_error_frame_reports_reason_without_showing_debug_text(self, up):
        """``upload_error`` 只按 ``code`` 出文案：``msg`` 是给日志的，不该进气泡。"""
        up.setup(xhr={"chunk": 128}, ws={"readyMode": "error", "readyCode": "too_large"})
        up.start(256)
        up.settled()
        b = _upload_bubbles(up.page)
        assert len(b) == 1
        assert b[0]["text"] == MOBILE_I18N["bubble_file_too_large"].replace(
            "{name}", _Upload.NAME)
        assert "debug text" not in b[0]["text"]
        assert b[0]["retry"] is False, "必然被同样拒绝的原因不给重发入口"
        assert _puts(up.page) == []

    def test_bad_ref_maps_to_bad_args(self, up):
        """``bad_ref`` 与 ``bad_args`` 同为「参数不对」——用户能做的只有换文件重发。"""
        up.setup(ws={"readyMode": "error", "readyCode": "bad_ref"})
        up.start(256)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_bad_args"].replace("{name}", _Upload.NAME)]
        assert _puts(up.page) == []

    def test_ready_timeout_reports_unknown(self, up):
        """``upload_ready`` 一直不来：报「未确认」，既不猜成功也不猜失败 —— 帧可能只是
        在路上，服务端也可能已经建好会话开始等了。"""
        up.setup(xhr={"chunk": 128}, ws={"readyMode": "none"})
        _set_file_timeouts(up.page, begin_ms=300)
        up.start(256)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_unknown"].replace("{name}", _Upload.NAME)]
        assert _puts(up.page) == []

    def test_disconnect_while_waiting_ready_reports_offline(self, up):
        up.setup(xhr={"chunk": 128}, ws={"readyMode": "none"})
        up.start(256)
        up.wait_begin()
        up.page.evaluate("() => window.__mockWS.triggerClose()")
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_offline"].replace("{name}", _Upload.NAME)]
        assert _puts(up.page) == []

    def test_short_keys_abort_the_session(self, up):
        """帧里的钥匙不是 32 字节：会话已经建出来了，必须显式作废，不能留给 TTL。"""
        up.setup(xhr={"chunk": 128, "keySize": 16})
        up.start(256)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_failed"].replace("{name}", _Upload.NAME)]
        assert _puts(up.page) == []
        assert _control(up.page)[1] == {"type": "upload_cancel", "sid": _Upload.SID}

    def test_bad_chunk_size_aborts_the_session(self, up):
        """``upload_ready.chunk`` 不合法：后面每一片都会撞 400，不如就地作废。"""
        up.setup(xhr={"chunk": 0})
        up.start(256)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_failed"].replace("{name}", _Upload.NAME)]
        assert _puts(up.page) == []
        assert _control(up.page)[1] == {"type": "upload_cancel", "sid": _Upload.SID}


class TestChunkShape:
    """数据面：每片的 URL / 三个头 / 密文长度。分片大小由服务端下发，客户端不自算。"""

    def test_puts_go_to_root_absolute_upload_endpoint(self, up):
        """URL 必须是**根绝对路径**：相对路径在 ``/{secret}/`` 页面下会落到另一条分支。"""
        up.setup(xhr={"chunk": 128})
        up.start(300)
        up.settled()
        for p in _puts(up.page):
            assert p["url"] == f"/api/upload/{_Upload.SID}"
            assert p["method"] == "PUT"
            assert p["sid"] == _Upload.SID

    def test_offsets_and_lengths_cover_the_file_exactly(self, up):
        up.setup(xhr={"chunk": 128})
        up.start(300)
        up.settled()
        puts = _puts(up.page)
        assert [p["offset"] for p in puts] == [0, 128, 256]
        assert [p["len"] for p in puts] == [128, 128, 44]

    def test_ciphertext_is_plaintext_plus_forty_eight_bytes(self, up):
        """线上长度恒等于明文长度 + 48（片序号 8 + nonce 24 + tag 16，两算法一致）。"""
        up.setup(xhr={"chunk": 128})
        up.start(300)
        up.settled()
        for p in _puts(up.page):
            assert p["bodyLen"] == p["len"] + 48

    def test_mac_covers_sid_offset_and_length(self, up):
        """替身拿同一把 ``k_mac`` 复算过签名：三个头里改任何一个都对不上。"""
        up.setup(xhr={"chunk": 128})
        up.start(300)
        up.settled()
        puts = _puts(up.page)
        assert [p["macOk"] for p in puts] == [True, True, True]
        assert all(p["mac"] for p in puts), "X-Pm-Mac 不能是空的"

    def test_body_decrypts_with_a_fresh_provider_from_k_body(self, up):
        """**上传用独立的加密流**：若复用跑 WS 的那个 provider 实例，它的 seq 早被握手
        帧推进过，这里第一片就解不开。"""
        up.setup(xhr={"chunk": 128})
        up.start(300)
        up.settled()
        r = up.page.evaluate("() => window.__mockXHR.verify()")
        assert "error" not in r, r
        assert [p["ok"] for p in r["parts"]] == [True, True, True]
        assert [p["len"] for p in r["parts"]] == [128, 128, 44]
        assert [p["head"] for p in r["parts"]] == [7, 7, 7], "解回来的应是最初的明文"

    def test_empty_file_sends_exactly_one_empty_chunk(self, up):
        """空文件也要发一片 ``len=0``：服务端只在 ``commit_chunk`` 里判「收齐」，
        一片都不发它就永远等不到 ``0 == 0`` 那次比较。"""
        up.setup(xhr={"chunk": 128})
        up.start(0, name="empty.bin")
        up.settled()
        puts = _puts(up.page)
        assert len(puts) == 1
        assert (puts[0]["offset"], puts[0]["len"]) == (0, 0)
        assert puts[0]["bodyLen"] == 48
        assert puts[0]["macOk"] is True

    def test_chunk_size_comes_from_the_server(self, up):
        """片大小跟着 ``upload_ready`` 走：将来调参不用改手机页面。"""
        up.setup(xhr={"chunk": 64})
        up.start(200)
        up.settled()
        assert [p["len"] for p in _puts(up.page)] == [64, 64, 64, 8]

    def test_chunks_are_serial(self, up):
        """串行逐片：第 2 片必须等第 1 片的响应。片级并发会让片乱序到达，把服务端的
        「三行判定」扩成「乱序窗口 + 缺口补齐」。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => window.__mockXHR.puts.length === 2")
        up.page.wait_for_timeout(300)      # 给「并发实现」足够时间把第 3 片也发出去
        assert len(_puts(up.page)) == 2
        assert up.busy() is True


class TestUploadProgress:
    """进度只认服务端推来的 ``upload_progress`` 帧；100% 只由 done 解锁（§5.13）。

    ⚠️ 这一组里最该守住的是**「进度帧没有权力」**：它只喂进度条，绝不能碰状态机
    （帧会丢、会晚到），100% 与收尾永远归最后一片的 HTTP 响应。
    """

    def test_progress_comes_from_server_frames(self, up):
        """服务端推来 192/384 ⇒ 50%，填充宽度同步（片内进度不再靠本地估算）。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => window._filePanel._received === 128")
        up.push_progress(192)
        up.page.wait_for_function("() => window._filePanel._received === 192")
        assert _pct(up.page) == 50
        assert _fill_width(up.page) == "50%"

    def test_progress_ignores_other_session(self, up):
        """帧里的 ``sid`` 跟当前会话对不上 ⇒ 整帧丢掉（防上一次上传留下的迟到帧）。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => window._filePanel._received === 128")
        up.push_progress(192, sid="sid-other")
        up.page.wait_for_timeout(80)
        assert _pct(up.page) == 33          # 33 只来自第 1 片的 HTTP 响应

    def test_progress_is_monotonic(self, up):
        """进度帧与 HTTP 响应走两条独立通道：值更小的迟到帧不能让进度回退。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => window._filePanel._received === 128")
        up.push_progress(192)
        up.page.wait_for_function("() => window._filePanel._received === 192")
        up.push_progress(64)
        up.page.wait_for_timeout(80)
        assert _pct(up.page) == 50

    def test_progress_frame_cannot_unlock_hundred_percent(self, up):
        """红线：进度帧**没有权力** —— 把整份都推过来也到不了 100%（还没 done）。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1, "doneFlag": False})
        up.start(384)
        up.page.wait_for_function("() => !!window._filePanel._sid")
        up.push_progress(384)
        up.page.wait_for_function("() => window._filePanel._received === 384")
        assert _pct(up.page) == 99
        assert up.busy() is True

    def test_lying_onprogress_cannot_move_the_bar(self, up):
        """回归守卫：谁把本地估算加回来，这条立刻挂（§5.13）。

        替身复刻 iOS 18 WebKit bug #277286 的 2^53 量级 ``loaded``/``total``；进度条
        只该反映 HTTP 响应带来的 33%，本地那一路一个字都不许影响。
        """
        up.setup(xhr={"chunk": 128, "hangFrom": 1, "lieProgress": True})
        up.start(384)
        up.page.wait_for_function("() => window._filePanel._received === 128")
        assert _pct(up.page) == 33
        assert _fill_width(up.page) == "33%"

    def test_rate_is_the_average_over_the_last_ten_seconds(self, up):
        """速率 = 最近 10 秒的平均：直接喂两个样本，值按窗口算（不依赖真实耗时）。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => !!window._filePanel._sid")
        up.page.evaluate(
            "() => { const p = window._filePanel, n = performance.now();"
            "  p._rateSamples = [{t: n - 10000, r: 0}, {t: n, r: 1024 * 1024}];"
            "  p._renderProgress(); }"
        )
        assert _rate_text(up.page) == "102 KB/s"      # 1MB/10s ≈ 104.9 KB/s

    def test_rate_switches_to_megabytes(self, up):
        """≥1MB/s 改用 MB/s（两位小数）：跨数量级都要能一眼读出来。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => !!window._filePanel._sid")
        up.page.evaluate(
            "() => { const p = window._filePanel, n = performance.now();"
            "  p._rateSamples = [{t: n - 10000, r: 0}, {t: n, r: 20 * 1024 * 1024}];"
            "  p._renderProgress(); }"
        )
        assert _rate_text(up.page) == "2.00 MB/s"

    def test_rate_decays_to_zero_when_stalled(self, up):
        """卡住时窗口按真实时间往前滑 ⇒ 速率归零，而不是冻在旧值上。

        样本停在 5 秒前、且值不再变：窗口内两个样本等值 ⇒ 0 B/s。
        """
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => !!window._filePanel._sid")
        up.page.evaluate(
            "() => { const p = window._filePanel, n = performance.now();"
            "  p._rateSamples = [{t: n - 10000, r: 4096}, {t: n - 5000, r: 4096}];"
            "  p._renderProgress(); }"
        )
        assert _rate_text(up.page) == "0 B/s"

    def test_rate_timer_is_stopped_on_finish(self, up):
        """补采样计时器必须随收尾停下：留着就是每传一次漏一个 interval。"""
        up.setup(xhr={"chunk": 128})
        up.start(384)
        up.settled()
        assert up.page.evaluate("() => window._filePanel._rateTimer") is None

    def test_done_bubble_carries_the_overall_average_rate(self, up):
        """完成气泡带这次传送的**全程平均**（值随时间变 ⇒ 只验形状）。"""
        up.setup(xhr={"chunk": 128})
        up.start(384)
        up.settled()
        text = _bubble_texts(up.page)[0]
        assert _strip_rate(text) == MOBILE_I18N["bubble_file_done"].replace(
            "{name}", _Upload.NAME)
        assert _RATE_SUFFIX.search(text), text

    def test_progress_caps_at_99_without_done(self, up):
        """全片发完但服务端始终没回 done：进度封在 99%，气泡报「未确认」。"""
        up.setup(xhr={"chunk": 128, "doneFlag": False})
        up.start(384)
        up.settled()
        assert _pct(up.page) == 99
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_unknown"].replace("{name}", _Upload.NAME)]

    def test_done_unlocks_hundred_percent(self, up):
        """数据「已发出」不等于「PC 已落盘」：满格只能由 done 解锁。"""
        up.setup(xhr={"chunk": 128})
        up.start(384)
        up.settled()
        assert _pct(up.page) == 100
        assert _upload_bubbles(up.page)[0]["done"] is True


class TestUploadFailure:
    """分片被拒 / 网络错 / 断连：每条出路都要有明确文案，且别把服务端会话晾着。"""

    def test_409_reports_mismatch_and_aborts(self, up):
        """409 = 偏移不是「当前该写的那一段」。停下 + 作废会话，并且可重发。"""
        up.setup(xhr={"chunk": 128, "failFrom": 2, "failStatus": 409})
        up.start(384)
        up.settled()
        b = _upload_bubbles(up.page)
        assert len(b) == 1
        assert b[0]["text"] == MOBILE_I18N["bubble_file_mismatch"].replace(
            "{name}", _Upload.NAME)
        assert b[0]["retry"] is True
        assert len(_puts(up.page)) == 2, "第 2 片被拒后就该停手，不再往下发"
        assert _control(up.page)[-1] == {"type": "upload_cancel", "sid": _Upload.SID}

    def test_401_reports_session_gone(self, up):
        """401 = 验签没过（会话不存在 / 已过期）。用户能理解的只有「电脑上没有了」。"""
        up.setup(xhr={"chunk": 128, "failFrom": 1, "failStatus": 401})
        up.start(384)
        up.settled()
        b = _upload_bubbles(up.page)
        assert b[0]["text"] == MOBILE_I18N["bubble_file_expired"].replace(
            "{name}", _Upload.NAME)
        assert b[0]["retry"] is True
        assert len(_puts(up.page)) == 1

    def test_413_reports_too_large_without_retry(self, up):
        up.setup(xhr={"chunk": 128, "failFrom": 1, "failStatus": 413})
        up.start(384)
        up.settled()
        b = _upload_bubbles(up.page)
        assert b[0]["text"] == MOBILE_I18N["bubble_file_too_large"].replace(
            "{name}", _Upload.NAME)
        assert b[0]["retry"] is False

    def test_400_reports_failed(self, up):
        """400 = 头自相矛盾（本端算错）：归到泛化失败，而不是「地址不对」之类。"""
        up.setup(xhr={"chunk": 128, "failFrom": 1, "failStatus": 400})
        up.start(384)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_failed"].replace("{name}", _Upload.NAME)]

    def test_network_error_reports_failed(self, up):
        up.setup(xhr={"chunk": 128, "mode": "fail"})
        up.start(384)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_failed"].replace("{name}", _Upload.NAME)]

    def test_xhr_timeout_reports_failed(self, up):
        up.setup(xhr={"chunk": 128, "mode": "timeout"})
        up.start(384)
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_failed"].replace("{name}", _Upload.NAME)]

    def test_disconnect_mid_flight_reports_offline_and_aborts_the_put(self, up):
        """断连时在飞的那一片就地掐掉：服务端已把该连接名下的会话全部作废，等它只会撞 401。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 1})
        up.start(384)
        up.page.wait_for_function("() => window.__mockXHR.puts.length === 2")
        up.page.evaluate("() => window.__mockWS.triggerClose()")
        up.settled()
        assert _bubble_texts(up.page) == [
            MOBILE_I18N["bubble_file_offline"].replace("{name}", _Upload.NAME)]
        assert up.page.evaluate("() => window.__mockXHR.aborted") == 1
        assert all(f["type"] != "upload_cancel" for f in _control(up.page)), \
            "断连时服务端已经连带作废了，不必再补一帧"

    def test_success_writes_a_done_bubble(self, up):
        up.setup(xhr={"chunk": 128})
        up.start(384, name="report.pdf")
        up.settled()
        b = _upload_bubbles(up.page)
        assert len(b) == 1
        assert _strip_rate(b[0]["text"]) == MOBILE_I18N["bubble_file_done"].replace(
            "{name}", "report.pdf")
        assert _RATE_SUFFIX.search(b[0]["text"]), "完成气泡要带全程平均速率"
        assert b[0]["done"] is True
        assert b[0]["retry"] is False, "完成态不挂重发入口"

    def test_photo_result_uses_the_photo_wording(self, up):
        """photo 走剪贴板：文案键与 file 分开，且重发无意义。"""
        up.setup(xhr={"chunk": 128})
        up.start(384, kind="photo", name="shot.png")
        up.settled()
        b = _upload_bubbles(up.page)
        assert _strip_rate(b[0]["text"]) == MOBILE_I18N["bubble_photo_done"].replace(
            "{name}", "shot.png")
        assert _RATE_SUFFIX.search(b[0]["text"]), "photo 的气泡同样带速率"
        assert b[0]["retry"] is False

    def test_lock_is_released_and_keys_are_dropped_on_failure(self, up):
        """失败同样要解锁 + 摘掉钥匙：留着只会让「上一次的 k_body」有机会被复用。"""
        up.setup(xhr={"chunk": 128, "mode": "fail"})
        up.start(384)
        s = up.settled()
        assert s["locked"] is False
        state = up.page.evaluate(
            "() => ({p: window._filePanel._provider, k: window._filePanel._kMac,"
            "        s: window._filePanel._sid})")
        assert state == {"p": None, "k": None, "s": None}


class TestUploadCancel:
    """取消是单向的：一帧 ``upload_cancel`` + 本地 ``xhr.abort()``，**不等回执**。"""

    def test_cancel_unlocks_immediately(self, up):
        """挂住第 1 片后点取消：界面立刻解锁（不靠任何超时），在飞的请求被掐掉。"""
        up.setup(xhr={"chunk": 128, "hangFrom": 0})
        up.start(384)
        up.page.wait_for_function("() => window.__mockXHR.puts.length === 1")
        up.page.evaluate("() => window._filePanel._cancel()")
        up.settled(timeout=1000)          # 取消路径上没有任何超时，1s 绰绰有余
        b = _upload_bubbles(up.page)
        assert b[0]["text"] == MOBILE_I18N["bubble_file_canceled"].replace(
            "{name}", _Upload.NAME)
        assert b[0]["retry"] is True
        assert up.page.evaluate("() => window.__mockXHR.aborted") == 1
        assert len(_puts(up.page)) == 1, "取消后不该再发新片"
        assert _control(up.page)[-1] == {"type": "upload_cancel", "sid": _Upload.SID}

    def test_cancel_while_waiting_for_ready_still_aborts_the_session(self, up):
        """取消恰好卡在「帧已发出、ready 还没回来」：服务端其实已经把会话建出来了，
        迟到的 ready 要把那个空 ``.part`` 显式收掉，而不是留它干等 TTL。"""
        up.setup(xhr={"chunk": 128}, ws={"readyDelay": 250})
        up.start(384)
        up.wait_begin()
        up.page.evaluate("() => window._filePanel._cancel()")
        up.settled(timeout=1000)
        up.page.wait_for_function(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'upload_cancel')",
            timeout=2000)
        assert _puts(up.page) == []
        assert len(_upload_bubbles(up.page)) == 1, "取消只该落一条气泡"
        assert _control(up.page)[-1] == {"type": "upload_cancel", "sid": _Upload.SID}

    def test_cancel_while_idle_is_a_silent_noop(self, up):
        """面板空闲时点取消：既不补发帧，也不落气泡。"""
        up.page.evaluate("() => window._filePanel._cancel()")
        up.page.wait_for_timeout(50)
        assert _control(up.page) == []
        assert _upload_bubbles(up.page) == []

    def test_resend_after_cancel_starts_a_new_session(self, up):
        """取消后点气泡重发：是**新会话**（新 id、新钥匙、新 ``received`` 基准）。"""
        page = up.page
        up.setup(xhr={"chunk": 128, "hangFrom": 0})
        up.start(384)
        page.wait_for_function("() => window.__mockXHR.puts.length === 1")
        page.evaluate("() => window._filePanel._cancel()")
        up.settled(timeout=1000)

        page.evaluate("() => { window.__mockXHR.hangFrom = null; }")
        page.on("dialog", lambda d: d.accept())
        page.locator("#chat-list .message.file-cancel").first.click()
        up.settled()

        frames = _control(page)
        assert [f["type"] for f in frames] == [
            "upload_begin", "upload_cancel", "upload_begin"]
        assert frames[-1]["id"] == _Upload.ID + 1, "重发是新传输号"
        assert [(p["offset"], p["len"]) for p in _puts(page)] == [
            (0, 128), (0, 128), (128, 128), (256, 128)]


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

        page.evaluate(
            "() => { window.__sendP = window._filePanel._start("
            f"{self.FAKE_FILE}, 'file'); }}"
        )
        page.wait_for_function(
            "() => window._filePanel._state === 'idle'", timeout=6000)
        assert page.evaluate("() => window.__wsClient.getConnected()") is True
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'upload_begin')"
        ) is True
        # 1024 字节 / 每片 128 ⇒ 8 片，全部发出且没走取消
        assert page.evaluate("() => window.__mockXHR.puts.length") == 8
        assert page.evaluate(
            "() => window.__mockWS.sentMessages.some(m => m.type === 'upload_cancel')"
        ) is False

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
    前者，且一条日志都没有。FilePanel 常驻订阅 ``_close``（断连），上传期间又会额外
    订阅 ``upload_ready`` —— 同一 type 上并存两个订阅者正是现在的常态，旧写法会让先
    注册的那个永远收不到帧。这几条用例把它钉死：谁改回单槽，这里立刻红。

    用例里的 ref 一律用 ``'probe'``：上传面板不再常驻订阅 ack，这里的 ack 是凭空造的，
    不会污染任何传输状态。
    """

    PROBE = "{type:'ack', ref:'probe', id:0, a:'end'}"

    def _slots(self, page, kind="_close"):
        """某 type 当前的槽数（FilePanel 构造时已注册 ``_close``）。"""
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
            "  const off = window.__wsClient.on('_close', () => {});"
            "  const n = window.__wsClient.hub.map.get('_close').slots.length;"
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


class TestUploadWiring:
    """传输层接线：订阅先于发帧、等完即刻退订、断连不被静默宽限期吞掉。

    这批用例守的不是「功能还能用」，而是几条极易在后续改动里悄悄退化的性质 ——
    退化了功能「看起来还是对的」，只是偶尔漏帧 / 偶尔慢。
    """

    SLOTS = (
        "() => { const m = window.__wsClient.hub.map;"
        "  const n = (t) => (m.get(t) ? m.get(t).slots.length : 0);"
        "  return {ready: n('upload_ready'), close: n('_close')}; }"
    )

    def test_ready_subscription_is_live_before_the_frame_goes_out(self, up):
        """**先挂订阅、再发** ``upload_begin``：局域网一个来回只要几毫秒，先发后订就会
        漏掉 ready，只能干等到保险超时（这里把超时收到 300ms，退化了立刻红）。"""
        up.setup(xhr={"chunk": 128}, ws={"readyDelay": -1})   # -1 = 同步回帧
        _set_file_timeouts(up.page, begin_ms=300)
        up.page.evaluate(
            "() => {"
            "  const panel = window._filePanel;"
            "  panel.transport.sendFrame = (f) => {"
            "    if (f.type === 'upload_begin') window.__mockWS.answerUploadBegin(f);"
            "    return true;"
            "  };"
            "}"
        )
        up.start(128)
        up.settled(timeout=2000)
        assert len(_puts(up.page)) == 1
        assert [_strip_rate(x) for x in _bubble_texts(up.page)] == [
            MOBILE_I18N["bubble_file_done"].replace("{name}", _Upload.NAME)]

    def test_wait_subscriptions_are_released_after_settle(self, up):
        """等待用过的订阅必须退干净（AbortSignal）：否则每传一次漏一份，上一次取消留下
        的迟到 ready 还会冒充这一次的。"""
        up.setup(xhr={"chunk": 128})
        before = up.page.evaluate(self.SLOTS)
        assert before == {"ready": 0, "close": 1}, before   # 常驻的只有断连那一条
        up.start(384)
        up.settled()
        assert up.page.evaluate(self.SLOTS) == before

    def test_close_signal_beats_the_silent_grace_window(self, up):
        """WS_CLOSE **不受静默宽限期影响**：等 ready 的上传必须立刻收尾。

        宽限期（``_startGrace``）只决定「界面表不表态」，它是给「唤起文件选择器顺带被
        掐了 socket、回前台 1s 内自愈」那种抖动用的；但「这条连接确实断了」是个事实。
        若有人把断连检测改挂到 WS_STATUS 上（那条会被宽限期吞掉），这条立刻红。
        """
        page = up.page
        up.setup(xhr={"chunk": 128}, ws={"readyMode": "none"})
        _set_file_timeouts(page, begin_ms=10000)
        page.evaluate("() => { window.__wsClient.connect = () => {}; }")   # 掐掉重连
        page.evaluate("() => window.__wsClient.beginPickerContext()")      # 之后的断连都进静默窗口
        up.start(384)
        up.wait_begin()
        page.evaluate("window.__mockWS.triggerClose()")
        # 宽限期是 3000ms：1500ms 内收尾，就说明没在等它
        up.settled(timeout=1500)
        assert _bubble_texts(page) == [
            MOBILE_I18N["bubble_file_offline"].replace("{name}", _Upload.NAME)]

    def test_timeouts_follow_the_link_profile(self, cloudflare_page):
        """两条兜底时限都按链路取（LAN 2 分钟 / CF 10 分钟）——它们是派生 getter。"""
        page = cloudflare_page
        r = page.evaluate(
            "() => { const F = window._filePanel.constructor;"
            "        return {lanPut: F.LINK_LAN.put, lanBegin: F.LINK_LAN.begin,"
            "                cfPut: F.LINK_CF.put, cfBegin: F.LINK_CF.begin,"
            "                put: F.PUT_TIMEOUT, begin: F.BEGIN_TIMEOUT}; }"
        )
        assert r == {"lanPut": 120000, "lanBegin": 10000,
                     "cfPut": 600000, "cfBegin": 30000,
                     "put": 600000, "begin": 30000}, r

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
